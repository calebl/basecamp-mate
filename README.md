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
- On cards assigned to the captain (and only those), the run reads the card's boosts. A 👍
  (any skin tone) by the captain is appended to `pending-comments.jsonl` as an approval
  record. It means **approve every recommendation on the card as recommended**; a comment
  is how to change one. The script never releases, answers, unassigns or moves anything
  because of a boost: firstmate records the decision in the backlog, and the next normal
  run unassigns the card. Other emoji, other people's boosts and boosts on unassigned
  cards are ignored; each boost id is recorded in `map.json` so it is emitted once.
- `map.json` maps `task|board` to a card id, so re-runs update the same card instead of
  creating a duplicate.
- `--dry-run` makes no Basecamp writes (it only reads boosts on assigned cards, and logs
  any new captain approval) and leaves `map.json` and the pending file alone; it logs, per card,
  whether it would be created, updated, moved, assigned or unassigned, and a total.

## Config and state

One config per home, normally `<home>/data/basecamp-sync/config.json`. Copy
[`examples/config.example.json`](examples/config.example.json) and fill in the real ids:

- `account`, `project`: Basecamp ids.
- `captain`: the captain's Basecamp person id (assignee, and whose comments are relayed).
- `profile` (optional): the `basecamp` CLI login every call runs as (`-P <profile>`),
  including `run.sh`'s token refresh. Absent means the CLI's default login. It changes
  only who acts; `captain` stays the assignee and the only person whose comments and 👍
  are relayed, so the acting user's own comments and boosts are ignored.
- `repos`: backlog repo name -> board name.
- `tables`: per board, the card table id (`table`) and a column id for each of
  `Triage`, `Not now`, `Figuring it out`, `In progress`, `Ready for QA`, `Done`.

Everything else lives beside the config, never in this repo:

| File | Kept by | Purpose |
| --- | --- | --- |
| `map.json` | the script | task/board -> card id, column, assignment, content digest, seen comments and boosts |
| `sync.log` | the script | one line per action, plus a counts line per run |
| `pending-comments.jsonl` | the script | captain comments and approvals waiting to be relayed, one JSON record per line (see below) |
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
failure to `sync.log`), then runs `sync.py`, all within a 240-second cap (each token
call gets at most 30 seconds).

### A dedicated Basecamp user

To act as its own user instead of the captain, invite that user to the project, sign it
in as a separate CLI login, verify it, and set `"profile"` in the config:

```sh
basecamp auth login -P firstmate --account <account id>
basecamp api get /my/profile.json -P firstmate
```

Its token is short-lived; `run.sh` renews it with `basecamp auth refresh -P <profile>`.
Full setup steps, and what changes for the captain: [docs/firstmate-account.md](docs/firstmate-account.md).

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

## Pending records

Each line of `pending-comments.jsonl` is one JSON object with a `kind`. Records written
before `kind` existed have none; treat a missing `kind` as `"comment"`.

- `comment`: `task`, `repo`, `card`, `comment` (id), `at`, `text`.
- `approval`: `task`, `repo`, `card`, `url` (card URL), `boost` (id), `at`. The captain
  gave the card a 👍: approve every recommendation on it as recommended.

## License

MIT. See [LICENSE](LICENSE).
