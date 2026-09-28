"""Unit tests for sync.py. The basecamp and tasks-axi CLIs are stubbed; nothing touches the network."""
import json, os, shutil, sys, tempfile, unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import sync  # noqa: E402

CAPTAIN = 33333333
ACTING = 53286738  # the dedicated firstmate user the CLI profile signs in as


class Stub:
    """Records every CLI call and answers like the basecamp CLI would."""

    def __init__(self, comments=None, boosts=None):
        self.profiles = []
        self.calls, self.next_id, self.comments, self.boosts = [], 500, comments or {}, boosts or {}
        self.lavish, self.lavish_calls = "", 0  # stdout of plain `lavish-axi`, or an exception to raise

    def __call__(self, cmd, **kw):
        if cmd[0] == "lavish-axi":
            self.lavish_calls += 1
            if isinstance(self.lavish, Exception):
                raise self.lavish
            return SimpleNamespace(stdout=self.lavish, stderr="", returncode=0)
        if cmd[0] != "basecamp":
            raise AssertionError(f"unexpected command {cmd}")
        args = cmd[3:-3]
        if args[:1] == ["-P"]:
            self.profiles.append(args[1])
            args = args[2:]
        else:
            self.profiles.append(None)
        self.calls.append(args)
        data = None
        if args[:2] == ["cards", "create"]:
            self.next_id += 1
            data = {"id": self.next_id}
        elif args[:2] == ["comments", "list"]:
            data = self.comments.get(args[2], [])
        elif args[:2] == ["api", "get"]:
            data = self.boosts.get(args[2].split("/")[4], [])
        return SimpleNamespace(stdout=json.dumps({"ok": True, "data": data}), stderr="", returncode=0)

    def verbs(self):
        return [a[1] if a[0] == "cards" else a[0] for a in self.calls if a[:2] not in (["comments", "list"], ["api", "get"])]


def item(id, section="Queued", repo="srv", hold=None, hold_kind=None, until=None, title="A task"):
    return {"id": id, "section": section, "title": title, "links": [], "repo": repo, "kind": "ship",
            "hold": hold, "hold_kind": hold_kind, "until": until, "blocked_by": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "state"))
        self.cfgdir = os.path.join(self.tmp, "cfg")
        os.makedirs(self.cfgdir)
        shutil.copy(os.path.join(ROOT, "examples", "config.example.json"), os.path.join(self.cfgdir, "config.json"))
        cfg = json.load(open(os.path.join(self.cfgdir, "config.json")))
        cfg["repos"] = {"srv": "server", "eng": "engine"}
        json.dump(cfg, open(os.path.join(self.cfgdir, "config.json"), "w"))
        self.stub = Stub()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def sync(self, dry=False):
        return sync.Sync(self.home, os.path.join(self.cfgdir, "config.json"), dry=dry, runner=self.stub)

    def side(self, name, value):
        json.dump(value, open(os.path.join(self.cfgdir, name), "w"))

    def cards(self):
        return json.load(open(os.path.join(self.cfgdir, "map.json")))

    def pr(self, task):
        open(os.path.join(self.home, "state", f"{task}.meta"), "w").write("pr=https://github.com/o/r/pull/1\n")


class ColumnRules(Base):
    def col(self, it, notnow=None):
        return self.sync().column_for(it, notnow or {})

    def test_queued_is_triage(self):
        self.assertEqual(self.col(item("a")), ("Triage", False))

    def test_in_flight_without_pr_is_in_progress(self):
        self.assertEqual(self.col(item("a", "In flight")), ("In progress", False))

    def test_in_flight_with_pr_is_ready_for_qa(self):
        self.pr("a")
        self.assertEqual(self.col(item("a", "In flight")), ("Ready for QA", False))

    def test_done(self):
        self.assertEqual(self.col(item("a", "Done")), ("Done", False))
        self.assertEqual(self.col(item("a", "Done"), {"a": "x"}), ("Done", False))

    def test_captain_hold_is_figuring_and_assigned(self):
        self.assertEqual(self.col(item("a", hold="pick", hold_kind="captain")), ("Figuring it out", True))

    def test_captain_hold_with_until_is_not_now(self):
        self.assertEqual(self.col(item("a", hold="pick", hold_kind="captain", until="2026-10-01")), ("Not now", False))

    def test_parked_is_not_now(self):
        self.assertEqual(self.col(item("a", hold="x", hold_kind="parked")), ("Not now", False))
        self.assertEqual(self.col(item("a", hold="parked by ruling")), ("Not now", False))

    def test_not_now_file_unless_captain_hold(self):
        self.assertEqual(self.col(item("a"), {"a": "later"}), ("Not now", False))
        self.assertEqual(self.col(item("a", hold="q", hold_kind="captain"), {"a": "later"}), ("Figuring it out", True))

    def test_figuring_file_moves_triage(self):
        self.side("figuring.json", {"a": "needs a plan"})
        self.sync().main([item("a")])
        self.assertEqual(self.cards()["a|server"]["column"], "Figuring it out")
        self.assertIn("needs a plan", self.stub.calls[0][3])


