# firstmate-basecamp-sync

Mirror a firstmate home's backlog and captain decisions onto Basecamp card tables.

The backlog is the source of truth; Basecamp is a one-way view of it. Each run reads
every backlog task, picks its board from the task's repo, picks a column, and then
creates, updates, moves, assigns or unassigns cards through the `basecamp` CLI.
No model calls.

Column rules:

| Task state | Column |
| --- | --- |
| Queued | Triage |
| Queued, listed in `figuring.json` | Figuring it out |
| Held for the captain (`hold-kind: captain`, no until date) | Figuring it out, **assigned to the captain** |
| Parked, has an until date, or listed in `not-now.json` | Not now |
| In flight | In progress |
| In flight with `pr=` in `state/<task>.meta` | Ready for QA |
| Done | Done |

Card notes are HTML blocks (`<div>`, `<ol>`/`<ul>`) with no raw newlines between them. When the captain hold is released the card is unassigned on the next run.

## Safety bounds

- Only the configured account, project and card tables are touched.
- Cards are never deleted, trashed or archived. Cards whose task left the backlog are
  left as they are (the run logs how many).
- Nothing is posted to chat or as a comment.
- New comments by the captain are appended to `pending-comments.jsonl` for firstmate to
  relay; they are never acted on.
- `map.json` maps `task|board` to a card id, so re-runs update the same card instead of
  creating a duplicate.
- `--dry-run` makes no Basecamp calls and leaves `map.json` alone; it logs, per card,
  whether it would be created, updated, moved, assigned or unassigned, and a total.

## Config and state

One config per home, normally `<home>/data/basecamp-sync/config.json`. Copy
[`examples/config.example.json`](examples/config.example.json) and fill in the real ids:

- `account`, `project`: Basecamp ids.
- `captain`: the captain's Basecamp person id (assignee, and whose comments are relayed).
- `repos`: backlog repo name -> board name.
- `tables`: per board, the card table id (`table`) and a column id for each of
  `Triage`, `Not now`, `Figuring it out`, `In progress`, `Ready for QA`, `Done`.

Everything else lives beside the config, never in this repo:

| File | Kept by | Purpose |
| --- | --- | --- |
| `map.json` | the script | task/board -> card id, column, assignment, content digest, seen comments |
| `sync.log` | the script | one line per action, plus a counts line per run |
| `pending-comments.jsonl` | the script | captain comments waiting to be relayed |
| `extra-repos.json` | hand | `{"task": ["board", ...]}`: extra boards for a task |
| `figuring.json` | hand | `{"task": "why"}`: queued tasks that need a plan approved |
| `not-now.json` | hand | `{"task": "why"}`: tasks deferred |
| `skip.json` | hand | `["task", ...]`: tasks never mirrored |
| `decisions.json` | hand | `{"task": {"question": "...", "items": ["..."], "note": "..."}}`: decision text for a card waiting on the captain, rendered as the question, a numbered list and a note in place of the raw hold reason |

## Reading the backlog

The backlog is read through `tasks-axi`, addressed the way firstmate's
`bin/fm-tasks-axi.sh` does it: run from the parent of `<home>/data`, with
`TASKS_AXI_FILE=<home>/data/backlog.md` for a markdown backend (per `.tasks.toml`).
`tasks-axi list` gives the task ids; `tasks-axi show <id> --full` gives state, repo,
hold reason/kind/until, `deps` (for "After"), and `links`.

`tasks-axi` has no JSON output for reads, so its plain `key: value` output is parsed.
One field falls back to text parsing: the **title**. `tasks-axi` keeps PR URLs inside
the title (they are cut out and merged with `links`), and on a row that repeats its
`(field: ...)` groups it keeps the leading ones in the title, so the title is cut at the
first `(repo:`/`(kind:`/`(priority:`/... the way the original markdown parser did.
`deps` is used rather than `blocked_by`, because `blocked_by` drops dependencies that
are already done while the cards list them all.

## Run it once

Needs Python 3.11+, `tasks-axi`, and an authenticated `basecamp` CLI.

```sh
python3 sync.py --home ~/path/to/home --config ~/path/to/home/data/basecamp-sync/config.json --dry-run
```

Drop `--dry-run` to apply. `run.sh <home> <config> [--dry-run]` is the scheduled entry
point: it refreshes the Basecamp OAuth token when under three days remain (logging a
failure to `sync.log`), then runs `sync.py` under a 25-minute timeout.

## Hourly with systemd

`~/.config/systemd/user/basecamp-sync.service` (also in [`examples/`](examples)):

```ini
[Unit]
Description=Mirror a firstmate backlog into its Basecamp project

[Service]
Type=oneshot
ExecStart=%h/src/firstmate-basecamp-sync/run.sh %h/path/to/firstmate-home %h/path/to/firstmate-home/data/basecamp-sync/config.json
```

`~/.config/systemd/user/basecamp-sync.timer`:

```ini
[Unit]
Description=Hourly Basecamp sync for a firstmate backlog

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now basecamp-sync.timer
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The `basecamp` CLI is stubbed; tests make no network calls.
