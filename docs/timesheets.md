# Timesheets

With `"timesheet": {}` (`init --timesheet`), the sync posts the agent's active time on each
piece of work to the Basecamp timesheet of its to-do or card: one entry per piece of work
per day, once. Off (the default), nothing about it runs: no calls, no files, no log lines.

The agent names its work. `sync.py timesheet start C --key <key> --on <to-do or card URL>
--workdir <dir>` starts counting from now, in the Claude Code transcripts of sessions run in
that directory (`~/.claude/projects/<the path, each character that is not a letter or digit
as ->`, or `CLAUDE_CONFIG_DIR`).
Use a directory only that work runs in (a worker's worktree), never the home, which every
session there shares: `start` refuses a directory another key is counting now, naming that
key, since its time would be counted for both. Repeat it with the same key for each other
directory the work runs in. Without `--on`, the
key's to-do (`todos.json`: a request or decision to-do) or mirrored card (`map.json`) is
used. `sync.py timesheet stop C --key <key>` stops counting. Nothing is guessed from paths
or task names.

**Active time** is read from the transcript lines' timestamps, between each start and stop:
a gap of at most 5 minutes between two lines of a transcript counts, and so does a whole
wait of at most 60 minutes on a tool call the agent made (a long command, a subagent),
from the call to its result, across the lines between them. A longer gap is idle, a longer
wait adds nothing beyond its short gaps, and a call with no result adds nothing. The spans of all a key's transcripts are merged, so time is never counted twice,
then split by local day (the config `timezone`, default `America/New_York`) and rounded to
the nearest quarter hour.

**Posting.** Hourly, each finished day (an hour past its local midnight, once its last
tool wait is known) with a quarter hour or
more is logged as one entry (`basecamp timesheet entry create`: `date`, `hours`,
`description` "Active agent time: <key>") as the agent's login, never as the owner. `timesheet.json` keeps every entry posted, and
before each post the sync reads the agent's entries on that to-do or card for that day: when
this key's entry (description "Active agent time: <key>") is already there (a post whose
answer was lost, or a run that stopped before saving), it is only added to `timesheet.json`,
so a re-run or a restart finds that day's entry instead of posting a second one. Another
key's entry on the same to-do or card that day does not stop the post: several pieces of
work can share one to-do. A key with no to-do or card is skipped and reported once
(`timesheet-unmapped`); its days post once it has one. A project whose timesheet refuses an
entry (forbidden or invalid) is reported once (`timesheet-refused`, one FAILED
line). From then on it is tried with one entry at most once a local day, logged without
FAILED, until an entry there posts again; then its other entries follow. A key whose to-do
or card is not found is reported once (`timesheet-missing`) and tried the same way, alone, so
the project's other keys keep posting; `timesheet start --on` names another. A stopped key whose
days wait only on such a refusal keeps them in `timesheet.json` as `unposted`, so its
transcripts are not read again (`timesheet start` adds new days to them); they go with that
daily try, and the key settles once they all post. Any other failed entry is a
FAILED line and is tried again on the next run. When one of a key's transcripts
cannot be read, that key posts nothing that run (a FAILED line) and is not settled, so no
day is saved with part of its time; a transcript removed while it is read is skipped and
logged. `sync.py timesheet post C
--dry-run` prints what would be posted and posts nothing. Only days after `start` count:
there is no backfill.

To turn it on in another agent home, give its agent this prompt (`SYNC` is its
basecamp-mate checkout, `HOME` its firstmate home):

> Turn on Basecamp timesheets for this home: add `"timesheet": {}` to
> `HOME/data/basecamp-sync/config.json`, check that `python3 SYNC/sync.py behaviors --home
> HOME --config HOME/data/basecamp-sync/config.json` lists `timesheet` as on, and copy the
> "Timesheets" section of `SYNC/prompts/defaults.md` into
> `HOME/data/basecamp-sync/defaults.md`. From now on follow it: run `sync.py timesheet start`
> with the to-do or card and the work's directory whenever work on a Basecamp to-do or card
> starts (a directory only that work runs in, such as a worker's worktree, never this home),
> and `sync.py timesheet stop` when it ends. Tell me when a project refuses
> timesheet entries, because that project does not have timesheet tracking turned on.
