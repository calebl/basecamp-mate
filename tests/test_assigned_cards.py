"""Tests for the assigned-cards behavior: cards the listened-to people assign to the agent's login, as requests.

/my/assignments.json, the cards, their comments and the notifications are stubbed; nothing touches the network.
"""
import json, os, sys, unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sync import ACTING, CAPTAIN  # noqa: E402
from test_tools import comment  # noqa: E402
from test_notifications import PROJECT, note  # noqa: E402
from test_assigned_todos import AssignBase, AssignStub, STRANGER, todo  # noqa: E402
import behaviors  # noqa: E402

EYES = "\U0001F440"


def card(id, by=CAPTAIN, to=(ACTING,), title="Address security issues", content="<p>Check <b>dependabot</b>.</p>",
         bucket=PROJECT):
    return {"id": id, "title": title, "content": content, "completed": False, "status": "active", "type": "Kanban::Card",
            "creator": {"id": by, "name": f"person-{by}"}, "assignees": [{"id": p, "name": f"person-{p}"} for p in to],
            "updated_at": "2026-10-21T13:33:17Z", "app_url": f"https://x/cards/{id}",
            "bucket": {"id": bucket, "name": f"Project {bucket}", "type": "Project"}}


class CardStub(AssignStub):
    """Adds cards: listed in /my/assignments.json as "card", refetched by id, and completed with `basecamp cards done`.

    Like Basecamp, the listing has each entry's own "assignees", and also lists a card that is not assigned to the
    acting user but holds a step that is ("steps": its step ids for the acting user), with those steps as "children".
    """

    def __init__(self):
        super().__init__()
        self.cards = {}

    def __call__(self, cmd, **kw):
        if cmd[0] == "basecamp":
            args = cmd[3:-3]
            core = args[2:] if args[:1] == ["-P"] else args
            ok = lambda data: SimpleNamespace(stdout=json.dumps({"ok": True, "data": data}), stderr="", returncode=0)
            if core[:3] == ["api", "get", "/my/assignments.json"]:
                out = super().__call__(cmd, **kw)
                data = json.loads(out.stdout)["data"]
                data["non_priorities"] += [{"id": c["id"], "type": "card", "content": c["title"], "completed": False,
                                            "bucket": {"id": c["bucket"]["id"]}, "assignees": c["assignees"],
                                            "children": [{"id": s, "type": "step", "completed": False,
                                                          "assignees": [{"id": self.me}],
                                                          "bucket": {"id": c["bucket"]["id"]}, "parent": {"id": c["id"]}}
                                                         for s in c.get("steps", [])]}
                                           for c in self.cards.values()
                                           if not c["completed"] and (self.me in [a["id"] for a in c["assignees"]]
                                                                      or c.get("steps"))]
                return ok(data)
            if core[:2] == ["api", "get"] and "/card_tables/cards/" in core[2]:
                self.calls.append(core)
                return ok(self.cards[core[2].rsplit("/", 1)[1][:-5]])
            if core[:2] == ["cards", "done"]:
                self.calls.append(core)
                self.cards[core[2]]["completed"] = True
                return ok({})
        return super().__call__(cmd, **kw)


class CardBase(AssignBase):
    def setUp(self):
        super().setUp()
        self.stub = CardStub()
        self.stub.lines = {"77": []}
        self.cfg(assigned_cards={}, assigned_todos=False)

    def add_card(self, c):
        self.stub.cards[str(c["id"])] = c
        return c


