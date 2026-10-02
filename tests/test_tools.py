"""Tests for the to-do and message tools, the to-do comment reader, the CLI and the layer boundary.

The basecamp CLI is stubbed; nothing touches the network.
"""
import io, json, os, sys, unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sync import ACTING, CAPTAIN, Base, Stub, item  # noqa: E402
import sync  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TodoStub(Stub):
    """Adds the to-do, comment-create and message calls to the basecamp stub, keeping each call's stdin."""

    def __init__(self):
        super().__init__()
        self.inputs, self.todo_records, self.fail_create = [], {}, False

    def __call__(self, cmd, **kw):
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
                              "checkin-answering": "off", "decision-todos": "on", "reports": "on"})


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
