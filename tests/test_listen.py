"""Tests for the owner-event listener: the event-feed reader, its dispatch to the readers, re-entry and the shared lock.

/events.json pages are stubbed; nothing touches the network.
"""
import json, os, sys, threading, time, unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sync import ACTING, CAPTAIN, item, line  # noqa: E402
from test_tools import TodoStub, ToolBase, boost, comment  # noqa: E402
import sync  # noqa: E402

PROJECT = 22222222
POS_409 = "Positions are bound to the filter set they were minted for. Acknowledge the filter change by re-entering with since=<id> or since=now."
POS_400 = "Unrecognized position. Resume with since=<id> or since=now."
POS_410 = "That position predates this feed's epoch, so the history behind it can't be served."
FILTER_400 = "Unknown event types: nope.created. Fix the filters; a position reset won't help."


def event(id, kind="chat.line.created", rid=1, who=CAPTAIN, bucket=PROJECT, **details):
    ev = {"id": id, "event_type": kind, "kind": kind.replace(".", "_"), "action": "created", "bucket_id": bucket,
          "creator_id": who, "performed_by_id": None, "recording_id": rid, "created_at": "te"}
    if details:
        ev["details"] = details
    return ev


class FeedStub(TodoStub):
    """Adds /events.json pages (queued responses, each a page dict or an error text) and comment refetches."""

    def __init__(self):
        super().__init__()
        self.feed, self.queries, self.parents = [], [], {}  # queued responses; each poll's query; comment id -> parent
        self.on_feed = None  # called on each poll, e.g. to block a thread

    def __call__(self, cmd, **kw):
        if cmd[0] == "basecamp":
            args = cmd[3:-3]
            core = args[2:] if args[:1] == ["-P"] else args
            if core[:2] == ["api", "get"] and core[2].startswith("/events.json?"):
                self.calls.append(core)
                q = {k: v[0] for k, v in parse_qs(urlsplit(core[2]).query).items()}
                self.queries.append(q)
                if self.on_feed:
                    self.on_feed()
                resp = self.feed.pop(0) if self.feed else {"events": [], "position": q.get("position") or "p-empty"}
                if isinstance(resp, str):
                    out = {"ok": False, "error": resp, "code": "validation" if "position" in resp.lower() else "api_error",
                           "retryable": False}
                else:
                    out = {"ok": True, "data": resp}
                return SimpleNamespace(stdout=json.dumps(out), stderr="", returncode=0 if out["ok"] else 7)
            if core[:2] == ["api", "get"] and "/comments/" in core[2]:
                self.calls.append(core)
                cid = int(core[2].split("/")[-1].split(".")[0])
                return SimpleNamespace(stdout=json.dumps({"ok": True, "data": {"id": cid, "parent": self.parents.get(cid, {})}}),
                                       stderr="", returncode=0)
        return super().__call__(cmd, **kw)

    def page(self, *events, position=None, more=False):
        p = {"events": list(events), "position": position or f"p{len(self.feed) + len(self.queries) + 1}"}
        if more:
            p["next"] = "https://3.basecampapi.com/1/events.json?position=" + p["position"]
        self.feed.append(p)


class ListenBase(ToolBase):
    def setUp(self):
        super().setUp()
        self.stub = FeedStub()
        self.cfg(profile="agent", todos={"todoset": "66"}, message_board="99", chats=[77], listen={})
        self.stub.lines = {"77": [line(1)]}
        self.poll()  # the timer seeds the chat cursor; cards: none yet
        self.stub.calls.clear()

    def listen(self, dry=False):
        return self.sync(dry=dry).listen_once()

    def feed_state(self):
        p = os.path.join(self.cfgdir, "feed.json")
        return json.load(open(p)) if os.path.exists(p) else None

    def kinds(self):
        return [r["kind"] for r in self.pending()]

    def log(self):
        return open(os.path.join(self.cfgdir, "sync.log")).read()


