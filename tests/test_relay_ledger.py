"""Tests for the relay ledger (relay.jsonl): a line per item relayed to the agent and per item the relay filter skipped,
its outcome (answered with `reply` or a to-do command, dismissed with `sync.py dismiss`), and the `sync.py stats`
report over it, tokens included.

The example comments and transcripts are made up; every Basecamp call is stubbed.
"""
import contextlib, io, json, os, sys, tempfile, unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sync import ACTING, line  # noqa: E402
from test_tools import comment  # noqa: E402
from test_notifications import EYES, PROJECT, note  # noqa: E402
from test_relay_filter import Base, doc  # noqa: E402
import behaviors  # noqa: E402
import stats  # noqa: E402
import sync  # noqa: E402
import tools  # noqa: E402


class Ledger(Base):
    def ledger(self, event=None):
        evs = tools.read_ledger(os.path.join(self.cfgdir, "relay.jsonl"))
        return [e for e in evs if event is None or e["event"] == event]

    def ask_in_chat(self, lid=2, content="ship it?"):
        self.stub.lines["77"].append(line(lid, content=content))
        self.notify(note(10 + lid, "Chat", thread=77, path="chats", section="chats"))
        self.poll()


class Relayed(Ledger):
    def test_a_relayed_item_gets_a_line_with_what_rebuilds_it(self):
        self.ask_in_chat()
        [ev] = self.ledger("relayed")
        self.assertEqual((ev["kind"], ev["why"], ev["recording"], ev["text"], ev["item"]),
                         ("chat-question", "chat", 2, "ship it?", "basecamp-chat-question-2"))
        self.assertEqual(ev["item"], behaviors.inbox_note(self.pending()[0], "1", PROJECT)[0])  # the note's request id
        self.assertEqual(ev["agent"]["id"], ACTING)
        self.assertEqual(ev["agent"]["home"], self.home)
        self.assertIn("url", ev)
        self.assertEqual(ev["author"]["name"], self.pending()[0]["author"]["name"])

    def test_a_followed_threads_reason_is_its_why(self):
        self.stub.comments["4"] = [doc(9, content=f'<p><bc-attachment sgid="sgid-{ACTING}"></bc-attachment> look</p>')]
        self.notify(note(10, thread=4, anchor=9, path="documents"))
        self.poll()
        [ev] = self.ledger("relayed")
        self.assertEqual((ev["kind"], ev["why"], ev["recording"], ev["bucket"]), ("mention", "addressed", 9, int(PROJECT)))

    def test_a_dry_run_writes_no_line(self):
        self.stub.lines["77"].append(line(2, content="ship it?"))
        self.notify(note(12, "Chat", thread=77, path="chats", section="chats"))
        self.poll(dry=True)
        self.assertEqual(self.ledger(), [])

    def test_the_note_says_how_to_dismiss(self):
        self.ask_in_chat()
        body = behaviors.inbox_note(self.pending()[0], "1", PROJECT)[1]
        self.assertIn("Not for you: sync.py dismiss --recording 2 (takes the read boost off and records it).", body)
        rec = {"kind": "todo-request", "key": "k", "todo": 5, "title": "x"}
        self.assertNotIn("dismiss", behaviors.inbox_note(rec, "1", PROJECT)[1])  # assigned work is the agent's

    def test_the_ledger_is_never_backed_up(self):
        self.assertIsNone(behaviors.BACKED_UP.fullmatch("relay.jsonl"))
        self.assertIsNotNone(behaviors.BACKED_UP.fullmatch("pending-comments.jsonl"))


class Skipped(Ledger):
    def test_a_skipped_thread_comment_keeps_its_log_line_and_gets_a_ledger_line(self):
        self.stub.comments["4"] = [doc(9)]
        self.notify(note(10, thread=4, anchor=9, path="documents"))
        self.poll()
        self.assertEqual(self.log().count("skipped comment 9 by Cap on Document 4"), 1)
        [ev] = self.ledger("skipped")
        self.assertEqual((ev["kind"], ev["recording"], ev["reason"], ev["text"]),
                         ("thread-comment", 9, "no answer awaited", "status: the import ran"))
        self.assertEqual(self.ledger("relayed"), [])

    def test_a_dry_run_logs_the_skip_but_writes_no_line(self):
        self.stub.comments["4"] = [doc(9)]
        self.notify(note(10, thread=4, anchor=9, path="documents"))
        self.poll(dry=True)
        self.assertIn("dry skipped comment 9", self.log())
        self.assertEqual(self.ledger(), [])


