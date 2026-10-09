"""Tests for trust modes: operators, whose word authorizes the agent as the captain's does, and participants (anyone on
the project, or with an email at a domain), who may ask but whose word authorizes nothing.

The basecamp CLI is stubbed; nothing touches the network.
"""
import json, os, sys, unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sync import ACTING, CAPTAIN, Base, item, line  # noqa: E402
from test_sync import boost as card_boost  # noqa: E402
from test_tools import boost, comment, received  # noqa: E402
from test_notifications import NotifBase, NotifStub, PROJECT, note  # noqa: E402
from test_assigned_todos import AssignBase, AssignStub, todo  # noqa: E402
from test_pings import CHAT, PingBase, PingStub, ping_line, reading  # noqa: E402
import test_init  # noqa: E402
import test_setup  # noqa: E402
import behaviors  # noqa: E402
import init_home  # noqa: E402

OP = 15151515  # an operator
MATE = 12121212  # someone in "people": a named participant
MEMBER = 16161616  # on the project, named nowhere
CLIENT = 17171717  # a client on the project
STRANGER = 13131313  # not on the project
MASKED = "p•••@•••.•••"  # an email address as Basecamp shows it to non-admins
PEOPLE = [{"id": CAPTAIN, "client": False}, {"id": ACTING, "client": False}, {"id": OP, "client": False},
          {"id": MEMBER, "name": "Mem", "client": False}, {"id": CLIENT, "client": True}]


def with_people(cls):
    """An instance of stub class `cls` that also answers the project's people read, counting the reads."""
    class People(cls):
        def __call__(self, cmd, **kw):
            args = cmd[3:-3]
            core = args[2:] if args[:1] == ["-P"] else args
            if cmd[0] == "basecamp" and core[:3] == ["api", "get", f"/projects/{PROJECT}/people.json"]:
                self.calls.append(core)
                if self.people_fail:
                    return SimpleNamespace(stdout=json.dumps({"ok": False, "error": "boom"}), stderr="", returncode=1)
                return SimpleNamespace(stdout=json.dumps({"ok": True, "data": self.people}), stderr="", returncode=0)
            return super().__call__(cmd, **kw)

        def people_reads(self):
            return self.calls.count(["api", "get", f"/projects/{PROJECT}/people.json"])
    stub = People()
    stub.people, stub.people_fail = list(PEOPLE), False
    return stub


def who(pid, email=None, name=None, client=None):
    return {"id": pid, "name": name, **({"email_address": email} if email else {}),
            **({"client": client} if client is not None else {})}


def note_of(rec):
    return behaviors.inbox_note(rec, "1", "2")[1]


class Config(Base):
    def cfg(self, **kw):
        p = os.path.join(self.cfgdir, "config.json")
        cfg = json.load(open(p))
        cfg.update(kw)
        json.dump(cfg, open(p, "w"))

    def test_operators_are_listened_to_and_never_include_the_captain(self):
        self.cfg(operators=[OP, CAPTAIN])
        s = self.sync()
        self.assertEqual((s.operators, s.people), ({OP}, {CAPTAIN, OP}))

    def test_malformed_trust_config_refused(self):
        for bad in ({"operators": OP}, {"participants": True}, {"participants": {"project": "yes"}},
                    {"participants": {"domains": "example.com"}}, {"participants": {"anyone": True}}):
            self.cfg(**bad)
            with self.assertRaises(ValueError, msg=bad):
                self.sync()
            self.cfg(operators=[], participants={})

    def test_roles(self):
        self.cfg(operators=[OP], people=[MATE], participants={"domains": ["@Partner.example"]})
        s = self.sync()
        self.assertEqual([s.role(who(p)) for p in (CAPTAIN, OP, MATE, STRANGER)], ["captain", "operator", "participant", None])
        self.assertEqual(s.role(who(STRANGER, "pat@partner.example")), "participant")
        self.assertIsNone(s.role(who(STRANGER, "pat@partner.example"), admitted=False))
        self.assertIsNone(s.role(who(STRANGER, "pat@partner.example", client=True)))
        self.assertIsNone(s.role(who(STRANGER, MASKED)))


