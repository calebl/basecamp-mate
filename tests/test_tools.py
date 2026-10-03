"""Tests for the to-do and message tools, the to-do comment reader, the CLI and the layer boundary.

The basecamp CLI is stubbed; nothing touches the network.
"""
import io, json, os, sys, unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sync import ACTING, CAPTAIN, Base, Stub, item, line  # noqa: E402
import sync  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TodoStub(Stub):
    """Adds the to-do, comment-create and message calls to the basecamp stub, keeping each call's stdin."""

    def __init__(self):
        super().__init__()
        self.inputs, self.todo_records, self.fail_create = [], {}, False
        self.notes, self.note_ids, self.inbox_exit = [], {}, 0  # fm-inbox.sh notes (request id, body, FM_HOME)

    def __call__(self, cmd, **kw):
        if cmd[0].endswith("/bin/fm-inbox.sh"):
            if isinstance(self.inbox_exit, Exception):
                raise self.inbox_exit
            assert cmd[1:3] == ["note", "--request-id"] and cmd[4:] == ["--json", "-"], cmd
            rid = cmd[3]
            if self.inbox_exit:
                return SimpleNamespace(stdout="", stderr="not woken", returncode=self.inbox_exit)
            outcome = "replay" if rid in self.note_ids else "created"
            if outcome == "created":
                self.note_ids[rid] = len(self.note_ids) + 1
                self.notes.append((rid, kw["input"], kw["env"]["FM_HOME"], cmd[0]))
            return SimpleNamespace(stdout=json.dumps({"outcome": outcome, "note_id": self.note_ids[rid]}), stderr="", returncode=0)
        if cmd[0] == "basecamp":
            args = cmd[3:-3]
            core = args[2:] if args[:1] == ["-P"] else args
            mine = (core[:2] in (["todos", "create"], ["todos", "complete"], ["comments", "create"], ["messages", "create"])
                    or (core[:2] == ["api", "get"] and "/todos/" in core[2]))
            if mine:
                self.profiles.append(args[1] if args[:1] == ["-P"] else None)
                self.calls.append(core)
                self.inputs.append(kw.get("input"))
                return SimpleNamespace(stdout=json.dumps(self.answer(core, kw.get("input"))), stderr="", returncode=0)
        return super().__call__(cmd, **kw)

    def answer(self, core, text):
        self.next_id += 1
        if core[:2] == ["todos", "create"]:
            if self.fail_create:
                return {"ok": False, "error": "boom"}
            self.todo_records[str(self.next_id)] = {"id": self.next_id, "content": core[2], "completed": False}
            return {"ok": True, "data": {"id": self.next_id, "app_url": f"https://x/todos/{self.next_id}"}}
        if core[:2] == ["comments", "create"]:
            c = {"id": self.next_id, "creator": {"id": self.me}, "content": text}
            self.comments.setdefault(core[2], []).append(c)
            return {"ok": True, "data": c}
        if core[:2] == ["messages", "create"]:
            return {"ok": True, "data": {"id": self.next_id, "app_url": f"https://x/messages/{self.next_id}"}}
        if core[:2] == ["todos", "complete"]:
            self.todo_records[core[2]]["completed"] = True
            return {"ok": True, "data": None}
        tid = core[2].split("/")[-1].split(".")[0]
        return {"ok": True, "data": dict(self.todo_records.get(tid, {"id": int(tid), "content": "Old one"}),
                                         app_url=f"https://x/todos/{tid}", created_at="t0")}

    def made(self, verb):
        return [c for c in self.calls if c[:2] == verb.split()]


def comment(id, who=CAPTAIN, content="<p>Merge it</p>"):
    return {"id": id, "creator": {"id": who}, "content": content, "created_at": "t", "app_url": f"https://x/todos/c{id}"}


