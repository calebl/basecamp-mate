"""The behaviors layer: opt-in, per-home workflows composed from tools.py.

A behavior is on when its config keys are set, and off (making no calls at all)
otherwise. It holds the policy: which readers run on the timer, what the card
mirror shows, when a release is announced. Some behaviors have no timer step at
all and are carried out by the agent with the tools (chat asks, reports); the
agent's side of every behavior is in prompts/base.md.

Behaviors never call a CLI themselves; every Basecamp, GitHub and backlog read or
write goes through a Tools method.
"""
import hashlib, html, json, os, re
from datetime import datetime, timezone

from tools import recording_type

COLUMNS = ("Triage", "Not now", "Figuring it out", "In progress", "Ready for QA", "Done")


class Behavior:
    name = ""  # as the README and `sync.py behaviors` name it
    keys = ()  # the config keys that turn it on or configure it
    timer = True  # False: the agent carries it out with the tools; nothing runs on the timer

    def __init__(self, t, cfg):
        self.t, self.on = t, False

    @property
    def runs(self):
        """Where it runs, as `sync.py behaviors` prints it."""
        return "timer" if self.timer else "agent"

    def run(self, items=None):
        pass


class CardMirror(Behavior):
    """Mirror the backlog onto card tables; relay the listened-to people's card comments and the captain's 👍 approvals."""
    name, keys = "card-mirror", ("tables", "repos", "cards")

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
    """Relay the listened-to people's chat questions, or every line they write in an "every_line" chat."""
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
    name, keys = "release-announcements", ("releases",)

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
    name, keys = "checkin-answering", ("checkins",)

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
    """Decisions for the owner as assigned to-dos: the agent creates and completes them; the timer relays the owner's comments."""
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
    sweep (default 10). The listener sees the events first; the timer's sweep of the agent's
    open assignments in the project is the backup.
    """
    name, keys = "assigned-todos", ("assigned_todos",)

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        opts = cfg.get("assigned_todos")
        if opts is not None and not isinstance(opts, (bool, dict)):
            raise ValueError('config "assigned_todos" must be {} or {"limit": <newly assigned to-dos read per sweep>}')
        self.on = opts is not None and opts is not False
        self.limit = int((opts if isinstance(opts, dict) else {}).get("limit", 10))
        if self.limit < 1:
            raise ValueError('config "assigned_todos" "limit" must be at least 1')

    def run(self, items=None):
        """The sweep: new requests, then the open ones' comments and boosts, then their edits and closing."""
        t = self.t
        t.discover_todo_requests(self.limit)
        t.read_todo_comments(requests=True)
        for key in t.request_keys():
            t.refresh_todo_request(key)


class Reports(Behavior):
    """The agent posts reports to the Message Board with `sync.py post-message`; the timer relays the owner's comments and boosts on them."""
    name, keys = "reports", ("message_board",)

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        self.on = cfg.get("message_board") is not None
        self.board = str(cfg.get("message_board"))

    def run(self, items=None):
        self.t.read_messages(self.board)