class Operators(NotifBase):
    def setUp(self):
        super().setUp()
        self.cfg(operators=[OP], people=[MATE])

    def test_an_operators_line_is_relayed_as_theirs_with_authority(self):
        self.stub.lines["77"] += [line(2, who=OP, content="ship it?"), line(3, who=MATE, content="really?")]
        self.poll()
        op, mate = self.pending()
        self.assertEqual([(r["author"]["id"], r["role"], r["captain"]) for r in (op, mate)],
                         [(OP, "operator", False), (MATE, "participant", False)])
        self.assertIn("(an operator: their word counts as the captain's)", note_of(op))
        self.assertNotIn("never a captain decision", note_of(op))
        self.assertIn("(not the captain)", note_of(mate))
        self.assertIn("never a captain decision", note_of(mate))

    def test_an_operators_comment_on_a_decision_todo_is_the_decision(self):
        tid = self.create()
        self.stub.comments[str(tid)] = [comment(1, who=OP, content="<p>Merge it</p>")]
        self.poll()
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["role"]), ("todo-comment", "operator"))
        self.assertIn("a decision: act, then sync.py todo complete --todo ta-x", note_of(rec))

    def test_without_operators_the_same_person_is_not_heard(self):
        self.cfg(operators=[], people=[])
        self.stub.lines["77"].append(line(2, who=OP, content="ship it?"))
        self.poll()
        self.assertEqual(self.pending(), [])


class OperatorApproval(Base):
    WAIT = dict(hold="pick", hold_kind="captain")

    def test_an_operators_thumbs_up_on_an_assigned_card_is_an_approval(self):
        p = os.path.join(self.cfgdir, "config.json")
        cfg = json.load(open(p))
        json.dump(dict(cfg, operators=[OP], people=[MATE]), open(p, "w"))
        self.sync().main([item("a", **self.WAIT)])  # seeds the card's boosts
        self.stub.boosts = {"501": [card_boost(7, who=OP), card_boost(8, who=MATE)]}
        self.sync().main([item("a", **self.WAIT)])
        with open(os.path.join(self.cfgdir, "pending-comments.jsonl")) as f:
            recs = [json.loads(ln) for ln in f]
        self.assertEqual([(r["kind"], r["boost"], r["role"]) for r in recs],
                         [("approval", 7, "operator"), ("boost", 8, "participant")])
        self.assertIn("card approval (the 👍 of person 15151515 (an operator", note_of(recs[0]))


class OperatorRequest(AssignBase):
    def test_a_todo_an_operator_assigns_is_captain_work(self):
        self.cfg(operators=[OP])
        self.add(todo(42, by=OP))
        self.poll()
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["role"]), ("todo-request", "operator"))
        self.assertIn("captain work", note_of(rec))


class ProjectParticipants(NotifBase):
    def setUp(self):
        super().setUp()
        self.stub = with_people(NotifStub)
        self.stub.lines = {"77": [line(1)]}
        self.cfg(participants={"project": True})

    def test_a_members_line_is_a_participants_question(self):
        self.stub.lines["77"] += [line(2, who=MEMBER, content="when?"), line(3, who=CLIENT, content="and me?"),
                                  line(4, who=STRANGER, content="me?"), line(5, who=ACTING, content="mine?")]
        self.poll()
        [rec] = self.pending()
        self.assertEqual((rec["line"], rec["role"], rec["captain"]), (2, "participant", False))
        self.assertIn("(not the captain)", note_of(rec))
        self.assertIn("never a captain decision", note_of(rec))
        self.assertEqual(self.stub.people_reads(), 1)  # once a run, however many unknown authors

    def test_no_people_read_while_only_named_people_write(self):
        self.stub.lines["77"].append(line(2, content="captain asks?"))
        self.poll()
        self.assertEqual((len(self.pending()), self.stub.people_reads()), (1, 0))

    def test_a_members_boost_is_never_recorded(self):
        mine = dict(line(2, who=ACTING, content="Done."), boosts_count=0)
        self.stub.lines["77"].append(mine)
        self.poll()
        mine["boosts_count"] = 1
        self.stub.boosts["2"] = [dict(boost(60, who=MEMBER), created_at="2099-01-01T00:00:00Z")]
        self.stub.my_boosts = received(2, dict(boost(60, who=MEMBER), created_at="2099-01-01T00:00:00Z"))
        self.poll()
        self.assertEqual(self.pending(), [])

    def test_a_members_unmonitored_input_is_not_recorded(self):
        self.notify(note(10, "Chat", thread=78, path="chats", section="chats", who=MEMBER))
        self.poll()
        self.assertEqual(self.pending(), [])

    def test_a_failed_people_read_uses_the_last_list(self):
        self.stub.lines["77"].append(line(2, who=MEMBER, content="when?"))
        self.poll()
        self.stub.people_fail = True
        self.stub.lines["77"].append(line(3, who=MEMBER, content="and now?"))
        self.poll()
        self.assertEqual([r["line"] for r in self.pending()], [2, 3])
        self.assertIn("using the last list (4 people)", self.log())


