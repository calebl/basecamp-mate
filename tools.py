"""The tools layer: small, single-purpose Basecamp operations, with no policy.

A tool does one explicit thing to the configured account and project, through the
`basecamp` CLI (or `gh`, `tasks-axi`, `lavish-axi` for the local and GitHub reads):

  - readers poll Basecamp and append what they find to pending-comments.jsonl, once
    each, with a cursor or seen-list kept beside the config: card comments and
    approvals, chat lines, the owner's lines in Pings (direct messages to the acting
    login, each in a bucket of its own), due check-in questions, comments on tracked
    to-dos, to-dos the owner assigns to the acting login (as requests, then their edits
    and closing), the owner's comments and @mentions on any other thread the acting login is
    subscribed to or mentioned in;
    the notification and boost readers (`/my/readings.json`, `/my/boosts.json`) only
    hand what changed to a behavior, `mark_read` marks notifications read, and the
    unmonitored recorder records, once per kind, the owner input no behavior handles;
  - commands post exactly what the agent hands them: `reply`, `ask`, `answer`,
    `todo create|track|comment|complete`, `post-message`; `unmonitored handle|forget`
    only edit local state;
  - primitives the behaviors compose: card create/update/move/assign/unassign, a
    Message Board post, acknowledgement boosts.

A tool never decides to act on what it reads. Which readers run on the timer, and
what the agent does with a pending record, is the behaviors layer (behaviors.py,
and prompts/base.md for the agent's side).

Every command that speaks as the agent (reply, ask, answer, the to-do commands and
post-message) posts nothing when no profile is set, or when the profile signs in as
the owner, and `--dry-run` only logs. All state lives in the directory holding the
config.
"""
import base64, contextlib, csv, fcntl, hashlib, html, json, os, re, subprocess, time, tomllib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

THUMBS, EYES = "\U0001F44D", "\U0001F440"


class BasecampError(RuntimeError):
    """A failed basecamp CLI call, keeping the CLI's error `code` and text."""

    def __init__(self, msg, code=None, error=None):
        super().__init__(msg)
        self.code, self.error = code, error or ""