class Pings(Behavior):
    """Relay every line the owner writes in a Ping (a direct message) with the agent's login; the agent answers with `sync.py reply`.

    "pings": {} turns it on; {"limit": n} reads at most n Pings a run (default 10), the
    most recently active first. A Ping is not in the project, so the listener finds its
    lines through a second, every-bucket poll of the event feed.
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


class OwnerEvents(Behavior):
    """Listen to the account event feed for the listened-to people's events and run the reader each one names: a faster wake.

    The people are the config's "people" (always with the captain); "owner" below means any of them.
    Run by `sync.py listen` (a systemd user service beside the timer), not by the timer.
    Each cycle polls the feed for the owner's chat lines, comments and boosts in this
    project and, per page, runs the existing reader for the surface each event points
    at, then inbox delivery. An event's own fields are never recorded; the readers
    refetch, record, acknowledge and dedupe exactly as on the timer, whose full run stays
    the repair sweep for anything the best-effort feed misses.

    Unmonitored events (on unless "listen" has "unmonitored": false, and only with a
    profile, since without one the sync's own writes are the owner's): the feed is read
    for every event type, still only the owner's in this project, and an owner event no
    behavior handles (a type outside TYPES, or one of TYPES on something nothing
    monitors) is recorded once per kind of thing as an `unmonitored` record for the agent
    to put to the owner.

    Pings (with the "pings" behavior on): a second poll, of the owner's chat lines and
    boosts in every bucket with its own position in pings-feed.json, runs the Ping reader
    whenever one of them is outside the project; a Ping is a bucket of its own.
    """
    name, keys, timer = "owner-events", ("listen",), False
    runs = "listener"
    TYPES = ("boost.created", "chat.line.created", "comment.created")
    # The to-do events the narrow feed adds for assigned-todos (the wide one has every type).
    TODO_TYPES = ("todo.assignment_changed", "todo.completed", "todo.created", "todo.description_changed")
    # The recording type an event type is about, for the unmonitored key without a refetch.
    SUBJECTS = {"todo": "Todo", "card": "Kanban::Card", "message": "Message", "question": "Question",
                "question.answer": "Question::Answer", "chat.line": "Chat::Lines"}

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        listen = cfg.get("listen")
        if listen is not None and not isinstance(listen, (bool, dict)):
            raise ValueError('config "listen" must be {} or {"interval": <seconds>, "unmonitored": false}')
        self.on = listen is not None and listen is not False
        opts = listen if isinstance(listen, dict) else {}
        self.interval = int(opts.get("interval") or 30)
        if self.interval < 10:
            raise ValueError('config "listen" "interval" must be at least 10 seconds')
        if not isinstance(opts.get("unmonitored", True), bool):
            raise ValueError('config "listen" "unmonitored" must be true or false')
        self.unmonitored = self.on and opts.get("unmonitored", True) and bool(t.profile)
        self.others = {}  # every behavior by name, set by configure()

    def run(self, items=None):
        """One listener cycle: every page of new owner events, each dispatched once handled. Returns the events seen."""
        narrow = self.TYPES + (self.TODO_TYPES if self.others.get("assigned-todos") and self.others["assigned-todos"].on else ())
        seen = self.t.read_feed(() if self.unmonitored else narrow, self.t.people, self.dispatch)
        if self.others.get("pings") and self.others["pings"].on:
            seen += self.t.read_feed(("boost.created", "chat.line.created"), self.t.people, self.dispatch_pings,
                                     every_bucket=True, store="pings-feed.json")
        return seen

    def dispatch_pings(self, events):
        """Run the Ping reader when the page has an owner line or boost outside the project, then deliver."""
        t = self.t
        outside = [ev for ev in events if str(ev.get("bucket_id")) != t.project and t.hears({"id": ev.get("creator_id")})]
        if not outside:
            return
        t.read_pings(self.others["pings"].limit)
        if self.others["inbox-delivery"].on:
            self.others["inbox-delivery"].deliver()
        t.log(f"listen: {len(outside)} event(s) outside the project -> pings")

    def dispatch(self, events):
        """Run, once each, the readers the page's events point at, then deliver what they recorded."""
        b, t = self.others, self.t
        want = {"cards": set(), "todos": set(), "chats": False, "checkins": False, "messages": False, "requests": {}}
        checks = []  # (event, its comment): each classified once the readers have run
        for ev in events:
            if str(ev.get("bucket_id")) != t.project or not t.hears({"id": ev.get("creator_id")}):
                continue  # the filters already say so; a stray event is never acted on
            kind, rid = ev.get("event_type"), ev.get("recording_id")
            checks.append((ev, None))
            if kind == "chat.line.created":
                want["chats"] = True
            elif kind == "comment.created":
                checks[-1] = (ev, self.comment_target(rid, want))
            elif kind and kind.startswith("todo.") and b["assigned-todos"].on:
                want["requests"].setdefault(rid, (kind, ev.get("creator_id")))
            elif kind == "boost.created" and not self.boost_target(rid, want):
                # A boost on something not yet seen (the agent's newest line, answer or post):
                # every cheap boost reader; a card's waits for the timer's sweep.
                want.update(chats=True, checkins=True, messages=True)
                want["todos"].update(t.load("todos.json", {}))
        ran = []
        if want["cards"] and b["card-mirror"].on:
            b["card-mirror"].relay(want["cards"])
            ran.append("cards " + ",".join(sorted(want["cards"])))
        if want["chats"] and b["chat-inbox"].on:
            t.read_chats(b["chat-inbox"].chats, b["chat-inbox"].every_line)
            ran.append("chats")
        todos = t.load("todos.json", {})
        want["todos"] = {k for k in want["todos"] if k in todos and
                         b["assigned-todos" if todos[k].get("request") else "decision-todos"].on}
        if want["todos"]:
            t.read_todo_comments(want["todos"])
            ran.append("todos " + ",".join(sorted(want["todos"])))
        for tid, (kind, by) in sorted(want["requests"].items()):
            # After the comment readers, so a comment made just before the closing is relayed first.
            key = f"request-{tid}"
            if key in t.request_keys():
                done = t.refresh_todo_request(key, by)
            elif kind in ("todo.created", "todo.assignment_changed"):
                done = "todo-request" if t.read_todo_request(tid, by) else None
            else:
                done = None
            ran.append(f"request {tid}" + (f" -> {done}" if done else ""))
        if want["checkins"] and b["checkin-answering"].on:
            t.read_answer_boosts(b["checkin-answering"].checkins["questionnaires"])
            ran.append("checkin answers")
        if want["messages"] and b["reports"].on:
            t.read_messages(b["reports"].board)
            ran.append("messages")
        if self.unmonitored and checks:
            found = self.detect(checks)
            if found:
                ran.append(f"unmonitored {found}")
        if b["inbox-delivery"].on:
            b["inbox-delivery"].deliver()
        t.log(f"listen: {len(events)} event(s) -> {'; '.join(ran) or 'nothing to read'}")

    def comment_target(self, cid, want):
        """A new owner comment: the reader for the mirrored card, tracked to-do or message it is on. Returns the comment."""
        try:
            c = self.t.comment(cid)
        except RuntimeError as e:
            self.t.log(f"listen: comment {cid}: {e}")
            return None
        ptype, pid = (c.get("parent") or {}).get("type"), (c.get("parent") or {}).get("id")
        if ptype == "Kanban::Card":
            want["cards"].update(k for k, r in self.t.load("map.json", {}).items() if r.get("card") == pid)
        elif ptype == "Todo":
            want["todos"].update(k for k, r in self.t.load("todos.json", {}).items() if r.get("todo") == pid)
        elif ptype == "Message":
            want["messages"] = True
        return c

    def detect(self, checks):
        """Record each owner event in `checks` that no enabled behavior handled; returns how many were new.

        Run after the readers, so their state already holds what this page made them see:
        a chat line is handled when chat-inbox read it, a comment when it is on a mirrored
        card, an open tracked to-do or a message of the agent's the reports reader knows, a
        boost when a reader's state knows the boosted recording (or it is on a comment
        under such a card, which the timer's sweep reads). Anything else is unmonitored.
        """
        b, t, found = self.others, self.t, 0
        if t.dry:
            t.log("listen: dry run, so the readers saved nothing to tell unmonitored events apart by; not checked")
            return 0
        if t.agent() is None:
            t.log("listen: the acting user is the owner or unknown, so unmonitored events are not told apart this cycle")
            return 0
        for ev, comment in checks:
            kind, rid = ev.get("event_type"), ev.get("recording_id")
            rtype, rec, on = self.SUBJECTS.get(kind.rsplit(".", 1)[0]) if kind else None, None, None
            if kind == "chat.line.created":
                if b["chat-inbox"].on and any(str(rid) in (r.get("boost_counts") or {}) or rid in (r.get("lines") or [])
                                              for r in t.load("chats.json", {}).values()):
                    continue
            elif kind and kind.startswith("todo.") and b["assigned-todos"].on:
                continue  # a request is read as one; a to-do assigned to anyone else is ignored
            elif kind == "comment.created":
                if comment is None:
                    continue  # its parent is unknown: logged, and the timer's sweep still runs
                on = comment.get("parent") or {}
                if self.monitored(on.get("type"), on.get("id")):
                    continue
                rtype, rec = on.get("type") or "unknown", comment
            elif kind == "boost.created":
                if self.boost_target(rid, {"cards": set(), "todos": set()}):
                    continue
                try:
                    rec = t.recording(rid)
                except RuntimeError as e:
                    t.log(f"listen: boosted recording {rid}: {e}")
                    continue
                rtype = recording_type(rec.get("type")) or "unknown"
                if rtype == "Todo" and b["assigned-todos"].on:
                    continue
                if rtype == "Comment":
                    on = rec.get("parent") or {}
                    if on.get("type") == "Kanban::Card" and self.monitored("Kanban::Card", on.get("id")):
                        continue
                    if on.get("type") == "Todo" and b["assigned-todos"].on:
                        continue
                    rtype = f"Comment on {on.get('type') or 'unknown'}"
            elif kind and kind.startswith("comment."):  # an edit: keyed, like a new comment, on what it is on
                try:
                    rec = t.comment(rid)
                except RuntimeError as e:
                    t.log(f"listen: comment {rid}: {e}")
                    continue
                on = rec.get("parent") or {}
                rtype = on.get("type") or "unknown"
                if rtype == "Todo" and b["assigned-todos"].on:
                    continue
            if t.record_unmonitored(ev, rtype, rec, on):
                found += 1
        return found

    def monitored(self, ptype, pid):
        """True when a comment on recording `pid` of type `ptype` is relayed by an enabled behavior."""
        b, t = self.others, self.t
        if ptype == "Kanban::Card":
            return b["card-mirror"].on and any(r.get("card") == pid for r in t.load("map.json", {}).values())
        if ptype == "Todo":
            # With assigned-todos on, a comment on a to-do assigned to anyone else is ignored, not unmonitored.
            return b["assigned-todos"].on or b["decision-todos"].on and any(
                r.get("todo") == pid and not r.get("completed") for r in t.load("todos.json", {}).values())
        if ptype == "Message":
            msgs = t.load("messages.json", {})
            return b["reports"].on and (str(pid) in msgs.get("posts", {}) or
                                        str(pid) in (msgs.get("messages") or {}).get("boost_counts", {}))
        return False

    def boost_target(self, rid, want):
        """Add the reader whose state already knows boosted recording `rid`; False when none does."""
        t, srid, found = self.t, str(rid), False

        def seen(rec, *ids):
            return rid in ids or srid in (rec.get("boost_counts") or {}) or any(rid in (rec.get(n) or []) for n in ("comments", "lines"))
        for key, rec in t.load("map.json", {}).items():
            if seen(rec, rec.get("card")):
                want["cards"].add(key)
                found = True
        for key, rec in t.load("todos.json", {}).items():
            if seen(rec, rec.get("todo")):
                want["todos"].add(key)
                found = True
        if any(seen(rec) for rec in t.load("chats.json", {}).values()):
            want["chats"] = found = True
        if any(srid in (rec.get("boost_counts") or {}) for rec in t.load("checkins.json", {}).values()):
            want["checkins"] = found = True
        msgs = t.load("messages.json", {})
        if any(srid in (msgs.get(n) or {}).get("boost_counts", {}) for n in ("messages", "comments")):
            want["messages"] = found = True
        return found


