"""Tests for the assigned-todos behavior: to-dos the listened-to people assign to the agent's login, as requests.

/my/assignments.json, the to-dos, their comments and boosts and the event feed are stubbed; nothing touches the network.
"""
import json, os, sys, unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sync import ACTING, CAPTAIN  # noqa: E402
from test_tools import boost, comment  # noqa: E402
from test_listen import FeedStub, ListenBase, PROJECT, event  # noqa: E402
import behaviors  # noqa: E402
import sync  # noqa: E402

PARTNER, STRANGER = 55555555, 66666666
OTHER = 44444444  # another project of the account
EYES = "\U0001F440"


def todo(id, by=CAPTAIN, to=(ACTING,), title="Rotate the API keys", description="<div>Both <b>staging</b> and prod.</div>",
         bucket=PROJECT):
    return {"id": id, "content": title, "description": description, "completed": False, "status": "active",
            "creator": {"id": by, "name": f"person-{by}"}, "assignees": [{"id": p, "name": f"person-{p}"} for p in to],
            "updated_at": "2026-10-04T12:00:00Z", "bucket": {"id": bucket, "name": f"Project {bucket}", "type": "Project"}}


class AssignStub(FeedStub):
    """Adds /my/assignments.json, answered from the stubbed to-dos open and assigned to the acting user, and keeps the
    project (-p) each call ran on."""

    def __init__(self):
        super().__init__()
        self.on = []  # (call, its -p project)

    def __call__(self, cmd, **kw):
        if cmd[0] == "basecamp":
            args = cmd[3:-3]
            core = args[2:] if args[:1] == ["-P"] else args
            self.on.append((core, cmd[-2]))
            if core[:3] == ["api", "get", "/my/assignments.json"]:
                self.calls.append(core)
                mine = [{"id": t["id"], "type": "todo", "content": t["content"], "completed": False,
                         "bucket": {"id": t["bucket"]["id"], "name": t["bucket"]["name"]}}
                        for t in self.todo_records.values()
                        if not t.get("completed") and self.me in [a["id"] for a in t.get("assignees", [])]]
                data = {"priorities": mine[:1], "non_priorities": mine[1:]}
                return SimpleNamespace(stdout=json.dumps({"ok": True, "data": data}), stderr="", returncode=0)
        return super().__call__(cmd, **kw)

    def paths(self, verb="get"):
        return [c[2] for c in self.calls if c[:2] == ["api", verb]]

    def fetched(self, tid, bucket=PROJECT):
        return self.paths().count(f"/buckets/{bucket}/todos/{tid}.json")

    def projects(self, verb):
        """The -p project of each `verb` call (e.g. "comments list")."""
        return [p for c, p in self.on if c[:2] == verb.split()]


class AssignBase(ListenBase):
    def setUp(self):
        super().setUp()
        self.stub = AssignStub()
        self.stub.lines = {"77": []}
        self.cfg(assigned_todos={}, people=[PARTNER])

    def add(self, t):
        self.stub.todo_records[str(t["id"])] = t
        return t

    def edit(self, tid, **kw):
        self.stub.todo_records[str(tid)].update(kw)

    def request(self, tid=42):
        return self.todos()[f"request-{tid}"]