class Sweep(CardBase):
    def test_a_card_the_captain_assigns_is_recorded_once_acknowledged_and_tracked(self):
        self.add_card(card(42))
        self.poll()
        self.poll()
        [rec] = self.pending()
        self.assertEqual({k: rec[k] for k in ("kind", "key", "todo", "title", "text", "captain", "url")},
                         {"kind": "card-request", "key": "request-42", "todo": 42, "title": "Address security issues",
                          "text": "Check dependabot .", "captain": True, "url": "https://x/cards/42"})
        self.assertEqual(self.stub.posted(), [("42", EYES)])
        self.assertEqual(self.request()["type"], "card")

    def test_its_comments_edit_and_completion_by_the_agent(self):
        self.add_card(card(42))
        self.poll()
        self.stub.comments["42"] = [comment(5)]
        self.stub.cards["42"]["title"] = "Address all security issues"
        self.poll()
        self.assertEqual([(r["kind"], r.get("type")) for r in self.pending()],
                         [("card-request", None), ("todo-comment", "card"), ("card-request-update", None)])
        s = self.sync()
        self.assertTrue(s.todo_complete("request-42"))
        self.assertIn(["cards", "done", "42"], [c[:3] for c in self.stub.calls])
        self.assertTrue(self.request()["completed"])

    def test_completing_a_card_already_done_records_it_completed(self):
        self.add_card(card(42))
        self.poll()
        self.stub.cards["42"]["completed"] = True
        self.assertTrue(self.sync().todo_complete("request-42"))
        self.assertNotIn(["cards", "done"], [c[:2] for c in self.stub.calls])
        self.assertTrue(self.request()["completed"])

    def test_the_owner_completing_it_closes_the_request(self):
        self.add_card(card(42))
        self.poll()
        self.stub.cards["42"]["completed"] = True
        self.poll()
        self.assertEqual((self.pending()[-1]["kind"], self.pending()[-1]["reason"]), ("card-request-closed", "completed"))

    def test_a_mirrored_card_or_one_assigned_by_a_stranger_is_never_a_request(self):
        self.add_card(card(42))
        self.add_card(card(43, by=STRANGER))
        with open(os.path.join(self.cfgdir, "map.json"), "w") as f:
            json.dump({"engine/t1": {"card": 42}}, f)
        self.poll()
        self.assertEqual(self.pending(), [])

    def test_a_card_listed_only_for_the_agents_step_is_never_fetched_nor_counted_against_the_limit(self):
        self.cfg(assigned_cards={"limit": 1})
        self.add_card(dict(card(41, to=(STRANGER,)), steps=[4101]))  # someone else's card, holding the agent's step
        self.add_card(card(42))
        self.poll()
        self.poll()
        self.assertEqual([(r["kind"], r["todo"]) for r in self.pending()], [("card-request", 42)])
        fetched = [c[2] for c in self.stub.calls if c[:2] == ["api", "get"] and "/card_tables/cards/" in c[2]]
        self.assertNotIn(f"/buckets/{PROJECT}/card_tables/cards/41.json", fetched)
        self.assertNotIn("request-41", self.todos())

    def test_assigned_todos_alone_leaves_cards_alone(self):
        self.cfg(assigned_todos={}, assigned_cards=False)
        self.add_card(card(42))
        self.add(todo(50))
        self.poll()
        self.assertEqual([r["kind"] for r in self.pending()], ["todo-request"])

    def test_malformed_config_refused(self):
        for bad in ([], {"limit": 0}, {"scope": "everywhere"}):
            self.cfg(assigned_cards=bad)
            with self.assertRaises(ValueError):
                self.sync()


class Notifications(CardBase):
    def setUp(self):
        super().setUp()
        self.cfg(inbox={})
        self.poll()
        self.stub.calls.clear()

    def notify(self, *items):
        self.stub.readings = {"unreads": list(items), "reads": []}
        self.sync().behaviors["notifications"].run()

    def test_an_event_assigning_a_card_is_a_request_in_the_same_run_not_unmonitored(self):
        self.add_card(card(42))
        self.notify(note(10, "Event", thread=42, path="cards", title="Address security issues"))
        self.assertEqual([r["kind"] for r in self.pending()], ["card-request"])
        self.assertEqual([n[0] for n in self.stub.notes], ["basecamp-card-request-42-1"])
        self.assertIn("Basecamp card assigned to you by the captain", self.stub.notes[0][1])
        self.assertEqual(self.stub.marked, [["10"]])

    def test_comments_and_completion_follow_it(self):
        self.add_card(card(42))
        self.notify(note(10, "Event", thread=42, path="cards"))
        self.stub.comments["42"] = [comment(5)]
        self.notify(note(10, "Event", thread=42, path="cards"), note(11, thread=42, anchor=5, path="cards"))
        self.stub.cards["42"]["completed"] = True
        self.notify(note(10, "Event", thread=42, path="cards"), note(11, thread=42, anchor=5, path="cards"),
                    note(12, "Event", thread=42, path="cards"))
        self.assertEqual([r["kind"] for r in self.pending()], ["card-request", "todo-comment", "card-request-closed"])

    def test_an_event_on_a_card_not_assigned_to_the_agent_stays_unmonitored(self):
        self.add_card(card(42, to=(CAPTAIN,)))
        self.notify(note(10, "Event", thread=42, path="cards"))
        self.assertEqual([(r["kind"], r["event_type"]) for r in self.pending()], [("unmonitored", "Event")])

    def test_off_an_event_is_unmonitored(self):
        self.cfg(assigned_cards=False)
        self.add_card(card(42))
        self.notify(note(10, "Event", thread=42, path="cards"))
        self.assertEqual([(r["kind"], r["event_type"]) for r in self.pending()], [("unmonitored", "Event")])


class Note(unittest.TestCase):
    def test_card_request_notes(self):
        rec = {"kind": "card-request", "key": "request-42", "todo": 42, "n": 1, "title": "Fix", "captain": True,
               "role": "captain", "author": {"id": CAPTAIN}}
        rid, body = behaviors.inbox_note(rec, 1, PROJECT)
        self.assertEqual(rid, "basecamp-card-request-42-1")
        self.assertIn("Basecamp card assigned to you by the captain, a request: 'Fix'", body)
        self.assertIn("moves the card to its table's Done column", body)
        rid, body = behaviors.inbox_note(dict(rec, kind="card-request-closed", reason="completed"), 1, PROJECT)
        self.assertIn("Basecamp card request 'Fix' (request-42) completed", body)


if __name__ == "__main__":
    unittest.main()
