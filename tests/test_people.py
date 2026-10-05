"""Tests for the people list: relaying several listened-to people while only the captain decides or approves.

The basecamp CLI and the notifications are stubbed; nothing touches the network.
"""
import io, json, os, sys, unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sync import ACTING, CAPTAIN, Base, Stub, item, line  # noqa: E402
from test_sync import boost as card_boost  # noqa: E402
from test_tools import boost, comment  # noqa: E402
from test_notifications import NotifBase as ListenBase, PROJECT, note  # noqa: E402
from test_pings import PingBase, ping_line, reading, CHAT  # noqa: E402
import test_init  # noqa: E402
import behaviors  # noqa: E402
import init_home  # noqa: E402
import sync  # noqa: E402

MATE = 12121212  # a listened-to person who is not the captain
STRANGER = 13131313  # someone on the project nobody listens to


def named(person_id):
    return {"id": person_id, "name": {CAPTAIN: "Cap", MATE: "Mate"}.get(person_id)}


class Config(Base):
    def cfg(self, **kw):
        p = os.path.join(self.cfgdir, "config.json")
        cfg = json.load(open(p))
        cfg.update(kw)
        json.dump(cfg, open(p, "w"))

    def test_captain_alone_listens_to_just_the_captain(self):
        self.assertEqual(self.sync().people, {CAPTAIN})

    def test_people_always_include_the_captain(self):
        self.cfg(people=[MATE])
        self.assertEqual(self.sync().people, {CAPTAIN, MATE})

    def test_people_must_be_a_list(self):
        self.cfg(people=MATE)
        with self.assertRaises(ValueError):
            self.sync()


class Relay(ListenBase):
    """Chat, to-do, message and notification relaying with a people list."""

    def setUp(self):
        super().setUp()
        self.cfg(people=[CAPTAIN, MATE])

    def test_every_listed_person_relayed_with_who_wrote_it(self):
        self.stub.lines["77"] += [line(2, content="captain asks?"), line(3, who=MATE, content="mate asks?"),
                                  line(4, who=STRANGER, content="stranger asks?")]
        self.poll()
        recs = self.pending()
        self.assertEqual([(r["line"], r["author"]["id"], r["captain"]) for r in recs],
                         [(2, CAPTAIN, True), (3, MATE, False)])
        self.assertEqual(sorted(self.stub.posted()), [("2", "\U0001F440"), ("3", "\U0001F440")])

    def test_a_listed_agent_login_is_never_relayed(self):
        self.cfg(people=[MATE, ACTING])
        self.stub.lines["77"].append(line(2, who=ACTING, content="my own line?"))
        self.poll()
        self.assertEqual(self.pending(), [])

    def test_todo_comment_from_another_person_is_input_not_the_decision(self):
        tid = self.create()
        self.stub.comments[str(tid)] = [comment(1, who=MATE, content="<p>Merge it</p>")]
        self.poll()
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["author"]["id"], rec["captain"]), ("todo-comment", MATE, False))
        _, body = behaviors.inbox_note(rec, "1", "2")
        self.assertIn("(not the captain) on decision to-do ta-x", body)
        self.assertIn("never the decision itself", body)
        self.assertNotIn("todo complete", body)

    def test_todo_boost_from_another_person_is_never_a_decision(self):
        tid = self.create()
        self.stub.boosts[str(tid)] = []
        self.poll()  # seeds the to-do's boosts
        self.stub.boosts[str(tid)] = [boost(70, who=MATE, content="yes")]
        self.poll()
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["captain"]), ("boost", False))
        _, body = behaviors.inbox_note(rec, "1", "2")
        self.assertIn("never a captain decision or approval", body)
        self.assertNotIn("todo complete", body)

    def test_a_notification_from_another_listed_person_is_relayed_as_theirs(self):
        self.stub.lines["77"].append(line(2, who=MATE))
        self.notify(note(10, "Chat", thread=77, path="chats", section="chats", who=MATE))
        self.poll()
        self.assertEqual([(r["kind"], r["captain"]) for r in self.pending()], [("chat-question", False)])

    def test_a_mention_from_someone_not_listened_to_is_dropped(self):
        self.stub.comments["4"] = [comment(9, who=STRANGER)]
        self.notify(note(10, "Mention", thread=4, anchor=9, path="documents", who=STRANGER))
        self.poll()
        self.assertEqual(self.pending(), [])

    def test_unmonitored_input_from_another_person_names_them(self):
        self.notify(note(10, "Chat", thread=78, path="chats", section="chats", who=MATE))
        self.stub.readings["unreads"][0]["creator"] = named(MATE)
        self.poll()
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["author"], rec["captain"]), ("unmonitored", named(MATE), False))
        _, body = behaviors.inbox_note(rec, "1", "2")
        self.assertTrue(body.startswith("Basecamp activity from Mate (not the captain) that nothing monitors"))