class ToolBase(Base):
    def setUp(self):
        super().setUp()
        self.stub = TodoStub()
        self.cfg(profile="agent", todos={"todoset": "66"}, message_board="99")

    def cfg(self, drop=(), **kw):
        p = os.path.join(self.cfgdir, "config.json")
        cfg = json.load(open(p))
        for k in drop:
            cfg.pop(k, None)
        cfg.update(kw)
        json.dump(cfg, open(p, "w"))

    def todos(self):
        p = os.path.join(self.cfgdir, "todos.json")
        return json.load(open(p)) if os.path.exists(p) else {}

    def pending(self):
        p = os.path.join(self.cfgdir, "pending-comments.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def create(self, key="ta-x", title="Merge? The fix", body="Evidence: https://github.com/o/r/pull/1", **kw):
        return self.sync(dry=kw.pop("dry", False)).todo_create(key, title, body, **kw)

    def poll(self, dry=False):
        self.sync(dry=dry).main([])


class TodoCreate(ToolBase):
    def test_loose_on_the_todoset_assigned_to_owner_and_tracked(self):
        tid = self.create()
        [call] = self.stub.made("todos create")
        self.assertEqual(call, ["todos", "create", "Merge? The fix", "--loose", "--todoset", "66", "--assignee", str(CAPTAIN),
                                "--description=Evidence: https://github.com/o/r/pull/1"])
        rec = self.todos()["ta-x"]
        self.assertEqual((rec["todo"], rec["url"], rec["cursor"], rec["title"]), (tid, f"https://x/todos/{tid}", 0, "Merge? The fix"))
        self.assertEqual(set(self.stub.profiles), {"agent"})

    def test_list_from_flag_or_config_and_bare_loose(self):
        self.create("a", todolist="7", due="2026-10-09")
        self.cfg(todos={"list": "8"})
        self.create("b", body="")
        self.cfg(todos={})
        self.create("c")
        a, b, c = self.stub.made("todos create")
        self.assertEqual(a[3:5] + a[-2:], ["--list", "7", "--due", "2026-10-09"])
        self.assertEqual(b[3:5], ["--list", "8"])
        self.assertFalse(any(x.startswith("--description") for x in b))
        self.assertEqual(c[3:5], ["--loose", "--assignee"])

    def test_tracked_key_never_duplicated(self):
        self.create()
        self.assertIsNone(self.create(title="Again"))
        self.assertEqual(len(self.stub.made("todos create")), 1)

    def test_refused_as_owner_without_profile_or_in_dry_run(self):
        self.stub.me = CAPTAIN
        self.assertIsNone(self.create())
        self.stub.me = ACTING
        self.assertIsNone(self.create(dry=True))
        self.cfg(profile=None)
        self.assertIsNone(self.create())
        self.assertEqual(self.stub.made("todos create"), [])
        self.assertEqual(self.todos(), {})

    def test_needs_config_and_a_safe_title(self):
        with self.assertRaises(ValueError):
            self.create(title="-rf")
        self.cfg(drop=("todos",))
        with self.assertRaises(RuntimeError):
            self.create()

    def test_failed_create_tracks_nothing(self):
        self.stub.fail_create = True
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertEqual(self.todos(), {})


class TodoComments(ToolBase):
    def setUp(self):
        super().setUp()
        self.tid = self.create()
        self.c = str(self.tid)

    def test_owner_comment_recorded_once_with_its_kind(self):
        self.stub.comments[self.c] = [comment(1, content="<p>Merge it &amp; ship</p>"), comment(2, who=1), comment(3, who=ACTING)]
        self.poll()
        self.poll()
        [rec] = self.pending()
        self.assertEqual(rec, {"kind": "todo-comment", "key": "ta-x", "todo": self.tid, "comment": 1, "question": False,
                               "url": "https://x/todos/c1", "text": "Merge it & ship", "at": "t"})
        self.assertEqual(self.todos()["ta-x"]["cursor"], 3)
        self.assertEqual(self.stub.posted(), [("1", "👍")])

    def test_question_gets_eyes_and_reply_comments_on_the_todo(self):
        self.stub.comments[self.c] = [comment(1, content="<p>there is an unresolved review comment?</p>")]
        self.poll()
        self.assertTrue(self.pending()[0]["question"])
        self.assertEqual(self.stub.posted(), [("1", "👀")])
        self.assertTrue(self.sync().reply(1, "Fixing it on the PR."))
        self.assertEqual(self.stub.made("comments create"), [["comments", "create", self.c, "-"]])
        self.assertEqual(self.stub.inputs[-1], "Fixing it on the PR.")
        self.assertEqual(self.stub.boosts["1"], [])
        self.assertEqual(self.todos()["ta-x"]["replied"], [1])
        self.assertFalse(self.sync().reply(1, "again"))
        self.poll()
        self.assertEqual(len(self.pending()), 1)  # the agent's own reply is not relayed

    def test_each_todo_keeps_its_own_cursor(self):
        other = self.create("ta-y")
        self.stub.comments[self.c] = [comment(1)]
        self.poll()
        self.stub.comments[str(other)] = [comment(5)]
        self.stub.comments[self.c].append(comment(9))
        self.poll()
        self.assertEqual([(r["key"], r["comment"]) for r in self.pending()], [("ta-x", 1), ("ta-x", 9), ("ta-y", 5)])
        self.assertEqual({k: v["cursor"] for k, v in self.todos().items()}, {"ta-x": 9, "ta-y": 5})

    def test_dry_run_records_and_boosts_nothing(self):
        self.stub.comments[self.c] = [comment(1)]
        self.poll(dry=True)
        self.assertEqual((self.pending(), self.stub.posted()), ([], []))
        self.assertEqual(self.todos()["ta-x"]["cursor"], 0)

    def test_owner_profile_records_but_never_boosts(self):
        self.stub.comments[self.c] = [comment(1)]
        self.stub.me = CAPTAIN
        self.poll()
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.stub.posted(), [])

    def test_completed_todo_is_no_longer_read(self):
        self.assertTrue(self.sync().todo_complete("ta-x"))
        self.assertEqual(self.stub.made("todos complete"), [["todos", "complete", self.c]])
        self.assertFalse(self.sync().todo_complete(self.c))  # by id; already completed
        self.stub.comments[self.c] = [comment(1)]
        self.poll()
        self.assertEqual(self.pending(), [])
        self.assertFalse([c for c in self.stub.calls if c[:2] == ["comments", "list"]])

    def test_reader_off_without_todos_config(self):
        self.stub.comments[self.c] = [comment(1)]
        self.cfg(drop=("todos",))
        self.poll()
        self.assertEqual(self.pending(), [])
        self.assertFalse([c for c in self.stub.calls if c[:2] == ["comments", "list"]])