class Outcomes(Ledger):
    def test_reply_records_the_item_answered(self):
        self.ask_in_chat()
        self.assertTrue(self.sync().reply(2, "Yes."))
        [ev] = self.ledger("answered")
        self.assertEqual((ev["recording"], ev["via"]), (2, "reply"))
        self.assertTrue(self.sync().reply(2, None, again=True, boost="👍"))
        self.assertEqual([e["via"] for e in self.ledger("answered")], ["reply", "reply --boost"])

    def test_dismiss_takes_the_eyes_off_and_records_it(self):
        self.ask_in_chat()
        self.assertEqual(self.stub.posted(), [("2", EYES)])
        self.assertTrue(self.sync().dismiss(2, "for Fitz"))
        self.assertEqual(len(self.stub.deletes()), 1)
        [ev] = self.ledger("dismissed")
        self.assertEqual((ev["recording"], ev["kind"], ev["reason"]), (2, "chat-question", "for Fitz"))
        self.assertIn("dismiss 2: removed", self.log())

    def test_dismiss_by_the_cli(self):
        self.ask_in_chat()
        cfg = os.path.join(self.cfgdir, "config.json")
        self.assertEqual(sync.cli(["dismiss", "--home", self.home, "--config", cfg, "--recording", "2"], runner=self.stub), 0)
        self.assertEqual(len(self.ledger("dismissed")), 1)

    def test_dismiss_refuses_an_id_nothing_relayed(self):
        with self.assertRaises(RuntimeError):
            self.sync().dismiss(424242)
        self.assertEqual(self.stub.deletes(), [])

    def test_dismiss_leaves_an_answered_item_alone(self):
        self.ask_in_chat()
        self.sync().reply(2, "Yes.")
        deletes = len(self.stub.deletes())
        self.assertFalse(self.sync().dismiss(2))
        self.assertEqual((len(self.stub.deletes()), self.ledger("dismissed")), (deletes, []))

    def test_a_dry_dismiss_changes_nothing(self):
        self.ask_in_chat()
        self.assertFalse(self.sync(dry=True).dismiss(2))
        self.assertEqual((self.stub.deletes(), self.ledger("dismissed")), ([], []))

    def test_dismiss_finds_an_item_relayed_before_the_ledger_began(self):
        self.ask_in_chat()
        os.remove(os.path.join(self.cfgdir, "relay.jsonl"))
        self.assertTrue(self.sync().dismiss(2))
        self.assertEqual(len(self.stub.deletes()), 1)

    def test_a_not_found_recording_is_still_dismissed(self):
        self.ask_in_chat()
        real = self.stub.__call__

        def gone(cmd, **kw):
            if "delete" in cmd:
                from types import SimpleNamespace
                return SimpleNamespace(stdout=json.dumps({"ok": False, "code": "not_found", "error": "Not found"}),
                                       stderr="", returncode=1)
            return real(cmd, **kw)
        s = sync.Sync(self.home, os.path.join(self.cfgdir, "config.json"), runner=gone)
        self.assertTrue(s.dismiss(2))
        self.assertEqual(len(self.ledger("dismissed")), 1)
        self.assertIn("dismiss 2: the recording was not found", self.log())


def write(path, evs):
    with open(path, "w") as f:
        for e in evs:
            f.write(json.dumps(e) + "\n")