class Sweep(AssignBase):
    def test_a_todo_the_captain_assigns_is_recorded_once_acknowledged_and_tracked(self):
        self.add(todo(42))
        self.poll()
        self.poll()
        [rec] = self.pending()
        self.assertEqual({k: rec[k] for k in ("kind", "key", "todo", "n", "reopened", "title", "text", "captain")},
                         {"kind": "todo-request", "key": "request-42", "todo": 42, "n": 1, "reopened": False,
                          "title": "Rotate the API keys", "text": "Both staging and prod.", "captain": True})
        self.assertEqual(rec["author"], {"id": CAPTAIN, "name": f"person-{CAPTAIN}"})
        self.assertEqual(rec["url"], "https://x/todos/42")
        self.assertEqual(self.stub.posted(), [("42", EYES)])
        req = self.request()
        self.assertEqual((req["todo"], req["cursor"], req["request"]["captain"]), (42, 0, True))

    def test_another_listened_to_person_is_recorded_as_not_the_captain(self):
        self.add(todo(42, by=PARTNER))
        self.poll()
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["captain"], rec["author"]["id"]), ("todo-request", False, PARTNER))

    def test_a_todo_assigned_by_someone_not_listened_to_is_ignored_and_fetched_once(self):
        self.add(todo(42, by=STRANGER))
        self.poll()
        self.poll()
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.stub.fetched(42), 1)
        self.assertNotIn("request-42", self.todos())

    def test_todos_assigned_to_anyone_else_or_in_other_projects_are_never_read(self):
        self.add(todo(42, to=(CAPTAIN,)))
        self.add(todo(43, bucket=OTHER))
        self.poll()
        self.assertEqual(self.pending(), [])
        self.assertEqual((self.stub.fetched(42), self.stub.fetched(43)), (0, 0))

    def test_comments_and_boosts_arrive_as_part_of_the_request(self):
        self.add(todo(42))
        self.stub.comments["42"] = [comment(5, content="<p>Prod first, please</p>")]
        self.poll()
        self.stub.comments["42"].append(comment(6, content="<p>Done yet?</p>"))
        self.stub.boosts["42"] = [boost(7)]
        self.poll()
        kinds = [(r["kind"], r.get("comment") or r.get("boost")) for r in self.pending()]
        # The comment already on it when it was assigned is part of the request too.
        self.assertEqual(kinds, [("todo-request", None), ("todo-comment", 5), ("todo-comment", 6), ("boost", 7)])
        self.assertTrue(all(r.get("request") and r["title"] == "Rotate the API keys" for r in self.pending()[1:]))
        self.assertEqual(self.pending()[-1]["surface"], "todo")
        self.assertTrue(self.sync().reply(6, "Prod is done."))

    def test_an_edit_is_an_update_recorded_once(self):
        self.add(todo(42))
        self.poll()
        self.edit(42, description="<div>Prod only.</div>")
        self.poll()
        self.poll()
        upd = self.pending()[-1]
        self.assertEqual(len(self.pending()), 2)
        self.assertEqual((upd["kind"], upd["text"], upd["author"]), ("todo-request-update", "Prod only.", None))

    def test_the_owner_completing_it_closes_the_request(self):
        self.add(todo(42))
        self.poll()
        self.edit(42, completed=True, completion={"creator": {"id": CAPTAIN, "name": "Cap"}, "created_at": "tc"})
        self.poll()
        self.stub.comments["42"] = [comment(9)]
        self.poll()
        closed = self.pending()[-1]
        self.assertEqual(len(self.pending()), 2)
        self.assertEqual((closed["kind"], closed["reason"], closed["captain"]), ("todo-request-closed", "completed", True))
        self.assertTrue(self.request()["completed"])
        self.assertFalse(self.sync().todo_complete("request-42"))
        self.assertEqual(self.stub.made("todos complete"), [])

    def test_unassigned_closes_it_and_assigned_again_is_a_new_request(self):
        self.add(todo(42))
        self.poll()
        self.edit(42, assignees=[{"id": CAPTAIN}])
        self.poll()
        self.assertEqual(self.pending()[-1]["reason"], "unassigned")
        self.assertFalse(self.sync().todo_complete(42))
        self.edit(42, assignees=[{"id": ACTING}])
        self.poll()
        again = self.pending()[-1]
        self.assertEqual((again["kind"], again["n"], again["reopened"]), ("todo-request", 2, True))
        self.assertEqual(self.stub.posted(), [("42", EYES)])  # the 👀 is already there

    def test_the_agent_completes_it_when_the_work_is_done_even_without_decision_todos(self):
        self.cfg(drop=("todos",))
        self.add(todo(42))
        self.poll()
        self.assertTrue(self.sync().todo_complete("42"))
        self.assertEqual(self.stub.made("todos complete"), [["todos", "complete", "42"]])
        self.poll()
        self.assertEqual([r["kind"] for r in self.pending()], ["todo-request"])
        with self.assertRaises(RuntimeError):
            self.create()

    def test_reads_at_most_limit_new_todos_a_sweep(self):
        self.cfg(assigned_todos={"limit": 2})
        for i in (41, 42, 43):
            self.add(todo(i))
        self.poll()
        self.assertEqual(len(self.pending()), 2)
        self.poll()
        self.assertEqual(len(self.pending()), 3)

    def test_refused_without_a_profile_or_as_the_owner(self):
        self.add(todo(42))
        self.stub.me = CAPTAIN
        self.poll()
        self.cfg(drop=("profile",))
        self.poll()
        self.assertNotIn("/my/assignments.json", self.stub.paths())
        self.assertEqual(self.pending(), [])

    def test_dry_run_records_and_boosts_nothing(self):
        self.add(todo(42))
        self.poll(dry=True)
        self.assertEqual((self.pending(), self.stub.posted(), self.todos()), ([], [], {}))

    def test_off_makes_no_calls(self):
        self.cfg(assigned_todos=False)
        self.add(todo(42))
        self.poll()
        self.assertNotIn("/my/assignments.json", self.stub.paths())

    def test_malformed_config_refused(self):
        for bad in ([], {"limit": 0}, {"scope": "everywhere"}):
            self.cfg(assigned_todos=bad)
            with self.assertRaises(ValueError):
                self.sync()


