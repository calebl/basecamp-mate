# One home per project and login

Two homes whose configs watch the same project with the same Basecamp login would each
read the same notifications, record the same input, acknowledge it twice and wake two
agents. The shared state lock cannot stop that, since it is per config, so every timer run
first claims what its config watches ([`runs.py`](../runs.py)): its project, and,
account-wide, its login's Pings (with `pings`), the to-dos assigned to it anywhere
(with `"assigned_todos": {"scope": "account"}`) and its @mentions and followed threads
anywhere (with `"mentions": {"scope": "account"}`). Two claims overlap when they share one of
those with the same login: the same Basecamp person when both runs know it, else the same
`profile`. A run beside another config's live claim that overlaps is refused: it reads
nothing, exits 1 and logs `FAILED duplicate run`, naming the other home, its config and
login, when it last ran or its pid if it is running now (once, then hourly, so the wake
check and `doctor` see it without flooding the log). When the other run stops, the next
run says `duplicate run cleared` and carries on. A chat its home watches with `sync.py
watch` is never claimed and never refuses a run: when another home's live run on the same
login claims that chat's project, the run skips that chat (logged once when that starts
and once when it ends), checked every run, so a home that starts after the watch is
covered too. The watches are read under the shared state lock the whole run holds, so a
watch started meanwhile waits for the next run. Two runs of the same config are never
duplicates: they share one claim and the state lock serializes them, and the explicit
commands (`reply`, `todo ...`) never claim.

Each config's claim is a file in `$BASECAMP_MATE_RUNS` (default
`~/.local/state/basecamp-mate/runs/`): the config and home, the login, what it watches, the
run's pid and process start time, and when it last ran. A claim is live while its run is
going (the pid, checked against its start time since pids are recycled) or for 10 minutes
after it last ran while its config still exists. The timer runs every 30 seconds, so a
killed run, a stopped timer or a removed config stops blocking within 10 minutes, and a
claim whose config is gone is removed. Checking and claiming are one step under a lock, so
two homes starting together cannot both claim. A dry run checks and claims nothing.

`basecamp-mate status` (`sync.py status`) lists every claim, live or stale, what it
watches and with which login, the live claims it overlaps, and any `sync.py` process
running (from `/proc`) whose config holds no claim. To run two homes on the same thing on
purpose, set `"allow_duplicate": true` in the config (or pass `--allow-duplicate` to one
run); the run then notes the overlap once and goes ahead. `doctor` flags homes on this
computer whose configs overlap, unless all of them allow it.
