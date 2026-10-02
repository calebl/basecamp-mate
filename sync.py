#!/usr/bin/env python3
"""Mirror a firstmate home's backlog onto Basecamp card tables.

Deterministic, no model calls. The backlog is the source of truth; Basecamp is a
view of it. Safety bounds, enforced here:
  - only the configured account, project and card tables are touched;
  - cards are created, updated, moved, assigned and unassigned, never deleted,
    trashed or archived, and the sync posts nothing to chat or as a comment; its
    other writes are a 👀 or 👍 boost acknowledging a captured captain comment or
    approval, and its one automatic post: a Message Board announcement per new
    GitHub release of a repo in the optional "releases" config. Only the explicit
    `sync.py reply` command posts a comment (an answer to a captain question) and
    removes the acting user's own 👀. Two more explicit commands post, never the
    sync run: `sync.py ask` (a new chat line @mentioning the owner) and
    `sync.py answer` (a check-in answer, at most once per question per day);
  - the id map (map.json) makes re-runs update the same card instead of
    duplicating it.
New comments the captain writes on cards, and his 👍 boost on a card assigned to
him (an approval of every recommendation on it), are appended to
pending-comments.jsonl for firstmate to relay; they are never acted on here.
When the profile signs in as someone other than the captain, that user boosts
each captured comment (or, for an approval, the card) once, as a visible
acknowledgement: 👀 on a question (a comment containing "?"), 👍 otherwise; a
failed boost is retried on the next run. `sync.py reply`, run by the relaying
agent, posts the answer on the card and then removes the 👀.

All runtime state (map.json, sync.log, pending-comments.jsonl and the hand-kept
extra-repos.json, figuring.json, not-now.json, skip.json, decisions.json, boards.json, chats.json) lives in the directory
that holds the config file.

Open Lavish review boards (read once per run from `lavish-axi`) whose file sits under
<home>/data/<task>/ are linked on that task's card as "Plan board". If lavish-axi is
missing or fails, each card keeps the board links it last had.

Usage: sync.py --home <firstmate home> --config <config.json> [--dry-run] [--include-prereleases]
       sync.py reply --home <home> --config <config.json> --recording <question comment id> --body-file <file> [--again] [--dry-run]
       sync.py ask --home <home> --config <config.json> --body-file <file> [--dry-run]
       sync.py answer --home <home> --config <config.json> --question <check-in question id> --body-file <file> [--dry-run]
       sync.py init <project URL> --login <profile> --home <home> [--captain <id or email>] [--repo-map TABLE=REPO] [--dry-run]
"""
import argparse, csv, hashlib, html, json, os, re, subprocess, sys, time, tomllib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

COLUMNS = ("Triage", "Not now", "Figuring it out", "In progress", "Ready for QA", "Done")
SECTIONS = {"in_flight": "In flight", "queued": "Queued", "held": "Queued", "done": "Done"}
PR_RE = r"https://github\.com/\S+?/pull/\d+"