class FeedReader(ListenBase):
    def test_enters_at_the_present_then_resumes_from_the_saved_position(self):
        self.stub.page(position="p1")
        self.listen()
        self.stub.page(position="p2")
        self.listen()
        first, second = self.stub.queries
        self.assertEqual(first, {"since": "now", "types": "boost.created,chat.line.created,comment.created",
                                 "buckets": str(PROJECT), "creators": str(CAPTAIN)})
        self.assertEqual(second["position"], "p1")
        self.assertNotIn("since", second)
        self.assertEqual(self.feed_state()["position"], "p2")

    def test_position_tokens_survive_url_encoding(self):
        token = "BAh7CUkiBnYGOgZFVGkGSSIGYQY7AFRsKwj/FGQB+BABJ==--3863fa9f"
        self.stub.page(position=token)
        self.listen()
        self.listen()
        self.assertEqual(self.stub.queries[1]["position"], token)

    def test_follows_next_and_drops_events_already_handed_over(self):
        self.stub.lines["77"].append(line(2))
        self.stub.page(event(10, rid=2), position="p1", more=True)
        self.stub.page(event(10, rid=2), event(11, rid=2), position="p2")
        self.listen()
        self.assertEqual([q.get("position") for q in self.stub.queries], [None, "p1"])
        self.assertEqual(self.feed_state(), {"filters": self.feed_state()["filters"], "position": "p2", "last_event": 11})
        self.assertEqual(self.kinds(), ["chat-question"])
        self.assertIn("listen: 1 event(s) -> chats", self.log())

    def test_position_saved_only_after_the_page_is_handled(self):
        self.stub.page(position="p1")
        self.listen()
        self.stub.lines["77"].append(line(2))
        self.stub.page(event(10, rid=2), position="p2")
        real = sync.Sync.read_chats

        def boom(s, *a, **kw):
            raise RuntimeError("disk full")
        sync.Sync.read_chats = boom
        try:
            with self.assertRaises(RuntimeError):
                self.listen()
        finally:
            sync.Sync.read_chats = real
        self.assertEqual(self.feed_state()["position"], "p1")
        self.stub.page(event(10, rid=2), position="p2")
        self.listen()  # the same page again, from the same position
        self.assertEqual(self.stub.queries[-1]["position"], "p1")
        self.assertEqual(self.kinds(), ["chat-question"])
        self.assertEqual(self.feed_state()["position"], "p2")

    def test_dry_run_saves_no_position_and_records_nothing(self):
        self.stub.lines["77"].append(line(2))
        self.stub.page(event(10, rid=2), position="p1")
        self.listen(dry=True)
        self.assertIsNone(self.feed_state())
        self.assertEqual(self.pending(), [])
        self.assertIn("dry chat 77: captain question 2", self.log())


class Reentry(ListenBase):
    def entered(self):
        self.stub.page(event(10, rid=1), position="p1")
        self.listen()
        self.stub.queries.clear()

    def test_filter_conflict_409_reenters_after_the_last_event(self):
        self.entered()
        self.stub.feed.append(POS_409)
        self.stub.page(position="p2")
        self.listen()
        self.assertEqual([q.get("position") or "since=" + q["since"] for q in self.stub.queries], ["p1", "since=10"])
        self.assertEqual(self.feed_state()["position"], "p2")
        self.assertIn("Re-entering with since=10", self.log())

    def test_unrecognized_position_400_reenters_after_the_last_event(self):
        self.entered()
        self.stub.feed.append(POS_400)
        self.stub.page(position="p2")
        self.listen()
        self.assertEqual(self.stub.queries[-1]["since"], "10")

    def test_position_before_the_epoch_410_reenters_at_the_epoch(self):
        self.entered()
        self.stub.feed.append(POS_410)
        self.stub.page(event(5, rid=1), event(12, rid=1), position="p2")
        self.listen()
        self.assertEqual(self.stub.queries[-1]["since"], "0")
        self.assertEqual(self.feed_state()["last_event"], 12)  # event 5 was handed over before; dropped

    def test_since_id_before_the_epoch_falls_back_to_the_epoch(self):
        self.entered()
        self.stub.feed += [POS_409, POS_410]
        self.stub.page(position="p2")
        self.listen()
        self.assertEqual([q.get("since") for q in self.stub.queries], [None, "10", "0"])

    def test_invalid_filter_is_never_retried(self):
        self.entered()
        self.stub.feed.append(FILTER_400)
        with self.assertRaises(RuntimeError):
            self.listen()
        self.assertEqual(len(self.stub.queries), 1)
        self.assertEqual(self.feed_state()["position"], "p1")

    def test_changed_filters_reenter_without_waiting_for_a_409(self):
        self.entered()
        self.cfg(captain=CAPTAIN + 1)
        self.stub.page(position="p2")
        self.listen()
        self.assertEqual(self.stub.queries[-1], {"since": "10", "types": "boost.created,chat.line.created,comment.created",
                                                 "buckets": str(PROJECT), "creators": str(CAPTAIN + 1)})
        self.assertIn("(filters changed)", self.log())