class SingleCaptain(ListenBase):
    """A config with only "captain" relays as before: other people's lines only move cursors."""

    def test_other_people_ignored(self):
        self.stub.lines["77"] += [line(2, who=MATE), line(3)]
        self.notify(note(10, "Chat", thread=77, path="chats", section="chats"))
        self.poll()
        [rec] = self.pending()
        self.assertEqual((rec["line"], rec["captain"]), (3, True))
        _, body = behaviors.inbox_note(rec, "1", "2")
        self.assertTrue(body.startswith("Basecamp chat line from the captain:"))

    def test_a_record_from_before_the_people_list_reads_as_the_captain(self):
        _, body = behaviors.inbox_note({"kind": "todo-comment", "key": "k", "comment": 1, "text": "go"}, "1", "2")
        self.assertIn("from the captain on decision to-do k", body)
        self.assertIn("todo complete --todo k", body)


class CardApprovals(Base):
    """Only the captain's 👍 on an assigned card is an approval; another listed person's is a boost."""
    WAIT = dict(hold="pick", hold_kind="captain")

    def setUp(self):
        super().setUp()
        p = os.path.join(self.cfgdir, "config.json")
        cfg = json.load(open(p))
        cfg["people"] = [MATE]
        json.dump(cfg, open(p, "w"))

    def pending(self):
        p = os.path.join(self.cfgdir, "pending-comments.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def test_only_the_captain_approves(self):
        self.sync().main([item("a", **self.WAIT)])  # seeds the card's boosts
        self.stub.boosts = {"501": [card_boost(7, who=MATE), card_boost(8, who=STRANGER), card_boost(9)]}
        self.sync().main([item("a", **self.WAIT)])
        recs = self.pending()
        self.assertEqual([(r["kind"], r["boost"], r["captain"]) for r in recs],
                         [("boost", 7, False), ("approval", 9, True)])

    def test_card_comments_from_listed_people(self):
        self.stub.comments = {"501": [{"id": 5, "creator": named(MATE), "content": "why?", "created_at": "t"},
                                      {"id": 6, "creator": named(STRANGER), "content": "and?", "created_at": "t"}]}
        self.sync().main([item("a")])
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["comment"], rec["author"], rec["captain"]), ("question", 5, named(MATE), False))
        _, body = behaviors.inbox_note(rec, "1", "2")
        self.assertIn("card question from Mate (not the captain)", body)
        self.assertIn("never a captain decision", body)


class Pings(PingBase):
    def test_a_listed_persons_ping_is_relayed_as_theirs(self):
        self.cfg(people=[MATE])
        self.stub.readings["unreads"] = [reading(people=(MATE, ACTING))]
        self.poll()  # starts the Ping's cursor
        self.say(ping_line(2, who=MATE))
        self.poll()
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["line"], rec["captain"]), ("ping", 2, False))

    def test_without_the_list_a_ping_with_someone_else_is_not_read(self):
        self.stub.readings["unreads"] = [reading(people=(MATE, ACTING))]
        self.poll()
        self.say(ping_line(2, who=MATE))
        self.poll()
        self.assertEqual(self.pending(), [])


class Checkins(Base):
    def test_a_question_carries_who_wrote_it(self):
        _, body = behaviors.inbox_note({"kind": "checkin", "question": 5, "date": "2026-10-04", "title": "Run /stow",
                                        "author": named(MATE), "captain": False}, "1", "2")
        self.assertIn("asked by Mate (not the captain)", body)
        self.assertIn("not a captain decision", body)


class Init(unittest.TestCase):
    init, tearDown = test_init.InitTest.init, test_init.InitTest.tearDown

    def setUp(self):
        test_init.InitTest.setUp(self)
        self.stub.people += [{"id": MATE, "name": "Mate", "email_address": "mate@example.com"},
                             {"id": STRANGER, "name": "Twin"}, {"id": STRANGER + 1, "name": "Twin"}]

    def test_listen_to_by_id_email_or_name(self):
        cfg = self.init(listen_to=["mate@example.com"]).discover()[0]
        self.assertEqual(cfg["people"], [CAPTAIN, MATE])
        self.assertEqual(self.init(listen_to=[str(MATE), "mate", str(CAPTAIN)]).discover()[0]["people"], [CAPTAIN, MATE])

    def test_without_listen_to_there_is_no_people_list(self):
        self.assertNotIn("people", self.init().discover()[0])

    def test_unknown_ambiguous_or_the_login_refused(self):
        for arg, why in (("nobody@example.com", "matches no person"), ("twin", "several people"),
                         (str(ACTING), "is the login firstmate itself")):
            with self.assertRaises(init_home.Refuse) as e:
                self.init(listen_to=[arg]).discover()
            self.assertIn(why, str(e.exception))

    def test_cli_flag_is_repeatable(self):
        seen = {}
        real = init_home.Init.__init__

        def spy(s, *a, **kw):
            seen.update(kw)
            raise init_home.Refuse("stop")
        init_home.Init.__init__ = spy
        try:
            with redirect_stderr(io.StringIO()):  # the retired --listen beside it is still accepted, not --listen-to
                init_home.cli(["https://app.basecamp.com/1/projects/2", "--login", "f", "--home", self.home,
                               "--listen-to", "a", "--listen-to", "b", "--listen"])
        finally:
            init_home.Init.__init__ = real
        self.assertEqual(seen["listen_to"], ["a", "b"])
        self.assertNotIn("listen", seen)


if __name__ == "__main__":
    unittest.main()