class TodoCommandsRefused(ToolBase):
    def setUp(self):
        super().setUp()
        self.create()

    def test_comment_posts_markdown_on_a_tracked_todo(self):
        self.assertTrue(self.sync().todo_comment("ta-x", "- one\n- two"))
        self.assertEqual(self.stub.inputs[-1], "- one\n- two")
        with self.assertRaises(RuntimeError):
            self.sync().todo_comment("nope", "x")

    def test_comment_and_complete_refused_as_owner_or_dry(self):
        self.stub.me = CAPTAIN
        self.assertFalse(self.sync().todo_comment("ta-x", "x"))
        self.assertFalse(self.sync().todo_complete("ta-x"))
        self.stub.me = ACTING
        self.assertFalse(self.sync(dry=True).todo_comment("ta-x", "x"))
        self.assertFalse(self.sync(dry=True).todo_complete("ta-x"))
        self.assertEqual(self.stub.made("comments create") + self.stub.made("todos complete"), [])
        self.assertNotIn("completed", self.todos()["ta-x"])


class TodoTrack(ToolBase):
    def test_adopts_a_todo_without_relaying_its_history(self):
        self.stub.comments["42"] = [comment(3), comment(4)]
        self.assertTrue(self.sync().todo_track("old", 42))
        rec = self.todos()["old"]
        self.assertEqual((rec["todo"], rec["cursor"], rec["title"]), (42, 4, "Old one"))
        self.assertEqual(self.stub.made("todos create"), [])
        self.poll()
        self.assertEqual(self.pending(), [])
        self.stub.comments["42"].append(comment(8))
        self.poll()
        self.assertEqual([r["comment"] for r in self.pending()], [8])
        self.assertFalse(self.sync().todo_track("other", 42))


