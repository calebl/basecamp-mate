#!/usr/bin/env python3
"""Connect an agent home to one Basecamp project, in two layers.

tools.py is the tools layer: small, single-purpose operations with no policy. The
readers poll Basecamp and append pending records; the commands post exactly what
the agent hands them (`reply`, `ask`, `answer`, `todo ...`, `post-message`).
behaviors.py is the behaviors layer: opt-in workflows a home turns on in its config
and that compose the tools: the card mirror ("tables", "repos"; off with "cards":
false), the chat inbox ("chats"), chat asks ("ask_chat"), check-in answering
("checkins"), release announcements ("releases"), decision to-dos ("todos"),
reports ("message_board"), inbox delivery ("inbox") and the owner-event listener
("listen", run by `sync.py listen` as a service beside the timer). prompts/base.md is
the agent's side of each behavior.

Deterministic, no model calls. Safety bounds, enforced here:
  - only the configured account, project, card tables, chats, check-ins, to-do set
    and message board are touched;
  - nothing is ever deleted, trashed or archived, except the acting user's own 👀
    boost once a question is answered; the timer run posts nothing to chat or as a
    comment, and its only writes besides the card mirror are 👀/👍 acknowledgement
    boosts and a Message Board announcement per new GitHub release;
  - only the explicit commands post, run by the agent, and each refuses to when no
    profile is set or the profile signs in as the owner;
  - id maps (map.json, todos.json, ...) make re-runs update or skip instead of
    duplicating.
All runtime state lives in the directory that holds the config file.

Usage: sync.py --home <home> --config <config.json> [--dry-run] [--include-prereleases]
       sync.py reply --home <home> --config <config.json> --recording <comment or line id> --body-file <file> [--again] [--dry-run]
       sync.py ask --home <home> --config <config.json> --body-file <file> [--dry-run]
       sync.py answer --home <home> --config <config.json> --question <check-in question id> --body-file <file> [--dry-run]
       sync.py todo create --home <home> --config <config.json> --key <key> --title <text> [--body-file <file>] [--list <id>] [--due YYYY-MM-DD] [--dry-run]
       sync.py todo track --home <home> --config <config.json> --key <key> --todo <id> [--dry-run]
       sync.py todo comment --home <home> --config <config.json> --todo <key or id> --body-file <file> [--dry-run]
       sync.py todo complete --home <home> --config <config.json> --todo <key or id> [--dry-run]
       sync.py post-message --home <home> --config <config.json> --subject <text> --body-file <file> [--dry-run]
       sync.py listen --home <home> --config <config.json> [--once] [--dry-run]
       sync.py behaviors --home <home> --config <config.json>
       sync.py init <project URL> --login <profile> --home <home> [--captain <id or email>] [--repo-map TABLE=REPO] [--dry-run]
"""
import argparse, subprocess, sys
from datetime import datetime, timezone  # noqa: F401  (kept importable from sync)

import behaviors
from behaviors import COLUMNS, render_decision, render_release, inline_md  # noqa: F401
from tools import (Tools, THUMBS, EYES, is_due, render_reply, is_emoji, is_thumbs_up,  # noqa: F401
                   parse_lavish, parse_show, task_item, none)


class Sync(Tools):
    """The tools for one home, with its behaviors configured; `main` is one timer run."""

    def __init__(self, home, config_path, dry=False, runner=subprocess.run, prereleases=False):
        super().__init__(home, config_path, dry=dry, runner=runner)
        self.behaviors = behaviors.configure(self, self.cfg, prereleases=prereleases)

    @property
    def card_mirror(self):
        return self.behaviors["card-mirror"]

    @property
    def cards(self):
        return self.card_mirror.on

    @property
    def tables(self):
        return self.card_mirror.tables

    @property
    def repo_map(self):
        return self.card_mirror.repo_map

    def column_for(self, it, notnow):
        return self.card_mirror.column_for(it, notnow)

    def body_for(self, *args, **kw):
        return self.card_mirror.body_for(*args, **kw)

    def main(self, items=None):
        """One run: each behavior that is on, in order; a behavior left out of the config makes no calls.

        It holds the shared state lock throughout, so a listener cycle waits for it.
        """
        plan = None
        with self.locked("sync"):
            for b in self.behaviors.values():
                if b.on and b.timer:
                    out = b.run(items)
                    plan = out if b is self.card_mirror else plan
        if not self.cards:
            self.log("cards off" + (" (dry run)" if self.dry else ""))
        return plan

    def listen_once(self):
        """One listener cycle under the shared state lock: the owner's new events, each dispatched to its reader."""
        with self.locked("sync"):
            return self.behaviors["owner-events"].run()


def listen(a, runner, sleep=None, cycles=None):
    """`sync.py listen`: a listener cycle every "interval" seconds until the config turns "listen" off.

    The config is re-read each cycle. A failed cycle is retried on the next; the third
    failure in a row is logged once as FAILED (so the wake check sees it) and recovery
    is logged too. Exits 0 when "listen" is off, so a Restart=on-failure service stays down.
    """
    import time
    sleep = sleep or time.sleep
    failures, n = 0, 0
    while cycles is None or n < cycles:
        n += 1
        s = Sync(a.home, a.config, dry=a.dry_run, runner=runner)
        ev = s.behaviors["owner-events"]
        if not ev.on:
            s.log('listen: "listen" is not set in the config; stopping')
            return 0
        try:
            s.listen_once()
            if failures >= 3:
                s.log(f"listen: recovered after {failures} failed cycles")
            failures = 0
        except Exception as e:
            failures += 1
            if failures == 3:
                s.log(f"FAILED listen {type(e).__name__}: {e} (3 cycles in a row; retrying every {ev.interval}s)")
            elif failures < 3:
                s.log(f"listen: cycle failed, retrying: {type(e).__name__}: {e}")
        if a.once:
            return 1 if failures else 0
        sleep(ev.interval)
    return 0