class Listener(AssignBase):
    def setUp(self):
        super().setUp()
        self.cfg(inbox={})
        self.poll()
        self.stub.calls.clear()

    def test_the_captain_assigning_a_todo_is_a_request_in_the_same_cycle(self):
        self.add(todo(42, by=STRANGER))  # created by someone else, assigned by the captain
        self.stub.page(event(10, kind="todo.assignment_changed", rid=42))
        self.listen()
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["author"]["id"], rec["captain"]), ("todo-request", CAPTAIN, True))
        self.assertEqual([n[0] for n in self.stub.notes], ["basecamp-todo-request-42-1"])
        self.assertEqual(self.stub.posted(), [("42", EYES)])

    def test_edit_and_completion_events_are_recorded_with_who_did_them(self):
        self.add(todo(42))
        self.stub.page(event(10, kind="todo.created", rid=42))
        self.listen()
        self.edit(42, description="<div>Prod only.</div>")
        self.stub.page(event(11, kind="todo.description_changed", rid=42))
        self.listen()
        self.edit(42, completed=True)
        self.stub.page(event(12, kind="todo.completed", rid=42))
        self.listen()
        self.assertEqual([(r["kind"], (r["author"] or {}).get("id")) for r in self.pending()],
                         [("todo-request", CAPTAIN), ("todo-request-update", CAPTAIN), ("todo-request-closed", CAPTAIN)])
        self.assertEqual(len(self.stub.notes), 3)

    def test_a_comment_on_a_request_runs_its_reader(self):
        self.add(todo(42))
        self.poll()
        self.stub.comments["42"] = [comment(5)]
        self.stub.parents[5] = {"type": "Todo", "id": 42}
        self.stub.page(event(10, kind="comment.created", rid=5))
        self.listen()
        self.assertEqual([r["kind"] for r in self.pending()], ["todo-request", "todo-comment"])

    def test_a_todo_assigned_to_someone_else_is_ignored_not_unmonitored(self):
        self.add(todo(42, to=(CAPTAIN,)))
        self.stub.parents[5] = {"type": "Todo", "id": 42}
        self.stub.recordings[6] = {"type": "Todo"}
        self.stub.recordings[7] = {"type": "Comment", "parent": {"type": "Todo", "id": 42}}
        self.stub.page(event(10, kind="todo.created", rid=42), event(11, kind="todo.completed", rid=42),
                       event(12, kind="comment.created", rid=5), event(13, kind="boost.created", rid=6),
                       event(14, kind="boost.created", rid=7))
        self.listen()
        self.assertEqual(self.pending(), [])
        self.assertFalse(os.path.exists(os.path.join(self.cfgdir, "unmonitored.json")))

    def test_off_they_stay_unmonitored(self):
        self.cfg(assigned_todos=False)
        self.add(todo(42))
        self.stub.page(event(10, kind="todo.created", rid=42))
        self.stub.recordings[42] = {"type": "Todo", "title": "Rotate the API keys"}
        self.listen()
        self.assertEqual([(r["kind"], r["key"]) for r in self.pending()], [("unmonitored", "todo.created/Todo")])

    def test_the_narrow_feed_adds_the_todo_events(self):
        self.cfg(listen={"unmonitored": False})
        self.listen()
        self.assertEqual(self.stub.queries[-1]["types"], "boost.created,chat.line.created,comment.created,"
                         "todo.assignment_changed,todo.completed,todo.created,todo.description_changed")

    def test_project_scope_polls_only_the_project(self):
        self.listen()
        self.assertEqual([q.get("buckets") for q in self.stub.queries], [str(PROJECT)])
        self.assertFalse(os.path.exists(os.path.join(self.cfgdir, "requests-feed.json")))