class PostMessage(ToolBase):
    def test_posts_on_the_configured_board(self):
        self.assertTrue(self.sync().post_message("Report: the deploy", "## Findings\n- one"))
        [call] = self.stub.made("messages create")
        self.assertEqual(call, ["messages", "create", "Report: the deploy", "-", "--message-board", "99"])
        self.assertEqual(self.stub.inputs[-1], "## Findings\n- one")

    def test_refused_as_owner_dry_or_unconfigured(self):
        self.stub.me = CAPTAIN
        self.assertFalse(self.sync().post_message("S", "b"))
        self.stub.me = ACTING
        self.assertFalse(self.sync(dry=True).post_message("S", "b"))
        self.assertEqual(self.stub.made("messages create"), [])
        self.cfg(drop=("message_board",))
        with self.assertRaises(RuntimeError):
            self.sync().post_message("S", "b")


class Cli(ToolBase):
    def run_cli(self, *argv, body="Body text"):
        f = os.path.join(self.tmp, "body.md")
        open(f, "w").write(body)
        argv = [a.replace("BODY", f) for a in argv]
        out = io.StringIO()
        with redirect_stdout(out):
            code = sync.cli([*argv[:2], "--home", self.home, "--config", os.path.join(self.cfgdir, "config.json"), *argv[2:]]
                            if argv[0] == "todo" else
                            [argv[0], "--home", self.home, "--config", os.path.join(self.cfgdir, "config.json"), *argv[1:]],
                            runner=self.stub)
        return code, out.getvalue()

    def test_todo_lifecycle(self):
        self.assertEqual(self.run_cli("todo", "create", "--key", "k", "--title", "Merge?", "--body-file", "BODY")[0], 0)
        tid = self.todos()["k"]["todo"]
        self.assertEqual(self.run_cli("todo", "comment", "--todo", "k", "--body-file", "BODY")[0], 0)
        self.assertEqual(self.run_cli("todo", "complete", "--todo", str(tid))[0], 0)
        self.assertIn("completed", self.todos()["k"])
        self.assertEqual(self.run_cli("todo", "complete", "--todo", "missing")[0], 1)
        self.assertIn("FAILED todo complete", open(os.path.join(self.cfgdir, "sync.log")).read())

    def test_post_message_and_behaviors(self):
        self.assertEqual(self.run_cli("post-message", "--subject", "Report", "--body-file", "BODY")[0], 0)
        code, out = self.run_cli("behaviors")
        self.assertEqual(code, 0)
        on = {ln.split()[0]: ln.split()[1] for ln in out.splitlines()}
        self.assertEqual(on, {"card-mirror": "on", "chat-inbox": "off", "chat-asks": "off", "release-announcements": "off",
                              "checkin-answering": "off", "decision-todos": "on", "reports": "on",
                              "pings": "off", "inbox-delivery": "off", "owner-events": "off"})


def boost(id, who=CAPTAIN, content="a"):
    return {"id": id, "booster": {"id": who}, "content": content, "created_at": "tb"}


class TodoBoosts(ToolBase):
    def setUp(self):
        super().setUp()
        self.tid = self.create()
        self.c = str(self.tid)

    def boost_reads(self):
        return [c[2].split("/")[4] for c in self.stub.calls if c[:2] == ["api", "get"] and c[2].endswith("/boosts.json")]

    def test_owner_boost_on_the_todo_recorded_once(self):
        self.stub.boosts[self.c] = [boost(70, content="<div>yes</div>"), boost(71, who=ACTING)]
        self.poll()
        self.poll()
        [rec] = self.pending()
        self.assertEqual(rec, {"kind": "boost", "surface": "todo", "key": "ta-x", "todo": self.tid, "boost": 70,
                               "recording": self.tid, "text": "yes", "url": f"https://x/todos/{self.tid}", "at": "tb"})
        self.assertEqual(self.todos()["ta-x"]["boost_seen"], [70])

    def test_owner_boost_on_the_agents_own_comment_is_an_answer(self):
        mine = dict(comment(5, who=ACTING, content="<p>Ready again</p>"), boosts_count=1)
        self.stub.comments[self.c] = [mine]
        self.stub.boosts["5"] = [boost(80)]
        self.poll()
        self.assertEqual([(r["kind"], r["surface"], r["boost"], r["recording"], r["text"], r["url"]) for r in self.pending()],
                         [("boost", "todo-comment", 80, 5, "a", "https://x/todos/c5")])
        reads = len(self.boost_reads())
        self.poll()  # unchanged boosts_count: the comment's boosts are not read again
        self.assertEqual(self.boost_reads()[reads:], [self.c])
        mine["boosts_count"] = 2
        self.stub.boosts["5"].append(boost(81, content="b"))
        self.poll()
        self.assertEqual([r["boost"] for r in self.pending()], [80, 81])

    def test_dry_run_records_no_boost(self):
        self.stub.boosts[self.c] = [boost(70)]
        self.poll(dry=True)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.todos()["ta-x"]["boost_seen"], [])

    def test_track_skips_existing_boosts(self):
        self.stub.comments["42"] = [dict(comment(3), boosts_count=1)]
        self.stub.boosts.update({"42": [boost(90)], "3": [boost(91)]})
        self.sync().todo_track("old", 42)
        self.poll()
        self.assertEqual([r for r in self.pending() if r["key"] == "old"], [])
        self.stub.boosts["42"].append(boost(92))
        self.poll()
        self.assertEqual([r["boost"] for r in self.pending() if r["key"] == "old"], [92])