EVENTS = [
    {"event": "relayed", "at": "2026-11-09T10:00:00Z", "kind": "mention", "why": "addressed", "recording": 1},
    {"event": "relayed", "at": "2026-11-09T11:00:00Z", "kind": "thread-comment", "why": "yours", "recording": 2},
    {"event": "relayed", "at": "2026-11-10T11:00:00Z", "kind": "thread-comment", "why": "yours", "recording": 3},
    {"event": "relayed", "at": "2026-11-16T09:00:00Z", "kind": "todo-request", "why": "assigned", "recording": 50, "key": "k1"},
    {"event": "relayed", "at": "2026-11-16T09:30:00Z", "kind": "ping", "why": "ping", "recording": 4},
    {"event": "skipped", "at": "2026-11-09T12:00:00Z", "kind": "thread-comment", "reason": "no answer awaited", "recording": 7},
    {"event": "skipped", "at": "2026-11-16T12:00:00Z", "kind": "thread-comment", "reason": "no answer awaited", "recording": 8},
    {"event": "answered", "at": "2026-11-09T10:05:00Z", "recording": 1, "via": "reply"},
    {"event": "dismissed", "at": "2026-11-09T11:05:00Z", "recording": 2, "reason": "for Fitz"},
    {"event": "dismissed", "at": "2026-11-10T11:05:00Z", "recording": 3},
    {"event": "answered", "at": "2026-11-16T10:00:00Z", "key": "k1", "via": "todo complete"},
]


