"""The behaviors layer: opt-in, per-home workflows composed from tools.py.

A behavior is on when its config keys are set, and off (making no calls at all)
otherwise. It holds the policy: which readers run on the timer, what the card
mirror shows, when a release is announced. Some behaviors have no timer step at
all and are carried out by the agent with the tools (chat asks, reports); the
agent's side of every behavior is in prompts/base.md.

Behaviors never call a CLI themselves; every Basecamp, GitHub and backlog read or
write goes through a Tools method.
"""
import hashlib, html, json, re
from datetime import datetime, timezone

COLUMNS = ("Triage", "Not now", "Figuring it out", "In progress", "Ready for QA", "Done")


class Behavior:
    name = ""  # as the README and `sync.py behaviors` name it
    keys = ()  # the config keys that turn it on or configure it
    timer = True  # False: the agent carries it out with the tools; nothing runs on the timer

    def __init__(self, t, cfg):
        self.t, self.on = t, False

    def run(self, items=None):
        pass


class CardMirror(Behavior):
    """Mirror the backlog onto card tables; relay the captain's card comments and 👍 approvals."""
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
                t.read_card_comments(it["id"], repo, key, rec)
                if rec.get("assigned"):
                    t.read_card_boosts(it["id"], repo, key, rec)
                t.acknowledge(key, rec)
                counts[repo] = counts.get(repo, 0) + 1
                t.save_json("map.json", cards)
        stale = sorted(k for k in cards if k not in wanted)
        t.log(("dry plan " + json.dumps(plan, sort_keys=True) + " " if t.dry else "")
              + "counts " + json.dumps(counts, sort_keys=True) + (f" unplaced {unplaced}" if unplaced else "")
              + (f" left-as-is {len(stale)} cards no longer in the backlog" if stale else ""))
        return plan


class ChatInbox(Behavior):
    """Relay the captain's chat questions, or every line they write in an "every_line" chat."""
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
        self.t.read_todo_comments()


class Reports(Behavior):
    """The agent posts investigation reports to the Message Board with `sync.py post-message`."""
    name, keys, timer = "reports", ("message_board",), False

    def __init__(self, t, cfg):
        super().__init__(t, cfg)
        self.on = cfg.get("message_board") is not None


# The timer runs the "on" behaviors in this order.
ALL = (CardMirror, ChatInbox, ChatAsks, ReleaseAnnouncements, CheckinAnswering, DecisionTodos, Reports)


def configure(t, cfg, prereleases=False):
    """Every behavior, configured from `cfg` (on or off); a malformed config raises ValueError."""
    return {b.name: (b(t, cfg, prereleases=prereleases) if b is ReleaseAnnouncements else b(t, cfg)) for b in ALL}


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