def common(prog, description):
    ap = argparse.ArgumentParser(prog=prog, description=description)
    ap.add_argument("--home", required=True)
    ap.add_argument("--config", required=True)
    return ap


def read(path):
    with open(path) as f:
        return f.read()


def command(a, what, call, runner):
    s = Sync(a.home, a.config, dry=getattr(a, "dry_run", False), runner=runner)
    try:
        with s.locked("sync"):
            call(s)
    except Exception as e:
        s.log(f"FAILED {what} {type(e).__name__}: {e}")
        return 1
    return 0


def todo_cli(argv, runner):
    sub = argv[:1][0] if argv else None
    descs = {"create": "Create a to-do assigned to the owner and track it under a key.",
             "track": "Track an existing to-do under a key; its comments so far are not relayed.",
             "comment": "Comment on a tracked to-do.",
             "complete": "Mark a tracked to-do complete."}
    if sub not in descs:
        print("usage: sync.py todo {create,track,comment,complete} --home <home> --config <config.json> ...", file=sys.stderr)
        return 2
    ap = common(f"sync.py todo {sub}", descs[sub])
    if sub in ("create", "track"):
        ap.add_argument("--key", required=True, help="a stable key for this to-do, e.g. the task id")
    if sub == "create":
        ap.add_argument("--title", required=True)
        ap.add_argument("--body-file", help="the description, Markdown")
        ap.add_argument("--list", help="a to-do list id (default: the config's, else loose on the to-do set)")
        ap.add_argument("--due", help="YYYY-MM-DD")
    elif sub == "track":
        ap.add_argument("--todo", required=True, type=int, help="the to-do's id")
    else:
        ap.add_argument("--todo", required=True, help="the tracked to-do's key or id")
    if sub == "comment":
        ap.add_argument("--body-file", required=True, help="the comment, Markdown")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv[1:])
    calls = {"create": lambda s: s.todo_create(a.key, a.title, read(a.body_file) if a.body_file else "", a.list, a.due),
             "track": lambda s: s.todo_track(a.key, a.todo),
             "comment": lambda s: s.todo_comment(a.todo, read(a.body_file)),
             "complete": lambda s: s.todo_complete(a.todo)}
    return command(a, f"todo {sub}", calls[sub], runner)


def cli(argv=None, runner=subprocess.run):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["init"]:
        import init_home
        return init_home.cli(argv[1:])
    if argv[:1] == ["todo"]:
        return todo_cli(argv[1:], runner)
    if argv[:1] == ["reply"]:
        ap = common("sync.py reply", "Answer a captain question where it was asked, then remove the 👀.")
        ap.add_argument("--recording", required=True, type=int, help="the question comment's or chat line's id")
        ap.add_argument("--body-file", required=True, help="plain-text answer (Markdown for a to-do comment)")
        ap.add_argument("--again", action="store_true", help="post even though this question was already answered")
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args(argv[1:])
        return command(a, f"reply {a.recording}", lambda s: s.reply(a.recording, read(a.body_file), again=a.again), runner)
    if argv[:1] in (["ask"], ["answer"]):
        what = argv[0]
        ap = common(f"sync.py {what}", "Post a new line in the configured ask_chat, @mentioning the owner." if what == "ask"
                    else "Answer a recorded check-in question for today.")
        if what == "answer":
            ap.add_argument("--question", required=True, type=int, help="the check-in question's id")
        ap.add_argument("--body-file", required=True, help="plain text")
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args(argv[1:])
        return command(a, what, lambda s: s.ask(read(a.body_file)) if what == "ask"
                       else s.answer(a.question, read(a.body_file)), runner)
    if argv[:1] == ["post-message"]:
        ap = common("sync.py post-message", "Post a message on the configured message_board.")
        ap.add_argument("--subject", required=True)
        ap.add_argument("--body-file", required=True, help="the message, Markdown")
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args(argv[1:])
        return command(a, "post-message", lambda s: s.post_message(a.subject, read(a.body_file)), runner)
    if argv[:1] == ["listen"]:
        ap = common("sync.py listen", "Poll the Basecamp event feed for the owner's events and run the matching readers.")
        ap.add_argument("--once", action="store_true", help="one cycle, then exit (non-zero when it failed)")
        ap.add_argument("--dry-run", action="store_true", help="read and log; record, boost and save nothing")
        return listen(ap.parse_args(argv[1:]), runner)
    if argv[:1] == ["behaviors"]:
        a = common("sync.py behaviors", "List the behaviors and whether this config turns each on.").parse_args(argv[1:])
        s = Sync(a.home, a.config, runner=runner)
        for b in s.behaviors.values():
            print(f"{b.name:22} {'on ' if b.on else 'off'}  {b.runs:8}  ({', '.join(b.keys)})")
        return 0
    ap = argparse.ArgumentParser(description="One timer run: every behavior the config turns on.")
    ap.add_argument("--home", required=True, help="firstmate home (holds data/backlog.md and state/)")
    ap.add_argument("--config", required=True, help="config.json; its directory holds all runtime state")
    ap.add_argument("--dry-run", action="store_true", help="plan only: no Basecamp calls, map.json untouched")
    ap.add_argument("--include-prereleases", action="store_true", help="also announce GitHub prereleases")
    a = ap.parse_args(argv)
    s = Sync(a.home, a.config, dry=a.dry_run, runner=runner, prereleases=a.include_prereleases)
    try:
        s.main()
    except Exception as e:
        s.log(f"FAILED {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
