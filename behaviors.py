"""The behaviors layer: opt-in, per-home workflows composed from tools.py.

A behavior is on when its config keys are set, and off (making no calls at all)
otherwise. It holds the policy: which readers run on the timer and how often, what
the card mirror shows, when a release is announced. The timer runs every 30 seconds;
each behavior's step runs when its `every` has passed: notifications and inbox
delivery every run, the card mirror, release announcements and check-ins every 5
minutes (and the to-do request sweep), and the readers notifications take over hourly,
as the repair sweep. Some
behaviors have no timer step at all and are carried out by the agent with the tools
(chat asks); the agent's side of every behavior is in prompts/base.md.

Behaviors never call a CLI themselves; every Basecamp, GitHub and backlog read or
write goes through a Tools method.
"""
import hashlib, html, json, os, re
from datetime import datetime, timezone

from tools import AUTHORITY, THREADS, parse_reading, recording_type

COLUMNS = ("Triage", "Not now", "Figuring it out", "In progress", "Ready for QA", "Done")
SLOW, SWEEP = 300, 3600  # seconds: the card mirror's, releases' and check-ins' cadence; the hourly repair sweep's


class Behavior:
    name = ""  # as the README and `sync.py behaviors` name it
    keys = ()  # the config keys that turn it on or configure it
    timer = True  # False: the agent carries it out with the tools; nothing runs on the timer
    every = 0  # seconds between its timer steps; 0: every timer run (every 30 seconds)

    def __init__(self, t, cfg):
        self.t, self.on = t, False

    @property
    def runs(self):
        """Where and how often it runs, as `sync.py behaviors` prints it."""
        if not self.timer:
            return "agent"
        return {0: "each run", SLOW: "5 min", SWEEP: "hourly"}.get(self.every, f"{self.every}s")

    def run(self, items=None):
        pass


class CardMirror(Behavior):
    """Mirror the backlog onto card tables; relay the listened-to people's card comments and the captain's (or an
    operator's) 👍 approvals."""
    name, keys, every = "card-mirror", ("tables", "repos", "cards"), SLOW

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        # On when the config has card tables, unless "cards" is false.
        self.on = cfg.get("cards", bool(cfg.get("tables")))
        if self.on and not cfg.get("tables"):
            raise ValueError('config "cards" is on but there are no "tables"')
        self.tables = cfg.get("tables", {}) if self.on else {}
        self.repo_map = cfg.get("repos", {}) if self.on else {}
        for board, tb in self.tables.items():
            missing = [c for c in ("table", *COLUMNS) if c not in tb]
            if missing:
                raise ValueError(f"config table {board} lacks {missing}")
        for repo, board in self.repo_map.items():
            if board not in self.tables:
                raise ValueError(f"config repo {repo} names unknown board {board}")

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
            return ("Ready for QA", False) if self.t.meta_pr(it["id"]) else ("In progress", False)
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
        pr = self.t.meta_pr(it["id"])
        prs = list(dict.fromkeys(it["links"] + ([pr] if pr else [])))
        if prs:
            parts.append("<div><strong>PRs</strong>:</div><ul>" + "".join(f'<li><a href="{html.escape(u)}">{html.escape(u)}</a></li>' for u in prs) + "</ul>")
        parts.append("<div><em>Kept in sync from the backlog; edits to this text are overwritten. Comments are read and relayed.</em></div>")
        return "".join(parts)

    def run(self, items=None):
        t = self.t
        cards = t.load("map.json", {})
        extra = t.load("extra-repos.json", {})
        notnow = t.load("not-now.json", {})
        figuring = t.load("figuring.json", {})
        skip = set(t.load("skip.json", []))
        decisions = t.load("decisions.json", {})
        board_owners = t.load("boards.json", {})
        live = t.lavish_boards()
        counts, unplaced, wanted = {}, [], set()
        plan = {"create": 0, "update": 0, "move": 0, "assign": 0, "unassign": 0}
        for it in (t.read_backlog() if items is None else items):
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
                tb = self.tables[repo]
                title = it["title"][:240]
                rec = cards.get(key)
                boards = task_boards if live is not None else (rec or {}).get("boards", [])
                body = self.body_for(it, col, waiting, notnow, decisions, boards)
                digest = hashlib.sha256((title + body).encode()).hexdigest()
                if t.dry:
                    if rec is None:
                        plan["create"] += 1
                        t.log(f"dry {key}: create -> {col}{' (assign)' if waiting else ''}")
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
                        t.log(f"dry {key}: {', '.join(acts) or 'unchanged'} [{col}]")
                    if rec is not None and rec.get("assigned"):
                        t.read_card_boosts(it["id"], repo, key, rec)
                    if rec is not None:
                        t.acknowledge(key, rec)
                    counts[repo] = counts.get(repo, 0) + 1
                    continue
                if rec is None:
                    card = t.card_create(tb["table"], tb[col], title, body, t.captain if waiting else None)
                    rec = {"card": card, "column": col, "assigned": waiting, "digest": digest, "comments": [], "boards": boards}
                    cards[key] = rec
                    t.log(f"created {key} card {card} in {col}")
                else:
                    if rec.get("digest") != digest:
                        t.card_update(rec["card"], tb["table"], title, body)
                        rec["digest"] = digest
                        rec["boards"] = boards
                        t.log(f"updated {key}")
                    if rec.get("column") != col:
                        t.card_move(rec["card"], tb["table"], tb[col])
                        t.log(f"moved {key} {rec.get('column')} -> {col}")
                        rec["column"] = col
                    if bool(rec.get("assigned")) != waiting:
                        if waiting:
                            t.card_assign(rec["card"], tb["table"])
                        else:
                            t.card_unassign(rec["card"])
                        rec["assigned"] = waiting
                        t.log(f"{'assigned' if waiting else 'unassigned'} {key}")
                self.read_card(it["id"], repo, key, rec)
                counts[repo] = counts.get(repo, 0) + 1
                t.save_json("map.json", cards)
        stale = sorted(k for k in cards if k not in wanted)
        t.log(("dry plan " + json.dumps(plan, sort_keys=True) + " " if t.dry else "")
              + "counts " + json.dumps(counts, sort_keys=True) + (f" unplaced {unplaced}" if unplaced else "")
              + (f" left-as-is {len(stale)} cards no longer in the backlog" if stale else ""))
        return plan

    def read_card(self, task, repo, key, rec):
        """The card readers for one mirrored card: the listened-to people's comments, and boosts when it is assigned."""
        self.t.read_card_comments(task, repo, key, rec)
        if rec.get("assigned"):
            self.t.read_card_boosts(task, repo, key, rec)
        self.t.acknowledge(key, rec)

    def relay(self, keys):
        """Run the card readers for the mirrored cards `keys` (task|board) alone, without touching the cards.

        A dry run reads only the boosts of an assigned card, as the mirror's does.
        """
        t = self.t
        cards = t.load("map.json", {})
        for key in sorted(keys):
            rec = cards.get(key)
            if rec is None:
                continue
            task, repo = key.split("|", 1)
            if t.dry:
                if rec.get("assigned"):
                    t.read_card_boosts(task, repo, key, rec)
                t.acknowledge(key, rec)
                continue
            self.read_card(task, repo, key, rec)
            t.save_json("map.json", cards)