class DomainParticipants(NotifBase):
    def setUp(self):
        super().setUp()
        self.cfg(participants={"domains": ["partner.example"]})

    def test_only_a_visible_email_at_the_domain_is_heard(self):
        self.stub.lines["77"] += [dict(line(2, content="when?"), creator=who(STRANGER, "pat@partner.example", "Pat")),
                                  dict(line(3, content="me?"), creator=who(STRANGER + 1, MASKED)),
                                  dict(line(4, content="and?"), creator=who(STRANGER + 2, "c@partner.example", client=True)),
                                  dict(line(5, content="else?"), creator=who(STRANGER + 3, "o@other.example"))]
        self.poll()
        self.assertEqual([(r["line"], r["role"]) for r in self.pending()], [(2, "participant")])


class ParticipantRequests(AssignBase):
    def test_a_todo_a_member_assigns_is_not_a_request(self):
        self.stub = with_people(AssignStub)
        self.stub.lines = {"77": []}
        self.cfg(participants={"project": True})
        self.add(todo(42, by=MEMBER))
        self.poll()
        self.assertEqual(self.pending(), [])
        self.assertNotIn("request-42", self.todos())


class ParticipantPings(PingBase):
    def test_a_members_ping_is_relayed_as_a_participants(self):
        self.stub = with_people(PingStub)
        self.stub.lines = {"77": [], str(CHAT): []}
        self.stub.readings["unreads"] = [reading(people=(MEMBER, ACTING))]
        self.cfg(participants={"project": True})
        self.poll()  # starts the Ping's cursor
        self.say(ping_line(2, who=MEMBER))
        self.poll()
        [rec] = self.pending()
        self.assertEqual((rec["kind"], rec["role"]), ("ping", "participant"))


class Init(unittest.TestCase):
    init, tearDown = test_init.InitTest.init, test_init.InitTest.tearDown

    def setUp(self):
        test_init.InitTest.setUp(self)
        self.stub.people += [{"id": OP, "name": "Op", "email_address": "op@example.com"},
                             {"id": MEMBER, "name": "Mem", "email_address": MASKED}]

    def test_operators_and_participants_written(self):
        cfg = self.init(operators=["op@example.com", str(CAPTAIN)], participants_project=True,
                        participants_domains=["@Partner.example"]).discover()[0]
        self.assertEqual((cfg["operators"], cfg["participants"]),
                         ([OP], {"project": True, "domains": ["partner.example"]}))

    def test_without_the_flags_the_config_is_unchanged(self):
        cfg = self.init().discover()[0]
        self.assertNotIn("operators", cfg)
        self.assertNotIn("participants", cfg)

    def test_a_masked_email_explains_itself_and_the_login_is_refused(self):
        for arg, why in (("mem@partner.example", "hides other people's email addresses"),
                         (str(test_init.ACTING), "is the login firstmate itself")):
            with self.assertRaises(init_home.Refuse) as e:
                self.init(operators=[arg]).discover()
            self.assertIn(why, str(e.exception))
            self.assertIn("--operator", str(e.exception))

    def test_a_bad_domain_refused(self):
        with self.assertRaises(init_home.Refuse):
            self.init(participants_domains=["not a domain"])

    def test_cli_flags(self):
        seen = {}
        real = init_home.Init.__init__

        def spy(s, *a, **kw):
            seen.update(kw)
            raise init_home.Refuse("stop")
        init_home.Init.__init__ = spy
        try:
            init_home.cli(["https://app.basecamp.com/1/projects/2", "--login", "f", "--home", self.home, "--operator", "a",
                           "--operator", "b", "--participants-project", "--participants-domain", "x.example"])
        finally:
            init_home.Init.__init__ = real
        self.assertEqual((seen["operators"], seen["participants_project"], seen["participants_domains"]),
                         (["a", "b"], True, ["x.example"]))


class Doctor(test_setup.Base):
    doctor, healthy = test_setup.DoctorTest.doctor, test_setup.DoctorTest.healthy

    def domains(self):
        with open(self.config) as f:
            cfg = json.load(f)
        cfg["participants"] = {"domains": ["example.com"]}
        with open(self.config, "w") as f:
            json.dump(cfg, f)

    def test_domains_with_visible_emails_pass(self):
        self.healthy()
        self.domains()
        self.assertEqual(self.doctor(), 0, self.text())
        self.assertIn("the participants' email domains can be matched", self.text())

    def test_domains_with_masked_emails_admit_nobody(self):
        self.healthy()
        self.domains()
        self.stub.people[0]["email_address"] = MASKED
        self.assertEqual(self.doctor(), 1)
        self.assertIn("The participants' email domains admit nobody", self.text())
        self.assertIn('"participants": {"project": true}', self.text())


if __name__ == "__main__":
    unittest.main()