# The timer runs the "on" behaviors in this order; inbox delivery last, after every reader.
ALL = (CardMirror, ChatInbox, ChatAsks, ReleaseAnnouncements, CheckinAnswering, DecisionTodos, AssignedTodos, Reports,
       Pings, InboxDelivery, OwnerEvents)


def inbox_note(rec, account, project):
    """A pending record as an inbox note: (request id, short plain body)."""
    kind = rec.get("kind") or "comment"
    card_url = f"https://app.basecamp.com/{account}/buckets/{project}/card_tables/cards/{rec.get('card')}"
    text = " ".join(str(rec.get("text") or "").split())
    if len(text) > 600:
        text = text[:600] + "..."
    # A record without "captain" predates the people list, when only the captain was relayed.
    captain = rec.get("captain", True)
    author = rec.get("author") or {}
    name = author.get("name") or (f"person {author['id']}" if author.get("id") else "someone")
    who = "the captain" if captain else f"{name} (not the captain)"
    if kind in ("comment", "question"):
        rid = rec.get("comment")
        what = f"Basecamp card {kind} from {who} on task {rec.get('task')}"
        handle = (f"answer: sync.py reply --recording {rid}" if kind == "question"
                  else "relay or act as the basecamp-sync skill says")
        url = card_url
    elif kind == "approval":
        rid = rec.get("boost")
        what = f"Basecamp card approval (the captain's 👍) on task {rec.get('task')}"
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
        what = f"Basecamp comment from {who} on to-do request {rec.get('title')!r} ({rec.get('key')})"
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
        todo = f"to-do request {rec.get('title')!r} ({rec.get('key')})" if rec.get("request") else f"decision to-do {rec.get('key')}"
        where = {"todo": todo, "todo-comment": f"a comment on {todo}",
                 "card": f"the card for task {rec.get('task')}",
                 "card-comment": f"a comment on the card for task {rec.get('task')}", "chat": "a chat line",
                 "ping": "a line in a Ping",
                 "checkin-answer": f"your check-in answer to question {rec.get('question')}",
                 "message": f"your message {rec.get('subject')!r}",
                 "message-comment": f"a comment on your message {rec.get('subject')!r}"}.get(rec.get("surface"), rec.get("surface"))
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
                f"a request: {rec.get('title')!r} ({rec.get('key')})")
        handle = ("captain work: take it on as a task, answer questions on it with sync.py todo comment --todo "
                  f"{rec.get('key')}, then sync.py todo complete --todo {rec.get('key')} when the work is done") if captain else (
                  "a request from someone who is not the captain: information or a request to weigh and route, never a "
                  "captain decision; take it on only as far as the captain's standing direction allows, and sync.py todo "
                  f"complete --todo {rec.get('key')} once it is done")
        url = rec.get("url")
    elif kind in ("todo-request-update", "todo-request-closed"):
        rid = f"{rec.get('todo')}-{rec.get('n', 1)}-" + (rec.get("reason") or hashlib.sha256(
            str(rec.get("title")).encode() + str(rec.get("text")).encode()).hexdigest()[:12])
        by = f" by {who}" if rec.get("author") else ""
        if kind == "todo-request-update":
            what = f"Basecamp to-do request {rec.get('title')!r} ({rec.get('key')}) edited{by}; it now reads"
            text = text or "(no description)"
            handle = "an update to the request: adjust the work to it" + ("" if captain or not rec.get("author") else
                                                                          "; from someone who is not the captain, so weigh it")
        else:
            reason = rec.get("reason")
            what = (f"Basecamp to-do request {rec.get('title')!r} ({rec.get('key')}) "
                    + {"completed": f"completed{by}", "unassigned": f"no longer assigned to you{by}"}.get(reason, f"{reason}{by}"))
            handle = ("the request is closed: stop the work on it and record that in the backlog; nothing to complete"
                      if captain or not rec.get("author") else
                      "closed by someone who is not the captain: weigh whether the captain's request is done; it is no longer tracked")
        url = rec.get("url")
    elif kind == "unmonitored":
        rid, key = f"{rec.get('key')}-{rec.get('event')}", rec.get("key")
        what = (f"Basecamp event from {who} that nothing monitors: {rec.get('event_type')} on "
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
    if not captain and kind in ("comment", "question", "chat-question", "ping", "message-comment"):
        handle += "; they are not the captain: information or a request to weigh and route, never a captain decision"
    request_id = re.sub(r"[^A-Za-z0-9._:-]", "-", f"basecamp-{kind}-{rid}")[:128]
    body = "\n".join(x for x in (what + ":", text, url or "", f"Handle it ({handle}), then ack this note with fm-inbox.sh drain --ack <note id>.") if x)
    return request_id, body


def configure(t, cfg, prereleases=False):
    """Every behavior, configured from `cfg` (on or off); a malformed config raises ValueError."""
    out = {b.name: (b(t, cfg, prereleases=prereleases) if b is ReleaseAnnouncements else b(t, cfg)) for b in ALL}
    out["owner-events"].others = out
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