class AccountWide(AssignBase):
    """"scope": "account": to-dos assigned to the agent in any project of the account are requests, each with its project."""

    def setUp(self):
        super().setUp()
        self.cfg(assigned_todos={"scope": "account"})

    def test_a_todo_in_another_project_is_a_request_carrying_its_project(self):
        self.add(todo(42, bucket=OTHER))
        self.add(todo(43))
        self.poll()
        self.poll()
        recs = {r["todo"]: r for r in self.pending()}
        self.assertEqual(sorted(recs), [42, 43])
        self.assertEqual(recs[42]["project"], {"id": OTHER, "name": f"Project {OTHER}"})
        self.assertEqual(recs[43]["project"], {"id": int(PROJECT), "name": f"Project {PROJECT}"})
        # Read once to record it, then each sweep's edit check: always in its own project.
        self.assertEqual((self.stub.fetched(42, OTHER), self.stub.fetched(42)), (3, 0))
        self.assertIn(f"/buckets/{OTHER}/recordings/42/boosts.json", self.stub.paths("post"))
        self.assertEqual((self.request(42)["bucket"], self.request(42)["project_name"]), (OTHER, f"Project {OTHER}"))

    def test_its_comments_boosts_replies_and_completion_go_to_its_project(self):
        self.add(todo(42, bucket=OTHER))
        self.poll()
        self.stub.comments["42"] = [comment(6, content="<p>Done yet?</p>")]
        self.stub.boosts["42"] = [boost(7)]
        self.poll()
        self.assertEqual([(r["kind"], r["project"]["id"]) for r in self.pending()],
                         [("todo-request", OTHER), ("todo-comment", OTHER), ("boost", OTHER)])
        self.assertEqual(set(self.stub.projects("comments list")), {str(OTHER)})
        self.assertTrue(self.sync().reply(6, "Almost."))
        self.assertTrue(self.sync().todo_comment("request-42", "Started."))
        self.assertTrue(self.sync().todo_complete("request-42"))
        self.assertEqual(self.stub.projects("comments create") + self.stub.projects("todos complete"), [str(OTHER)] * 3)
        self.assertIn(f"/buckets/{OTHER}/recordings/6/boosts.json", self.stub.paths("post"))  # the 👀 on the question
        self.assertTrue(any(p.startswith(f"/buckets/{OTHER}/boosts/") for p in self.stub.paths("delete")))

    def test_its_edit_and_closing_are_read_in_its_project(self):
        self.add(todo(42, bucket=OTHER))
        self.poll()
        self.edit(42, description="<div>Prod only.</div>")
        self.poll()
        self.edit(42, assignees=[{"id": CAPTAIN}])
        self.poll()
        self.assertEqual([(r["kind"], r.get("reason"), r["project"]["id"]) for r in self.pending()],
                         [("todo-request", None, OTHER), ("todo-request-update", None, OTHER),
                          ("todo-request-closed", "unassigned", OTHER)])

    def test_the_project_scope_default_is_unchanged(self):
        self.cfg(assigned_todos={})
        self.add(todo(42, bucket=OTHER))
        self.poll()
        self.assertEqual((self.pending(), self.stub.fetched(42, OTHER)), ([], 0))


class AccountWideListener(AssignBase):
    def setUp(self):
        super().setUp()
        self.cfg(assigned_todos={"scope": "account"}, inbox={})
        self.poll()
        self.stub.calls.clear()

    def other(self, *events):
        """Queue an empty page for the project's poll, then `events` for the account-wide request poll."""
        self.stub.page()
        self.stub.page(*events)

    def test_a_second_poll_reads_every_bucket_with_its_own_position(self):
        self.listen()
        main, requests = self.stub.queries
        self.assertEqual(main.get("buckets"), str(PROJECT))
        self.assertNotIn("buckets", requests)
        self.assertEqual(requests["types"], "boost.created,comment.created,todo.assignment_changed,todo.completed,"
                         "todo.created,todo.description_changed")
        self.assertTrue(os.path.exists(os.path.join(self.cfgdir, "requests-feed.json")))

    def test_a_todo_assigned_in_another_project_is_a_request_in_the_same_cycle(self):
        self.add(todo(42, by=STRANGER, bucket=OTHER))
        self.other(event(10, kind="todo.assignment_changed", rid=42, bucket=OTHER))
        self.listen()
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["author"]["id"], rec["project"]["id"]), ("todo-request", CAPTAIN, OTHER))
        [(rid, body, _, _)] = self.stub.notes
        self.assertEqual(rid, "basecamp-todo-request-42-1")
        self.assertIn(f"in project 'Project {OTHER}' ({OTHER})", body)
        self.assertIn("route the work", body)

    def test_comments_boosts_and_closing_on_it_follow_it(self):
        self.add(todo(42, bucket=OTHER))
        self.other(event(10, kind="todo.created", rid=42, bucket=OTHER))
        self.listen()
        self.stub.comments["42"] = [comment(5)]
        self.stub.parents[5] = {"type": "Todo", "id": 42}
        self.other(event(11, kind="comment.created", rid=5, bucket=OTHER))
        self.listen()
        self.stub.boosts["42"] = [boost(8)]
        self.other(event(12, kind="boost.created", rid=42, bucket=OTHER))
        self.listen()
        self.edit(42, completed=True)
        self.other(event(13, kind="todo.completed", rid=42, bucket=OTHER))
        self.listen()
        self.assertEqual([r["kind"] for r in self.pending()], ["todo-request", "todo-comment", "boost", "todo-request-closed"])
        self.assertIn(f"/buckets/{OTHER}/comments/5.json", self.stub.paths())
        self.assertEqual(len(self.stub.notes), 4)

    def test_other_projects_events_on_untracked_todos_read_nothing(self):
        self.add(todo(42, to=(CAPTAIN,), bucket=OTHER))
        self.other(event(10, kind="todo.created", rid=42, bucket=OTHER), event(11, kind="comment.created", rid=5, bucket=OTHER),
                   event(12, kind="boost.created", rid=6, bucket=OTHER), event(13, kind="todo.created", rid=42, who=STRANGER,
                                                                          bucket=OTHER))
        self.listen()
        self.assertEqual(self.pending(), [])
        self.assertNotIn(f"/buckets/{OTHER}/comments/5.json", self.stub.paths())
        self.assertEqual(self.stub.fetched(42, OTHER), 1)