class Dispatch(ListenBase):
    """Each event runs the existing reader for its surface; records, acknowledgements and notes are the readers'."""

    def test_owner_chat_line_is_recorded_acknowledged_and_delivered_in_the_same_cycle(self):
        self.cfg(inbox={})
        self.poll()  # inbox delivery starts after the existing records
        self.stub.lines["77"].append(line(2, content="ship it?"))
        self.stub.page(event(10, rid=2))
        self.listen()
        self.assertEqual(self.kinds(), ["chat-question"])
        self.assertIn(("2", "\U0001F440"), self.stub.posted())
        self.assertEqual([n[0] for n in self.stub.notes], ["basecamp-chat-question-2"])

    def test_stray_events_read_nothing(self):
        self.stub.lines["77"].append(line(2))
        self.stub.page(event(10, rid=2, who=ACTING), event(11, rid=2, bucket=1))
        self.listen()
        self.assertEqual(self.pending(), [])
        self.assertFalse([c for c in self.stub.calls if "/chats/" in c[2]])

    def test_comment_on_a_mirrored_card_runs_that_cards_readers_only(self):
        self.sync().main([item("a"), item("b")])  # cards 501 and 502
        self.stub.calls.clear()
        self.stub.comments = {"501": [comment(9, content="<p>why?</p>")]}
        self.stub.parents[9] = {"id": 501, "type": "Kanban::Card"}
        self.stub.page(event(10, "comment.created", rid=9))
        self.listen()
        self.assertEqual(self.kinds(), ["question"])
        self.assertEqual([c for c in self.stub.calls if c[:2] == ["comments", "list"]], [["comments", "list", "501"]])
        self.assertFalse([c for c in self.stub.calls if c[0] == "cards"])  # the mirror itself never runs
        self.assertIn(9, self.cards()["a|server"]["comments"])

    def test_comment_on_a_tracked_todo_runs_that_todos_reader(self):
        tid = self.create()
        self.create(key="other")
        self.stub.calls.clear()
        self.stub.comments[str(tid)] = [comment(9)]
        self.stub.parents[9] = {"id": tid, "type": "Todo"}
        self.stub.page(event(10, "comment.created", rid=9))
        self.listen()
        self.assertEqual(self.kinds(), ["todo-comment"])
        self.assertEqual([c[2] for c in self.stub.calls if c[:2] == ["comments", "list"]], [str(tid)])

    def test_comment_on_something_unwatched_reads_only_the_comment(self):
        self.stub.parents[9] = {"id": 4, "type": "Upload"}
        self.stub.page(event(10, "comment.created", rid=9))
        self.listen()
        self.assertEqual([c[2].split("?")[0] for c in self.stub.calls], ["/events.json", f"/buckets/{PROJECT}/comments/9.json"])
        self.assertIn("nothing to read", self.log())

    def test_comment_on_the_agents_message_runs_the_message_reader(self):
        recent = sync.datetime.now(sync.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.stub.boosts["99"] = [{"id": 20, "creator": {"id": ACTING}, "subject": "Report", "created_at": recent,
                                   "boosts_count": 0, "comments_count": 1, "app_url": "https://x/m/20"}]
        self.stub.comments["20"] = [comment(25, content="<p>dig deeper?</p>")]
        self.stub.parents[25] = {"id": 20, "type": "Message"}
        self.stub.page(event(10, "comment.created", rid=25))
        self.listen()
        self.assertEqual([(r["kind"], r["message"], r["comment"]) for r in self.pending()], [("message-comment", 20, 25)])
        self.assertIn(("25", "\U0001F440"), self.stub.posted())

    def test_boost_on_a_known_todo_runs_its_reader(self):
        tid = self.create()
        self.poll()  # seeds the to-do's boosts
        self.stub.boosts[str(tid)] = [boost(70, content="yes")]
        self.stub.page(event(10, "boost.created", rid=tid, boost_id=70))
        self.listen()
        self.assertEqual([(r["kind"], r["surface"], r["text"]) for r in self.pending()], [("boost", "todo", "yes")])

    def test_boost_on_a_seen_chat_line_runs_the_chat_reader(self):
        self.stub.lines["77"][0]["boosts_count"] = 1
        self.stub.boosts["1"] = [boost(60, content="ok")]
        self.stub.page(event(10, "boost.created", rid=1, boost_id=60))
        self.listen()
        self.assertEqual([(r["surface"], r["recording"]) for r in self.pending()], [("chat", 1)])
        self.assertFalse([c for c in self.stub.calls if "/message_boards/" in c[2]])

    def test_boost_on_an_unseen_recording_runs_every_cheap_boost_reader(self):
        recent = sync.datetime.now(sync.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.stub.page(event(10, "boost.created", rid=20, boost_id=50))
        self.stub.boosts["99"] = []
        self.listen()  # nothing seen yet: chats, to-dos, check-in answers (off here) and messages are read
        read = " ".join(c[2] for c in self.stub.calls if c[:2] == ["api", "get"])
        self.assertIn("/chats/77/", read)
        self.assertIn("/message_boards/99/", read)
        self.stub.boosts["99"] = [{"id": 20, "creator": {"id": ACTING}, "subject": "Report", "created_at": recent,
                                   "boosts_count": 1, "app_url": "https://x/m/20"}]
        self.stub.boosts["20"] = [boost(50, content="agree")]
        self.stub.page(event(11, "boost.created", rid=20, boost_id=50))
        self.listen()  # the message's first read seeds its count, as on the timer: no history replayed
        self.assertEqual(self.pending(), [])

    def test_off_behaviors_are_never_read(self):
        self.cfg(drop=("chats", "todos", "message_board"))
        self.stub.page(event(10, rid=1), event(11, "boost.created", rid=5))
        self.listen()
        self.assertEqual([c[2].split("?")[0] for c in self.stub.calls], ["/events.json"])


class SharedLock(ListenBase):
    def test_a_timer_sweep_during_a_listener_cycle_waits_and_records_nothing_twice(self):
        self.stub.lines["77"].append(line(2))
        self.stub.page(event(10, rid=2))
        entered, release = threading.Event(), threading.Event()

        def block():
            entered.set()
            release.wait(5)
        self.stub.on_feed = block
        listener = threading.Thread(target=self.listen)
        listener.start()
        self.assertTrue(entered.wait(5))
        sweep = threading.Thread(target=self.poll)
        sweep.start()
        time.sleep(0.2)
        self.assertTrue(sweep.is_alive())  # waiting on the shared lock
        self.assertFalse([c for c in self.stub.calls if "/chats/" in c[2]])
        self.stub.on_feed = None
        release.set()
        listener.join(5)
        sweep.join(5)
        self.assertEqual(self.kinds(), ["chat-question"])
        self.assertEqual([r["line"] for r in self.pending()], [2])

    def test_the_sweep_first_then_the_event_records_nothing_new(self):
        self.stub.lines["77"].append(line(2))
        self.poll()
        self.stub.page(event(10, rid=2))
        self.listen()
        self.assertEqual(self.kinds(), ["chat-question"])
        self.assertEqual(self.stub.posted(), [("2", "\U0001F440")])

    def test_commands_hold_the_lock_too(self):
        held = []
        real = sync.Sync.locked

        def spy(s, name):
            held.append(name)
            return real(s, name)
        sync.Sync.locked = spy
        try:
            f = os.path.join(self.tmp, "b.md")
            open(f, "w").write("x")
            sync.cli(["post-message", "--home", self.home, "--config", os.path.join(self.cfgdir, "config.json"),
                      "--subject", "S", "--body-file", f], runner=self.stub)
        finally:
            sync.Sync.locked = real
        self.assertEqual(held, ["sync"])


class ListenLoop(ListenBase):
    def args(self, **kw):
        return SimpleNamespace(home=self.home, config=os.path.join(self.cfgdir, "config.json"), dry_run=False, once=False, **kw)

    def test_failures_logged_once_as_failed_then_recovery(self):
        self.stub.page(position="p1")
        self.listen()
        self.stub.feed += [FILTER_400] * 4
        sleeps = []
        self.assertEqual(sync.listen(self.args(), self.stub, sleep=sleeps.append, cycles=5), 0)
        log = self.log()
        self.assertEqual(log.count("FAILED listen"), 1)
        self.assertEqual(log.count("listen: cycle failed, retrying"), 2)
        self.assertIn("listen: recovered after 4 failed cycles", log)
        self.assertEqual(sleeps, [30] * 5)

    def test_stops_cleanly_when_listen_is_off_and_honours_the_interval(self):
        self.cfg(listen={"interval": 45})
        sleeps = []
        sync.listen(self.args(), self.stub, sleep=sleeps.append, cycles=2)
        self.assertEqual(sleeps, [45, 45])
        self.cfg(drop=("listen",))
        self.assertEqual(sync.listen(self.args(), self.stub, sleep=sleeps.append), 0)
        self.assertIn('"listen" is not set', self.log())

    def test_cli_once_and_behaviors_listing(self):
        cfg = os.path.join(self.cfgdir, "config.json")
        self.assertEqual(sync.cli(["listen", "--home", self.home, "--config", cfg, "--once"], runner=self.stub), 0)
        self.stub.feed.append(FILTER_400)
        self.assertEqual(sync.cli(["listen", "--home", self.home, "--config", cfg, "--once"], runner=self.stub), 1)
        s = self.sync()
        self.assertEqual((s.behaviors["owner-events"].on, s.behaviors["owner-events"].runs), (True, "listener"))

    def test_malformed_listen_config_refused(self):
        for bad in ([], {"interval": 2}):
            self.cfg(listen=bad)
            with self.assertRaises(ValueError):
                self.sync()


if __name__ == "__main__":
    unittest.main()