class Tools:
    def __init__(self, home, config_path, dry=False, runner=subprocess.run):
        self.home = os.path.abspath(home)
        self.dir = os.path.dirname(os.path.abspath(config_path))
        self.cfg = cfg = json.load(open(config_path))
        self.account, self.project = str(cfg["account"]), str(cfg["project"])
        self.captain = int(cfg["captain"])  # the owner: assignee, and the only person whose word is a decision
        # The people whose lines, comments and boosts are relayed: "people", always with the captain.
        people = cfg.get("people", [])
        if not isinstance(people, list):
            raise ValueError('config "people" must be a list of Basecamp person ids')
        self.people = {self.captain, *(int(p) for p in people)}
        self.profile = cfg.get("profile")
        self.ask_chat = str(cfg["ask_chat"]) if cfg.get("ask_chat") is not None else None
        self.checkins = cfg.get("checkins")
        self.todos = cfg.get("todos")
        self.assigned_todos = cfg.get("assigned_todos") not in (None, False)
        # "account": to-dos assigned to the acting user in any project of the account are requests; else this project's.
        self.assigned_scope = (cfg.get("assigned_todos") or {}).get("scope", "project") \
            if isinstance(cfg.get("assigned_todos"), dict) else "project"
        self.message_board = str(cfg["message_board"]) if cfg.get("message_board") is not None else None
        self.dry, self.run = dry, runner
        self._acting = False  # acting person id once resolved; None when it can't be
        self._acting_sgid = None  # the acting person's mention sgid, to spot @mentions in chat
        self.errors = 0  # failed basecamp calls this run, so a caller can tell whether a reader it ran fully worked
        self.data = os.path.join(self.home, "data")
        self.state = os.path.join(self.home, "state")

    # --- plumbing: files, log, CLIs ---

    def path(self, name):
        return os.path.join(self.dir, name)

    def load(self, name, default):
        p = self.path(name)
        return json.load(open(p)) if os.path.exists(p) else default

    def save_json(self, name, value):
        tmp = self.path(name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(value, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path(name))

    @contextlib.contextmanager
    def locked(self, name):
        """Hold an exclusive lock on state file `name` while it is read, changed and saved; a dry run writes no lock file.

        locked("sync") is the shared state lock: a timer run, each listener cycle and
        each command hold it throughout, so two of them never write state at once.
        """
        if self.dry:
            yield
            return
        with open(self.path(name + ".lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def log(self, msg):
        line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
        print(line)
        with open(self.path("sync.log"), "a") as f:
            f.write(line + "\n")

    def pending_lines(self):
        """The lines of pending-comments.jsonl, oldest first."""
        p = self.path("pending-comments.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def record(self, rec):
        """Append one pending record for the agent."""
        with open(self.path("pending-comments.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def bc(self, *args, input=None, project=None):
        """Run the basecamp CLI on the configured project, or on `project` (another bucket of the account) when given."""
        prof = ["-P", self.profile] if self.profile else []
        cmd = ["basecamp", "-a", self.account, *prof, *args, "-p", str(project or self.project), "--json"]
        for attempt in range(3):
            kw = {"input": input} if input is not None else {}
            r = self.run(cmd, capture_output=True, text=True, timeout=120, **kw)
            try:
                out = json.loads(r.stdout)
            except ValueError:
                out = {"ok": False, "error": (r.stdout + r.stderr)[:300]}
            if out.get("ok"):
                return out.get("data")
            if not out.get("retryable") or attempt == 2:
                self.errors += 1
                raise BasecampError(f"basecamp {' '.join(args[:3])}: {out.get('error')}", out.get("code"), out.get("error"))
            time.sleep(3)

    def gh(self, *args):
        r = self.run(["gh", *args], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args[:3])}: {(r.stdout + r.stderr)[:300]}")
        return json.loads(r.stdout)

    # --- local readers: the backlog through tasks-axi (as bin/fm-tasks-axi.sh does), lavish-axi ---

    def tasks_axi(self, *args):
        root = os.path.dirname(self.data)
        env = dict(os.environ)
        backend = "markdown"
        toml = os.path.join(root, ".tasks.toml")
        if os.path.exists(toml):
            with open(toml, "rb") as f:
                backend = tomllib.load(f).get("backend", "markdown")
        if backend == "markdown":
            env["TASKS_AXI_FILE"] = os.path.join(self.data, "backlog.md")
        else:
            env.pop("TASKS_AXI_FILE", None)
        r = self.run(["tasks-axi", *args], capture_output=True, text=True, timeout=120, cwd=root, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"tasks-axi {' '.join(args)}: {(r.stdout + r.stderr)[:300]}")
        return r.stdout

    def read_backlog(self):
        rows = self.tasks_axi("list", "--limit", "100000").splitlines()
        ids = [ln.strip().split(",", 1)[0] for ln in rows if ln.startswith("  ") and "," in ln]
        return [task_item(parse_show(self.tasks_axi("show", i, "--full"))) for i in ids]

    def meta_pr(self, task):
        path = os.path.join(self.state, f"{task}.meta")
        if not os.path.exists(path):
            return None
        for line in open(path):
            if line.startswith("pr="):
                return line[3:].strip()
        return None

    def lavish_boards(self):
        """Open Lavish sessions by owning task: {task: [url, ...]}, or None when unreadable.

        lavish-axi has no machine-readable listing, so its plain `sessions[N]{...}` table
        is parsed. URLs are kept exactly as printed.
        """
        try:
            r = self.run(["lavish-axi"], capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                raise RuntimeError((r.stdout + r.stderr)[:300])
            return parse_lavish(r.stdout, self.data)
        except Exception as e:
            self.log(f"lavish-axi unavailable, keeping existing board links: {type(e).__name__}: {e}")
            return None

    # --- identity and acknowledgement boosts ---

    def acting_id(self):
        """The person the profile signs in as, looked up once per run: an id, None to skip, or "retry"."""
        if self._acting is False:
            self._acting = None
            if self.profile:
                try:
                    me = self.bc("api", "get", "/my/profile.json") or {}
                    self._acting, self._acting_sgid = me.get("id"), me.get("attachable_sgid")
                except RuntimeError as e:
                    self.log(f"acting identity unknown, acknowledgement boosts wait for the next run: {e}")
                    self._acting = "retry"
        return self._acting

    def refused(self, what, who="captain"):
        """True (logged) when a command must not post: no profile, or the profile is the owner."""
        me = self.acting_id()
        if me == "retry":
            raise RuntimeError("acting identity unknown")
        if me is None or me == self.captain:
            self.log(f"{what}: acting user is the {who} or unset, not posting")
            return True
        return False

    def acknowledge(self, key, rec, bucket=None):
        """Boost each queued recording (an owner comment or line, or the card for an approval) once.

        rec["ack"] holds [recording, emoji] pairs still to boost: 👀 for a question,
        👍 otherwise. Each moves to rec["acked"] as [recording, emoji, boost id] once
        boosted, so a failure is retried next run and a re-run never boosts twice.
        Nothing is boosted when no profile is set or it signs in as the captain
        himself; the queue is dropped. 👀 is only ever removed by the `reply` command.
        """
        done = [a[:2] for a in rec.get("acked", [])]
        queue = [q for q in rec.get("ack", []) if q not in done]
        if not queue:
            return
        me = self.acting_id()
        if me == "retry":
            return
        if me is None or me == self.captain:
            rec.pop("ack", None)
            return
        for rid, emoji in queue:
            if self.dry:
                self.log(f"dry {key}: acknowledge {rid} with {emoji}")
                continue
            try:
                bid = self.boost(me, rid, emoji, bucket)
                self.log(f"acknowledged {key}: {emoji} on {rid}")
            except RuntimeError as e:
                self.log(f"acknowledge {key} {rid} failed, retrying next run: {e}")
                continue
            rec.setdefault("acked", []).append([rid, emoji, bid])
            rec["ack"].remove([rid, emoji])

    def boosts_by(self, me, rid, emoji, bucket=None):
        path = f"/buckets/{bucket or self.project}/recordings/{rid}/boosts.json"
        return [b for b in self.bc("api", "get", path) or []
                if (b.get("booster") or {}).get("id") == me and is_emoji(b.get("content", ""), emoji)]

    def boost(self, me, rid, emoji, bucket=None):
        """The acting user's boost id for `emoji` on `rid` (in `bucket`, default the project), posting one only when absent."""
        have = self.boosts_by(me, rid, emoji, bucket)
        if have:
            return have[0].get("id")
        path = f"/buckets/{bucket or self.project}/recordings/{rid}/boosts.json"
        return (self.bc("api", "post", path, "-d", json.dumps({"content": emoji})) or {}).get("id")

    # --- card primitives (composed by the card mirror behavior; never run by hand) ---

    def card_create(self, table, column, title, body, assignee=None):
        args = ["cards", "create", title, body, "--card-table", table, "--column", column]
        if assignee is not None:
            args += ["--assignee", str(assignee)]
        return self.bc(*args)["id"]

    def card_update(self, card, table, title, body):
        self.bc("cards", "update", str(card), "--card-table", table, "--title", title, "--body", body)

    def card_move(self, card, table, column):
        self.bc("cards", "move", str(card), "--card-table", table, "--to", column)

    def card_assign(self, card, table):
        self.bc("cards", "update", str(card), "--card-table", table, "--assignee", str(self.captain))

    def card_unassign(self, card):
        self.bc("unassign", str(card), "--card", "--from", str(self.captain))

    def message(self, board, subject, content):
        """Post an HTML message on Message Board `board`; returns the message. No refusal rules."""
        return self.bc("api", "post", f"/buckets/{self.project}/message_boards/{board}/messages.json",
                       "-d", json.dumps({"subject": subject, "content": content, "status": "active"})) or {}

    def inbox_note(self, fm_home, request_id, body):
        """Queue `body` as a note in firstmate home `fm_home`'s inbox through its bin/fm-inbox.sh.

        Idempotent by `request_id`: a repeat replays the original note instead of adding
        one. Returns the outcome ("created" or "replay"); raises on any failure,
        including exit 3 (saved but firstmate not woken), which a retry with the same
        request id repairs.
        """
        cmd = [os.path.join(fm_home, "bin", "fm-inbox.sh"), "note", "--request-id", request_id, "--json", "-"]
        env = {k: v for k, v in os.environ.items() if k not in ("FM_ROOT_OVERRIDE", "FM_STATE_OVERRIDE")}
        env["FM_HOME"] = fm_home
        r = self.run(cmd, input=body, capture_output=True, text=True, timeout=30, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"fm-inbox.sh note exited {r.returncode}: {(r.stdout + r.stderr).strip()[:300]}")
        try:
            return json.loads(r.stdout).get("outcome")
        except ValueError:
            return None

    # --- readers: poll Basecamp, append pending records, acknowledge ---

    def read_boosts(self, rec, surface, context, counted=None, always=(), bucket=None):
        """Record the owner's new boosts on recordings as `boost` records: a boost is an answer.

        `counted` is [(recording id, url, boosts_count)], each read only when its count
        differs from rec["boost_counts"]; `always` is [(recording id, url)], read every
        run. Boost ids already recorded are in rec["boost_seen"]. The first time a record
        has no "boost_counts" (for counted) or no "boost_seen" (for always), they are
        seeded without recording, so turning this on never replays history. `bucket` is
        where the recordings are (default the project). A dry run records nothing.
        """
        counts, seen = rec.get("boost_counts"), rec.get("boost_seen")
        seed_counts, seed_seen = counts is None, seen is None
        counts, seen = dict(counts or {}), list(seen or [])
        items = [(rid, url, None) for rid, url in always] + list(counted or [])
        for rid, url, count in items:
            if count is not None:
                if seed_counts:
                    counts[str(rid)] = count
                    continue
                if count == counts.get(str(rid), 0):
                    continue
            try:
                boosts = self.bc("api", "get", f"/buckets/{bucket or self.project}/recordings/{rid}/boosts.json") or []
            except RuntimeError as e:
                self.log(f"boosts {surface} {rid}: {e}")
                continue
            for b in sorted(boosts, key=lambda b: b.get("id", 0)):
                bid = b.get("id")
                if bid in seen or not self.hears(b.get("booster")):
                    continue
                if self.dry:
                    self.log(f"dry {surface} {rid}: owner boost {bid}")
                    continue
                seen.append(bid)
                if count is None and seed_seen:
                    continue
                self.record({"kind": "boost", "surface": surface, **context, "recording": rid, "boost": bid,
                             "text": html.unescape(re.sub(r"<[^>]+>", "", b.get("content", ""))).strip(),
                             "url": url, "at": b.get("created_at"), **self.author(b.get("booster"))})
                self.log(f"new owner boost on {surface} {rid}: {bid}")
            if count is not None:
                counts[str(rid)] = count
        if counted is not None:
            rec["boost_counts"] = counts
        if always or seen != list(rec.get("boost_seen") or []):
            rec["boost_seen"] = seen

    def read_card_comments(self, task, repo, key, rec):
        """Record the new comments of the people listened to on card rec["card"]: "question" when it has "?", else "comment"."""
        try:
            comments = self.bc("comments", "list", str(rec["card"])) or []
        except RuntimeError as e:
            self.log(f"comments {key}: {e}")
            comments = []
        for c in comments:
            cid = c.get("id")
            if cid in rec.setdefault("comments", []):
                continue
            rec["comments"].append(cid)
            if self.hears(c.get("creator")):
                text = re.sub(r"<[^>]+>", "", c.get("content", ""))
                kind = "question" if "?" in html.unescape(text) else "comment"
                self.record({"kind": kind, "task": task, "repo": repo, "card": rec["card"], "comment": cid,
                             "at": c.get("created_at"), "text": text, **self.author(c.get("creator"))})
                rec.setdefault("ack", []).append([cid, EYES if kind == "question" else THUMBS])
                self.log(f"new captain {kind} on {key}: {cid}")
        self.read_boosts(rec, "card-comment", {"task": task, "repo": repo, "card": rec["card"]},
                         counted=[(c.get("id"), c.get("app_url"), c.get("boosts_count") or 0) for c in comments])

    def read_card_boosts(self, task, repo, key, rec):
        """Record the captain's 👍 on card rec["card"] as an approval, and any other boost by someone listened to as a `boost`.

        Only the captain approves: another listed person's 👍 is a `boost` like any other.
        A dry run reads and logs only. Other boosts already on the card the first time
        it is read (no rec["card_boost_seen"]) are seeded, not recorded.
        """
        path = f"/buckets/{self.project}/recordings/{rec['card']}/boosts.json"
        try:
            boosts = self.bc("api", "get", path) or []
        except RuntimeError as e:
            self.log(f"boosts {key}: {e}")
            return
        url = f"https://app.basecamp.com/{self.account}/buckets/{self.project}/card_tables/cards/{rec['card']}"
        seed = "card_boost_seen" not in rec
        for b in boosts:
            bid = b.get("id")
            if bid in rec.get("boosts", []) or bid in rec.get("card_boost_seen", []):
                continue
            if not self.hears(b.get("booster")):
                continue
            if not is_thumbs_up(b.get("content", "")) or (b.get("booster") or {}).get("id") != self.captain:
                if self.dry:
                    self.log(f"dry {key}: owner boost {bid}")
                    continue
                rec.setdefault("card_boost_seen", []).append(bid)
                if not seed:
                    self.record({"kind": "boost", "surface": "card", "task": task, "repo": repo, "card": rec["card"],
                                 "recording": rec["card"], "boost": bid,
                                 "text": html.unescape(re.sub(r"<[^>]+>", "", b.get("content", ""))).strip(),
                                 "url": url, "at": b.get("created_at"), **self.author(b.get("booster"))})
                    self.log(f"new owner boost on {key}: {bid}")
                continue
            if self.dry:
                self.log(f"dry {key}: captain approval boost {bid}")
                self.acknowledge(key, dict(rec, ack=[*rec.get("ack", []), [rec["card"], THUMBS]]))
                continue
            rec.setdefault("boosts", []).append(bid)
            self.record({"kind": "approval", "task": task, "repo": repo, "card": rec["card"], "url": url,
                         "boost": bid, "text": re.sub(r"<[^>]+>", "", b.get("content", "")).strip(), "at": b.get("created_at"),
                         **self.author(b.get("booster"))})
            if [rec["card"], THUMBS] not in rec.setdefault("ack", []):
                rec["ack"].append([rec["card"], THUMBS])
            self.log(f"captain approval on {key}: boost {bid}")
        if not self.dry:
            rec.setdefault("card_boost_seen", [])

    def read_chats(self, chats, every_line=(), addressed=()):
        """Record the chat lines of the people listened to in `chats` as chat-question records.

        Per chat, chats.json keeps a cursor (the newest line id seen). The first run
        only sets the cursor, so old history is not relayed. A listened-to line that
        mentions the acting user or contains "?" is recorded and queued for a 👀; every
        other line is skipped, unless the chat is in `every_line`, where every such
        line is recorded. `addressed` holds line ids a notification says address the
        acting user (an @mention, or a Campfire reply to one of its lines, which carries
        no mention): each is recorded like a question, even when the cursor has passed
        it, unless it already was. A dry run reads and logs only.
        """
        state = self.load("chats.json", {})
        for chat in chats:
            try:
                lines = self.bc("api", "get", f"/buckets/{self.project}/chats/{chat}/lines.json") or []
            except RuntimeError as e:
                self.log(f"chat {chat}: {e}")
                continue
            lines = sorted(lines, key=lambda l: l.get("id", 0))
            rec = dict(state.get(chat, {}))
            first = "cursor" not in rec
            cursor = rec.get("cursor", 0)
            for ln in lines:
                lid = ln.get("id", 0)
                late = lid <= cursor and lid in addressed and lid not in rec.get("lines", []) and not first
                if lid <= cursor and not late:
                    continue
                cursor = max(cursor, lid)
                if first or not self.hears(ln.get("creator")):
                    continue
                content = ln.get("content", "")
                text = html.unescape(re.sub(r"<[^>]+>", "", content)).strip()
                if "?" not in text and chat not in every_line and lid not in addressed:
                    self.acting_id()
                    if not (self._acting_sgid and self._acting_sgid in content):
                        continue
                if self.dry:
                    self.log(f"dry chat {chat}: captain question {lid}")
                    self.acknowledge(f"chat {chat}", dict(rec, ack=[*rec.get("ack", []), [lid, EYES]]))
                    continue
                self.record({"kind": "chat-question", "chat": int(chat), "line": lid,
                             "url": ln.get("app_url") or f"https://3.basecamp.com/{self.account}/buckets/{self.project}/chats/{chat}@{lid}",
                             "text": text, "at": ln.get("created_at"), **self.author(ln.get("creator"))})
                rec.setdefault("lines", []).append(lid)
                rec.setdefault("ack", []).append([lid, EYES])
                self.log(f"new captain chat question in {chat}: {lid}")
            self.read_boosts(rec, "chat", {"chat": int(chat)},
                             counted=[(ln.get("id"), ln.get("app_url"), ln.get("boosts_count") or 0) for ln in lines])
            if self.dry:
                if first:
                    self.log(f"dry chat {chat}: would start the cursor at {cursor}")
                self.acknowledge(f"chat {chat}", rec)
                continue
            rec["cursor"] = cursor
            state[chat] = rec
            self.acknowledge(f"chat {chat}", rec)
            self.save_json("chats.json", state)

    def today(self):
        tz = (self.checkins or {}).get("timezone")
        return datetime.now(ZoneInfo(tz) if tz else None)

    def read_checkins(self, questionnaires):
        """Record each check-in question that is due today and not yet answered by the acting user.

        Due: not paused, today's weekday in the schedule's "days" (0 = Sunday), the
        start_date reached, and the schedule's hour:minute passed in the configured
        "timezone" (the machine's local time when unset). Each question is recorded
        once per day as a `checkin`, with who wrote the question; nothing is answered here.
        checkins.json keeps question id -> {"recorded": [dates], "answered": [dates]}.
        A dry run reads and logs only.
        """
        now = self.today()
        date = now.strftime("%Y-%m-%d")
        state = self.load("checkins.json", {})
        for qn in questionnaires:
            try:
                questions = self.bc("api", "get", f"/buckets/{self.project}/questionnaires/{qn}/questions.json") or []
            except RuntimeError as e:
                self.log(f"checkins {qn}: {e}")
                continue
            for q in questions:
                qid = str(q.get("id"))
                rec = state.get(qid, {})
                if date in rec.get("recorded", []) or not is_due(q, now):
                    continue
                me = self.acting_id()
                if me == "retry":
                    return
                try:
                    if self.answered_today(q["id"], me, date):
                        continue
                except RuntimeError as e:
                    self.log(f"checkin {qid}: {e}")
                    continue
                if self.dry:
                    self.log(f"dry checkin {qid}: due {date}")
                    continue
                self.record({"kind": "checkin", "questionnaire": int(qn), "question": q["id"], "date": date,
                             "title": q.get("title"), "url": q.get("app_url"), "at": now.isoformat(timespec="seconds"),
                             **self.author(q.get("creator"))})
                rec.setdefault("recorded", []).append(date)
                state[qid] = rec
                self.save_json("checkins.json", state)
                self.log(f"checkin {qid} due {date}")

    def agent(self):
        """The acting user's id when it is someone other than the owner, else None."""
        me = self.acting_id()
        return None if me in (None, "retry", self.captain) else me

    def hears(self, person):
        """True when `person` (a creator or booster object) is someone the sync listens to.

        The captain always is. Another listed person is, unless they are the acting user
        itself, so the agent's own lines are never relayed back to it even when listed.
        """
        pid = (person or {}).get("id")
        if pid == self.captain:
            return True
        return pid in self.people and pid != self.acting_id()

    def author(self, person):
        """A record's author fields: who wrote it ({"id", "name"}) and whether that is the captain."""
        person = person or {}
        return {"author": {"id": person.get("id"), "name": person.get("name")}, "captain": person.get("id") == self.captain}

    def read_answer_boosts(self, questionnaires, days=7):
        """The owner's boosts on check-in answers the acting user posted in the last `days` days.

        One answers read per question that has answers, then a boosts read per answer
        whose boosts_count changed. State is the question's entry in checkins.json.
        """
        me = self.agent()
        if me is None:
            return
        since = (self.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        state = self.load("checkins.json", {})
        for qn in questionnaires:
            try:
                questions = self.bc("api", "get", f"/buckets/{self.project}/questionnaires/{qn}/questions.json") or []
                for q in questions:
                    if not q.get("answers_count"):
                        continue
                    answers = self.bc("api", "get", f"/buckets/{self.project}/questions/{q['id']}/answers.json") or []
                    mine = [a for a in answers if (a.get("creator") or {}).get("id") == me
                            and (a.get("group_on") or (a.get("created_at") or "")[:10]) >= since]
                    rec = state.setdefault(str(q["id"]), {})
                    self.read_boosts(rec, "checkin-answer", {"question": q["id"]},
                                     counted=[(a["id"], a.get("app_url"), a.get("boosts_count") or 0) for a in mine])
            except RuntimeError as e:
                self.log(f"checkin answer boosts {qn}: {e}")
        if not self.dry:
            self.save_json("checkins.json", state)

    def read_messages(self, board, days=14, only=None):
        """The owner's comments and boosts on messages the acting user posted on `board` in the last `days` days.

        One read of the board's newest messages; a boosts read per message whose
        boosts_count changed; a comments read per such message that has comments, and a
        boosts read per comment whose count changed. Each owner comment newer than the
        message's cursor is recorded once as a `message-comment`, acknowledged like a card
        comment (👀 when it contains "?", 👍 otherwise). A message has no cursor until its
        first read, so feedback already on a recent post is relayed, not skipped. `only`
        (a message id, from a notification) reads that one message instead of the board,
        whatever its age, when the acting user posted it. State is messages.json. A dry
        run reads and logs only.
        """
        me = self.agent()
        if me is None:
            return
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            if only is not None:
                msgs = [self.bc("api", "get", f"/buckets/{self.project}/messages/{only}.json") or {}]
            else:
                msgs = self.bc("api", "get", f"/buckets/{self.project}/message_boards/{board}/messages.json") or []
        except RuntimeError as e:
            self.log(f"messages {only or board}: {e}")
            return
        mine = [m for m in msgs if (m.get("creator") or {}).get("id") == me
                and (only is not None or (m.get("created_at") or "") >= since)]
        with self.locked("messages.json"):
            state = self.load("messages.json", {})
            for m in mine:
                ctx = {"message": m["id"], "subject": m.get("subject")}
                self.read_boosts(state.setdefault("messages", {}), "message", ctx,
                                 counted=[(m["id"], m.get("app_url"), m.get("boosts_count") or 0)])
                if not m.get("comments_count"):
                    continue
                try:
                    comments = self.bc("comments", "list", str(m["id"])) or []
                except RuntimeError as e:
                    self.log(f"message {m['id']} comments: {e}")
                    continue
                post = state.setdefault("posts", {}).setdefault(str(m["id"]), {"cursor": 0})
                cursor = post["cursor"]
                for c in sorted(comments, key=lambda c: c.get("id", 0)):
                    cid = c.get("id", 0)
                    if cid <= cursor:
                        continue
                    cursor = cid
                    if not self.hears(c.get("creator")):
                        continue
                    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c.get("content", "")))).strip()
                    question = "?" in text
                    if self.dry:
                        self.log(f"dry message {m['id']}: owner comment {cid}")
                        continue
                    self.record({"kind": "message-comment", "message": m["id"], "subject": m.get("subject"),
                                 "comment": cid, "question": question, "url": c.get("app_url") or m.get("app_url"),
                                 "text": text, "at": c.get("created_at"), **self.author(c.get("creator"))})
                    post.setdefault("comments", []).append(cid)
                    post.setdefault("ack", []).append([cid, EYES if question else THUMBS])
                    self.log(f"new owner comment on message {m['id']}: {cid}")
                self.acknowledge(f"message {m['id']}", post)
                if not self.dry:
                    post["cursor"] = cursor
                self.read_boosts(state.setdefault("comments", {}), "message-comment", ctx,
                                 counted=[(c["id"], c.get("app_url"), c.get("boosts_count") or 0) for c in comments])
            if not self.dry:
                self.save_json("messages.json", state)

    def answered_today(self, qid, me, date):
        """True when `me` already answered question `qid` for `date` in Basecamp."""
        if me is None:
            return False
        answers = self.bc("api", "get", f"/buckets/{self.project}/questions/{qid}/answers.json") or []
        return any((a.get("creator") or {}).get("id") == me and (a.get("group_on") or (a.get("created_at") or "")[:10]) == date
                   for a in answers)

    def read_todo_comments(self, keys=None, requests=None):
        """Record the owner's new comments on each open tracked to-do (or only those in `keys`) as `todo-comment` records.

        todos.json keeps, per key, the to-do id and a cursor (the newest comment id
        seen). Each comment by someone listened to newer than the cursor is recorded once
        and queued for a 👀 when it contains "?", a 👍 otherwise; other people's comments,
        the agent's own included, only move the cursor. Completed or closed to-dos are
        not read. `requests` True reads only to-do requests (records carry "request":
        true), False only the agent's decision to-dos, None both. A dry run reads and
        logs only.
        """
        for key in sorted(self.load("todos.json", {})):
            if keys is not None and key not in keys:
                continue
            with self.locked("todos.json"):
                state = self.load("todos.json", {})
                rec = state.get(key)
                if rec is None or rec.get("completed") or rec.get("closed"):
                    continue
                if requests is not None and bool(rec.get("request")) != requests:
                    continue
                try:
                    comments = self.bc("comments", "list", str(rec["todo"]), project=rec.get("bucket")) or []
                except RuntimeError as e:
                    self.log(f"todo {key}: {e}")
                    continue
                if self.dry:
                    rec = json.loads(json.dumps(rec))
                cursor = rec.get("cursor", 0)
                for c in sorted(comments, key=lambda c: c.get("id", 0)):
                    cid = c.get("id", 0)
                    if cid <= cursor:
                        continue
                    cursor = cid
                    if not self.hears(c.get("creator")):
                        continue
                    text = html.unescape(re.sub(r"<[^>]+>", " ", c.get("content", "")))
                    text = re.sub(r"\s+", " ", text).strip()
                    question = "?" in text
                    rec.setdefault("ack", []).append([cid, EYES if question else THUMBS])
                    if self.dry:
                        self.log(f"dry todo {key}: owner comment {cid}")
                        continue
                    self.record({"kind": "todo-comment", "key": key, "todo": rec["todo"], "comment": cid,
                                 "question": question, "url": c.get("app_url") or rec.get("url"),
                                 "text": text, "at": c.get("created_at"), **request_fields(rec),
                                 **self.author(c.get("creator"))})
                    rec.setdefault("comments", []).append(cid)
                    self.log(f"new owner comment on todo {key}: {cid}")
                self.read_todo_boosts(key, rec, comments)
                self.acknowledge(f"todo {key}", rec, rec.get("bucket"))
                if not self.dry:
                    rec["cursor"] = cursor
                    self.save_json("todos.json", state)

    def read_todo_boosts(self, key, rec, comments):
        """The owner's boosts on the to-do (read every run) and on its comments (when their count changes)."""
        ctx, bucket = {"key": key, "todo": rec["todo"], **request_fields(rec)}, rec.get("bucket")
        self.read_boosts(rec, "todo", ctx, always=[(rec["todo"], rec.get("url"))], bucket=bucket)
        self.read_boosts(rec, "todo-comment", ctx, bucket=bucket,
                         counted=[(c.get("id"), c.get("app_url"), c.get("boosts_count") or 0) for c in comments])

    def readings(self):
        """The acting user's notifications, one read of /my/readings.json: {"unreads": [...], "reads": [first page], ...}."""
        return self.bc("api", "get", "/my/readings.json") or {}

    def my_boosts(self):
        """The boosts on the acting user's own recordings (its chat lines, comments, to-dos, cards, messages, answers),
        one read of /my/boosts.json, each with its "recording" (type, bucket, parent). Not in Basecamp's API docs."""
        return self.bc("api", "get", "/my/boosts.json") or []

    def mark_read(self, ids):
        """Mark the acting user's notifications `ids` read, through `basecamp notifications read` (PUT /my/unreads.json)."""
        if ids:
            self.bc("notifications", "read", *[str(i) for i in ids])

    def ping_conversations(self, data=None):
        """The Ping conversations (direct messages, each a chat in its own "circle" bucket) the acting user is in with someone listened to.

        One read of /my/readings.json (or `data`, a read already made): its "pings" section,
        read and unread, lists each recently active Ping with a listened-to person among
        its participants or as its creator; the bucket and chat ids come from its
        subscription_url. Newest activity first.
        """
        data = self.readings() if data is None else data
        found = {}
        for r in (data.get("unreads") or []) + (data.get("reads") or []):
            m = re.search(r"/buckets/(\d+)/recordings/(\d+)/", r.get("subscription_url") or "")
            people = [p.get("id") for p in r.get("participants") or []] + [(r.get("creator") or {}).get("id")]
            if r.get("section") != "pings" or r.get("type") != "Chat" or not m or not self.people & set(people):
                continue
            found.setdefault(m.group(2), {"bucket": m.group(1), "chat": m.group(2), "title": r.get("bucket_name"),
                                          "url": r.get("app_url"), "updated_at": r.get("updated_at") or ""})
        return sorted(found.values(), key=lambda p: p["updated_at"], reverse=True)

    def read_pings(self, limit=10, convs=None):
        """Record each new line someone listened to writes in a Ping with the acting user as a `ping` record, once.

        Pings are found with ping_conversations(), at most `limit` per run, newest activity
        first; a Ping drops out of the readings once it goes quiet, and a new line puts it
        back. `convs` (from a notification) reads those Pings instead. pings.json keeps
        "since" (when the behavior first ran) and, per Ping chat, a
        cursor (the newest line id seen): every owner line newer than both is recorded and
        queued for a 👀, so a Ping that starts after "since" is relayed from its first line
        and history from before it never is. The owner's boosts on lines in a Ping are
        `boost` records, as in a chat. Refused (logged) unless the acting user is someone
        other than the owner: /my/readings.json is the acting user's. A dry run reads and
        logs only.
        """
        if self.agent() is None:
            self.log("pings: the acting user is the owner, unset or unknown; Pings not read")
            return
        state = self.load("pings.json", {})
        since = state.get("since") or now_iso()
        try:
            convs = self.ping_conversations()[:limit] if convs is None else convs
        except RuntimeError as e:
            self.log(f"pings: {e}")
            return
        for p in convs:
            bucket, chat = p["bucket"], p["chat"]
            try:
                lines = self.bc("api", "get", f"/buckets/{bucket}/chats/{chat}/lines.json") or []
            except RuntimeError as e:
                self.log(f"ping {chat}: {e}")
                continue
            lines = sorted(lines, key=lambda l: l.get("id", 0))
            rec = json.loads(json.dumps(state.get("pings", {}).get(chat, {})))
            rec.update(bucket=int(bucket), title=p["title"], url=p["url"])
            cursor = rec.get("cursor", 0)
            for ln in lines:
                lid = ln.get("id", 0)
                if lid <= cursor:
                    continue
                cursor = lid
                if not self.hears(ln.get("creator")) or not later(ln.get("created_at"), since):
                    continue
                if self.dry:
                    self.log(f"dry ping {chat}: owner line {lid}")
                    continue
                self.record({"kind": "ping", "bucket": int(bucket), "chat": int(chat), "line": lid, "title": p["title"],
                             "url": ln.get("app_url") or f"https://app.basecamp.com/{self.account}/circles/{bucket}@{lid}",
                             "text": html.unescape(re.sub(r"<[^>]+>", "", ln.get("content", ""))).strip(),
                             "at": ln.get("created_at"), **self.author(ln.get("creator"))})
                rec.setdefault("lines", []).append(lid)
                rec.setdefault("ack", []).append([lid, EYES])
                self.log(f"new owner ping line in {chat}: {lid}")
            self.read_boosts(rec, "ping", {"bucket": int(bucket), "chat": int(chat)}, bucket=bucket,
                             counted=[(ln.get("id"), ln.get("app_url"), ln.get("boosts_count") or 0) for ln in lines])
            self.acknowledge(f"ping {chat}", rec, bucket)
            if not self.dry:
                rec["cursor"] = cursor
                state.setdefault("pings", {})[chat] = rec
        if not self.dry:
            state["since"] = since
            self.save_json("pings.json", state)

    # --- to-do requests: to-dos the people listened to assign to the acting user ---

    def todo(self, tid, bucket=None):
        """To-do `tid` in `bucket` (default the project), refetched: content (its name), description, assignees, creator,
        completed, completion, status, and the bucket (project) it is in."""
        return self.bc("api", "get", f"/buckets/{bucket or self.project}/todos/{tid}.json", project=bucket) or {}

    def assigned_todo_buckets(self):
        """{to-do id: its bucket id} for the open to-dos assigned to the acting user, from one read of /my/assignments.json:
        in this project, or in every project of the account when the scope is "account"."""
        data = self.bc("api", "get", "/my/assignments.json") or {}
        items = data if isinstance(data, list) else (data.get("priorities") or []) + (data.get("non_priorities") or [])
        found = {}
        for a in items:
            bucket = str((a.get("bucket") or {}).get("id") or self.project)
            if (a.get("id") and not a.get("completed") and (a.get("type") or "todo").lower() == "todo"
                    and (self.assigned_scope == "account" or bucket == self.project)):
                found.setdefault(a["id"], bucket)
        return found

    def request_keys(self):
        """The keys of the open to-do requests in todos.json."""
        return sorted(k for k, r in self.load("todos.json", {}).items()
                      if r.get("request") and not r.get("completed") and not r.get("closed"))

    def read_todo_request(self, tid, by=None, bucket=None):
        """Record to-do `tid` as a `todo-request` once, when it is open, assigned to the acting user, and `by` is listened to.

        `by` is who assigned it (an event's creator id); None means its creator, as the
        sweep has no other way to tell. `bucket` is the project it is in (default the
        configured one); the request keeps it, with the project's name, so everything later
        read or posted on it goes there, and every record of it carries the project. A new request is tracked in todos.json as
        "request-<id>" with a cursor of 0, so the comments already on it are relayed as
        part of it, and the to-do is queued for a 👀. A closed request assigned again is
        a new request under the same key. Returns True when recorded; False for an
        open request already tracked, a to-do assigned to someone else, or one not
        assigned by someone listened to (the sweep remembers those in
        assigned-todos.json, so it fetches each once). A dry run records nothing.
        """
        me = self.agent()
        key = f"request-{tid}"
        rec = self.load("todos.json", {}).get(key)
        if me is None or (rec is not None and not rec.get("completed") and not rec.get("closed")):
            return False
        try:
            todo = self.todo(tid, bucket)
        except RuntimeError as e:
            self.log(f"todo request {tid}: {e}")
            return False
        if (todo.get("completed") or todo.get("status") not in (None, "active")
                or me not in [a.get("id") for a in todo.get("assignees") or []]):
            return False
        creator = todo.get("creator") or {}
        who = creator if by is None or by == creator.get("id") else {"id": by, "name": None}
        if not self.hears(who):
            if by is None and not self.dry:
                state = self.load("assigned-todos.json", {})
                state["ignored"] = sorted({*state.get("ignored", []), tid})
                self.save_json("assigned-todos.json", state)
            self.log(f"todo request {tid}: not assigned by someone listened to, ignored")
            return False
        if self.dry:
            self.log(f"dry todo request {tid}: record and acknowledge with {EYES}")
            return False
        with self.locked("todos.json"):
            state = self.load("todos.json", {})
            rec = state.get(key) or {"todo": tid, "cursor": 0, "created": todo.get("created_at")}
            for k in ("completed", "closed"):
                rec.pop(k, None)
            n = rec.get("requests", 0) + 1
            rec.update(title=todo.get("content"), url=todo.get("app_url"), digest=todo_digest(todo), requests=n,
                       request=self.author(who), bucket=int(bucket or self.project),
                       project_name=(todo.get("bucket") or {}).get("name") or rec.get("project_name"))
            if [tid, EYES] not in [a[:2] for a in rec.get("acked", [])] + rec.get("ack", []):
                rec.setdefault("ack", []).append([tid, EYES])
            self.record({"kind": "todo-request", "key": key, "todo": tid, "n": n, "reopened": n > 1,
                         "title": todo.get("content"), "text": plain(todo.get("description")),
                         "assignees": [a.get("name") for a in todo.get("assignees") or []],
                         "url": todo.get("app_url"), "at": todo.get("updated_at") or todo.get("created_at"),
                         "project": request_project(rec), **self.author(who)})
            self.log(f"todo request {key}: todo {tid} in project {rec['bucket']} assigned by {who.get('id')}")
            self.acknowledge(f"todo {key}", rec, rec["bucket"])
            state[key] = rec
            self.save_json("todos.json", state)
        return True

    def discover_todo_requests(self, limit=10):
        """The sweep: run read_todo_request for each open to-do assigned to the acting user that is not yet an open request.

        Only this project's to-dos, unless the scope is "account": then every project's.
        At most `limit` to-dos are fetched a run; one already found not to be assigned by
        someone listened to is never fetched again. Refused (logged) unless the acting
        user is someone other than the owner: /my/assignments.json is the acting user's.
        """
        if self.agent() is None:
            self.log("todo requests: the acting user is the owner, unset or unknown; assigned to-dos not read")
            return
        try:
            found = self.assigned_todo_buckets()
        except RuntimeError as e:
            self.log(f"todo requests: {e}")
            return
        tracked = {self.load("todos.json", {})[k]["todo"] for k in self.request_keys()}
        ignored = set(self.load("assigned-todos.json", {}).get("ignored", []))
        for tid in [i for i in found if i not in tracked and i not in ignored][:limit]:
            self.read_todo_request(tid, bucket=found[tid])

    def refresh_todo_request(self, key, by=None):
        """Record what changed on open to-do request `key` since it was last read: an edit, or its closing.

        Completed (by anyone), trashed or archived, or no longer assigned to the acting
        user: a `todo-request-closed` record (`reason` completed, trashed, archived or
        unassigned, and who completed it when Basecamp says), and the request stops
        being read; completed also marks it completed, so `todo complete` is not needed.
        Its name or description changed: a `todo-request-update` record with the new
        text. `by` is who did it, when an event says; otherwise Basecamp does not say who
        edited it. A dry run records nothing. Returns the record kind, or None.
        """
        me = self.agent()
        rec = self.load("todos.json", {}).get(key)
        if me is None or rec is None or not rec.get("request") or rec.get("completed") or rec.get("closed"):
            return None
        try:
            todo = self.todo(rec["todo"], rec.get("bucket"))
        except RuntimeError as e:
            self.log(f"todo request {key}: {e}")
            return None
        who = {"id": by, "name": None} if by is not None else None
        if todo.get("completed"):
            reason = "completed"
            who = (todo.get("completion") or {}).get("creator") or who
        elif todo.get("status") not in (None, "active"):
            reason = todo.get("status")
        elif me not in [a.get("id") for a in todo.get("assignees") or []]:
            reason = "unassigned"
        elif todo_digest(todo) != rec.get("digest"):
            reason = None
        else:
            return None
        kind = "todo-request-closed" if reason else "todo-request-update"
        if self.dry:
            self.log(f"dry todo request {key}: {kind}" + (f" ({reason})" if reason else ""))
            return None
        authored = self.author(who) if who else {"author": None, "captain": None}
        with self.locked("todos.json"):
            state = self.load("todos.json", {})
            rec = state[key]
            out = {"kind": kind, "key": key, "todo": rec["todo"], "n": rec.get("requests", 1),
                   "title": todo.get("content") or rec.get("title"), "url": todo.get("app_url") or rec.get("url"),
                   "at": (todo.get("completion") or {}).get("created_at") or todo.get("updated_at"),
                   "project": request_project(rec), **authored}
            if reason:
                out["reason"] = reason
                rec["closed"] = reason
                if reason == "completed":
                    rec["completed"] = now_iso()
            else:
                out["text"] = plain(todo.get("description"))
                rec.update(digest=todo_digest(todo), title=todo.get("content") or rec.get("title"))
            self.record(out)
            self.save_json("todos.json", state)
        self.log(f"todo request {key}: {kind}" + (f" ({reason})" if reason else ""))
        return kind

    # --- refetches, @mentions in threads nothing tracks, and unmonitored input ---

    def comment(self, cid, bucket=None):
        """Comment `cid` (in `bucket`, default the project), refetched from Basecamp; its "parent" names what it is on."""
        return self.bc("api", "get", f"/buckets/{bucket or self.project}/comments/{cid}.json", project=bucket) or {}

    def recording(self, rid, bucket=None):
        """Recording `rid` of any type (in `bucket`, default the project), refetched (type, title, content, creator, parent, app_url)."""
        return self.bc("api", "get", f"/buckets/{bucket or self.project}/recordings/{rid}.json", project=bucket) or {}

    def read_mention(self, bucket, chat, rid):
        """Record a listened-to person's line `rid` in chat `chat` that @mentions the acting user (or is a Campfire reply
        to one of its lines) when no chat reader relays that chat, once, as a `chat-question` kept in chats.json, so
        `reply` answers it there; queued for a 👀. Returns True when recorded. A dry run records nothing."""
        if self.agent() is None or rid in self.load("chats.json", {}).get(str(chat), {}).get("lines", []):
            return False
        try:
            ln = self.bc("api", "get", f"/buckets/{bucket}/chats/{chat}/lines/{rid}.json", project=bucket) or {}
        except RuntimeError as e:
            self.log(f"mention {rid} in chat {chat}: {e}")
            return False
        if not self.hears(ln.get("creator")):
            return False
        if self.dry:
            self.log(f"dry mention {rid} in chat {chat}: record and acknowledge with {EYES}")
            return False
        state = self.load("chats.json", {})
        rec = state.setdefault(str(chat), {})
        self.record({"kind": "chat-question", "chat": int(chat), "line": rid, "mention": True,
                     "url": ln.get("app_url") or f"https://3.basecamp.com/{self.account}/buckets/{bucket}/chats/{chat}@{rid}",
                     "text": plain(ln.get("content")), "at": ln.get("created_at"), **self.author(ln.get("creator"))})
        rec.setdefault("lines", []).append(rid)
        rec.setdefault("ack", []).append([rid, EYES])
        self.log(f"new owner mention in chat {chat}: {rid}")
        self.acknowledge(f"chat {chat}", rec)
        self.save_json("chats.json", state)
        return True

    def read_thread(self, bucket, thread, since, mentions=(), ptype=None, title=None):
        """Record the listened-to people's new comments on recording `thread` (in `bucket`), a thread no other reader
        tracks but the acting user is subscribed to or @mentioned in: a card whatever its backlog state, a document, an
        upload, someone else's message or to-do, a check-in answer.

        threads.json keeps, per "<bucket>:<thread>", a cursor (the newest comment id seen),
        the comments recorded, acknowledgements and replies. A thread's first read starts at
        comment `since` (the notification's first unread comment), so what was already
        read before it is not replayed. Each new comment by someone listened to is a
        `mention` record when it is in `mentions` (a notification said so) or @mentions the
        acting user, else a `thread-comment`, and is queued for a 👀 when it is a mention or
        contains "?", a 👍 otherwise. `reply` answers either with a comment on the thread.
        `ptype` and `title` name the thread when the comments do not. The owner's boosts on
        its comments (the agent's replies included) are `boost` records with surface
        `thread-comment`. A dry run reads and logs only.
        """
        if self.agent() is None:
            return
        try:
            comments = self.bc("comments", "list", str(thread), project=bucket) or []
        except RuntimeError as e:
            self.log(f"thread {thread}: {e}")
            return
        key = f"{bucket}:{thread}"
        state = self.load("threads.json", {})
        rec = json.loads(json.dumps(state.get(key) or {"bucket": int(bucket), "thread": thread, "cursor": (since or 1) - 1,
                                                       "parent_type": ptype, "title": title}))
        self.acting_id()
        cursor = rec["cursor"]
        for c in sorted(comments, key=lambda c: c.get("id", 0)):
            cid = c.get("id", 0)
            if cid <= cursor:
                continue
            cursor = cid
            if not self.hears(c.get("creator")):
                continue
            text = plain(c.get("content"))
            on = c.get("parent") or {}
            mention = cid in mentions or bool(self._acting_sgid and self._acting_sgid in (c.get("content") or ""))
            if self.dry:
                self.log(f"dry thread {thread}: owner {'mention' if mention else 'comment'} {cid}")
                continue
            rec.update(parent_type=on.get("type") or rec.get("parent_type"), title=on.get("title") or rec.get("title"))
            self.record({"kind": "mention" if mention else "thread-comment", "bucket": int(bucket), "parent": thread,
                         "parent_type": rec.get("parent_type"), "title": rec.get("title"), "comment": cid,
                         "question": "?" in text, "url": c.get("app_url") or on.get("app_url"), "text": text,
                         "at": c.get("created_at"), **self.author(c.get("creator"))})
            rec.setdefault("comments", []).append(cid)
            rec.setdefault("ack", []).append([cid, EYES if mention or "?" in text else THUMBS])
            self.log(f"new owner {'mention' if mention else 'comment'} on {rec.get('parent_type') or 'thread'} {thread}: {cid}")
        self.read_boosts(rec, "thread-comment", {"bucket": int(bucket), "parent": thread, "title": rec.get("title")},
                         bucket=bucket, counted=[(c.get("id"), c.get("app_url"), c.get("boosts_count") or 0) for c in comments])
        self.acknowledge(f"thread {thread}", rec, bucket)
        if not self.dry:
            rec["cursor"] = cursor
            state[key] = rec
            self.save_json("threads.json", state)

    def record_unmonitored(self, kind, rtype, rid, who, at=None, rec=None, on=None, bucket=None, ref=None):
        """Record owner input no behavior handles as an `unmonitored` record, once per kind of thing.

        The key is "<kind>/<recording type>", in the event feed's words so keys recorded
        before notifications replaced it still apply: `kind` is what happened
        (comment.created, chat.line.created, boost.created, todo.assignment_changed,
        todo.completed) and the type is, for a comment, the type of what it is on, e.g.
        "comment.created/Document". unmonitored.json keeps each key with the first
        notification (or boost) recorded, how many were seen and, once the agent marks it
        handled, the decision; a key already there is only counted, so the owner is asked
        once. `rtype` None means recording `rid`'s own type, refetched; otherwise the
        recording (or `rec`, already fetched) is read only for a new key, for its title,
        excerpt and link, and a failed read still records what is known. `who` is the
        person, `on` the parent a comment is on, `bucket` where `rid` is (default the
        project) and `ref` the notification or boost id. A dry run records and saves
        nothing. Returns the key when recorded.
        """
        state = self.load("unmonitored.json", {})
        if rtype is None or f"{kind}/{rtype}" not in state:
            if rec is None:
                errors = self.errors
                try:
                    rec = self.recording(rid, bucket)
                except RuntimeError as e:
                    self.log(f"unmonitored: recording {rid}: {e}")
                    rec, self.errors = {}, errors  # recorded anyway, so the caller has nothing to retry
            rtype = rtype or recording_type(rec.get("type")) or "unknown"
        key = f"{kind}/{rtype}"
        if key in state:
            if not self.dry:
                state[key]["seen"] = state[key].get("seen", 1) + 1
                self.save_json("unmonitored.json", state)
            return None
        if self.dry:
            self.log(f"dry unmonitored: {key} ({ref})")
            return None
        title = (on or {}).get("title") or rec.get("title") or rec.get("subject")
        texts = (plain(rec.get(f)) for f in ("content", "description"))  # a to-do's content is its name; its notes are the description
        text = next((x for x in texts if x and x != title), "")
        who = {"id": (who or {}).get("id"), "name": (who or {}).get("name") or (rec.get("creator") or {}).get("name")}
        self.record({"kind": "unmonitored", "key": key, "event_type": kind, "recording_type": rtype,
                     "notification": ref, "recording": rid, "title": title,
                     "text": text[:300] + ("..." if len(text) > 300 else ""), "creator": who, **self.author(who),
                     "url": rec.get("app_url") or (on or {}).get("app_url"), "at": at})
        state[key] = {"notification": ref, "recording": rid, "recorded_at": now_iso(), "seen": 1}
        self.save_json("unmonitored.json", state)
        self.log(f"unmonitored owner input: {key} ({ref})")
        return key

    # --- commands: explicit posts, run by the agent ---

    def reply(self, rid, text, again=False):
        """Answer the captain's comment or line `rid` where it was made, then take the acting user's 👀 off it.

        A card comment is answered with a comment on that card, a chat or Ping line with
        a new line in that chat or Ping, and a to-do, message or followed thread's comment (or
        mention) with a comment on that to-do, message or thread (as Markdown, rendered by the CLI). Run only by the relaying agent; the sync
        itself never posts comments. A reply is recorded in the "replied" list and a
        second one is refused unless `again`. A failed post removes nothing, so the
        👀 stays.
        """
        ping = next((c for c, r in self.load("pings.json", {}).get("pings", {}).items() if rid in r.get("lines", [])), None)
        if ping is not None:
            return self.reply_ping(ping, rid, text, again)
        chats = self.load("chats.json", {})
        chat = next((c for c, r in chats.items() if rid in r.get("lines", [])), None)
        todo_key = None
        if chat is None:
            todo_key = next((k for k, r in self.load("todos.json", {}).items() if rid in r.get("comments", [])), None)
        if todo_key is not None:
            return self.reply_todo(todo_key, rid, text, again)
        post = None
        if chat is None:
            post = next((k for k, r in self.load("messages.json", {}).get("posts", {}).items() if rid in r.get("comments", [])), None)
        if post is not None:
            return self.reply_message(post, rid, text, again)
        thread = None if chat is not None else next(
            (k for k, r in self.load("threads.json", {}).items() if rid in r.get("comments", [])), None)
        if thread is not None:
            return self.reply_thread(thread, rid, text, again)
        if chat is not None:
            store, rec = ("chats.json", chats), chats[chat]
            target = f"/buckets/{self.project}/chats/{chat}/lines.json"
        else:
            cards = self.load("map.json", {})
            rec = next((r for r in cards.values() if rid in r.get("comments", [])), None)
            if rec is None:
                raise RuntimeError(f"no card in map.json, chat in chats.json, Ping in pings.json, to-do in todos.json, "
                                   f"message in messages.json or thread in threads.json has {rid}")
            store = ("map.json", cards)
            target = f"/buckets/{self.project}/recordings/{rec['card']}/comments.json"
        where = f"chat {chat}" if chat is not None else f"card {rec['card']}"
        if rid in rec.get("replied", []) and not again:
            self.log(f"reply {rid}: already replied, pass --again to post another")
            return False
        if self.refused(f"reply {rid}"):
            return False
        body = render_reply(text)
        if self.dry:
            self.log(f"dry reply {rid}: post in {where}, then remove {EYES}")
            return False
        payload = {"content": body, "content_type": "text/html"} if chat is not None else {"content": body}
        self.bc("api", "post", target, "-d", json.dumps(payload))
        rec.setdefault("replied", []).append(rid)
        self.save_json(*store)
        self.log(f"reply {rid}: posted in {where}")
        self.remove_eyes(rid)
        return True

    def reply_ping(self, chat, rid, text, again):
        """Answer the owner's line `rid` in Ping chat `chat` with a new line there, in the Ping's own bucket."""
        rec = self.load("pings.json", {})["pings"][chat]
        if rid in rec.get("replied", []) and not again:
            self.log(f"reply {rid}: already replied, pass --again to post another")
            return False
        if self.refused(f"reply {rid}"):
            return False
        bucket = rec["bucket"]
        if self.dry:
            self.log(f"dry reply {rid}: post in ping {chat}, then remove {EYES}")
            return False
        self.bc("api", "post", f"/buckets/{bucket}/chats/{chat}/lines.json",
                "-d", json.dumps({"content": render_reply(text), "content_type": "text/html"}))
        state = self.load("pings.json", {})
        state["pings"][chat].setdefault("replied", []).append(rid)
        self.save_json("pings.json", state)
        self.log(f"reply {rid}: posted in ping {chat}")
        self.remove_eyes(rid, bucket)
        return True

    def reply_todo(self, key, rid, text, again):
        with self.locked("todos.json"):
            rec = self.load("todos.json", {})[key]
            if rid in rec.get("replied", []) and not again:
                self.log(f"reply {rid}: already replied, pass --again to post another")
                return False
        if self.refused(f"reply {rid}"):
            return False
        if self.dry:
            self.log(f"dry reply {rid}: comment on todo {key}, then remove {EYES}")
            return False
        self.bc("comments", "create", str(rec["todo"]), "-", input=text, project=rec.get("bucket"))
        with self.locked("todos.json"):
            state = self.load("todos.json", {})
            state[key].setdefault("replied", []).append(rid)
            self.save_json("todos.json", state)
        self.log(f"reply {rid}: posted on todo {key}")
        self.remove_eyes(rid, rec.get("bucket"))
        return True

    def reply_message(self, mid, rid, text, again):
        """Answer the owner's comment `rid` on message `mid` with a comment there (Markdown)."""
        with self.locked("messages.json"):
            rec = self.load("messages.json", {})["posts"][mid]
        if rid in rec.get("replied", []) and not again:
            self.log(f"reply {rid}: already replied, pass --again to post another")
            return False
        if self.refused(f"reply {rid}"):
            return False
        if self.dry:
            self.log(f"dry reply {rid}: comment on message {mid}, then remove {EYES}")
            return False
        self.bc("comments", "create", mid, "-", input=text)
        with self.locked("messages.json"):
            state = self.load("messages.json", {})
            state["posts"][mid].setdefault("replied", []).append(rid)
            self.save_json("messages.json", state)
        self.log(f"reply {rid}: posted on message {mid}")
        self.remove_eyes(rid)
        return True

    def reply_thread(self, key, rid, text, again):
        """Answer the owner's comment `rid` on thread `key` (threads.json) with a comment (Markdown) there, in its bucket."""
        rec = self.load("threads.json", {})[key]
        if rid in rec.get("replied", []) and not again:
            self.log(f"reply {rid}: already replied, pass --again to post another")
            return False
        if self.refused(f"reply {rid}"):
            return False
        where = f"{rec.get('parent_type') or 'thread'} {rec['thread']}"
        if self.dry:
            self.log(f"dry reply {rid}: comment on {where}, then remove {EYES}")
            return False
        self.bc("comments", "create", str(rec["thread"]), "-", input=text, project=rec.get("bucket"))
        state = self.load("threads.json", {})
        state[key].setdefault("replied", []).append(rid)
        self.save_json("threads.json", state)
        self.log(f"reply {rid}: posted on {where}")
        self.remove_eyes(rid, rec.get("bucket"))
        return True

    def remove_eyes(self, rid, bucket=None):
        for b in self.boosts_by(self.acting_id(), rid, EYES, bucket):
            self.bc("api", "delete", f"/buckets/{bucket or self.project}/boosts/{b.get('id')}.json")
            self.log(f"reply {rid}: removed {EYES} boost {b.get('id')}")

    def owner_mention(self):
        """The owner's mention attachment, from their person record's sgid."""
        me = self.bc("api", "get", f"/people/{self.captain}.json") or {}
        if not me.get("attachable_sgid"):
            raise RuntimeError(f"person {self.captain} has no attachable_sgid")
        return f'<bc-attachment sgid="{html.escape(me["attachable_sgid"])}" content-type="application/vnd.basecamp.mention"></bc-attachment>'

    def ask(self, text):
        """Post `text` as a new line in the "ask_chat" chat, @mentioning the owner.

        Run only by the relaying agent; the sync itself never posts to chat. Refused,
        like `reply`, when no profile is set or it signs in as the owner.
        """
        if self.ask_chat is None:
            raise RuntimeError('config has no "ask_chat"')
        if self.refused("ask", "owner"):
            return False
        if self.dry:
            self.log(f"dry ask: post in chat {self.ask_chat}")
            return False
        body = render_reply(text)
        body = body.replace("<div>", "<div>" + self.owner_mention() + " ", 1) if body else "<div>" + self.owner_mention() + "</div>"
        line = self.bc("api", "post", f"/buckets/{self.project}/chats/{self.ask_chat}/lines.json",
                       "-d", json.dumps({"content": body, "content_type": "text/html"})) or {}
        self.log(f"ask: posted line {line.get('id')} in chat {self.ask_chat}")
        return True

    def answer(self, qid, text):
        """Answer check-in question `qid` for today as the acting user, at most once a day.

        Run only by the relaying agent; the sync itself never answers. Refused, like
        `reply`, when no profile is set or it signs in as the owner, and when the
        acting user already answered today (recorded in checkins.json or in Basecamp).
        """
        if not self.checkins:
            raise RuntimeError('config has no "checkins"')
        date = self.today().strftime("%Y-%m-%d")
        state = self.load("checkins.json", {})
        rec = state.get(str(qid), {})
        if date in rec.get("answered", []):
            self.log(f"answer {qid}: already answered {date}")
            return False
        if self.refused(f"answer {qid}", "owner"):
            return False
        if self.answered_today(qid, self.acting_id(), date):
            self.log(f"answer {qid}: already answered {date}")
            return False
        if self.dry:
            self.log(f"dry answer {qid}: answer for {date}")
            return False
        self.bc("checkins", "answer", "create", str(qid), render_reply(text), "--date", date)
        rec.setdefault("answered", []).append(date)
        state[str(qid)] = rec
        self.save_json("checkins.json", state)
        self.log(f"answer {qid}: answered for {date}")
        return True

    def need_todos(self, requests=True):
        """Refuse a to-do command the config does not allow: "todos" for any, or "assigned_todos" for a tracked to-do request."""
        if self.todos is None and not (requests and self.assigned_todos):
            raise RuntimeError('config has no "todos"' + (' or "assigned_todos"' if requests else ""))

    def todo_create(self, key, title, description="", todolist=None, due=None):
        """Create a to-do assigned to the owner and track it in todos.json under `key`.

        It goes loose on the configured to-do set ("todos": {"todoset": id}, or the
        project's only one), or into a list (`todolist`, else "todos": {"list": id}).
        The description is Markdown, rendered by the CLI. A key already tracked is
        refused, so a re-run never creates a duplicate. Returns the to-do id, or None
        when nothing was created.
        """
        self.need_todos(requests=False)
        if not key or not title.strip():
            raise ValueError("a to-do needs a key and a title")
        if title.startswith("-"):
            raise ValueError("a to-do title cannot start with '-'")
        with self.locked("todos.json"):
            have = self.load("todos.json", {}).get(key)
        if have is not None:
            self.log(f"todo create {key}: already tracked as todo {have['todo']} {have.get('url') or ''}".rstrip())
            return None
        if self.refused(f"todo create {key}", "owner"):
            return None
        todolist = todolist or self.todos.get("list")
        where = ["--list", str(todolist)] if todolist else ["--loose"] + (
            ["--todoset", str(self.todos["todoset"])] if self.todos.get("todoset") else [])
        if self.dry:
            self.log(f"dry todo create {key}: {' '.join(where)}, assigned to {self.captain}")
            return None
        args = ["todos", "create", title.strip(), *where, "--assignee", str(self.captain)]
        if description.strip():
            args.append(f"--description={description.strip()}")
        if due:
            args += ["--due", due]
        data = self.bc(*args) or {}
        tid = data["id"]
        with self.locked("todos.json"):
            state = self.load("todos.json", {})
            state[key] = {"todo": tid, "title": title.strip(), "url": data.get("app_url"), "cursor": 0,
                          "boost_counts": {}, "boost_seen": [], "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            self.save_json("todos.json", state)
        self.log(f"todo create {key}: todo {tid} {data.get('app_url') or ''}".rstrip())
        return tid

    def todo_track(self, key, tid):
        """Track an existing to-do under `key`; its comments and boosts so far are not relayed. Writes nothing to Basecamp."""
        self.need_todos(requests=False)
        with self.locked("todos.json"):
            state = self.load("todos.json", {})
            if key in state or any(r["todo"] == tid for r in state.values()):
                self.log(f"todo track {key}: key or todo {tid} already tracked")
                return False
        todo = self.bc("api", "get", f"/buckets/{self.project}/todos/{tid}.json") or {}
        comments = self.bc("comments", "list", str(tid)) or []
        cursor = max([c.get("id", 0) for c in comments], default=0)
        if self.dry:
            self.log(f"dry todo track {key}: todo {tid}, cursor {cursor}")
            return False
        with self.locked("todos.json"):
            state = self.load("todos.json", {})
            state[key] = {"todo": tid, "title": todo.get("content"), "url": todo.get("app_url"), "cursor": cursor,
                          "created": todo.get("created_at")}  # no boost state: the first read seeds it
            if todo.get("completed"):
                state[key]["completed"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.save_json("todos.json", state)
        self.log(f"todo track {key}: todo {tid}, cursor {cursor}")
        return True

    def tracked(self, ref):
        """The (key, record) of a tracked to-do, by key or by to-do id."""
        state = self.load("todos.json", {})
        if ref in state:
            return ref, state[ref]
        hit = next(((k, r) for k, r in state.items() if str(r["todo"]) == str(ref)), None)
        if hit is None:
            raise RuntimeError(f"no tracked to-do {ref} in todos.json")
        return hit

    def todo_comment(self, ref, text):
        """Post `text` (Markdown) as a comment on a tracked to-do."""
        self.need_todos()
        key, rec = self.tracked(ref)
        if not text.strip():
            raise ValueError("empty comment")
        if self.refused(f"todo comment {key}", "owner"):
            return False
        if self.dry:
            self.log(f"dry todo comment {key}: comment on todo {rec['todo']}")
            return False
        c = self.bc("comments", "create", str(rec["todo"]), "-", input=text, project=rec.get("bucket")) or {}
        self.log(f"todo comment {key}: comment {c.get('id')} on todo {rec['todo']}")
        return True

    def todo_complete(self, ref):
        """Mark a tracked to-do complete; its comments are no longer read."""
        self.need_todos()
        key, rec = self.tracked(ref)
        if rec.get("completed"):
            self.log(f"todo complete {key}: already completed {rec['completed']}")
            return False
        if rec.get("closed"):
            self.log(f"todo complete {key}: the request was closed ({rec['closed']}), not completing it")
            return False
        if self.refused(f"todo complete {key}", "owner"):
            return False
        if self.dry:
            self.log(f"dry todo complete {key}: complete todo {rec['todo']}")
            return False
        self.bc("todos", "complete", str(rec["todo"]), project=rec.get("bucket"))
        with self.locked("todos.json"):
            state = self.load("todos.json", {})
            state[key]["completed"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.save_json("todos.json", state)
        self.log(f"todo complete {key}: completed todo {rec['todo']}")
        return True

    def post_message(self, subject, body):
        """Post a message (`body` as Markdown, rendered by the CLI) on the configured "message_board"."""
        if self.message_board is None:
            raise RuntimeError('config has no "message_board"')
        if not subject.strip() or subject.startswith("-"):
            raise ValueError("a message needs a subject that does not start with '-'")
        if self.refused("post-message", "owner"):
            return False
        if self.dry:
            self.log(f"dry post-message: {subject.strip()!r} on board {self.message_board}")
            return False
        m = self.bc("messages", "create", subject.strip(), "-", "--message-board", self.message_board, input=body) or {}
        self.log(f"post-message: message {m.get('id')} {m.get('app_url') or ''}".rstrip())
        return True

    def unmonitored_handle(self, key, decision):
        """Mark unmonitored key `key` handled with the owner's `decision`; it stays quiet until forgotten."""
        state = self.load("unmonitored.json", {})
        if key not in state:
            raise ValueError(f"no unmonitored key {key!r} (see sync.py unmonitored list)")
        if not decision.strip():
            raise ValueError("a decision is needed")
        if self.dry:
            self.log(f"dry unmonitored handle: {key}")
            return
        state[key]["handled"] = {"at": now_iso(), "decision": decision.strip()}
        self.save_json("unmonitored.json", state)
        self.log(f"unmonitored: {key} handled: {decision.strip()}")

    def unmonitored_forget(self, key):
        """Drop unmonitored key `key`, so the next such event is recorded (and put to the owner) again."""
        state = self.load("unmonitored.json", {})
        if key not in state:
            raise ValueError(f"no unmonitored key {key!r} (see sync.py unmonitored list)")
        if self.dry:
            self.log(f"dry unmonitored forget: {key}")
            return
        del state[key]
        self.save_json("unmonitored.json", state)
        self.log(f"unmonitored: {key} forgotten")


def request_fields(rec):
    """The fields that mark a record as part of a to-do request: its title and project, for a to-do request's record; none otherwise."""
    return {"request": True, "title": rec.get("title"), "project": request_project(rec)} if rec.get("request") else {}


def request_project(rec):
    """The project a to-do request is in, {"id", "name"}; a request tracked before projects were kept has no id (the configured one)."""
    return {"id": rec.get("bucket"), "name": rec.get("project_name")}


def todo_digest(todo):
    """A digest of a to-do's name and description, to notice an edit to either."""
    return hashlib.sha256(f"{todo.get('content') or ''}\n{todo.get('description') or ''}".encode()).hexdigest()


def plain(content):
    """Basecamp rich text as one line of plain text."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", content or ""))).strip()


def recording_type(t):
    """A recording type as an unmonitored key names it: every kind of chat line is "Chat::Lines"."""
    return "Chat::Lines" if (t or "").startswith("Chat::Lines") else t


# The thread a notification's app_url names, by its path under /buckets/<id>/, as a recording type.
THREADS = {"todos": "Todo", "messages": "Message", "cards": "Kanban::Card", "documents": "Document",
           "uploads": "Upload", "question_answers": "Question::Answer", "questions": "Question",
           "schedule_entries": "Schedule::Entry", "chats": "Chat::Transcript"}


def parse_reading(r):
    """Where a notification (an item of /my/readings.json) points: {"bucket", "thread", "anchor", "path"}.

    The bucket and the thread (the commented recording, the chat, the to-do) come from its
    subscription_url, the anchor (the first unread comment, the mentioning line, the
    to-do) from readable_identifier (base64 of gid://bc3/Recording/<id>), and the path
    (todos, messages, cards, chats, ... or the Ping's circles) from app_url. Any part it
    lacks is None.
    """
    m = re.search(r"/buckets/(\d+)/recordings/(\d+)/", r.get("subscription_url") or "")
    ident = r.get("readable_identifier") or ""
    try:
        gid = base64.b64decode(ident + "=" * (-len(ident) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        gid = ""
    a = re.fullmatch(r"gid://bc3/Recording/(\d+)", gid)
    p = re.search(r"basecamp\.com/\d+/(?:buckets/\d+/(?:card_tables/)?)?([a-z_]+)", r.get("app_url") or "")
    return {"bucket": m.group(1) if m else None, "thread": int(m.group(2)) if m else None,
            "anchor": int(a.group(1)) if a else None, "path": p.group(1) if p else None}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def later(at, since):
    """True when ISO time `at` is after ISO time `since`; an unreadable `at` counts as later, so nothing is dropped."""
    try:
        return datetime.fromisoformat(at) > datetime.fromisoformat(since)
    except (TypeError, ValueError):
        return True


def is_due(q, now):
    """Whether check-in question `q` has come due by `now` (local time), from its schedule."""
    if q.get("paused"):
        return False
    sch = q.get("schedule") or {}
    if (now.isoweekday() % 7) not in sch.get("days", []):
        return False
    if (sch.get("start_date") or "") > now.strftime("%Y-%m-%d"):
        return False
    end = sch.get("end_date")
    if end and end < now.strftime("%Y-%m-%d"):
        return False
    return (now.hour, now.minute) >= (int(sch.get("hour", 0)), int(sch.get("minute", 0)))


def render_reply(text):
    """Plain text to comment HTML: escaped, one <div> per paragraph, no raw newlines."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join("<div>" + "<br>".join(html.escape(l) for l in p.splitlines()) + "</div>" for p in paras)


def is_emoji(content, emoji):
    return is_thumbs_up(content) if emoji == THUMBS else re.sub(r"<[^>]+>", "", content).strip() == emoji


def is_thumbs_up(content):
    """True for 👍 alone, with or without a skin-tone modifier or variation selector."""
    return re.fullmatch("\U0001F44D[\U0001F3FB-\U0001F3FF]?️?", re.sub(r"<[^>]+>", "", content).strip()) is not None


def parse_lavish(text, data):
    """Map the open sessions in plain `lavish-axi` output to the task dir under `data` holding each file."""
    root = os.path.realpath(data) + os.sep
    out, inside = {}, False
    for row in csv.reader(text.splitlines()):
        if row and re.match(r"sessions\[\d+\]\{", row[0]):
            inside = True
            continue
        if not inside:
            continue
        if not row or not row[0].startswith("  "):
            break
        if len(row) < 3 or row[1].strip() != "open":
            continue
        f = os.path.realpath(row[0].strip())
        if not f.startswith(root):
            continue
        task = f[len(root):].split(os.sep)[0]
        if task and f[len(root) + len(task):].startswith(os.sep):
            out.setdefault(task, []).append(row[2].strip())
    return out


SECTIONS = {"in_flight": "In flight", "queued": "Queued", "held": "Queued", "done": "Done"}
PR_RE = r"https://github\.com/\S+?/pull/\d+"


def parse_show(text):
    """Parse `tasks-axi show --full` output: `  key: value`, strings JSON-quoted."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"  (\w+): (.*)$", line)
        if m:
            v = m.group(2)
            out[m.group(1)] = json.loads(v) if v.startswith('"') else v
    return out


def none(v):
    return None if v in (None, "", "-", "none") else v


def task_item(t):
    """Map tasks-axi fields to the item shape the column rules use.

    The one parsing fallback is the title: tasks-axi keeps PR URLs inside it, and
    on a row with repeated `(field: ...)` groups it keeps the leading ones too,
    so both are cut out here the way the original markdown parser did.
    """
    title = re.split(r" \((?:repo|kind|priority|since|merged|hold|closed)[:)]", t.get("title", ""))[0]
    links = re.findall(PR_RE, t.get("title", ""))
    for link in (none(t.get("links")) or "").split(","):
        links += re.findall(PR_RE, link)
    deps = none(t.get("deps")) or ""
    return {"id": t["id"], "section": SECTIONS.get(t.get("state"), "Queued"),
            "title": re.sub(r"\s*https://\S+", "", title).strip(),
            "links": list(dict.fromkeys(links)), "repo": none(t.get("repo")) or "",
            "kind": none(t.get("kind")) or "", "hold": none(t.get("hold_reason")),
            "hold_kind": none(t.get("hold_kind")), "until": none(t.get("hold_until")),
            "blocked_by": [d[len("blocked-by:"):] for d in deps.split(",") if d.startswith("blocked-by:")]}
