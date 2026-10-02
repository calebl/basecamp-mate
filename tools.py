"""The tools layer: small, single-purpose Basecamp operations, with no policy.

A tool does one explicit thing to the configured account and project, through the
`basecamp` CLI (or `gh`, `tasks-axi`, `lavish-axi` for the local and GitHub reads):

  - readers poll Basecamp and append what they find to pending-comments.jsonl, once
    each, with a cursor or seen-list kept beside the config: card comments and
    approvals, chat lines, due check-in questions, comments on tracked to-dos;
  - commands post exactly what the agent hands them: `reply`, `ask`, `answer`,
    `todo create|track|comment|complete`, `post-message`;
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
import contextlib, csv, fcntl, html, json, os, re, subprocess, time, tomllib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

THUMBS, EYES = "\U0001F44D", "\U0001F440"


class Tools:
    def __init__(self, home, config_path, dry=False, runner=subprocess.run):
        self.home = os.path.abspath(home)
        self.dir = os.path.dirname(os.path.abspath(config_path))
        self.cfg = cfg = json.load(open(config_path))
        self.account, self.project = str(cfg["account"]), str(cfg["project"])
        self.captain = int(cfg["captain"])  # the owner: assignee, and the only person whose comments are relayed
        self.profile = cfg.get("profile")
        self.ask_chat = str(cfg["ask_chat"]) if cfg.get("ask_chat") is not None else None
        self.checkins = cfg.get("checkins")
        self.todos = cfg.get("todos")
        self.message_board = str(cfg["message_board"]) if cfg.get("message_board") is not None else None
        self.dry, self.run = dry, runner
        self._acting = False  # acting person id once resolved; None when it can't be
        self._acting_sgid = None  # the acting person's mention sgid, to spot @mentions in chat
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
        """Hold an exclusive lock on state file `name` while it is read, changed and saved; a dry run writes no lock file."""
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

    def record(self, rec):
        """Append one pending record for the agent."""
        with open(self.path("pending-comments.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def bc(self, *args, input=None):
        prof = ["-P", self.profile] if self.profile else []
        cmd = ["basecamp", "-a", self.account, *prof, *args, "-p", self.project, "--json"]
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
                raise RuntimeError(f"basecamp {' '.join(args[:3])}: {out.get('error')}")
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

    def acknowledge(self, key, rec):
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
                bid = self.boost(me, rid, emoji)
                self.log(f"acknowledged {key}: {emoji} on {rid}")
            except RuntimeError as e:
                self.log(f"acknowledge {key} {rid} failed, retrying next run: {e}")
                continue
            rec.setdefault("acked", []).append([rid, emoji, bid])
            rec["ack"].remove([rid, emoji])

    def boosts_by(self, me, rid, emoji):
        path = f"/buckets/{self.project}/recordings/{rid}/boosts.json"
        return [b for b in self.bc("api", "get", path) or []
                if (b.get("booster") or {}).get("id") == me and is_emoji(b.get("content", ""), emoji)]

    def boost(self, me, rid, emoji):
        """The acting user's boost id for `emoji` on `rid`, posting one only when absent."""
        have = self.boosts_by(me, rid, emoji)
        if have:
            return have[0].get("id")
        path = f"/buckets/{self.project}/recordings/{rid}/boosts.json"
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

    # --- readers: poll Basecamp, append pending records, acknowledge ---

    def read_card_comments(self, task, repo, key, rec):
        """Record the captain's new comments on card rec["card"]: "question" when it has "?", else "comment"."""
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
            if (c.get("creator") or {}).get("id") == self.captain:
                text = re.sub(r"<[^>]+>", "", c.get("content", ""))
                kind = "question" if "?" in html.unescape(text) else "comment"
                self.record({"kind": kind, "task": task, "repo": repo, "card": rec["card"], "comment": cid,
                             "at": c.get("created_at"), "text": text})
                rec.setdefault("ack", []).append([cid, EYES if kind == "question" else THUMBS])
                self.log(f"new captain {kind} on {key}: {cid}")

    def read_card_boosts(self, task, repo, key, rec):
        """Record the captain's 👍 on card rec["card"] as an approval. A dry run reads and logs only."""
        path = f"/buckets/{self.project}/recordings/{rec['card']}/boosts.json"
        try:
            boosts = self.bc("api", "get", path) or []
        except RuntimeError as e:
            self.log(f"boosts {key}: {e}")
            return
        for b in boosts:
            bid = b.get("id")
            if bid in rec.get("boosts", []):
                continue
            if (b.get("booster") or {}).get("id") != self.captain or not is_thumbs_up(b.get("content", "")):
                continue
            if self.dry:
                self.log(f"dry {key}: captain approval boost {bid}")
                self.acknowledge(key, dict(rec, ack=[*rec.get("ack", []), [rec["card"], THUMBS]]))
                continue
            rec.setdefault("boosts", []).append(bid)
            self.record({"kind": "approval", "task": task, "repo": repo, "card": rec["card"],
                         "url": f"https://app.basecamp.com/{self.account}/buckets/{self.project}/card_tables/cards/{rec['card']}",
                         "boost": bid, "at": b.get("created_at")})
            if [rec["card"], THUMBS] not in rec.setdefault("ack", []):
                rec["ack"].append([rec["card"], THUMBS])
            self.log(f"captain approval on {key}: boost {bid}")

    def read_chats(self, chats, every_line=()):
        """Record the captain's chat lines in `chats` as chat-question records.

        Per chat, chats.json keeps a cursor (the newest line id seen). The first run
        only sets the cursor, so old history is not relayed. A captain line that
        mentions the acting user or contains "?" is recorded and queued for a 👀; every
        other line is skipped, unless the chat is in `every_line`, where every owner
        line is recorded. A dry run reads and logs only.
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
                if lid <= cursor:
                    continue
                cursor = lid
                if first or (ln.get("creator") or {}).get("id") != self.captain:
                    continue
                content = ln.get("content", "")
                text = html.unescape(re.sub(r"<[^>]+>", "", content)).strip()
                if "?" not in text and chat not in every_line:
                    self.acting_id()
                    if not (self._acting_sgid and self._acting_sgid in content):
                        continue
                if self.dry:
                    self.log(f"dry chat {chat}: captain question {lid}")
                    self.acknowledge(f"chat {chat}", dict(rec, ack=[*rec.get("ack", []), [lid, EYES]]))
                    continue
                self.record({"kind": "chat-question", "chat": int(chat), "line": lid,
                             "url": ln.get("app_url") or f"https://3.basecamp.com/{self.account}/buckets/{self.project}/chats/{chat}@{lid}",
                             "text": text, "at": ln.get("created_at")})
                rec.setdefault("lines", []).append(lid)
                rec.setdefault("ack", []).append([lid, EYES])
                self.log(f"new captain chat question in {chat}: {lid}")
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
        once per day as a `checkin`; nothing is answered here.
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
                             "title": q.get("title"), "url": q.get("app_url"), "at": now.isoformat(timespec="seconds")})
                rec.setdefault("recorded", []).append(date)
                state[qid] = rec
                self.save_json("checkins.json", state)
                self.log(f"checkin {qid} due {date}")

    def answered_today(self, qid, me, date):
        """True when `me` already answered question `qid` for `date` in Basecamp."""
        if me is None:
            return False
        answers = self.bc("api", "get", f"/buckets/{self.project}/questions/{qid}/answers.json") or []
        return any((a.get("creator") or {}).get("id") == me and (a.get("group_on") or (a.get("created_at") or "")[:10]) == date
                   for a in answers)

    def read_todo_comments(self):
        """Record the owner's new comments on each open tracked to-do as `todo-comment` records.

        todos.json keeps, per key, the to-do id and a cursor (the newest comment id
        seen). Each owner comment newer than the cursor is recorded once and queued
        for a 👀 when it contains "?", a 👍 otherwise; other people's comments, the
        agent's own included, only move the cursor. Completed to-dos are not read.
        A dry run reads and logs only.
        """
        for key in sorted(self.load("todos.json", {})):
            with self.locked("todos.json"):
                state = self.load("todos.json", {})
                rec = state.get(key)
                if rec is None or rec.get("completed"):
                    continue
                try:
                    comments = self.bc("comments", "list", str(rec["todo"])) or []
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
                    if (c.get("creator") or {}).get("id") != self.captain:
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
                                 "text": text, "at": c.get("created_at")})
                    rec.setdefault("comments", []).append(cid)
                    self.log(f"new owner comment on todo {key}: {cid}")
                self.acknowledge(f"todo {key}", rec)
                if not self.dry:
                    rec["cursor"] = cursor
                    self.save_json("todos.json", state)

    # --- commands: explicit posts, run by the agent ---

    def reply(self, rid, text, again=False):
        """Answer the captain's comment or line `rid` where it was made, then take the acting user's 👀 off it.

        A card comment is answered with a comment on that card, a chat line with a
        new line in that chat, and a to-do comment with a comment on that to-do (as
        Markdown, rendered by the CLI). Run only by the relaying agent; the sync
        itself never posts comments. A reply is recorded in the "replied" list and a
        second one is refused unless `again`. A failed post removes nothing, so the
        👀 stays.
        """
        chats = self.load("chats.json", {})
        chat = next((c for c, r in chats.items() if rid in r.get("lines", [])), None)
        todo_key = None
        if chat is None:
            todo_key = next((k for k, r in self.load("todos.json", {}).items() if rid in r.get("comments", [])), None)
        if todo_key is not None:
            return self.reply_todo(todo_key, rid, text, again)
        if chat is not None:
            store, rec = ("chats.json", chats), chats[chat]
            target = f"/buckets/{self.project}/chats/{chat}/lines.json"
        else:
            cards = self.load("map.json", {})
            rec = next((r for r in cards.values() if rid in r.get("comments", [])), None)
            if rec is None:
                raise RuntimeError(f"no card in map.json, chat in chats.json or to-do in todos.json has {rid}")
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
        self.bc("comments", "create", str(rec["todo"]), "-", input=text)
        with self.locked("todos.json"):
            state = self.load("todos.json", {})
            state[key].setdefault("replied", []).append(rid)
            self.save_json("todos.json", state)
        self.log(f"reply {rid}: posted on todo {key}")
        self.remove_eyes(rid)
        return True

    def remove_eyes(self, rid):
        for b in self.boosts_by(self.acting_id(), rid, EYES):
            self.bc("api", "delete", f"/buckets/{self.project}/boosts/{b.get('id')}.json")
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

    def need_todos(self):
        if self.todos is None:
            raise RuntimeError('config has no "todos"')

    def todo_create(self, key, title, description="", todolist=None, due=None):
        """Create a to-do assigned to the owner and track it in todos.json under `key`.

        It goes loose on the configured to-do set ("todos": {"todoset": id}, or the
        project's only one), or into a list (`todolist`, else "todos": {"list": id}).
        The description is Markdown, rendered by the CLI. A key already tracked is
        refused, so a re-run never creates a duplicate. Returns the to-do id, or None
        when nothing was created.
        """
        self.need_todos()
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
                          "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            self.save_json("todos.json", state)
        self.log(f"todo create {key}: todo {tid} {data.get('app_url') or ''}".rstrip())
        return tid

    def todo_track(self, key, tid):
        """Track an existing to-do under `key`; its comments so far are not relayed. Writes nothing to Basecamp."""
        self.need_todos()
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
                          "created": todo.get("created_at")}
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
        c = self.bc("comments", "create", str(rec["todo"]), "-", input=text) or {}
        self.log(f"todo comment {key}: comment {c.get('id')} on todo {rec['todo']}")
        return True

    def todo_complete(self, ref):
        """Mark a tracked to-do complete; its comments are no longer read."""
        self.need_todos()
        key, rec = self.tracked(ref)
        if rec.get("completed"):
            self.log(f"todo complete {key}: already completed {rec['completed']}")
            return False
        if self.refused(f"todo complete {key}", "owner"):
            return False
        if self.dry:
            self.log(f"dry todo complete {key}: complete todo {rec['todo']}")
            return False
        self.bc("todos", "complete", str(rec["todo"]))
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