class Sync:
    def __init__(self, home, config_path, dry=False, runner=subprocess.run, prereleases=False):
        self.home = os.path.abspath(home)
        self.dir = os.path.dirname(os.path.abspath(config_path))
        cfg = json.load(open(config_path))
        self.account, self.project = str(cfg["account"]), str(cfg["project"])
        self.captain = int(cfg["captain"])
        self.profile = cfg.get("profile")
        # A chat is an id, or {"chat": id, "every_line": true} to relay every owner line.
        self.chats, self.every_line = [], set()
        for c in cfg.get("chats", []):
            cid = str(c["chat"] if isinstance(c, dict) else c)
            self.chats.append(cid)
            if isinstance(c, dict) and c.get("every_line"):
                self.every_line.add(cid)
        self.ask_chat = str(cfg["ask_chat"]) if cfg.get("ask_chat") is not None else None
        self.checkins = cfg.get("checkins")
        if self.checkins is not None and not isinstance(self.checkins.get("questionnaires"), list):
            raise ValueError('config "checkins" needs "questionnaires" (a list of questionnaire ids)')
        self.tables = cfg["tables"]
        self.repo_map = cfg["repos"]
        self.releases = cfg.get("releases")
        if self.releases is not None:
            if not self.releases.get("board") or not isinstance(self.releases.get("repos"), dict):
                raise ValueError('config "releases" needs "board" (message board id) and "repos" ({"owner/name": ...})')
        self.prereleases = prereleases or bool((self.releases or {}).get("prereleases"))
        for board, t in self.tables.items():
            missing = [c for c in ("table", *COLUMNS) if c not in t]
            if missing:
                raise ValueError(f"config table {board} lacks {missing}")
        for repo, board in self.repo_map.items():
            if board not in self.tables:
                raise ValueError(f"config repo {repo} names unknown board {board}")
        self.dry, self.run = dry, runner
        self._acting = False  # acting person id once resolved; None when it can't be
        self._acting_sgid = None  # the acting person's mention sgid, to spot @mentions in chat
        self.data = os.path.join(self.home, "data")
        self.state = os.path.join(self.home, "state")

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

    def path(self, name):
        return os.path.join(self.dir, name)

    def load(self, name, default):
        p = self.path(name)
        return json.load(open(p)) if os.path.exists(p) else default

    def log(self, msg):
        line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
        print(line)
        with open(self.path("sync.log"), "a") as f:
            f.write(line + "\n")

    def bc(self, *args):
        prof = ["-P", self.profile] if self.profile else []
        cmd = ["basecamp", "-a", self.account, *prof, *args, "-p", self.project, "--json"]
        for attempt in range(3):
            r = self.run(cmd, capture_output=True, text=True, timeout=120)
            try:
                out = json.loads(r.stdout)
            except ValueError:
                out = {"ok": False, "error": (r.stdout + r.stderr)[:300]}
            if out.get("ok"):
                return out.get("data")
            if not out.get("retryable") or attempt == 2:
                raise RuntimeError(f"basecamp {' '.join(args[:3])}: {out.get('error')}")
            time.sleep(3)

    # --- backlog through tasks-axi, addressed the way bin/fm-tasks-axi.sh does ---

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

    def parse_backlog(self):
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

    def column_for(self, it, notnow):
        if it["section"] != "Done" and it["id"] in notnow and not (it["hold"] and it["hold_kind"] == "captain"):
            return "Not now", False
        parked = it["hold_kind"] == "parked" or (it["hold"] or "").startswith("parked")
        if it["section"] == "Done":
            return "Done", False
        if it["hold"] and it["hold_kind"] == "captain":
            return ("Not now", False) if it["until"] else ("Figuring it out", True)
        if parked or it["until"]:
            return "Not now", False
        if it["section"] == "In flight":
            return ("Ready for QA", False) if self.meta_pr(it["id"]) else ("In progress", False)
        return "Triage", False

    def body_for(self, it, col, waiting, notnow, decisions=None, boards=()):
        decisions = decisions or {}
        parts = [f"<div><strong>Task</strong>: {html.escape(it['id'])} &middot; <strong>Status</strong>: {col}</div>"]
        if boards:
            links = ", ".join(f'<a href="{html.escape(u)}">{html.escape(u)}</a>' for u in boards)
            parts.append(f"<div><strong>Plan board</strong>: {links}</div>")
        if waiting and it["id"] in decisions:
            parts.append(render_decision(decisions[it["id"]]))
        elif waiting:
            parts.append(f"<div><strong>Waiting on you</strong>: {html.escape(it['hold'])}</div>")
        elif it["id"] in notnow:
            parts.append(f"<div><strong>Not now</strong>: {html.escape(notnow[it['id']])}</div>")
        elif it["hold"]:
            parts.append(f"<div><strong>On hold</strong>: {html.escape(it['hold'])}</div>")
        if it["blocked_by"]:
            parts.append(f"<div><strong>After</strong>: {html.escape(', '.join(it['blocked_by']))}</div>")
        pr = self.meta_pr(it["id"])
        prs = list(dict.fromkeys(it["links"] + ([pr] if pr else [])))
        if prs:
            parts.append("<div><strong>PRs</strong>:</div><ul>" + "".join(f'<li><a href="{html.escape(u)}">{html.escape(u)}</a></li>' for u in prs) + "</ul>")
        parts.append("<div><em>Kept in sync from the backlog; edits to this text are overwritten. Comments are read and relayed.</em></div>")
        return "".join(parts)

    def save(self, cards):
        self.save_json("map.json", cards)

    def main(self, items=None):
        cards = self.load("map.json", {})
        extra = self.load("extra-repos.json", {})
        notnow = self.load("not-now.json", {})
        figuring = self.load("figuring.json", {})
        skip = set(self.load("skip.json", []))
        decisions = self.load("decisions.json", {})
        board_owners = self.load("boards.json", {})
        live = self.lavish_boards()
        counts, unplaced, wanted = {}, [], set()
        plan = {"create": 0, "update": 0, "move": 0, "assign": 0, "unassign": 0}
        for it in (self.parse_backlog() if items is None else items):
            if it["id"] in skip:
                continue
            repos = []
            if it["repo"] in self.repo_map:
                repos.append(self.repo_map[it["repo"]])
            repos += [r for r in extra.get(it["id"], []) if r not in repos]
            if not repos:
                unplaced.append(f"{it['id']} (repo {it['repo'] or 'none'})")
                continue
            col, waiting = self.column_for(it, notnow)
            if col == "Triage" and it["id"] in figuring:
                col = "Figuring it out"
                it = dict(it, hold=it["hold"] or figuring[it["id"]])
            if live is not None:
                owners = [it["id"]] + [o for o in board_owners.get(it["id"], []) if o != it["id"]]
                task_boards = list(dict.fromkeys(u for o in owners for u in live.get(o, [])))
            for repo in repos:
                key = f"{it['id']}|{repo}"
                wanted.add(key)
                t = self.tables[repo]
                title = it["title"][:240]
                rec = cards.get(key)
                boards = task_boards if live is not None else (rec or {}).get("boards", [])
                body = self.body_for(it, col, waiting, notnow, decisions, boards)
                digest = hashlib.sha256((title + body).encode()).hexdigest()
                if self.dry:
                    if rec is None:
                        plan["create"] += 1
                        self.log(f"dry {key}: create -> {col}{' (assign)' if waiting else ''}")
                    else:
                        acts = []
                        if rec.get("digest") != digest:
                            acts.append("update")
                        if rec.get("column") != col:
                            acts.append(f"move {rec.get('column')} -> {col}")
                        if bool(rec.get("assigned")) != waiting:
                            acts.append("assign" if waiting else "unassign")
                        for a in acts:
                            plan[a.split()[0]] += 1
                        self.log(f"dry {key}: {', '.join(acts) or 'unchanged'} [{col}]")
                    if rec is not None and rec.get("assigned"):
                        self.relay_boosts(it["id"], repo, key, rec)
                    if rec is not None:
                        self.acknowledge(key, rec)
                    counts[repo] = counts.get(repo, 0) + 1
                    continue
                if rec is None:
                    args = ["cards", "create", title, body, "--card-table", t["table"], "--column", t[col]]
                    if waiting:
                        args += ["--assignee", str(self.captain)]
                    data = self.bc(*args)
                    rec = {"card": data["id"], "column": col, "assigned": waiting, "digest": digest, "comments": [], "boards": boards}
                    cards[key] = rec
                    self.log(f"created {key} card {data['id']} in {col}")
                else:
                    if rec.get("digest") != digest:
                        self.bc("cards", "update", str(rec["card"]), "--card-table", t["table"], "--title", title, "--body", body)
                        rec["digest"] = digest
                        rec["boards"] = boards
                        self.log(f"updated {key}")
                    if rec.get("column") != col:
                        self.bc("cards", "move", str(rec["card"]), "--card-table", t["table"], "--to", t[col])
                        self.log(f"moved {key} {rec.get('column')} -> {col}")
                        rec["column"] = col
                    if bool(rec.get("assigned")) != waiting:
                        if waiting:
                            self.bc("cards", "update", str(rec["card"]), "--card-table", t["table"], "--assignee", str(self.captain))
                        else:
                            self.bc("unassign", str(rec["card"]), "--card", "--from", str(self.captain))
                        rec["assigned"] = waiting
                        self.log(f"{'assigned' if waiting else 'unassigned'} {key}")
                self.relay_comments(it["id"], repo, key, rec)
                if rec.get("assigned"):
                    self.relay_boosts(it["id"], repo, key, rec)
                self.acknowledge(key, rec)
                counts[repo] = counts.get(repo, 0) + 1
                self.save(cards)
        if self.chats:
            self.relay_chats()
        if self.releases:
            self.announce_releases()
        if self.checkins:
            self.relay_checkins()
        stale = sorted(k for k in cards if k not in wanted)
        self.log(("dry plan " + json.dumps(plan, sort_keys=True) + " " if self.dry else "")
                 + "counts " + json.dumps(counts, sort_keys=True) + (f" unplaced {unplaced}" if unplaced else "")
                 + (f" left-as-is {len(stale)} cards no longer in the backlog" if stale else ""))
        return plan

    def relay_comments(self, task, repo, key, rec):
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
                with open(self.path("pending-comments.jsonl"), "a") as f:
                    f.write(json.dumps({"kind": kind, "task": task, "repo": repo, "card": rec["card"], "comment": cid,
                                        "at": c.get("created_at"), "text": text}) + "\n")
                rec.setdefault("ack", []).append([cid, EYES if kind == "question" else THUMBS])
                self.log(f"new captain {kind} on {key}: {cid}")

    def relay_chats(self):
        """Capture the captain's questions for firstmate in the configured chats.

        Per chat, chats.json keeps a cursor (the newest line id seen). The first run
        only sets the cursor, so old history is not relayed. A captain line that
        mentions the acting user or contains "?" is appended to the pending file as a
        chat-question and queued for a 👀; every other line is skipped, unless the
        chat is configured with "every_line", where every owner line is captured.
        A dry run reads and logs only.
        """
        state = self.load("chats.json", {})
        for chat in self.chats:
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
                if "?" not in text and chat not in self.every_line:
                    self.acting_id()
                    if not (self._acting_sgid and self._acting_sgid in content):
                        continue
                if self.dry:
                    self.log(f"dry chat {chat}: captain question {lid}")
                    self.acknowledge(f"chat {chat}", dict(rec, ack=[*rec.get("ack", []), [lid, EYES]]))
                    continue
                with open(self.path("pending-comments.jsonl"), "a") as f:
                    f.write(json.dumps({"kind": "chat-question", "chat": int(chat), "line": lid,
                                        "url": ln.get("app_url") or f"https://3.basecamp.com/{self.account}/buckets/{self.project}/chats/{chat}@{lid}",
                                        "text": text, "at": ln.get("created_at")}) + "\n")
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

    def gh(self, *args):
        r = self.run(["gh", *args], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args[:3])}: {(r.stdout + r.stderr)[:300]}")
        return json.loads(r.stdout)

    def announce_releases(self):
        """Post one Message Board announcement per new GitHub release: the sync's one automatic post.

        Per repo, releases.json keeps "since" (when the watch started), "seeded" (tags that
        existed then, never announced) and "announced" (tag -> message id). The first run
        only seeds. Drafts are always skipped and prereleases unless enabled. A failed read
        or post is logged and retried next run; it never fails the sync. Messages are never
        edited or deleted. Posted as the acting user whoever that is: an announcement is
        not an acknowledgement. A dry run reads and logs only.
        """
        state = self.load("releases.json", {})
        board = str(self.releases["board"])
        for repo, spec in self.releases["repos"].items():
            spec = spec if isinstance(spec, dict) else {"name": spec}
            name = spec.get("name") or repo.split("/")[-1]
            name = name[:1].upper() + name[1:]
            rec = state.get(repo)
            try:
                listed = self.gh("release", "list", "-R", repo, "--limit", "30",
                                 "--json", "tagName,isDraft,isPrerelease,publishedAt")
            except Exception as e:
                self.log(f"releases {repo}: {type(e).__name__}: {e}")
                continue
            if rec is None:
                tags = sorted(r["tagName"] for r in listed if not r.get("isDraft"))
                if self.dry:
                    self.log(f"dry releases {repo}: would seed the watch with {', '.join(tags) or 'no releases'}")
                    continue
                state[repo] = {"since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "seeded": tags, "announced": {}}
                self.save_json("releases.json", state)
                self.log(f"releases {repo}: watch started, seeded {', '.join(tags) or 'no releases'}")
                continue
            done = set(rec.get("seeded", [])) | set(rec.get("announced", {}))
            new = [r for r in listed if not r.get("isDraft") and (self.prereleases or not r.get("isPrerelease"))
                   and r["tagName"] not in done and (r.get("publishedAt") or "") > rec["since"]]
            for r in sorted(new, key=lambda r: r["publishedAt"]):
                tag = r["tagName"]
                subject = f"{name} {tag} released"
                if self.dry:
                    self.log(f"dry releases {repo}: post {subject!r}")
                    continue
                try:
                    rel = self.gh("release", "view", tag, "-R", repo, "--json", "tagName,body,url,isDraft,isPrerelease")
                    body = render_release(rel.get("body") or "", rel["url"], spec.get("note"))
                    msg = self.bc("api", "post", f"/buckets/{self.project}/message_boards/{board}/messages.json",
                                  "-d", json.dumps({"subject": subject, "content": body, "status": "active"})) or {}
                except Exception as e:
                    self.log(f"releases {repo} {tag}: announcement failed, retrying next run: {type(e).__name__}: {e}")
                    continue
                rec.setdefault("announced", {})[tag] = msg.get("id")
                self.save_json("releases.json", state)
                self.log(f"releases {repo}: announced {tag} as message {msg.get('id')}")

    def save_json(self, name, value):
        tmp = self.path(name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(value, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path(name))

    def relay_boosts(self, task, repo, key, rec):
        """Queue the captain's thumbs up on a card assigned to him as an approval record.

        Only cards assigned to the captain are read, so the calls are bounded by the
        open decisions. Nothing is acted on here; the backlog records the decision
        and the normal sync then unassigns the card. A dry run reads and logs only.
        """
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
            with open(self.path("pending-comments.jsonl"), "a") as f:
                f.write(json.dumps({"kind": "approval", "task": task, "repo": repo, "card": rec["card"],
                                    "url": f"https://app.basecamp.com/{self.account}/buckets/{self.project}/card_tables/cards/{rec['card']}",
                                    "boost": bid, "at": b.get("created_at")}) + "\n")
            if [rec["card"], THUMBS] not in rec.setdefault("ack", []):
                rec["ack"].append([rec["card"], THUMBS])
            self.log(f"captain approval on {key}: boost {bid}")

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

    def acknowledge(self, key, rec):
        """Boost each queued recording (a captain comment, or the card for an approval) once.

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

    def reply(self, rid, text, again=False):
        """Answer the captain's question `rid` where it was asked, then take the acting user's 👀 off it.

        A card comment is answered with a comment on that card; a chat line with a
        new line in that chat.

        Run only by the relaying agent; the sync itself never posts comments. A reply
        is recorded in the card's "replied" list and a second one is refused unless
        `again`. A failed post removes nothing, so the 👀 stays.
        """
        chats = self.load("chats.json", {})
        chat = next((c for c, r in chats.items() if rid in r.get("lines", [])), None)
        if chat is not None:
            store, rec = ("chats.json", chats), chats[chat]
            target = f"/buckets/{self.project}/chats/{chat}/lines.json"
        else:
            cards = self.load("map.json", {})
            rec = next((r for r in cards.values() if rid in r.get("comments", [])), None)
            if rec is None:
                raise RuntimeError(f"no card in map.json or chat in chats.json has {rid}")
            store = ("map.json", cards)
            target = f"/buckets/{self.project}/recordings/{rec['card']}/comments.json"
        where = f"chat {chat}" if chat is not None else f"card {rec['card']}"
        if rid in rec.get("replied", []) and not again:
            self.log(f"reply {rid}: already replied, pass --again to post another")
            return False
        me = self.acting_id()
        if me == "retry":
            raise RuntimeError("acting identity unknown")
        if me is None or me == self.captain:
            self.log(f"reply {rid}: acting user is the captain or unset, not posting")
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
        for b in self.boosts_by(me, rid, EYES):
            self.bc("api", "delete", f"/buckets/{self.project}/boosts/{b.get('id')}.json")
            self.log(f"reply {rid}: removed {EYES} boost {b.get('id')}")
        return True

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
        me = self.acting_id()
        if me == "retry":
            raise RuntimeError("acting identity unknown")
        if me is None or me == self.captain:
            self.log("ask: acting user is the owner or unset, not posting")
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

    def today(self):
        tz = self.checkins.get("timezone")
        return datetime.now(ZoneInfo(tz) if tz else None)

    def relay_checkins(self):
        """Record each check-in question that is due today and not yet answered by the acting user.

        Due: not paused, today's weekday in the schedule's "days" (0 = Sunday), the
        start_date reached, and the schedule's hour:minute passed in the configured
        "timezone" (the machine's local time when unset). Each question is recorded
        once per day in the pending file as a `checkin`; nothing is answered here.
        checkins.json keeps question id -> {"recorded": [dates], "answered": [dates]}.
        A dry run reads and logs only.
        """
        now = self.today()
        date = now.strftime("%Y-%m-%d")
        state = self.load("checkins.json", {})
        for qn in self.checkins["questionnaires"]:
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
                with open(self.path("pending-comments.jsonl"), "a") as f:
                    f.write(json.dumps({"kind": "checkin", "questionnaire": int(qn), "question": q["id"], "date": date,
                                        "title": q.get("title"), "url": q.get("app_url"), "at": now.isoformat(timespec="seconds")}) + "\n")
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
        me = self.acting_id()
        if me == "retry":
            raise RuntimeError("acting identity unknown")
        if me is None or me == self.captain:
            self.log(f"answer {qid}: acting user is the owner or unset, not posting")
            return False
        if self.answered_today(qid, me, date):
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


THUMBS, EYES = "\U0001F44D", "\U0001F440"


def render_reply(text):
    """Plain text to comment HTML: escaped, one <div> per paragraph, no raw newlines."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join("<div>" + "<br>".join(html.escape(l) for l in p.splitlines()) + "</div>" for p in paras)


def inline_md(text):
    """One line of Markdown to escaped HTML: links, bare URLs, **bold**, `code` as plain text."""
    out, pos = [], 0
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)|(https?://[^\s<>()]+)", text):
        out.append(html.escape(text[pos:m.start()]))
        url = m.group(2) or m.group(3)
        out.append(f'<a href="{html.escape(url)}">{html.escape(m.group(1) or url)}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    s = "".join(out)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return re.sub(r"`([^`]+)`", r"\1", s)


def render_release(notes, url, note=None):
    """Release notes (GitHub Markdown) to Basecamp rich text: headings bold, lists, a link to the release."""
    parts, items = [], []

    def flush():
        if items:
            parts.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()
    for line in notes.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        m = re.match(r"[*+-]\s+(.*)", line)
        if m:
            items.append(inline_md(m.group(1)))
            continue
        flush()
        if not line:
            continue
        h = re.match(r"#{1,6}\s+(.*)", line)
        parts.append(f"<div><strong>{inline_md(h.group(1))}</strong></div>" if h else f"<div>{inline_md(line)}</div>")
    flush()
    parts.append(f'<div><a href="{html.escape(url)}">{html.escape(url)}</a></div>')
    if note:
        parts.append(f"<div>{inline_md(note)}</div>")
    return "".join(parts)


def is_emoji(content, emoji):
    return is_thumbs_up(content) if emoji == THUMBS else re.sub(r"<[^>]+>", "", content).strip() == emoji


def is_thumbs_up(content):
    """True for 👍 alone, with or without a skin-tone modifier or variation selector."""
    return re.fullmatch("\U0001F44D[\U0001F3FB-\U0001F3FF]?\uFE0F?", re.sub(r"<[^>]+>", "", content).strip()) is not None


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


def render_decision(d):
    """A decision card body from decisions.json: a plain question, then a numbered list."""
    out = f"<div><strong>Waiting on you</strong>: {html.escape(d['question'])}</div>"
    if d.get("items"):
        out += "<ol>" + "".join(f"<li>{html.escape(i)}</li>" for i in d["items"]) + "</ol>"
    if d.get("note"):
        out += f"<div>{html.escape(d['note'])}</div>"
    return out


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


def cli(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["init"]:
        import init_home
        return init_home.cli(argv[1:])
    if argv[:1] == ["reply"]:
        ap = argparse.ArgumentParser(prog="sync.py reply", description="Answer a captain question on its card, then remove the 👀.")
        ap.add_argument("--home", required=True)
        ap.add_argument("--config", required=True)
        ap.add_argument("--recording", required=True, type=int, help="the question comment's id")
        ap.add_argument("--body-file", required=True, help="plain-text answer")
        ap.add_argument("--again", action="store_true", help="post even though this question was already answered")
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args(argv[1:])
        s = Sync(a.home, a.config, dry=a.dry_run)
        try:
            with open(a.body_file) as f:
                s.reply(a.recording, f.read(), again=a.again)
        except Exception as e:
            s.log(f"FAILED reply {a.recording} {type(e).__name__}: {e}")
            return 1
        return 0
    if argv[:1] in (["ask"], ["answer"]):
        what = argv[0]
        ap = argparse.ArgumentParser(prog=f"sync.py {what}", description=(
            "Post a new line in the configured ask_chat, @mentioning the owner." if what == "ask"
            else "Answer a recorded check-in question for today."))
        ap.add_argument("--home", required=True)
        ap.add_argument("--config", required=True)
        if what == "answer":
            ap.add_argument("--question", required=True, type=int, help="the check-in question's id")
        ap.add_argument("--body-file", required=True, help="plain text")
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args(argv[1:])
        s = Sync(a.home, a.config, dry=a.dry_run)
        try:
            with open(a.body_file) as f:
                text = f.read()
            s.ask(text) if what == "ask" else s.answer(a.question, text)
        except Exception as e:
            s.log(f"FAILED {what} {type(e).__name__}: {e}")
            return 1
        return 0
    ap = argparse.ArgumentParser(description="Mirror a firstmate backlog onto Basecamp card tables.")
    ap.add_argument("--home", required=True, help="firstmate home (holds data/backlog.md and state/)")
    ap.add_argument("--config", required=True, help="config.json; its directory holds all runtime state")
    ap.add_argument("--dry-run", action="store_true", help="plan only: no Basecamp calls, map.json untouched")
    ap.add_argument("--include-prereleases", action="store_true", help="also announce GitHub prereleases")
    a = ap.parse_args(argv)
    s = Sync(a.home, a.config, dry=a.dry_run, prereleases=a.include_prereleases)
    try:
        s.main()
    except Exception as e:
        s.log(f"FAILED {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