class Assignment(Base):
    def test_create_assigns_captain_when_waiting(self):
        self.sync().main([item("a", hold="pick", hold_kind="captain")])
        create = self.stub.calls[0]
        self.assertEqual(create[:2], ["cards", "create"])
        self.assertEqual(create[create.index("--assignee") + 1], str(CAPTAIN))
        self.assertTrue(self.cards()["a|server"]["assigned"])

    def test_unassign_after_decision(self):
        self.sync().main([item("a", hold="pick", hold_kind="captain")])
        self.stub.calls.clear()
        self.sync().main([item("a")])
        self.assertIn(["unassign", "501", "--card", "--from", str(CAPTAIN)], self.stub.calls)
        rec = self.cards()["a|server"]
        self.assertFalse(rec["assigned"])
        self.assertEqual(rec["column"], "Triage")

    def test_assign_existing_card(self):
        self.sync().main([item("a")])
        self.stub.calls.clear()
        self.sync().main([item("a", hold="pick", hold_kind="captain")])
        assign = [c for c in self.stub.calls if "--assignee" in c]
        self.assertEqual(len(assign), 1)
        self.assertEqual(assign[0][:3], ["cards", "update", "501"])


class IdMap(Base):
    def test_create_then_rerun_is_noop(self):
        self.sync().main([item("a"), item("b", repo="eng")])
        self.assertEqual(self.stub.verbs(), ["create", "create"])
        self.assertEqual(set(self.cards()), {"a|server", "b|engine"})
        self.stub.calls.clear()
        self.sync().main([item("a"), item("b", repo="eng")])
        self.assertEqual(self.stub.verbs(), [])

    def test_changed_title_updates_same_card(self):
        self.sync().main([item("a")])
        self.stub.calls.clear()
        self.sync().main([item("a", title="Renamed")])
        self.assertEqual(self.stub.verbs(), ["update"])
        self.assertEqual(self.stub.calls[0][2], "501")
        self.assertEqual(len(self.cards()), 1)

    def test_column_change_moves_card(self):
        self.sync().main([item("a")])
        self.stub.calls.clear()
        self.sync().main([item("a", "Done")])
        self.assertIn(["cards", "move", "501", "--card-table", "100", "--to", "106"], self.stub.calls)

    def test_extra_repos_make_one_card_per_board(self):
        self.side("extra-repos.json", {"a": ["engine"]})
        self.sync().main([item("a")])
        self.assertEqual(set(self.cards()), {"a|server", "a|engine"})

    def test_skip_and_unplaced(self):
        self.side("skip.json", ["a"])
        self.sync().main([item("a"), item("b", repo="elsewhere")])
        self.assertEqual(self.stub.calls, [])

    def test_dry_run_makes_no_calls_and_keeps_map(self):
        plan = self.sync(dry=True).main([item("a")])
        self.assertEqual(plan["create"], 1)
        self.assertEqual(self.stub.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.cfgdir, "map.json")))
        self.sync().main([item("a")])
        self.stub.calls.clear()
        plan = self.sync(dry=True).main([item("a", "Done")])
        self.assertEqual((plan["create"], plan["move"]), (0, 1))
        self.assertEqual(self.stub.calls, [])

    def test_never_destructive_or_posting(self):
        self.sync().main([item("a", hold="q", hold_kind="captain")])
        self.sync().main([item("a", "Done")])
        for c in self.stub.calls:
            self.assertNotIn(c[0], ("chat", "messages", "trash", "archive"))
            self.assertFalse(c[0] == "comments" and c[1] != "list", c)
            self.assertFalse(c[0] == "cards" and c[1] not in ("create", "update", "move"), c)


class Comments(Base):
    def test_captain_comments_go_to_pending_once(self):
        self.stub.comments = {"501": [{"id": 9, "creator": {"id": CAPTAIN}, "content": "<p>yes</p>", "created_at": "t"},
                                      {"id": 10, "creator": {"id": 1}, "content": "other"}]}
        self.sync().main([item("a")])
        self.sync().main([item("a")])
        lines = open(os.path.join(self.cfgdir, "pending-comments.jsonl")).read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "yes")
        self.assertEqual(self.cards()["a|server"]["comments"], [9, 10])
        self.assertEqual(json.loads(lines[0])["kind"], "comment")


def boost(id, who=CAPTAIN, content="👍"):
    return {"id": id, "booster": {"id": who}, "content": content, "created_at": "t"}