class ChatInbox(Behavior):
    """Relay the listened-to people's chat questions, or every line they write in an "every_line" chat.

    Notifications run the chat reader for a chat as soon as it has a new line; the timer's
    step is the repair sweep (hourly with notifications on, else every 5 minutes).
    """
    name, keys = "chat-inbox", ("chats",)

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        # A chat is an id, or {"chat": id, "every_line": true} to relay every owner line.
        self.chats, self.every_line = [], set()
        for c in cfg.get("chats", []):
            cid = str(c["chat"] if isinstance(c, dict) else c)
            self.chats.append(cid)
            if isinstance(c, dict) and c.get("every_line"):
                self.every_line.add(cid)
        self.on = bool(self.chats)

    def run(self, items=None):
        self.t.read_chats(self.chats, self.every_line)


class ChatAsks(Behavior):
    """The agent puts questions to the owner in chat with `sync.py ask`."""
    name, keys, timer = "chat-asks", ("ask_chat",), False

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        self.on = cfg.get("ask_chat") is not None


class ReleaseAnnouncements(Behavior):
    """Post one Message Board announcement per new GitHub release: the sync's one automatic post."""
    name, keys, every = "release-announcements", ("releases",), SLOW

    def __init__(self, t, cfg, prereleases=False):
        super().__init__(t, cfg)
        self.releases = cfg.get("releases")
        if self.releases is not None:
            if not self.releases.get("board") or not isinstance(self.releases.get("repos"), dict):
                raise ValueError('config "releases" needs "board" (message board id) and "repos" ({"owner/name": ...})')
        self.prereleases = prereleases or bool((self.releases or {}).get("prereleases"))
        self.on = bool(self.releases)

    def run(self, items=None):
        """Per repo, releases.json keeps "since" (when the watch started), "seeded" (tags that
        existed then, never announced) and "announced" (tag -> message id). The first run
        only seeds. Drafts are always skipped and prereleases unless enabled. A failed read
        or post is logged and retried next run; it never fails the sync. Messages are never
        edited or deleted. Posted as the acting user whoever that is: an announcement is
        not an acknowledgement. A dry run reads and logs only.
        """
        t = self.t
        state = t.load("releases.json", {})
        board = str(self.releases["board"])
        for repo, spec in self.releases["repos"].items():
            spec = spec if isinstance(spec, dict) else {"name": spec}
            name = spec.get("name") or repo.split("/")[-1]
            name = name[:1].upper() + name[1:]
            rec = state.get(repo)
            try:
                listed = t.gh("release", "list", "-R", repo, "--limit", "30",
                              "--json", "tagName,isDraft,isPrerelease,publishedAt")
            except Exception as e:
                t.log(f"releases {repo}: {type(e).__name__}: {e}")
                continue
            if rec is None:
                tags = sorted(r["tagName"] for r in listed if not r.get("isDraft"))
                if t.dry:
                    t.log(f"dry releases {repo}: would seed the watch with {', '.join(tags) or 'no releases'}")
                    continue
                state[repo] = {"since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "seeded": tags, "announced": {}}
                t.save_json("releases.json", state)
                t.log(f"releases {repo}: watch started, seeded {', '.join(tags) or 'no releases'}")
                continue
            done = set(rec.get("seeded", [])) | set(rec.get("announced", {}))
            new = [r for r in listed if not r.get("isDraft") and (self.prereleases or not r.get("isPrerelease"))
                   and r["tagName"] not in done and (r.get("publishedAt") or "") > rec["since"]]
            for r in sorted(new, key=lambda r: r["publishedAt"]):
                tag = r["tagName"]
                subject = f"{name} {tag} released"
                if t.dry:
                    t.log(f"dry releases {repo}: post {subject!r}")
                    continue
                try:
                    rel = t.gh("release", "view", tag, "-R", repo, "--json", "tagName,body,url,isDraft,isPrerelease")
                    msg = t.message(board, subject, render_release(rel.get("body") or "", rel["url"], spec.get("note")))
                except Exception as e:
                    t.log(f"releases {repo} {tag}: announcement failed, retrying next run: {type(e).__name__}: {e}")
                    continue
                rec.setdefault("announced", {})[tag] = msg.get("id")
                t.save_json("releases.json", state)
                t.log(f"releases {repo}: announced {tag} as message {msg.get('id')}")


class CheckinAnswering(Behavior):
    """Record due check-in questions; the agent answers each once a day with `sync.py answer`."""
    name, keys, every = "checkin-answering", ("checkins",), SLOW

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        self.checkins = cfg.get("checkins")
        if self.checkins is not None and not isinstance(self.checkins.get("questionnaires"), list):
            raise ValueError('config "checkins" needs "questionnaires" (a list of questionnaire ids)')
        self.on = bool(self.checkins)

    def run(self, items=None):
        self.t.read_checkins(self.checkins["questionnaires"])
        self.t.read_answer_boosts(self.checkins["questionnaires"])


class DecisionTodos(Behavior):
    """Decisions for the owner as assigned to-dos: the agent creates and completes them; the owner's comments are relayed.

    Notifications run a to-do's reader when it has a new comment; the timer's step reads
    every open one as the repair sweep (hourly with notifications on, else every 5 minutes).
    """
    name, keys = "decision-todos", ("todos",)

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        todos = cfg.get("todos")
        if todos is not None and not isinstance(todos, dict):
            raise ValueError('config "todos" must be an object: {} or {"todoset": id} or {"list": id}')
        self.on = todos is not None

    def run(self, items=None):
        self.t.read_todo_comments(requests=False)


class AssignedTodos(Behavior):
    """To-dos the listened-to people assign to the agent's login, as requests: recorded once, acknowledged with 👀 and
    tracked, so their comments, boosts, edits and closing arrive as records of the request; the agent completes each
    with `sync.py todo complete` when the work is done. To-dos assigned to anyone else are ignored.

    "assigned_todos": {} turns it on; {"limit": n} fetches at most n newly assigned to-dos a
    sweep (default 10). {"scope": "account"} takes to-dos assigned to the agent in every
    project of the account, each request carrying its project; the default, "project", only
    the configured project's. Only one home per login should run "account" (the main home,
    which routes each request to the right domain). Notifications see the assignment,
    comments and completion first; the timer's sweep every 5 minutes, of the agent's open
    assignments and every open request, is the repair sweep and the only reader of a
    request's edits and the owner's boosts on it, which reach no notification.
    """
    name, keys, every = "assigned-todos", ("assigned_todos",), SLOW

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        opts = cfg.get("assigned_todos")
        if opts is not None and not isinstance(opts, (bool, dict)):
            raise ValueError('config "assigned_todos" must be {} or {"limit": <newly assigned to-dos read per sweep>, '
                             '"scope": "project" | "account"}')
        self.on = opts is not None and opts is not False
        opts = opts if isinstance(opts, dict) else {}
        self.limit = int(opts.get("limit", 10))
        if self.limit < 1:
            raise ValueError('config "assigned_todos" "limit" must be at least 1')
        self.scope = opts.get("scope", "project")
        if self.scope not in ("project", "account"):
            raise ValueError('config "assigned_todos" "scope" must be "project" (the default) or "account"')
        self.account_wide = self.on and self.scope == "account"

    def run(self, items=None):
        """The sweep: new requests, then the open ones' comments and boosts, then their edits and closing."""
        t = self.t
        t.discover_todo_requests(self.limit)
        t.read_todo_comments(requests=True)
        for key in t.request_keys():
            t.refresh_todo_request(key)


class Reports(Behavior):
    """The agent posts reports to the Message Board with `sync.py post-message`; the owner's comments and boosts on them are relayed.

    Notifications run a message's reader when it has a new comment or boost; the timer's
    step reads the agent's recent posts as the repair sweep.
    """
    name, keys = "reports", ("message_board",)

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        self.on = cfg.get("message_board") is not None
        self.board = str(cfg.get("message_board"))

    def run(self, items=None):
        self.t.read_messages(self.board)


class Pings(Behavior):
    """Relay every line the owner writes in a Ping (a direct message) with the agent's login; the agent answers with `sync.py reply`.

    "pings": {} turns it on; {"limit": n} reads at most n Pings a sweep (default 10), the
    most recently active first. Notifications run the reader for a Ping as soon as it has
    a new line; the timer's step is the repair sweep.
    """
    name, keys = "pings", ("pings",)

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        pings = cfg.get("pings")
        if pings is not None and not isinstance(pings, (bool, dict)):
            raise ValueError('config "pings" must be {} or {"limit": <Pings read per run>}')
        self.on = pings is not None and pings is not False
        self.limit = int((pings if isinstance(pings, dict) else {}).get("limit", 10))
        if self.limit < 1:
            raise ValueError('config "pings" "limit" must be at least 1')

    def run(self, items=None):
        self.t.read_pings(self.limit)


class InboxDelivery(Behavior):
    """Deliver each new pending record to the firstmate inbox as a note: the wake for the agent.

    "inbox": {} delivers to the --home's bin/fm-inbox.sh; {"fm_home": "<home>"} to another
    home's. Records are delivered in order from a line cursor in inbox.json; the first
    run starts at the records that existed when it began, so history is not delivered.
    Each note's request id (basecamp-<kind>-<id>) makes a replay not a new note. A failed
    note stops the run's delivery, is logged (FAILED once per request id, so the wake
    check sees it) and retried next run; it never fails the sync. A dry run logs only.
    """
    name, keys = "inbox-delivery", ("inbox",)

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        inbox = cfg.get("inbox")
        if inbox is not None and inbox is not True and not isinstance(inbox, dict):
            raise ValueError('config "inbox" must be {} or {"fm_home": "<firstmate home>"}')
        self.on = inbox is not None and inbox is not False
        self.fm_home = os.path.abspath((inbox if isinstance(inbox, dict) else {}).get("fm_home") or t.home)
        # Where a first run starts: the records that exist before this run's readers append.
        self.start = len(t.pending_lines()) if self.on else 0

    def run(self, items=None):
        self.deliver()

    def deliver(self):
        t = self.t
        state = t.load("inbox.json", None)
        if state is None:
            state = {"cursor": self.start, "failing": []}
        lines = t.pending_lines()
        for i in range(state["cursor"], len(lines)):
            try:
                rec = json.loads(lines[i])
            except ValueError:
                t.log(f"inbox: line {i + 1} of pending-comments.jsonl is not JSON, skipped")
                state["cursor"] = i + 1
                continue
            rid, body = inbox_note(rec, t.account, t.project)
            if t.dry:
                t.log(f"dry inbox: note {rid}")
                continue
            try:
                outcome = t.inbox_note(self.fm_home, rid, body)
            except Exception as e:
                if rid in state["failing"]:
                    t.log(f"inbox: note {rid} still failing, retrying next run: {type(e).__name__}: {e}")
                else:
                    state["failing"].append(rid)
                    t.log(f"FAILED inbox note {rid}, retrying next run: {type(e).__name__}: {e}")
                break
            state["cursor"] = i + 1
            state["failing"] = [f for f in state["failing"] if f != rid]
            t.log(f"inbox: note {rid} {outcome or 'sent'}")
        if not t.dry:
            t.save_json("inbox.json", state)


class Notifications(Behavior):
    """Read the agent login's Basecamp notifications and boosts every run, and run the reader for each thread that changed.

    "Owner" below means anyone the sync listens to (the captain, "operators", "people" and admitted "participants").
    On whenever the config has a "profile" ("notifications": false turns it off):
    /my/readings.json and /my/boosts.json are the acting user's, so it reads nothing
    while the acting user is the owner, unset or unknown. It takes over reading the
    owner's input from chat-inbox, pings, decision-todos, reports, assigned-todos and the
    card mirror's comments, whose own readers then run as the hourly repair sweep.

    Each run makes one read of /my/readings.json (unread items and the first page of read
    ones, every project) and keeps a cursor per item in notifications.json (item id ->
    its unread_at). An item is a pointer to a whole thread ("this to-do has 2 new
    comments", "this chat has a new line"), so for each one whose cursor moved, the
    existing reader for that thread runs once, records what is new by its own cursor and
    acknowledges it, exactly as the sweep would:

      - a project chat in "chats": the chat reader, for that chat; an @mention or a
        Campfire reply to the agent's line there is recorded even without "?";
      - a Ping with an owner in it (pings on): the Ping reader, for that Ping;
      - a comment or @mention on a mirrored card, an open tracked to-do or the agent's
        own message: that card's, to-do's or message's reader;
      - a to-do assigned to the agent, or completed (assigned-todos on; in any project
        with "scope": "account"): the to-do request reader, or the request's edit and
        closing check;
      - a check-in Reminder (checkin-answering on): the check-in reader;
      - an owner's @mention of the agent in a thread nothing tracks: a `chat-question`
        (in a chat) or `mention` record of its own.

    Owner input in the project that none of these handles (a comment on a Document, a
    line in a chat not in "chats", a to-do assigned with assigned-todos off) is an
    `unmonitored` record, once per kind, unless "notifications" has "unmonitored": false
    (or an old "listen" had it). Items in other projects are another home's: never read
    and never marked read here, except a Ping and an account-wide to-do request.

    Boosts: one read of /my/boosts.json, the boosts on the agent's own recordings. Each new
    owner boost runs the reader whose state holds the boosted recording, which records it
    as a `boost` (or the captain's 👍 on an assigned card as an `approval`) once this same
    read lists it (Tools.confirmed); a boost on
    something nothing monitors in the project is unmonitored. A boost on a to-do request
    (the owner's own to-do) is not listed there and waits for the sweep.

    Mark read: once an item is handled with no failed call, it is marked read
    ("mark_read": false keeps them unread), so the unread list stays under Basecamp's
    100 and the sidebar honest. The cursor, never the read flag, decides what is new, so
    someone marking everything read loses nothing, and an item whose reader failed keeps
    its old cursor and is retried next run. The first run only seeds the cursors and the
    boosts seen, so history is not replayed. A dry run reads, logs and saves nothing.
    """
    name, keys = "notifications", ("notifications",)
    QUIET = ("BoostReport", "Bulletin", "Onboarding")  # Basecamp's own items: never owner input

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        opts = cfg.get("notifications")
        if opts is not None and not isinstance(opts, (bool, dict)):
            raise ValueError('config "notifications" must be false or {"mark_read": false, "unmonitored": false}')
        self.on = bool(t.profile) and opts is not False
        opts = opts if isinstance(opts, dict) else {}
        old = cfg.get("listen") if isinstance(cfg.get("listen"), dict) else {}
        for k in ("mark_read", "unmonitored"):
            if not isinstance(opts.get(k, True), bool):
                raise ValueError(f'config "notifications" "{k}" must be true or false')
        self.mark = opts.get("mark_read", True)
        self.unmonitored = self.on and opts.get("unmonitored", old.get("unmonitored", True) is not False)
        self.others = {}  # every behavior by name, set by configure()

    def run(self, items=None):
        """One pass: the changed notifications and new boosts, each thread's reader once, then inbox delivery."""
        t = self.t
        state = t.load("notifications.json", {})
        if t.agent() is None:
            if not state.get("refused") and not t.dry:
                t.log("notifications: the acting user is the owner, unset or unknown; notifications not read")
                t.save_json("notifications.json", dict(state, refused=True))
            return
        state.pop("refused", None)
        try:
            data = t.readings()
        except RuntimeError as e:
            t.log(f"notifications: {e}")
            return
        received = t.received()  # the same read the boost readers confirm boosts against this run
        boosts = None if received is None else list(received.values())
        unread = {str(r.get("id")) for r in data.get("unreads") or []}
        readings = sorted((data.get("unreads") or []) + (data.get("reads") or []), key=stamp)
        first, first_boosts = "items" not in state, "boosts" not in state  # boosts unread on the first run seed later
        cursors, seen = state.get("items") or {}, set(state.get("boosts") or [])
        want = {"cards": set(), "chats": {}, "todos": set(), "requests": {}, "checkins": False, "answers": False,
                "messages": set(), "pings": {}, "mentions": {}, "threads": {}}
        work = []  # (ref, mine, [group], check): one per changed notification or new boost
        for r in readings:
            rid = str(r.get("id"))
            if not first and cursors.get(rid) != stamp(r):
                work.append((rid, *self.classify(r, want, data)))
        for b in boosts or []:
            if b.get("id") not in seen and not first_boosts:
                work.append((("boost", b.get("id")), *self.classify_boost(b, want, data)))
        failed = self.read(want, {g for w in work for g in w[2]})
        done = []
        for ref, mine, groups, check in work:
            if any(g in failed for g in groups):
                continue
            if check is not None:
                before = t.errors
                check()
                if t.errors > before:
                    failed.add(f"check {ref}")
                    continue
            done.append((ref, mine))
        if failed:
            t.log(f"notifications: retrying next run: {', '.join(sorted(map(str, failed)))}")
        if t.dry:
            return
        handled = {ref for ref, _ in done}
        state["items"] = {str(r.get("id")): stamp(r) if first or str(r.get("id")) in handled else cursors.get(str(r.get("id")))
                          for r in readings}
        state["items"] = {k: v for k, v in state["items"].items() if v is not None}
        if boosts is not None:
            listed = {b.get("id") for b in boosts}
            took = listed if first_boosts else seen | {ref[1] for ref in handled if isinstance(ref, tuple)}
            state["boosts"] = sorted(took & listed)
        mark = sorted({ref for ref, mine in done if mine and ref in unread} | (set(state.get("unmarked") or []) & unread))
        state["unmarked"] = []
        t.save_json("notifications.json", state)
        if self.mark and mark:
            try:
                t.mark_read(mark)
            except RuntimeError as e:
                t.log(f"notifications: marking {len(mark)} read failed, retrying next run: {e}")
                state["unmarked"] = mark
                t.save_json("notifications.json", state)
        if first:
            t.log(f"notifications: started; {len(readings)} notification(s) and {len(boosts or [])} boost(s) seeded, none replayed")
        if done and self.others["inbox-delivery"].on:
            self.others["inbox-delivery"].deliver()

    def read(self, want, groups):
        """Run each reader `want` names once, in order; returns the groups whose reader had a failed call."""
        b, t, failed = self.others, self.t, set()

        def step(group, call):
            if group not in groups:
                return
            before = t.errors
            call()
            if t.errors > before:
                failed.add(group)
        step("cards", lambda: b["card-mirror"].relay(want["cards"]))
        for chat, addressed in sorted(want["chats"].items()):
            step(f"chat {chat}", lambda c=chat, a=addressed: t.read_chats([c], b["chat-inbox"].every_line, a))
        step("todos", lambda: t.read_todo_comments(want["todos"]))
        # After the comment readers, so a comment made just before the closing is relayed first.
        for tid, req in sorted(want["requests"].items()):
            step(f"request {tid}", lambda r={tid: req}: self.read_requests(r))
        step("checkins", lambda: t.read_checkins(b["checkin-answering"].checkins["questionnaires"]))
        step("answers", lambda: t.read_answer_boosts(b["checkin-answering"].checkins["questionnaires"]))
        for mid in sorted(want["messages"]):
            step(f"message {mid}", lambda m=mid: t.read_messages(b["reports"].board, only=m))
        for chat, conv in sorted(want["pings"].items()):
            step(f"ping {chat}", lambda c=conv: t.read_pings(convs=[c]))
        for rid, (bucket, chat) in sorted(want["mentions"].items()):
            step(f"mention {rid}", lambda r=rid, bk=bucket, c=chat: t.read_mention(bk, c, r))
        for thread, (bucket, since, mentions, ptype, title) in sorted(want["threads"].items()):
            step(f"thread {thread}", lambda th=thread, bk=bucket, sn=since, m=mentions, pt=ptype, ti=title:
                 t.read_thread(bk, th, sn, m, pt, ti))
        return failed

    def read_requests(self, requests):
        """For each to-do in `requests` ({to-do id: (what happened, who, bucket)}): an open request's edit and closing
        check, else a new request when it was assigned. Returns what ran, for the log."""
        t, ran = self.t, []
        for tid, (kind, by, bucket) in sorted(requests.items()):
            key = f"request-{tid}"
            if key in t.request_keys():
                done = t.refresh_todo_request(key, by)
            elif kind == "todo.assignment_changed":
                done = "todo-request" if t.read_todo_request(tid, by, bucket) else None
            else:
                done = None
            ran.append(f"request {tid}" + (f" -> {done}" if done else ""))
        return ran

    def classify(self, r, want, data):
        """One changed notification: (this home's?, the reader groups it needs, an unmonitored check to run after them)."""
        b, t = self.others, self.t
        kind, where = r.get("type"), parse_reading(r)
        bucket, thread, anchor, path = where["bucket"], where["thread"], where["anchor"], where["path"]
        who = r.get("creator") or {}
        if kind in self.QUIET:
            return False, [], None
        if kind == "Reminder":  # a check-in question asked of the agent: a faster due signal than the schedule
            if b["checkin-answering"].on:
                want["checkins"] = True
                return True, ["checkins"], None
            return False, [], None
        if r.get("section") == "pings":
            conv = next((c for c in t.ping_conversations(data) if str(c["chat"]) == str(thread)), None)
            if not b["pings"].on or conv is None:
                return False, [], None
            want["pings"][str(thread)] = conv
            return True, [f"ping {thread}"], None
        here, todos = bucket == t.project, t.load("todos.json", {})
        if kind in ("Assignment", "Completion"):  # thread and anchor are the to-do
            what = "todo.assignment_changed" if kind == "Assignment" else "todo.completed"
            tracked = f"request-{thread}" in t.request_keys()
            if tracked or b["assigned-todos"].on and (here or b["assigned-todos"].account_wide):
                want["requests"].setdefault(thread, (what, who.get("id"), None if here else bucket))
                return True, [f"request {thread}"], None
            if not here:
                return False, [], None
            if any(rec.get("todo") == thread for rec in todos.values()) or not t.hears(who, admitted=False):
                return True, [], None  # a decision to-do's own closing, or not from someone listened to
            return True, [], self.check(what, "Todo", thread, r)
        if not here:
            keys = {k for k, rec in todos.items() if rec.get("request") and rec.get("todo") == thread and path == "todos"
                    and str(rec.get("bucket")) == bucket and not rec.get("completed") and not rec.get("closed")}
            if keys and kind in ("Comment", "Mention"):
                want["todos"] |= keys
                return True, ["todos"], None
            return False, [], None
        mention = kind == "Mention"
        if r.get("section") == "chats" or path == "chats":
            ci = b["chat-inbox"]
            if ci.on and str(thread) in ci.chats:
                want["chats"].setdefault(str(thread), set()).update([anchor] if mention else [])
                return True, [f"chat {thread}"], None
            if mention and t.hears(who):
                want["mentions"][anchor] = (bucket, thread)
                return True, [f"mention {anchor}"], None
            return True, [], self.check("chat.line.created", "Chat::Lines", thread, r) if t.hears(who, admitted=False) else None
        if kind not in ("Comment", "Mention"):  # a kind Basecamp adds later: unmonitored when an owner did it
            return True, [], self.check(f"{kind}", None, thread, r) if t.hears(who, admitted=False) else None
        if path == "cards" and b["card-mirror"].on:
            keys = {k for k, rec in t.load("map.json", {}).items() if rec.get("card") == thread}
            if keys:
                want["cards"] |= keys
                return True, ["cards"], None
        if path == "todos":
            mine = {k: rec for k, rec in todos.items() if rec.get("todo") == thread}
            keys = {k for k, rec in mine.items() if not rec.get("completed") and not rec.get("closed")
                    and b["assigned-todos" if rec.get("request") else "decision-todos"].on}
            if keys:
                want["todos"] |= keys
                return True, ["todos"], None
        # Any other thread the agent is subscribed to or mentioned in: its own reader, whatever the backlog says.
        if path == "messages" and b["reports"].on:
            want["messages"].add(thread)

            def after():  # the agent's own message was read above; anyone else's is a thread like any other
                if not self.agent_message(thread):
                    t.read_thread(bucket, thread, anchor, {anchor} if mention else (), "Message", topic(r))
            return True, [f"message {thread}"], after
        self.thread(want, bucket, thread, anchor, mention, THREADS.get(path), topic(r))
        return True, [f"thread {thread}"], None

    def thread(self, want, bucket, thread, anchor, mention, ptype, title):
        """Add thread `thread` to the threads to read: from its first unread comment `anchor`, `anchor` a mention or not."""
        bk, since, mentions, pt, ti = want["threads"].get(thread, (bucket, anchor, set(), ptype, title))
        want["threads"][thread] = (bk, min(x for x in (since, anchor) if x) if since or anchor else None,
                                   mentions | ({anchor} if mention else set()), pt, ti)

    def classify_boost(self, bst, want, data):
        """One new boost on the agent's own recording: (this home's?, the reader groups it needs, an unmonitored check)."""
        b, t = self.others, self.t
        rec, booster = bst.get("recording") or {}, bst.get("booster") or {}
        rid, rtype = rec.get("id"), recording_type(rec.get("type"))
        bucket, parent = str((rec.get("bucket") or {}).get("id") or ""), rec.get("parent") or {}
        if not t.hears(booster, admitted=False):
            return False, [], None
        on_todo = rid if rtype == "Todo" else parent.get("id") if rtype == "Comment" and parent.get("type") == "Todo" else None
        todos = t.load("todos.json", {})
        keys = {k for k, r in todos.items() if on_todo is not None and r.get("todo") == on_todo
                and str(r.get("bucket") or t.project) == bucket}
        if rtype == "Chat::Lines" and bucket != t.project:
            circles = {str(c["bucket"]) for c in t.ping_conversations(data)} | {
                str(p.get("bucket")) for p in t.load("pings.json", {}).get("pings", {}).values()}
            if not b["pings"].on or bucket not in circles:
                return False, [], None  # a line in another project's chat is another home's
            chat = str(parent.get("id"))
            want["pings"][chat] = {"bucket": bucket, "chat": chat, "title": (rec.get("bucket") or {}).get("name"),
                                   "url": rec.get("app_url")}
            return True, [f"ping {chat}"], None
        if keys:
            open_keys = {k for k in keys if not todos[k].get("completed") and not todos[k].get("closed")}
            want["todos"] |= open_keys
            return True, ["todos"] if open_keys else [], None
        if bucket != t.project:
            return False, [], None
        if rtype == "Chat::Lines" and b["chat-inbox"].on and str(parent.get("id")) in b["chat-inbox"].chats:
            want["chats"].setdefault(str(parent.get("id")), set())
            return True, [f"chat {parent.get('id')}"], None
        card = rid if rtype == "Kanban::Card" else parent.get("id") if parent.get("type") == "Kanban::Card" else None
        cards = {k for k, r in t.load("map.json", {}).items() if card is not None and r.get("card") == card}
        if cards and b["card-mirror"].on:
            want["cards"] |= cards
            return True, ["cards"], None
        if rtype == "Question::Answer" and b["checkin-answering"].on:
            want["answers"] = True
            return True, ["answers"], None
        msg = rid if rtype == "Message" else parent.get("id") if parent.get("type") == "Message" else None
        if msg is not None and b["reports"].on:
            want["messages"].add(msg)
            return True, [f"message {msg}"], None
        known = f"{bucket}:{parent.get('id')}" in t.load("threads.json", {}) if rtype == "Comment" else False
        if known:
            self.thread(want, bucket, parent.get("id"), None, False, parent.get("type"), parent.get("title"))
            return True, [f"thread {parent.get('id')}"], None
        if on_todo is not None and b["assigned-todos"].on:
            return True, [], None  # a to-do the agent made, assigned to anyone else
        what = rtype if rtype != "Comment" else f"Comment on {parent.get('type') or 'unknown'}"
        return True, [], self.check("boost.created", what or "unknown", rid, None, booster=booster, at=bst.get("created_at"),
                                    ref=bst.get("id"), on=parent if rtype == "Comment" else None, rec=rec)

    def check(self, what, rtype, rid, r, booster=None, at=None, ref=None, on=None, rec=None):
        """The unmonitored check for notification `r` (or a boost): a call that records it once per kind, or None when off.

        A notification's own title, excerpt and link stand in for the recording, which is
        refetched only when its type is unknown (rtype None).
        """
        if not self.unmonitored:
            return None
        t = self.t
        if r is not None:
            who, at, ref, bucket = r.get("creator"), r.get("unread_at") or r.get("created_at"), r.get("id"), parse_reading(r)["bucket"]
            rec = None if rtype is None else {"title": r.get("title"), "content": r.get("content_excerpt"), "app_url": r.get("app_url")}
        else:
            who, bucket = booster, None
        return lambda: t.record_unmonitored(what, rtype, rid, who, at=at, rec=rec, on=on, bucket=bucket, ref=ref)

    def agent_message(self, mid):
        """True when the reports reader knows message `mid` as one of the agent's."""
        msgs = self.t.load("messages.json", {})
        return str(mid) in msgs.get("posts", {}) or str(mid) in (msgs.get("messages") or {}).get("boost_counts", {})


def topic(r):
    """A notification's thread title: its title without the "Re: " a comment's carries."""
    return re.sub(r"^Re: ", "", r.get("title") or "") or None


def stamp(r):
    """A notification's cursor: when it last became unread (new activity), else when it changed."""
    return r.get("unread_at") or r.get("updated_at") or r.get("created_at") or ""


# The timer runs the "on" behaviors that are due in this order: notifications first, so the owner's input is not held
# up by the card mirror; inbox delivery last, after every reader.
ALL = (Notifications, CardMirror, ChatInbox, ChatAsks, ReleaseAnnouncements, CheckinAnswering, DecisionTodos,
       AssignedTodos, Reports, Pings, InboxDelivery)
# The readers notifications take over: with notifications on they run as the hourly repair sweep.
# assigned-todos keeps its 5-minute sweep: a request's edits and the owner's boosts on it reach no notification.
SWEPT = ("chat-inbox", "decision-todos", "reports", "pings")


def inbox_note(rec, account, project):
    """A pending record as an inbox note: (request id, short plain body)."""
    kind = rec.get("kind") or "comment"
    card_url = f"https://app.basecamp.com/{account}/buckets/{project}/card_tables/cards/{rec.get('card')}"
    text = " ".join(str(rec.get("text") or "").split())
    if len(text) > 600:
        text = text[:600] + "..."
    # A record without "captain" predates the people list, when only the captain was relayed; one without "role" predates
    # operators, when only the captain's word counted. `captain` below means the word authorizes: the captain's or an operator's.
    role = rec.get("role", "captain" if rec.get("captain", True) else None)
    captain = role in AUTHORITY
    author = rec.get("author") or {}
    name = author.get("name") or (f"person {author['id']}" if author.get("id") else "someone")
    who = ("the captain" if role == "captain" else f"{name} (an operator: their word counts as the captain's)"
           if role == "operator" else f"{name} (not the captain)")
    # A to-do request in another project of the account (assigned-todos "scope": "account") names it, for routing.
    proj = rec.get("project") or {}
    elsewhere = (f" in project {proj.get('name') or proj.get('id')!r} ({proj.get('id')})"
                 if proj.get("id") and str(proj.get("id")) != str(project) else "")
    if kind in ("comment", "question"):
        rid = rec.get("comment")
        what = f"Basecamp card {kind} from {who} on task {rec.get('task')}"
        handle = (f"answer: sync.py reply --recording {rid}" if kind == "question"
                  else "relay or act as the basecamp-sync skill says")
        url = card_url
    elif kind == "approval":
        rid = rec.get("boost")
        mark = f"the 👍 of {who}" if role == "operator" else "the captain's 👍"
        what = f"Basecamp card approval ({mark}) on task {rec.get('task')}"
        text = text or "approve every recommendation on the card as recommended"
        handle, url = "record the decision in the backlog", rec.get("url") or card_url
    elif kind == "chat-question":
        rid = rec.get("line")
        what, handle, url = f"Basecamp chat line from {who}", f"answer: sync.py reply --recording {rid}", rec.get("url")
    elif kind == "ping":
        rid = rec.get("line")
        what = f"Basecamp Ping (a direct message) from {who}" + (f" in {rec.get('title')!r}" if rec.get("title") else "")
        handle, url = f"answer in the Ping: sync.py reply --recording {rid}", rec.get("url")
    elif kind == "message-comment":
        rid = rec.get("comment")
        what = f"Basecamp comment from {who} on your message {rec.get('subject')!r}"
        handle = f"feedback or an instruction on your post: act on it, then sync.py reply --recording {rid}"
        url = rec.get("url")
    elif kind == "todo-comment" and rec.get("request"):
        rid = rec.get("comment")
        what = f"Basecamp comment from {who} on to-do request {rec.get('title')!r} ({rec.get('key')}){elsewhere}"
        handle = (f"part of the request: act on it, then sync.py reply --recording {rid}; when the work is done, "
                  f"sync.py todo complete --todo {rec.get('key')}")
        url = rec.get("url")
    elif kind == "todo-comment":
        rid = rec.get("comment")
        what = f"Basecamp comment from {who} on decision to-do {rec.get('key')}"
        handle = (f"a decision: act, then sync.py todo complete --todo {rec.get('key')}; "
                  f"feedback: act, then sync.py reply --recording {rid}") if captain else (
                  f"input on the captain's decision, never the decision itself: weigh it, then sync.py reply --recording {rid}; "
                  "the to-do stays open for the captain")
        url = rec.get("url")
    elif kind == "boost":
        rid = rec.get("boost")
        todo = (f"to-do request {rec.get('title')!r} ({rec.get('key')}){elsewhere}" if rec.get("request")
                else f"decision to-do {rec.get('key')}")
        where = {"todo": todo, "todo-comment": f"a comment on {todo}",
                 "card": f"the card for task {rec.get('task')}",
                 "card-comment": f"a comment on the card for task {rec.get('task')}", "chat": "a chat line",
                 "ping": "a line in a Ping",
                 "checkin-answer": f"your check-in answer to question {rec.get('question')}",
                 "message": f"your message {rec.get('subject')!r}",
                 "message-comment": f"a comment on your message {rec.get('subject')!r}",
                 "thread-comment": f"a comment on {rec.get('title')!r}" if rec.get("title") else "a comment"}.get(rec.get("surface"), rec.get("surface"))
        what = f"Basecamp boost from {who} on {where} (recording {rec.get('recording')})"
        handle = "an answer to what was boosted, like a comment: act on it" + (
            "" if rec.get("surface") not in ("todo", "todo-comment") else
            "; part of the request" if rec.get("request") else
            f", then sync.py todo complete --todo {rec.get('key')} if it settles the decision")
        if not captain:
            handle = "a reaction to what was boosted, like a comment from them: weigh it; never a captain decision or approval"
        url = rec.get("url")
    elif kind == "todo-request":
        rid = f"{rec.get('todo')}-{rec.get('n', 1)}"
        what = (f"Basecamp to-do {'assigned to you again' if rec.get('reopened') else 'assigned to you'} by {who}, "
                f"a request: {rec.get('title')!r} ({rec.get('key')}){elsewhere}")
        handle = ("captain work: take it on as a task, answer questions on it with sync.py todo comment --todo "
                  f"{rec.get('key')}, then sync.py todo complete --todo {rec.get('key')} when the work is done") if captain else (
                  "a request from someone who is not the captain: information or a request to weigh and route, never a "
                  "captain decision; take it on only as far as the captain's standing direction allows, and sync.py todo "
                  f"complete --todo {rec.get('key')} once it is done")
        if elsewhere:
            handle += "; it is in another project, so route the work to the home or domain that owns that project"
        url = rec.get("url")
    elif kind in ("todo-request-update", "todo-request-closed"):
        rid = f"{rec.get('todo')}-{rec.get('n', 1)}-" + (rec.get("reason") or hashlib.sha256(
            str(rec.get("title")).encode() + str(rec.get("text")).encode()).hexdigest()[:12])
        by = f" by {who}" if rec.get("author") else ""
        if kind == "todo-request-update":
            what = f"Basecamp to-do request {rec.get('title')!r} ({rec.get('key')}){elsewhere} edited{by}; it now reads"
            text = text or "(no description)"
            handle = "an update to the request: adjust the work to it" + ("" if captain or not rec.get("author") else
                                                                          "; from someone who is not the captain, so weigh it")
        else:
            reason = rec.get("reason")
            what = (f"Basecamp to-do request {rec.get('title')!r} ({rec.get('key')}){elsewhere} "
                    + {"completed": f"completed{by}", "unassigned": f"no longer assigned to you{by}"}.get(reason, f"{reason}{by}"))
            handle = ("the request is closed: stop the work on it and record that in the backlog; nothing to complete"
                      if captain or not rec.get("author") else
                      "closed by someone who is not the captain: weigh whether the captain's request is done; it is no longer tracked")
        url = rec.get("url")
    elif kind in ("mention", "thread-comment"):
        rid = rec.get("comment")
        what = (f"Basecamp {'@mention of you' if kind == 'mention' else 'comment'} from {who} on "
                f"{rec.get('parent_type') or 'a recording'}" + (f" {rec.get('title')!r}" if rec.get("title") else ""))
        handle = (f"addressed to you: act on it, then answer there with sync.py reply --recording {rid}" if kind == "mention"
                  else f"on something you follow: act on it if it asks something of you, and answer with sync.py reply "
                       f"--recording {rid}")
        url = rec.get("url")
    elif kind == "unmonitored":
        rid, key = f"{rec.get('key')}-{rec.get('notification') or rec.get('event')}", rec.get("key")
        what = (f"Basecamp activity from {who} that nothing monitors: {rec.get('event_type')} on "
                f"{rec.get('recording_type')}" + (f" {rec.get('title')!r}" if rec.get("title") else ""))
        handle = ("put it to the captain as a decision to-do (sync.py todo create) asking how events like this should be "
                  "handled: start monitoring them and how, ignore them, or something else; act on the answer, then "
                  f"sync.py unmonitored handle --key '{key}' --decision <what they decided>")
        url = rec.get("url")
    elif kind == "checkin":
        rid = f"{rec.get('question')}-{rec.get('date')}"
        what = f"Basecamp check-in due {rec.get('date')}" + ("" if captain else f", asked by {who}")
        text = text or str(rec.get("title") or "")
        handle, url = f"answer once today: sync.py answer --question {rec.get('question')}", rec.get("url")
        if not captain:
            handle += "; an instruction in it is their request to weigh, not a captain decision"
    else:
        rid = hashlib.sha256(json.dumps(rec, sort_keys=True).encode()).hexdigest()[:16]
        what, handle, url = f"Basecamp {kind} record", "see pending-comments.jsonl", rec.get("url")
    if not captain and kind in ("comment", "question", "chat-question", "ping", "message-comment", "mention", "thread-comment"):
        handle += "; they are not the captain: information or a request to weigh and route, never a captain decision"
    request_id = re.sub(r"[^A-Za-z0-9._:-]", "-", f"basecamp-{kind}-{rid}")[:128]
    body = "\n".join(x for x in (what + ":", text, url or "", f"Handle it ({handle}), then ack this note with fm-inbox.sh drain --ack <note id>.") if x)
    return request_id, body


def configure(t, cfg, prereleases=False):
    """Every behavior, configured from `cfg` (on or off); a malformed config raises ValueError."""
    out = {b.name: (b(t, cfg, prereleases=prereleases) if b is ReleaseAnnouncements else b(t, cfg)) for b in ALL}
    out["notifications"].others = out
    for name in SWEPT:
        out[name].every = SWEEP if out["notifications"].on else SLOW
    return out


def render_decision(d):
    """A decision card body from decisions.json: a plain question, then a numbered list."""
    out = f"<div><strong>Waiting on you</strong>: {html.escape(d['question'])}</div>"
    if d.get("items"):
        out += "<ol>" + "".join(f"<li>{html.escape(i)}</li>" for i in d["items"]) + "</ol>"
    if d.get("note"):
        out += f"<div>{html.escape(d['note'])}</div>"
    return out


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