class Inbox(ToolBase):
    def setUp(self):
        super().setUp()
        self.cfg(chats=[77], checkins={"questionnaires": [55], "timezone": "UTC"})
        self.stub.lines = {"77": [line(1)]}
        self.stub.questions = []
        self.poll()  # cursor set; inbox still off
        self.stub.lines["77"].append(line(2))
        self.poll()  # one record from before the inbox was turned on
        self.cfg(inbox={})

    def test_new_records_become_notes_once(self):
        self.poll()
        self.assertEqual(self.stub.notes, [])  # the record from before is not delivered
        tid = self.create()
        self.stub.lines["77"].append(line(3, content="ship it?"))
        self.stub.comments[str(tid)] = [comment(9, content="<p>Merge it</p>")]
        self.stub.boosts[str(tid)] = [boost(70)]
        self.poll()
        self.poll()
        self.assertEqual([n[0] for n in self.stub.notes],
                         ["basecamp-chat-question-3", "basecamp-todo-comment-9", "basecamp-boost-70"])
        rid, body, fm_home, cmd = self.stub.notes[1]
        self.assertEqual((fm_home, cmd), (self.home, os.path.join(self.home, "bin", "fm-inbox.sh")))
        self.assertEqual(body.splitlines()[:3], ["Basecamp comment from the captain on decision to-do ta-x:",
                                                 "Merge it", "https://x/todos/c9"])
        self.assertIn("drain --ack", body)
        self.assertEqual(json.load(open(os.path.join(self.cfgdir, "inbox.json")))["cursor"], len(self.pending()))

    def test_failed_note_logged_once_and_retried_in_order(self):
        self.poll()
        self.stub.lines["77"] += [line(3), line(4)]
        self.stub.inbox_exit = 3
        self.poll()
        self.poll()
        log = open(os.path.join(self.cfgdir, "sync.log")).read()
        self.assertEqual(log.count("FAILED inbox note basecamp-chat-question-3"), 1)
        self.assertIn("still failing", log)
        self.assertEqual(self.stub.notes, [])
        self.stub.inbox_exit = FileNotFoundError("fm-inbox.sh")
        self.poll()  # a missing fm-inbox.sh never fails the run
        self.stub.inbox_exit = 0
        self.poll()
        self.assertEqual([n[0] for n in self.stub.notes], ["basecamp-chat-question-3", "basecamp-chat-question-4"])
        self.assertEqual(json.load(open(os.path.join(self.cfgdir, "inbox.json")))["failing"], [])

    def test_replay_is_not_a_new_note(self):
        self.poll()
        self.stub.lines["77"].append(line(3))
        self.poll()
        os.remove(os.path.join(self.cfgdir, "inbox.json"))
        state = {"cursor": 0, "failing": []}
        json.dump(state, open(os.path.join(self.cfgdir, "inbox.json"), "w"))
        self.poll()  # every record again: the earlier one is created, the delivered one replays
        self.assertEqual([n[0] for n in self.stub.notes], ["basecamp-chat-question-3", "basecamp-chat-question-2"])

    def test_other_home_dry_run_and_off(self):
        self.cfg(inbox={"fm_home": os.path.join(self.tmp, "main")})
        self.poll(dry=True)
        self.stub.lines["77"].append(line(3))
        self.poll(dry=True)
        self.assertEqual(self.stub.notes, [])
        self.assertFalse(os.path.exists(os.path.join(self.cfgdir, "inbox.json")))
        self.poll()
        self.stub.lines["77"].append(line(4))
        self.poll()
        self.assertEqual([(n[0], n[2]) for n in self.stub.notes],
                         [("basecamp-chat-question-3", os.path.join(self.tmp, "main")),
                          ("basecamp-chat-question-4", os.path.join(self.tmp, "main"))])

    def test_note_for_every_kind(self):
        recs = [{"task": "t", "card": 5, "comment": 9, "text": "ok"}, {"kind": "question", "task": "t", "card": 5, "comment": 10, "text": "why?"},
                {"kind": "approval", "task": "t", "card": 5, "boost": 11, "url": "u"},
                {"kind": "chat-question", "chat": 1, "line": 12, "text": "hi", "url": "u"},
                {"kind": "checkin", "question": 13, "date": "2026-10-02", "title": "Open issues?", "url": "u"},
                {"kind": "todo-comment", "key": "k", "comment": 14, "text": "x", "url": "u"},
                {"kind": "boost", "surface": "todo-comment", "key": "k", "boost": 15, "recording": 3, "text": "a", "url": "u"}]
        ids = [sync.behaviors.inbox_note(r, "1", "2")[0] for r in recs]
        self.assertEqual(ids, ["basecamp-comment-9", "basecamp-question-10", "basecamp-approval-11", "basecamp-chat-question-12",
                               "basecamp-checkin-13-2026-10-02", "basecamp-todo-comment-14", "basecamp-boost-15"])
        _, body = sync.behaviors.inbox_note(recs[1], "1", "2")
        self.assertIn("https://app.basecamp.com/1/buckets/2/card_tables/cards/5", body)
        self.assertIn("sync.py reply --recording 10", body)
        self.assertIn("Open issues?", sync.behaviors.inbox_note(recs[4], "1", "2")[1])
        self.assertIn("a comment on decision to-do k", sync.behaviors.inbox_note(recs[6], "1", "2")[1])


