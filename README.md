# firstmate-basecamp-sync

A set of independent, opt-in tools that connect a firstmate home to one Basecamp
project through the `basecamp` CLI. No model calls. Each tool runs only when its config
keys are present; a home picks just the pieces it needs:

| Tool | Config keys | What it does |
| --- | --- | --- |
| Card mirror | `tables`, `repos` (off with `"cards": false`) | mirrors the backlog onto card tables; relays the owner's card comments and 👍 approvals |
| Chat relay | `chats` | records the owner's chat questions (or every line) for the agent |
| Chat asks | `ask_chat` | `sync.py ask` posts a question to the owner in chat |
| Check-ins | `checkins` | records due check-in questions; `sync.py answer` answers them |
| Release announcements | `releases` | posts a Message Board message per new GitHub release |
| Replies | (any of the above) | `sync.py reply` answers a recorded card or chat question |

Every tool shares `account`, `project`, `captain` (the owner's person id) and the optional
`profile`. A config without `tables` (or with `"cards": false`) never reads the backlog,
never calls a card or card-table endpoint, and never writes `map.json`; the other tools
run the same either way. Set such a home up with `sync.py init --no-cards`.

## Card mirror

The backlog is the source of truth; Basecamp is a one-way view of it. Each run reads
every backlog task, picks its board from the task's repo, picks a column, and then
creates, updates, moves, assigns or unassigns cards.

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

If an agent hosts a Lavish review board for a task, the card links it. Each run reads the
live sessions once from plain `lavish-axi` (it has no machine-readable listing, so its
`sessions[N]{file,status,url,...}` table is parsed, with a 15s cap). A session with status
`open` whose file is under `<home>/data/<task>/` belongs to that task, and its URL, exactly
as printed, is listed as **Plan board** near the top of the note. `boards.json` adds boards
owned by other tasks (e.g. a scout's plan). Once a session ends or disappears the link
drops on the next run. If `lavish-axi` is missing, fails or times out, the run logs it and
each card keeps the links it had.

Card notes are HTML blocks (`<div>`, `<ol>`/`<ul>`) with no raw newlines between them. When the captain hold is released the card is unassigned on the next run.

## Safety bounds

- Only the configured account, project and card tables are touched.
- Cards are never deleted, trashed or archived. Cards whose task left the backlog are
  left as they are (the run logs how many).
- The sync never posts to chat or as a comment. Its one automatic post is a release
  announcement, and only on the Message Board (below). Otherwise the only thing that
  posts is the explicit `sync.py reply` command below, run by the relaying agent to
  answer a captain question where it was asked: a comment on the card, or a line in the chat.
  Two more explicit commands post, both opt-in: `sync.py ask` (a new chat line for the
  owner) and `sync.py answer` (a check-in answer). The sync run never calls either.
- Release announcements: for each GitHub repo in the optional `releases` config, each run
  lists recent releases (`gh release list`, then `gh release view --json` for the notes)
  and posts one Message Board message per new one as the acting user, with the subject
  `<Name> <tag> released` (e.g. "Terminal v0.3.0 released") and a body of the release
  notes rendered to Basecamp rich text (escaped; headings bold, lists, links), a link to
  the release page, and the repo's optional `note`. The first run for a repo only seeds
  the watch with the releases that exist then, so no backlog is announced; after that a
  release is new when it was published after the watch started and is not yet recorded.
  Drafts are always skipped, and prereleases unless `"prereleases": true` or
  `--include-prereleases`. Announced tags are kept per repo in `releases.json`, so a
  release is posted once; messages are never edited or deleted. A failed `gh` read or
  post is logged and retried next run and never fails the sync. It posts even with no
  `profile` or a profile signed in as the captain: an announcement is not an
  acknowledgement. Merges and PRs are never announced.
- Chat questions: for each chat id in the optional `chats` config list, each run reads the
  chat's lines newer than a cursor kept in `chats.json` (the first run only sets the
  cursor, so older history is not relayed). A captain line that mentions the acting user
  or contains `?` is appended to `pending-comments.jsonl` as a `chat-question`, recorded
  once, and gets the acting user's 👀. Lines from anyone else, the acting user included,
  are never captured. `sync.py reply --recording <line id>` answers it with a new line in
  that chat as the acting user and then removes the 👀, under the same `--again` and
  failure rules as a card reply. A chat configured as `{"chat": <id>, "every_line": true}`
  relays every line the owner posts there, not only mention or `?` lines; each goes
  through the same record, cursor and 👀 rules.
- Asking the owner: with `ask_chat` set,
  `sync.py ask --home <home> --config <config.json> --body-file <file>` posts the file's
  plain text (rendered like a reply) as a new line in that chat as the acting user,
  starting with an @mention of the owner (their person record's `attachable_sgid`, read
  from `/people/<id>.json`). Post one question or decision per line. With no profile, or a
  profile signed in as the owner, it posts nothing; `--dry-run` logs only. The owner's
  answer comes back through the chat relay when the chat is also in `chats`.
- Check-ins: with `checkins` set, each run lists the questions of every configured
  questionnaire and records each one that is due today as a `checkin` in
  `pending-comments.jsonl`, once per question per day. A question is due when it is not
  paused, today's weekday is in its schedule `days` (Basecamp numbering, 0 = Sunday), its
  `start_date` has been reached (and any `end_date` not passed), and its `hour`:`minute`
  has passed in the configured `timezone` (the machine's local time when unset). One the
  acting user already answered today is not recorded. The run never answers;
  `sync.py answer --home <home> --config <config.json> --question <id> --body-file <file>`
  does, through `basecamp checkins answer create <id> <content> --date <today>`, at most
  once per question per day (checked in `checkins.json` and in the question's answers),
  and under the same refusal rules as `ask`.
- Acknowledgement boost: when the run records a new captain comment or approval, the
  acting user boosts it once (the comment itself, or the card for an approval). A comment
  whose text contains `?` is recorded with `"kind": "question"` and gets 👀 ("looking into
  it"); other comments and approvals get 👍 ("got it"). The sync never removes a 👀. Once the relaying agent
  has the answer it runs
  `sync.py reply --home <home> --config <config.json> --recording <question comment id> --body-file <file>`,
  which posts the file's plain text (HTML-escaped, one `<div>` per paragraph) as a comment
  on that card as the acting user, then removes the acting user's 👀 from the question; no
  👍 is added. The question id is recorded in the card's `replied` list in `map.json` and a
  second reply is refused unless `--again` is passed. A failed post removes nothing, so the
  👀 stays. With no profile, or a profile signed in as the captain, it posts nothing;
  `--dry-run` logs the plan only. The acting user's own replies are never relayed, since
  only the captain's comments are. Boosts are posted via `POST /buckets/<project>/recordings/<id>/boosts.json`. The
  acting identity is read from `/my/profile.json` once per run; with no `profile`, or a
  profile that signs in as the captain, nothing is boosted, so the captain never appears
  to boost his own items. A recording that already has the acting user's 👍 is not
  boosted again, and boosted ids are kept in `map.json` (`ack` queued, `acked` done). A
  failed boost is logged and retried next run; it never blocks the pending record or
  fails the run. The acting user's boost is never read as an approval, since only the
  captain's boosts are.
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
- `--dry-run` makes no Basecamp writes (it only reads boosts on assigned cards and the
  acting identity, and logs any new captain approval and the acknowledgement boosts it
  would add) and leaves `map.json` and the pending file alone; it logs, per card,
  whether it would be created, updated, moved, assigned or unassigned, and a total.

## Set up a home

```sh
python3 sync.py init https://app.basecamp.com/<account>/projects/<project> --login firstmate --home <home> --dry-run
```

`init` reads the project as the `--login` profile and discovers every id below: the card
tables in the project's dock, each matched to a backlog repo by its title
(case-insensitive, surrounding spaces ignored) against the names in the home's
`data/projects.md`; each table's columns by their exact names; the login's own identity
(`/my/profile.json`); the captain; the chats in the dock; and, when the dock has one
Message Board, `releases`: its id and the GitHub repo behind each mapped repo's
`<home>/projects/<repo>` origin (renames followed through `gh repo view`), named by its
board. A repo's `note` is added by hand. `--captain <person id or
email>` names the captain; by default it is the project's one account owner other than the
login. `--repo-map <table>=<repo>` (repeatable) maps a table whose title is not a
registered project's name. A table whose title matches no registered project and that
`--repo-map` does not name (a general "Ideas" board, say) is skipped: `init` prints a line
for it, leaves it out of the config, never reads its columns, and the sync never touches
it. At least one table must be included. Any other missing or ambiguous table, column,
repo or identity is refused with a message, and nothing is guessed or written. `--create-missing-columns`
creates a missing Figuring it out, In progress or Ready for QA column (the only Basecamp
write `init` makes); Triage, Not now and Done are built in and never created.

Without `--dry-run` it then:

- writes `<home>/data/basecamp-sync/config.json` (refusing if one exists and differs,
  unless `--force`) and creates the empty hand-kept side files that are missing;
- installs and enables the systemd user units `basecamp-sync-<home path>.service` and
  `.timer` (every 5 minutes, 240s cap), running this checkout's `run.sh`;
- writes `<home>/state/basecamp-sync.check.sh` and registers it with the home's own
  `bin/fm-check-register.sh`, so the home wakes on new pending records and failed runs.

`--no-cards` sets a home up without the card mirror: it reads no card tables (the project
need not have any), writes a config without `tables` or `repos`, creates only
`pending-comments.jsonl`, and takes every registered repo with a GitHub origin as a
`releases` source, named after the repo. It cannot be combined with `--repo-map` or
`--create-missing-columns`. Chats, the Message Board, the timer and the wake check are set
up as usual.

Re-running with the same inputs changes nothing. `--dry-run` prints the discovered config
and what it would write, install or refuse, and writes nothing.

### The agent skill

[`skills/basecamp-sync/SKILL.md`](skills/basecamp-sync/SKILL.md) is the operating contract
a firstmate or second mate follows in a home that uses the sync. Install it once for Claude,
Codex and Pi from this checkout:

```sh
for d in ~/.claude/skills ~/.codex/skills ~/.pi/agent/skills; do mkdir -p "$d" && ln -sfn "$PWD/skills/basecamp-sync" "$d/basecamp-sync"; done
```

## Config and state

One config per home, normally `<home>/data/basecamp-sync/config.json`, written by `init`
or by hand from
[`examples/config.example.json`](examples/config.example.json):

- `account`, `project`: Basecamp ids.
- `captain`: the captain's Basecamp person id (assignee, and whose comments are relayed).
- `profile` (optional): the `basecamp` CLI login every call runs as (`-P <profile>`),
  including `run.sh`'s token refresh. Absent means the CLI's default login. It changes
  only who acts; `captain` stays the assignee and the only person whose comments and 👍
  are relayed, so the acting user's own comments and boosts are ignored.
- `chats` (optional): chat (Campfire) ids whose captain questions are relayed.
  An entry may be `{"chat": <id>, "every_line": true}` to relay every owner line in that chat.
- `ask_chat` (optional): the chat id `sync.py ask` posts in.
- `checkins` (optional): `{"questionnaires": ["<questionnaire id>", ...], "timezone":
  "<IANA zone, e.g. America/Chicago>"}`. Use the account's time zone so the schedule's
  hour and minute match Basecamp's.
- `releases` (optional): `{"board": "<message board id>", "repos": {"owner/name": "<name>"
  or {"name": "<name>", "note": "<text>"}}, "prereleases": false}`. The name is the
  subject's first word (capitalized; `init` uses the mapped board name) and the note is
  appended to the body, e.g. `` "note": "Run `ta upgrade` to install." ``.
- `cards` (optional): the card mirror's switch. Absent means on when `tables` is set and
  off otherwise; `false` turns it off even with `tables` present.
- `repos` (card mirror): backlog repo name -> board name.
- `tables` (card mirror): per board, the card table id (`table`) and a column id for each of
  `Triage`, `Not now`, `Figuring it out`, `In progress`, `Ready for QA`, `Done`.

Everything else lives beside the config, never in this repo:

| File | Kept by | Purpose |
| --- | --- | --- |
| `map.json` | the script (card mirror) | task/board -> card id, column, assignment, content digest, linked boards, seen comments and boosts, acknowledgement boosts queued and done, questions replied to |
| `releases.json` | the script | GitHub repo -> when the watch started (`since`), the tags seeded then, and the tags announced (tag -> message id) |
| `chats.json` | the script | chat id -> line cursor, captured question lines, acknowledgement boosts queued and done, questions replied to |
| `checkins.json` | the script | check-in question id -> dates recorded as due (`recorded`) and dates answered (`answered`) |
| `sync.log` | the script | one line per action, plus a counts line per run |
| `pending-comments.jsonl` | the script | captain comments and approvals waiting to be relayed, one JSON record per line (see below) |
| `extra-repos.json` | hand | `{"task": ["board", ...]}`: extra boards for a task |
| `figuring.json` | hand | `{"task": "why"}`: queued tasks that need a plan approved |
| `not-now.json` | hand | `{"task": "why"}`: tasks deferred |
| `skip.json` | hand | `["task", ...]`: tasks never mirrored |
| `boards.json` | hand | `{"task": ["board-owning task", ...]}`: extra tasks whose open Lavish boards are linked on this task's card |
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

## Hourly with systemd by hand

`init` installs a 5-minute timer; to set one up by hand instead,
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

The `basecamp`, `gh` and `lavish-axi` CLIs are stubbed, and `init`'s systemd and check
registration sit behind a fake; tests make no network calls and touch no real home.

## Pending records

Each line of `pending-comments.jsonl` is one JSON object with a `kind`. Records written
before `kind` existed have none; treat a missing `kind` as `"comment"`.

- `comment`: `task`, `repo`, `card`, `comment` (id), `at`, `text`.
- `question`: the same fields as `comment`, for a comment containing `?`.
- `chat-question`: `chat` (id), `line` (id), `url`, `text`, `at`.
- `checkin`: `questionnaire`, `question` (ids), `date` (local, `YYYY-MM-DD`), `title`,
  `url`, `at`. A check-in question came due today; answer it with `sync.py answer`.
- `approval`: `task`, `repo`, `card`, `url` (card URL), `boost` (id), `at`. The captain
  gave the card a 👍: approve every recommendation on it as recommended.

## License

MIT. See [LICENSE](LICENSE).