class Note(unittest.TestCase):
    def test_request_notes(self):
        rid, body = behaviors.inbox_note({"kind": "todo-request", "key": "request-42", "todo": 42, "n": 1,
                                          "title": "Rotate keys", "text": "Prod too.", "url": "https://x/todos/42",
                                          "captain": True}, "1", "2")
        self.assertEqual(rid, "basecamp-todo-request-42-1")
        self.assertIn("assigned to you by the captain, a request: 'Rotate keys'", body)
        self.assertIn("captain work", body)
        self.assertIn("sync.py todo complete --todo request-42", body)
        _, body = behaviors.inbox_note({"kind": "todo-request", "key": "request-42", "todo": 42, "captain": False,
                                        "author": {"id": PARTNER, "name": "Pat"}}, "1", "2")
        self.assertIn("by Pat (not the captain)", body)
        self.assertIn("never a captain decision", body)
        _, body = behaviors.inbox_note({"kind": "todo-comment", "request": True, "key": "request-42", "title": "Rotate keys",
                                        "comment": 5, "captain": True}, "1", "2")
        self.assertIn("on to-do request 'Rotate keys' (request-42)", body)
        self.assertIn("part of the request", body)
        _, body = behaviors.inbox_note({"kind": "boost", "surface": "todo", "request": True, "key": "request-42",
                                        "title": "Rotate keys", "boost": 8}, "1", "2")
        self.assertIn("on to-do request 'Rotate keys'", body)
        self.assertNotIn("settles the decision", body)

    def test_a_request_in_the_configured_project_does_not_name_it(self):
        _, body = behaviors.inbox_note({"kind": "todo-request", "key": "request-42", "todo": 42, "captain": True,
                                        "project": {"id": 2, "name": "Home"}}, "1", "2")
        self.assertNotIn("in project", body)
        _, body = behaviors.inbox_note({"kind": "todo-request-closed", "key": "request-42", "todo": 42, "reason": "trashed",
                                        "captain": None, "author": None, "project": {"id": 3, "name": "Ops"}}, "1", "2")
        self.assertIn("(request-42) in project 'Ops' (3) trashed", body)

    def test_update_and_closed_notes(self):
        rid, body = behaviors.inbox_note({"kind": "todo-request-update", "key": "request-42", "todo": 42, "title": "Rotate",
                                          "text": "Prod only.", "author": None, "captain": None}, "1", "2")
        self.assertTrue(rid.startswith("basecamp-todo-request-update-42-1-"))
        self.assertIn("edited; it now reads", body)
        rid, body = behaviors.inbox_note({"kind": "todo-request-closed", "key": "request-42", "todo": 42, "title": "Rotate",
                                          "reason": "completed", "author": {"id": CAPTAIN, "name": "Cap"},
                                          "captain": True}, "1", "2")
        self.assertEqual(rid, "basecamp-todo-request-closed-42-1-completed")
        self.assertIn("completed by the captain", body)
        self.assertIn("the request is closed", body)


if __name__ == "__main__":
    unittest.main()