class Boosts(Base):
    WAIT = dict(hold="pick", hold_kind="captain")

    def pending(self):
        p = os.path.join(self.cfgdir, "pending-comments.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def boost_calls(self):
        return [c for c in self.stub.calls if c[:2] == ["api", "get"]]

    def test_captain_thumbs_up_emitted_once(self):
        self.stub.boosts = {"501": [boost(7), boost(8, content="👍🏽")]}
        self.sync().main([item("a", **self.WAIT)])
        self.sync().main([item("a", **self.WAIT)])
        recs = self.pending()
        self.assertEqual([r["boost"] for r in recs], [7, 8])
        self.assertEqual(recs[0]["kind"], "approval")
        self.assertEqual((recs[0]["task"], recs[0]["repo"], recs[0]["card"], recs[0]["at"]), ("a", "server", 501, "t"))
        self.assertTrue(recs[0]["url"].endswith("/card_tables/cards/501"))
        self.assertNotIn("unassign", self.stub.verbs())
        self.assertTrue(self.cards()["a|server"]["assigned"])

    def test_others_and_other_emoji_ignored(self):
        self.stub.boosts = {"501": [boost(7, who=1), boost(8, content="🎉"), boost(9, content="👍👍")]}
        self.sync().main([item("a", **self.WAIT)])
        self.assertEqual(self.pending(), [])

    def test_unassigned_card_never_queried(self):
        self.stub.boosts = {"501": [boost(7)]}
        self.sync().main([item("a")])
        self.sync().main([item("a")])
        self.sync(dry=True).main([item("a")])
        self.assertEqual(self.boost_calls(), [])
        self.assertEqual(self.pending(), [])

    def test_dry_run_reads_but_records_nothing(self):
        self.sync().main([item("a", **self.WAIT)])
        self.stub.boosts = {"501": [boost(7)]}
        self.sync(dry=True).main([item("a", **self.WAIT)])
        self.assertEqual(len(self.boost_calls()), 2)
        self.assertEqual(self.pending(), [])
        self.assertNotIn("boosts", self.cards()["a|server"])


class Profile(Base):
    WAIT = dict(hold="pick", hold_kind="captain")

    def set_profile(self, name):
        p = os.path.join(self.cfgdir, "config.json")
        cfg = json.load(open(p))
        cfg["profile"] = name
        json.dump(cfg, open(p, "w"))

    def run_all_kinds(self):
        self.stub.comments = {"501": []}
        self.sync().main([item("a", **self.WAIT)])
        self.sync().main([item("a")])
        kinds = {tuple(c[:2]) for c in self.stub.calls}
        self.assertTrue({("cards", "create"), ("comments", "list"), ("api", "get"), ("unassign", "501")} <= kinds, kinds)

    def test_profile_passed_on_every_call(self):
        self.set_profile("firstmate")
        self.run_all_kinds()
        self.assertEqual(set(self.stub.profiles), {"firstmate"})

    def test_no_profile_flag_when_unset(self):
        self.run_all_kinds()
        self.assertEqual(set(self.stub.profiles), {None})

    def test_acting_user_comments_and_boosts_ignored(self):
        self.set_profile("firstmate")
        self.stub.comments = {"501": [{"id": 9, "creator": {"id": ACTING}, "content": "<p>mine</p>", "created_at": "t"}]}
        self.stub.boosts = {"501": [boost(7, who=ACTING)]}
        self.sync().main([item("a", **self.WAIT)])
        self.assertFalse(os.path.exists(os.path.join(self.cfgdir, "pending-comments.jsonl")))
        self.assertIn(["cards", "create"], [c[:2] for c in self.stub.calls])
        create = next(c for c in self.stub.calls if c[:2] == ["cards", "create"])
        self.assertEqual(create[create.index("--assignee") + 1], str(CAPTAIN))


class NoteHtml(Base):
    def test_blocks_have_no_raw_newlines(self):
        self.pr("a")
        it = dict(item("a", "In flight", hold="line one\nline two"), blocked_by=["b"], links=["https://github.com/o/r/pull/2"])
        body = self.sync().body_for(it, "Ready for QA", False, {})
        self.assertNotIn("\n", body.replace("line one\nline two", ""))
        self.assertTrue(body.startswith("<div>") and body.endswith("</div>"))
        self.assertIn("<ul><li><a href=\"https://github.com/o/r/pull/2\">", body)
        self.assertIn("<li><a href=\"https://github.com/o/r/pull/1\">", body)

    def test_decision_renders_numbered_list(self):
        self.side("decisions.json", {"a": {"question": "Pick <one>", "items": ["first", "second & more"], "note": "rec first"}})
        self.sync().main([item("a", hold="raw hold", hold_kind="captain")])
        body = self.stub.calls[0][3]
        self.assertIn("<div><strong>Waiting on you</strong>: Pick &lt;one&gt;</div>"
                      "<ol><li>first</li><li>second &amp; more</li></ol><div>rec first</div>", body)
        self.assertNotIn("raw hold", body)
        self.assertNotIn("\n", body)

    def test_decision_ignored_when_not_waiting(self):
        body = self.sync().body_for(item("a"), "Triage", False, {}, {"a": {"question": "Q", "items": ["x"]}})
        self.assertNotIn("<ol>", body)


URL = "http://omarchy.tailcdcf4d.ts.net:4387/session/"


class Boards(Base):
    def sessions(self, *rows):
        head = "bin: ~/lavish\ndescription: x\nsessions[%d]{file,status,url,pending_prompts,listener}:\n" % len(rows)
        body = "".join(f'  {f},{st},"{u}",0,none\n' for f, st, u in rows)
        self.stub.lavish = head + body + "visual_guidance[1]: x\n"

    def board(self, task, name="review/index.html"):
        return os.path.join(self.home, "data", task, name)

    def body(self):
        return [c for c in self.stub.calls if c[:2] in (["cards", "create"], ["cards", "update"])][-1]

    def test_open_board_lands_on_card(self):
        self.sessions((self.board("a"), "open", URL + "aa"), (self.board("a", "b.html"), "open", URL + "ab"),
                      (self.board("other"), "open", URL + "zz"))
        self.sync().main([item("a")])
        note = self.stub.calls[0][3]
        self.assertIn(f'<strong>Plan board</strong>: <a href="{URL}aa">{URL}aa</a>, <a href="{URL}ab">', note)
        self.assertNotIn(URL + "zz", note)
        self.assertEqual(self.stub.lavish_calls, 1)
        self.assertLess(note.index("Plan board"), note.index("Kept in sync"))

    def test_closed_or_ended_session_is_skipped_and_link_drops(self):
        self.sessions((self.board("a"), "open", URL + "aa"))
        self.sync().main([item("a")])
        self.sessions((self.board("a"), "ended", URL + "aa"), (self.board("a", "x.html"), "feedback", URL + "ax"))
        self.sync().main([item("a")])
        self.assertEqual(self.body()[:2], ["cards", "update"])
        self.assertNotIn("Plan board", self.body()[-1])

    def test_boards_json_maps_scout_boards(self):
        self.side("boards.json", {"a": ["a-scout"]})
        self.sessions((self.board("a-scout"), "open", URL + "sc"))
        self.sync().main([item("a"), item("b")])
        notes = [c[3] for c in self.stub.calls if c[:2] == ["cards", "create"]]
        self.assertIn(URL + "sc", notes[0])
        self.assertNotIn(URL + "sc", notes[1])

    def test_lavish_failure_keeps_links_and_run_succeeds(self):
        self.sessions((self.board("a"), "open", URL + "aa"))
        self.sync().main([item("a")])
        n = len(self.stub.calls)
        for err in (FileNotFoundError("lavish-axi"), sync.subprocess.TimeoutExpired("lavish-axi", 15)):
            self.stub.lavish = err
            self.sync().main([item("a")])
        self.assertEqual([c for c in self.stub.calls[n:] if c[0] == "cards"], [])
        self.assertIn(URL + "aa", self.cards()["a|server"]["boards"])
        self.assertIn("lavish-axi unavailable", open(os.path.join(self.cfgdir, "sync.log")).read())

    def test_unchanged_link_makes_no_update(self):
        self.sessions((self.board("a"), "open", URL + "aa"))
        self.sync().main([item("a")])
        n = len(self.stub.calls)
        self.sync().main([item("a")])
        self.assertEqual([c for c in self.stub.calls[n:] if c[0] == "cards"], [])


class TasksAxi(unittest.TestCase):
    def test_show_parsing_and_mapping(self):
        text = '''task:
  id: ta-x
  title: "Fix it https://github.com/o/r/pull/7 (repo: srv) (kind: chore)"
  state: in_flight
  hold_reason: "Pick one"
  hold_kind: captain
  hold_until: "-"
  kind: ship
  repo: srv
  deps: "blocked-by:ta-a,blocked-by:ta-b"
  links: "pr:https://github.com/o/r/pull/7,pr:https://github.com/o/r/pull/8"
'''
        it = sync.task_item(sync.parse_show(text))
        self.assertEqual(it["title"], "Fix it")
        self.assertEqual(it["section"], "In flight")
        self.assertEqual(it["links"], ["https://github.com/o/r/pull/7", "https://github.com/o/r/pull/8"])
        self.assertEqual(it["blocked_by"], ["ta-a", "ta-b"])
        self.assertEqual((it["hold"], it["hold_kind"], it["until"]), ("Pick one", "captain", None))


if __name__ == "__main__":
    unittest.main()