class Report(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.t = type("T", (), {"home": self.dir, "path": lambda _, n: os.path.join(self.dir, n)})()
        write(os.path.join(self.dir, "relay.jsonl"), EVENTS)

    def test_counts_per_week_and_in_total(self):
        r = stats.report(self.t, tokens=False)
        self.assertEqual([(p["period"], p["relayed"], p["answered"], p["dismissed"], p["open"], p["skipped"])
                          for p in r["periods"]],
                         [("2026-11-09", 3, 1, 2, 0, 1), ("2026-11-16", 2, 1, 0, 1, 1)])
        tot = r["total"]
        self.assertEqual((tot["relayed"], tot["answered"], tot["dismissed"], tot["open"], tot["skipped"]), (5, 2, 2, 1, 2))
        self.assertEqual((tot["dismissed_share"], tot["dismissed_of_relayed"]), (0.5, 0.4))

    def test_the_top_noise_source_comes_first(self):
        r = stats.report(self.t, tokens=False)
        self.assertEqual((r["by"][0]["kind"], r["by"][0]["why"], r["by"][0]["dismissed"]), ("thread-comment", "yours", 2))
        self.assertEqual(r["skipped_by"], [{"kind": "thread-comment", "reason": "no answer awaited", "count": 2}])

    def test_since_and_until_bound_the_days(self):
        r = stats.report(self.t, since=date(2026, 11, 10), until=date(2026, 11, 16), period="all", tokens=False)
        self.assertEqual([(p["period"], p["relayed"], p["skipped"]) for p in r["periods"]], [("all", 3, 1)])

    def test_answered_wins_over_dismissed_and_a_key_answers_only_what_came_before(self):
        evs = [{"event": "relayed", "at": "2026-11-09T10:00:00Z", "kind": "ping", "recording": 1},
               {"event": "dismissed", "at": "2026-11-09T10:01:00Z", "recording": 1},
               {"event": "answered", "at": "2026-11-09T10:02:00Z", "recording": 1, "via": "reply"},
               {"event": "answered", "at": "2026-11-09T10:03:00Z", "key": "k", "via": "todo comment"},
               {"event": "relayed", "at": "2026-11-09T10:04:00Z", "kind": "todo-request-update", "recording": 9, "key": "k"}]
        items, _ = stats.fold(evs)
        self.assertEqual([i["outcome"] for i in items], ["answered", "open"])

    def test_the_text_report_and_the_cli(self):
        text = stats.render(stats.report(self.t, tokens=False))
        self.assertIn("thread-comment/yours: 2 relayed, 0 answered, 2 dismissed, 0 open (noise 100%)", text)
        self.assertIn("total", text)


def turn(kind, at, **kw):
    return json.dumps({"type": kind, "timestamp": at, **kw})


def assistant(mid, at, tokens, command=None):
    content = [{"type": "tool_use", "id": f"tu-{mid}", "name": "Bash", "input": {"command": command}}] if command else \
        [{"type": "text", "text": "thinking it over"}]
    usage = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": tokens - 10, "output_tokens": 10}
    return turn("assistant", at, message={"id": mid, "content": content, "usage": usage})


def shown(at, text):
    return turn("user", at, message={"content": [{"type": "tool_result", "tool_use_id": "x", "content": text}]})


class Tokens(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.t = type("T", (), {"home": self.dir, "path": lambda _, n: os.path.join(self.dir, n)})()
        self.tx = os.path.join(self.dir, "transcripts")
        os.makedirs(os.path.join(self.tx, "s1", "subagents"))
        write(os.path.join(self.dir, "relay.jsonl"), [
            {"event": "relayed", "at": "2026-11-09T10:00:00Z", "kind": "mention", "why": "addressed", "recording": 11},
            {"event": "relayed", "at": "2026-11-09T10:00:00Z", "kind": "thread-comment", "why": "yours", "recording": 12},
            {"event": "relayed", "at": "2026-11-09T10:00:00Z", "kind": "ping", "why": "ping", "recording": 13},
            {"event": "answered", "at": "2026-11-09T10:10:00Z", "recording": 11, "via": "reply"},
            {"event": "dismissed", "at": "2026-11-09T10:10:00Z", "recording": 12},
            {"event": "dismissed", "at": "2026-11-09T10:20:00Z", "recording": 13}])
        lines = [
            assistant("m0", "2026-11-09T09:00:00Z", 1000),  # before any note: in the session total only
            shown("2026-11-09T10:01:00Z", "Commands: sync.py reply --recording 11.\nNot for you: sync.py dismiss --recording 11\n"
                                          "Commands: sync.py reply --recording 12.\n"),
            assistant("m1", "2026-11-09T10:02:00Z", 200),  # shared by 11 and 12
            assistant("m1", "2026-11-09T10:02:00Z", 200),  # the same message on a second line: counted once
            assistant("m2", "2026-11-09T10:03:00Z", 100, "python3 sync.py dismiss --home h --config c --recording 12"),
            assistant("m3", "2026-11-09T10:04:00Z", 300, "python3 sync.py reply --home h --config c --recording 11 --body-file f"),
            # 13 is shown but answered in no transcript: not attributed
            shown("2026-11-09T10:05:00Z", "Commands: sync.py reply --recording 13 (answers in the Ping)."),
            assistant("m4", "2026-11-09T10:06:00Z", 50),
        ]
        with open(os.path.join(self.tx, "s1.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")
        with open(os.path.join(self.tx, "s1", "subagents", "a.jsonl"), "w") as f:
            f.write(assistant("sub1", "2026-11-09T10:03:30Z", 5000) + "\n")

    def test_session_total_and_per_item_estimates(self):
        r = stats.report(self.t, transcripts=[self.tx])
        tk = r["tokens"]
        self.assertEqual(tk["session_total"], 1000 + 200 + 100 + 300 + 50 + 5000)
        self.assertEqual(tk["files"], 2)
        # m1 and m2 are in both spans (12 was dismissed while 11 was open), so each item gets half of them
        self.assertEqual(tk["answered"], {"items": 1, "attributed": 1, "tokens": 450, "per_item": 450})  # 100 + 50 + 300
        self.assertEqual(tk["dismissed"], {"items": 2, "attributed": 1, "tokens": 150, "per_item": 150})  # 100 + 50
        self.assertEqual([(b["kind"], b["tokens_attributed"]) for b in r["by"]],
                         [("ping", 0), ("thread-comment", 150), ("mention", 450)])
        text = stats.render(r)
        self.assertIn("dismissed: 2 item(s), 1 attributed, ~150 tokens (~150 per item)", text)

    def test_a_missing_transcript_directory_reports_none(self):
        r = stats.report(self.t, transcripts=[os.path.join(self.dir, "nowhere")])
        self.assertEqual((r["tokens"]["session_total"], r["tokens"]["answered"]["attributed"]), (0, 0))

    def test_the_cli_prints_json(self):
        cfg = os.path.join(self.dir, "config.json")
        with open(cfg, "w") as f:
            json.dump({"account": "1", "project": "2", "captain": 3}, f)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sync.cli(["stats", "--home", self.dir, "--config", cfg, "--by", "all", "--transcripts", self.tx, "--json"])
        self.assertEqual(code, 0)
        r = json.loads(out.getvalue())
        self.assertEqual((r["total"]["relayed"], r["total"]["dismissed"]), (3, 2))
        self.assertEqual(r["tokens"]["answered"]["tokens"], 450)


if __name__ == "__main__":
    unittest.main()