class Boosts(ToolBase):
    """Owner boosts on every monitored surface, each recorded once with its text, recording and surface."""

    def boosts(self):
        return [(r["surface"], r["recording"], r["boost"], r["text"]) for r in self.pending() if r["kind"] == "boost"]

    def test_chat_lines_including_the_agents_own(self):
        self.cfg(chats=[77])
        self.stub.lines = {"77": [line(1)]}
        self.poll()  # seeds the cursor and boost counts
        mine = dict(line(2, who=ACTING, content="Done, see the PR."), boosts_count=1)
        self.stub.lines["77"].append(mine)
        self.stub.boosts["2"] = [boost(60, content="thanks"), boost(61, who=ACTING)]
        self.poll()
        self.poll()
        self.assertEqual(self.boosts(), [("chat", 2, 60, "thanks")])
        self.assertEqual(self.pending()[-1]["chat"], 77)

    def test_history_is_seeded_not_replayed(self):
        self.cfg(chats=[77])
        self.stub.lines = {"77": [line(1)]}
        self.poll()
        self.stub.lines["77"][0]["boosts_count"] = 1
        self.stub.boosts["1"] = [boost(60)]
        chats = os.path.join(self.cfgdir, "chats.json")
        state = json.load(open(chats))
        state["77"].pop("boost_counts")  # chats.json from before boosts were read
        json.dump(state, open(chats, "w"))
        self.poll()
        self.assertEqual(self.boosts(), [])
        self.stub.lines["77"][0]["boosts_count"] = 2
        self.stub.boosts["1"].append(boost(62, content="more"))
        self.poll()
        self.assertEqual([b[2] for b in self.boosts()], [60, 62])  # a seeded count cannot tell old boosts apart

    def test_card_boosts_keep_approval_and_carry_text(self):
        self.sync().main([item("a", hold="q", hold_kind="captain")])  # seeds the card
        self.stub.boosts["501"] = [boost(7, content="👍"), boost(8, content="later please")]
        self.stub.comments = {"501": [dict(comment(9, who=ACTING, content="my reply"), boosts_count=1)]}
        self.stub.boosts["9"] = [boost(10, content="ok")]
        self.sync().main([item("a", hold="q", hold_kind="captain")])
        self.sync().main([item("a", hold="q", hold_kind="captain")])
        approvals = [(r["boost"], r["text"]) for r in self.pending() if r["kind"] == "approval"]
        self.assertEqual(approvals, [(7, "👍")])
        self.assertEqual(sorted(self.boosts()), [("card", 501, 8, "later please"), ("card-comment", 9, 10, "ok")])

    def test_checkin_answers_the_agent_posted(self):
        self.cfg(checkins={"questionnaires": [55], "timezone": "UTC"})
        now = sync.datetime(2026, 10, 2, 9, 30, tzinfo=sync.timezone.utc)
        q = {"id": 1, "title": "Run /stow and report", "answers_count": 2, "paused": True}
        self.stub.questions = [q]
        self.stub.answers["1"] = [{"id": 30, "creator": {"id": ACTING}, "group_on": "2026-10-01", "boosts_count": 0},
                                  {"id": 31, "creator": {"id": CAPTAIN}, "group_on": "2026-10-01", "boosts_count": 1},
                                  {"id": 32, "creator": {"id": ACTING}, "group_on": "2026-09-01", "boosts_count": 1}]

        def run():
            s = self.sync()
            s.today = lambda: now
            s.main([])
        run()  # seeds
        self.stub.answers["1"][0]["boosts_count"] = 1
        self.stub.boosts.update({"30": [boost(40, content="good")], "31": [boost(41)], "32": [boost(42)]})
        run()
        run()
        self.assertEqual(self.boosts(), [("checkin-answer", 30, 40, "good")])
        self.assertEqual(self.pending()[-1]["question"], 1)

    def test_messages_the_agent_posted_and_their_comments(self):
        recent = sync.datetime.now(sync.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        msgs = [{"id": 20, "creator": {"id": ACTING}, "subject": "Report", "created_at": recent, "boosts_count": 0,
                 "comments_count": 1, "app_url": "https://x/m/20"},
                {"id": 21, "creator": {"id": CAPTAIN}, "subject": "His", "created_at": recent, "boosts_count": 1},
                {"id": 22, "creator": {"id": ACTING}, "subject": "Old", "created_at": "2020-01-01T00:00:00Z", "boosts_count": 1}]
        self.stub.boosts["99"] = msgs  # the stub answers the board's messages read from its boosts table
        self.stub.comments["20"] = [dict(comment(25), boosts_count=0)]
        self.poll()  # seeds
        msgs[0]["boosts_count"] = 1
        self.stub.comments["20"][0]["boosts_count"] = 1
        self.stub.boosts.update({"20": [boost(50, content="agree")], "25": [boost(51)], "21": [boost(52)], "22": [boost(53)]})
        self.poll()
        self.poll()
        self.assertEqual(self.boosts(), [("message", 20, 50, "agree"), ("message-comment", 25, 51, "a")])
        self.assertEqual(self.pending()[0]["subject"], "Report")

    def test_owner_profile_reads_no_agent_posts(self):
        self.stub.me = CAPTAIN
        self.poll()
        self.assertFalse([c for c in self.stub.calls if "/message_boards/" in " ".join(c)])


class MessageComments(ToolBase):
    """The owner's comments on the agent's own recent Message Board posts are relayed like card comments."""

    def setUp(self):
        super().setUp()
        recent = sync.datetime.now(sync.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.msgs = [{"id": 20, "creator": {"id": ACTING}, "subject": "Report: deploy", "created_at": recent,
                      "comments_count": 2, "app_url": "https://x/m/20"},
                     {"id": 21, "creator": {"id": CAPTAIN}, "subject": "His own", "created_at": recent, "comments_count": 1},
                     {"id": 22, "creator": {"id": ACTING}, "subject": "Old", "created_at": "2020-01-01T00:00:00Z", "comments_count": 1}]
        self.stub.boosts["99"] = self.msgs  # the stub answers the board's messages read from its boosts table
        self.stub.comments = {"20": [comment(30, content="<p>Rerun it with the new flag</p>"), comment(31, who=ACTING)],
                              "21": [comment(32)], "22": [comment(33)]}

    def test_feedback_already_on_a_recent_post_is_relayed_once(self):
        self.poll()
        self.poll()
        [rec] = [r for r in self.pending() if r["kind"] == "message-comment"]
        self.assertEqual(rec, {"kind": "message-comment", "message": 20, "subject": "Report: deploy", "comment": 30,
                               "question": False, "url": "https://x/todos/c30", "text": "Rerun it with the new flag", "at": "t"})
        self.assertEqual(self.stub.posted(), [("30", "👍")])
        self.assertFalse([c for c in self.stub.calls if c[:3] in (["comments", "list", "21"], ["comments", "list", "22"])])

    def test_new_question_gets_eyes_and_reply_comments_on_the_post(self):
        self.poll()
        self.stub.comments["20"].append(comment(40, content="Why only one run?"))
        self.poll()
        self.assertEqual([r["comment"] for r in self.pending() if r["kind"] == "message-comment"], [30, 40])
        self.assertIn(("40", "👀"), self.stub.posted())
        self.assertTrue(self.sync().reply(40, "Because the second one was a dry run."))
        self.assertEqual(self.stub.made("comments create"), [["comments", "create", "20", "-"]])
        self.assertEqual(self.stub.boosts["40"], [])
        self.assertFalse(self.sync().reply(40, "again"))
        self.poll()
        self.assertEqual(len([r for r in self.pending() if r["kind"] == "message-comment"]), 2)

    def test_dry_run_and_owner_profile_record_nothing(self):
        self.poll(dry=True)
        self.assertEqual(self.pending(), [])
        self.assertFalse(os.path.exists(os.path.join(self.cfgdir, "messages.json")))
        self.stub.me = CAPTAIN
        self.poll()
        self.assertEqual(self.pending(), [])

    def test_delivered_to_the_inbox(self):
        self.cfg(inbox={})
        self.poll()
        [(rid, body, _, _)] = self.stub.notes
        self.assertEqual(rid, "basecamp-message-comment-30")
        self.assertEqual(body.splitlines()[:3], ["Basecamp comment from the captain on your message 'Report: deploy':",
                                                 "Rerun it with the new flag", "https://x/todos/c30"])


class Layers(unittest.TestCase):
    def test_behaviors_never_call_a_cli_directly(self):
        src = open(os.path.join(ROOT, "behaviors.py")).read()
        for banned in ("subprocess", ".bc(", ".run(", "basecamp\"", "open("):
            self.assertNotIn(banned, src)

    def test_tools_hold_no_behavior_policy(self):
        src = open(os.path.join(ROOT, "tools.py")).read()
        self.assertNotIn("import behaviors", src)
        self.assertNotIn("COLUMNS", src)

    def test_example_config_turns_on_the_same_behaviors_as_before(self):
        s = sync.Sync("/nonexistent", os.path.join(ROOT, "examples", "config.example.json"), runner=Stub())
        self.assertEqual({n for n, b in s.behaviors.items() if b.on},
                         {"card-mirror", "chat-inbox", "chat-asks", "checkin-answering"})
        self.assertEqual(s.behaviors["chat-inbox"].every_line, {"88888888"})

    def test_old_config_runs_the_same_calls(self):
        """A pre-layers config (cards and chats, no todos or message_board) makes no new kind of call."""
        b = Base("run")
        b.setUp()
        try:
            b.stub.comments = {"501": [comment(9)]}
            b.sync().main([item("a", hold="q", hold_kind="captain")])
            kinds = {tuple(c[:2]) for c in b.stub.calls}
            self.assertEqual(kinds, {("cards", "create"), ("comments", "list"), ("api", "get")})
        finally:
            b.tearDown()


if __name__ == "__main__":
    unittest.main()
