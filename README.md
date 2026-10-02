# firstmate-basecamp-sync

Connects an agent home to one Basecamp project through the `basecamp` CLI, in two
layers. No model calls.

## Two layers: tools and behaviors

**Tools** ([`tools.py`](tools.py)) are small, explicit, single-purpose operations with no
policy. Each does one thing to the configured account and project:

| Tool | Kind | What it does |
| --- | --- | --- |
| card comment and 👍 readers | reader | record the captain's new card comments (`comment`, `question`) and their 👍 on an assigned card (`approval`) |
| chat reader | reader | records the captain's chat lines (questions and mentions, or every line) as `chat-question` |
| check-in reader | reader | records each check-in question due today as `checkin` |
| to-do comment reader | reader | records the owner's new comments on tracked open to-dos as `todo-comment` |
| message comment reader | reader | records the owner's new comments on the agent's own recent Message Board posts as `message-comment` |
| boost readers | reader | record the owner's new boosts, with their text, on every monitored surface as `boost` (below) |
| event-feed reader | reader | polls Basecamp's account event feed (`/events.json`) for this project from a saved position and hands each page of thin events to a behavior; records nothing itself |
| unmonitored-event recorder | reader | records an owner event that no behavior handles as `unmonitored`, once per kind of thing (below) |
| `sync.py reply` | command | answers a recorded comment, chat line or to-do comment where it was made, then removes the 👀 |
| `sync.py ask` | command | posts a new chat line @mentioning the owner |
| `sync.py answer` | command | answers a check-in question, once per question per day |
| `sync.py todo create` | command | creates a to-do assigned to the owner, with a description, and tracks it under a key |
| `sync.py todo track` | command | tracks an existing to-do under a key, without relaying its old comments |
| `sync.py todo comment` | command | comments on a tracked to-do |
| `sync.py todo complete` | command | completes a tracked to-do |
| `sync.py post-message` | command | posts a message (subject and body) on the configured message board |
| `sync.py unmonitored list\|handle\|forget` | command | lists the unmonitored-event keys, marks one handled with the owner's decision, or drops one so it is raised again |
| inbox note | primitive | queues a note in a firstmate home's inbox through its `bin/fm-inbox.sh note --request-id` |
| card, Message Board and boost primitives | primitive | create, update, move, assign and unassign a card; post a board message; add an acknowledgement boost |

A reader polls and appends what it finds to `pending-comments.jsonl`, once each, keeping a
cursor or seen-list beside the config; it never acts on what it reads. A command posts
exactly what the agent hands it, and only when the agent runs it.

**Behaviors** ([`behaviors.py`](behaviors.py)) are opt-in workflows a home turns on in its
config, each composed from tools. A behavior whose keys are absent makes no calls at all.
Some have a timer step (run every 5 minutes by `run.sh`); `owner-events` runs in its own
listener service; the others are carried out by the agent with the commands. The agent's side of each is in
[`prompts/base.md`](prompts/base.md).

| Behavior | Config keys | `init` flag | Timer step | Agent side |
| --- | --- | --- | --- | --- |
| `card-mirror` | `tables`, `repos` (off with `"cards": false`) | default (`--no-cards` to leave off) | mirror the backlog; card comment and 👍 readers | relay comments and approvals; `reply` |
| `chat-inbox` | `chats` | default; `--every-line` | chat reader | `reply` to each line |
| `chat-asks` | `ask_chat` | by hand | none | `ask` |
| `release-announcements` | `releases` | default, when the dock has one message board | post one message per new GitHub release | none |
| `checkin-answering` | `checkins` | `--checkins <time zone>` | check-in reader | `answer` |
| `decision-todos` | `todos` | `--todos` | to-do comment reader | `todo create`, `reply`/`todo comment`, `todo complete` |
| `reports` | `message_board` | `--reports` | comment and boost readers on the agent's messages | `post-message`; `reply` to feedback |
| `inbox-delivery` | `inbox` | `--inbox` | deliver each new pending record as a firstmate inbox note | handle the note, then `fm-inbox.sh drain --ack` |
| `owner-events` | `listen` | `--listen` | none: runs in the listener service (`sync.py listen`, below) | records arrive sooner; an `unmonitored` record becomes a decision to-do |

The boundary: behaviors never run a CLI themselves (every Basecamp, GitHub, backlog or
Lavish call goes through a tool), and tools never decide whether to run or what to do
with what they read. `sync.py behaviors --home <home> --config <config.json>` lists which
behaviors a config turns on.

Every piece shares `account`, `project`, `captain` (the owner's person id) and the optional
`profile`. A config without `tables` (or with `"cards": false`) never reads the backlog,
never calls a card or card-table endpoint, and never writes `map.json`; the other
behaviors run the same either way. Set such a home up with `sync.py init --no-cards`.

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

## The owner-event listener

With `"listen": {}` (or `{"interval": <seconds>}`, at least 10; default 30), `sync.py
listen --home <home> --config <config.json>` runs beside the timer as a systemd user
service that `init --listen` installs. Every interval it polls Basecamp's account event
feed (`GET /events.json`, outbound only, through the same CLI login) for the owner's
`chat.line.created`, `comment.created` and `boost.created` events in this project
(filtered by `buckets`, `creators` and `types`), and for each page runs the existing
reader for the surface each event points at:

| Event | Reader run |
| --- | --- |
| an owner chat line | the chat reader, for the configured chats |
| an owner comment | refetches the comment for its parent, then that mirrored card's comment and 👍 readers, that tracked to-do's reader, or the reader for the agent's messages |
| an owner boost | the reader whose state already knows the boosted recording (a card, a to-do, a chat line, a check-in answer, a message); for a recording none has seen yet, every reader that reads boosts except the card mirror's |

Then it records unmonitored events (below) and runs inbox delivery, when that is on. An event is a thin pointer and only says
which reader to run: records, 👀/👍 acknowledgements, seen-lists and inbox notes are the
readers' own, exactly as on the timer, so the agent sees the same records, typically within
about a minute instead of up to five. The feed is best effort by Basecamp's own contract
(polls lag about 30 seconds, and events can be late, duplicated or missed), so the timer
keeps running everything: the card mirror, check-ins, release announcements and its full
read of every surface, which is the repair sweep for anything the feed missed.

The position is kept in `feed.json` and saved only after a page's readers and delivery
have finished; a failed cycle reads the same page again, and the readers' seen-lists make
that harmless. The first poll enters at the present, so no history is replayed. When
Basecamp refuses the saved position it re-enters on its own: a position from before the
feed's epoch (410) at the epoch, and one bound to other filters (409, e.g. after the
`captain` changed) or unrecognized (400) right after the last event it handled. An
invalid filter is never retried. The CLI passes on neither the HTTP status nor the
response's `reason`, so these are told apart by Basecamp's error text. A failed cycle is
retried on the next; the third in a row logs `FAILED listen` once (the wake check sees
it) and recovery is logged. The listener reads the config each cycle and exits cleanly
when `listen` is removed; the service restarts it only after a failure. `--once` runs one
cycle; `--dry-run` reads and logs, and records, boosts and saves nothing. After updating
this checkout, restart the service (`systemctl --user restart basecamp-sync-<home
path>-listen.service`), or re-run `init`.

### Unmonitored events

Unless `listen` has `"unmonitored": false`, the listener also notices the owner doing
something in the project that no enabled behavior handles, so the agent can ask the owner
what to do about it. For that it reads the feed for every event type in Basecamp's catalog
(no `types` filter, so types Basecamp adds later arrive too), still only the owner's
(`creators`) and only this project (`buckets`), at most 20 pages a cycle. After the page's
readers have run, each owner event is checked against what they saw:

| Event | Handled when |
| --- | --- |
| a chat line | `chat-inbox` is on and its reader saw the line (it is in a relayed chat) |
| a comment | it is on a mirrored card, an open tracked to-do, or one of the agent's posts the reports reader knows |
| a boost | a reader's state knows the boosted recording (the same check that picks the reader), or it is on a comment under a mirrored card, which the timer's sweep reads |
| any other type | never: `todo.created`, `todo.completed`, `card.moved`, `message.created`, `question.answer.created`, a comment edit, ... |

Anything else is recorded as an `unmonitored` record (fields below) and delivered like any
other record. It is keyed on the event type and the recording type, e.g.
`todo.created/Todo`, `comment.created/Document` (for a comment, the type of what it is on),
`boost.created/Comment on Upload` or `chat.line.created/Chat::Lines`, and each key is
recorded once: `unmonitored.json` beside the config keeps the keys seen, the first event,
a count of later ones and, once the agent runs `sync.py unmonitored handle --key <key>
--decision <text>`, the owner's decision; the key then stays quiet. `sync.py unmonitored
forget --key <key>` drops it, so the next such event is recorded (and put to the owner)
again; `sync.py unmonitored list` prints the file. The recording is refetched
(`/buckets/<project>/recordings/<id>.json`, or the comment) only for a new key, for the
record's title, excerpt and link. Without a `profile`, or with one that signs in as the
owner, the sync's own writes would be owner events, so nothing is checked (and the feed
stays on the three handled types); a dry run checks nothing either.

The timer run, each listener cycle and each command hold one shared lock (`sync.lock`
beside the config) throughout, so two of them never read and write the state at once; a
listener cycle that comes due during a timer run waits for it.

## Safety bounds

- Only the configured account, project, card tables, chats, check-ins, to-do set and
  message board are touched. The listener only reads (the feed, a comment's parent, an
  unmonitored event's recording) and runs the same readers; it posts nothing beyond their
  👀/👍 acknowledgements.
- Cards are never deleted, trashed or archived. Cards whose task left the backlog are
  left as they are (the run logs how many).
- The sync never posts to chat or as a comment. Its one automatic post is a release
  announcement, and only on the Message Board (below). Otherwise the only thing that
  posts is the explicit `sync.py reply` command below, run by the relaying agent to
  answer a captain question where it was asked: a comment on the card, or a line in the chat.
  Two more explicit commands post, both opt-in: `sync.py ask` (a new chat line for the
  owner) and `sync.py answer` (a check-in answer). The sync run never calls either, nor
  the to-do commands or `sync.py post-message` (below).
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
- Decision to-dos: with `todos` set,
  `sync.py todo create --home <home> --config <config.json> --key <key> --title <text> --body-file <file>`
  creates a to-do assigned to the owner, loose on the configured to-do set
  (`"todos": {"todoset": <id>}`, or the project's only one with `"todos": {}`), or in a
  to-do list (`--list <id>`, or `"todos": {"list": <id>}`), with an optional `--due`. The
  body file is the description in Markdown, rendered by the CLI (bare URLs become links).
  The to-do is tracked in `todos.json` under the key; a key already tracked is refused, so
  a re-run never creates a duplicate. Each run reads the comments (and boosts) of every tracked to-do
  that is not completed and records each owner comment newer than that to-do's cursor as
  a `todo-comment`, once, with the same acknowledgement as a card comment (👀 when it
  contains `?`, 👍 otherwise); other people's comments, the acting user's included, only
  move the cursor. `sync.py reply --recording <comment id>` answers one with a comment on
  the to-do (Markdown) and removes the 👀; `sync.py todo comment --todo <key or id>
  --body-file <file>` posts any other comment; `sync.py todo complete --todo <key or id>`
  completes it, after which its comments are no longer read. `sync.py todo track --key
  <key> --todo <id>` adopts a to-do made some other way: its existing comments are skipped
  and only later ones are recorded. Only tracked to-dos can be commented on or completed;
  to-dos are never deleted, trashed or archived. All of these post nothing with no
  profile or a profile signed in as the owner, and `--dry-run` only logs.
- Reports: with `message_board` set,
  `sync.py post-message --home <home> --config <config.json> --subject <text> --body-file <file>`
  posts one message on that board (Markdown body, rendered by the CLI) as the acting user,
  under the same refusal rules as `reply`. Each run of the command posts a new message;
  messages are never edited or deleted. Each run reads the board's newest messages and,
  for each one the acting user posted in the last 14 days that has comments, records
  every owner comment newer than that message's cursor as a `message-comment`, once,
  acknowledged like a card comment (👀 with `?`, 👍 otherwise). A message has no cursor
  until its first read, so feedback already on a recent post is relayed. `sync.py reply
  --recording <comment id>` answers one with a comment on the message (Markdown) and
  removes the 👀.
- Boosts are answers: a boost can carry short text, and the owner's boost on anything a
  behavior monitors is recorded once as a `boost` with its text, the boosted recording and
  the surface. The reads stay bounded: a recording's boosts are read only when its
  `boosts_count` changes, except the few read every run. The surfaces:
  `chat` (every line in the newest page of each relayed chat, the agent's own lines included);
  `card` (an assigned card, read every run as for approvals; a 👍 is still an `approval`, now with its `text`);
  `card-comment` (every comment on a mirrored card);
  `todo` (a tracked open to-do, read every run) and `todo-comment` (its comments);
  `checkin-answer` (the agent's own check-in answers from the last 7 days);
  `message` and `message-comment` (the agent's own posts on `message_board` from the last
  14 days, and their comments). The first time a surface's state has no boost counts,
  they are seeded without recording, so turning this on (or upgrading) never replays old
  boosts. The acting user's own boosts are never recorded.
- Inbox delivery: with `inbox` set (`{}` for the `--home`, or `{"fm_home": "<home>"}`),
  each run ends by delivering every record appended to `pending-comments.jsonl` since the
  last delivery as a note through `<fm_home>/bin/fm-inbox.sh note --request-id
  basecamp-<kind>-<id> --json -` (FM_HOME set to that home). The note body is short and
  plain: what it is and who wrote it, the text, the Basecamp link and how to handle it.
  The request id (the comment, line or boost id; question and date for a check-in) makes a
  replay return the original note rather than a new one. Records go in order from a
  line cursor in `inbox.json`; the first run starts after the records that existed
  before it. A failed note (any non-zero exit, including 3, "saved but not woken") stops
  that run's delivery, is logged as `FAILED inbox note <id>` once, and is retried next
  run; it never fails the sync. `pending-comments.jsonl` stays the record either way,
  and a home without `inbox` keeps the file-plus-wake-check flow unchanged. With
  `init --inbox` the wake check watches only FAILED lines, since each note is its own wake.
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

Four opt-in flags turn on more behaviors from the project's dock, each refusing when the
dock lacks what it needs: `--todos` (`todos` with the one enabled to-do set),
`--reports` (`message_board`, the one enabled message board), `--every-line` (every
discovered chat as `{"chat": <id>, "every_line": true}`) and `--checkins <IANA time zone>`
(`checkins` with the one enabled Automatic Check-ins questionnaire). Without them the
config is the same as before they existed, so re-running `init` on an older home changes
nothing. `--no-releases` leaves `releases` out. `--inbox` adds `"inbox": {}` and installs
the failures-only wake check. `--listen` adds `"listen": {}` and installs, enables and
starts `basecamp-sync-<home path>-listen.service` (`Type=simple`, `Restart=on-failure`
after 30s, running this checkout's `sync.py listen`), restarting it when its unit
changed; without `listen` in the config no service is installed.

`--no-cards` sets a home up without the card mirror: it reads no card tables (the project
need not have any), writes a config without `tables` or `repos`, creates only
`pending-comments.jsonl`, and takes every registered repo with a GitHub origin as a
`releases` source, named after the repo. It cannot be combined with `--repo-map` or
`--create-missing-columns`. Chats, the Message Board, the timer and the wake check are set
up as usual.

Re-running with the same inputs changes nothing. `--dry-run` prints the discovered config
and what it would write, install or refuse, and writes nothing.

### The base prompt and the agent skill

[`prompts/base.md`](prompts/base.md) is a base prompt that, given a fresh firstmate home,
this checkout and a Basecamp project URL, sets the home up and re-establishes every
behavior: which to turn on, how to handle each pending record, the decision to-do
lifecycle, chat replies, check-in answers, reports and the safety bounds. Hand it to the
agent once, with `SYNC`, `HOME` and the project URL filled in.

[`skills/basecamp-sync/SKILL.md`](skills/basecamp-sync/SKILL.md) is the operating contract
a firstmate or second mate follows in a home that uses the sync, and points to the base
prompt. Install it once for Claude, Codex and Pi from this checkout:

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
- `todos` (optional): `{}`, `{"todoset": "<to-do set id>"}` or `{"list": "<to-do list id>"}`:
  where `sync.py todo create` puts the owner's to-dos (loose on the set, or in the list);
  turns on the to-do comment reader.
- `message_board` (optional): the message board id `sync.py post-message` posts on; the
  owner's boosts on the agent's messages there are relayed.
- `inbox` (optional): `{}` or `{"fm_home": "<firstmate home>"}`: deliver each new pending
  record as a note in that home's inbox (default: the `--home`).
- `listen` (optional): `{}` or `{"interval": <seconds>}`: run the owner-event listener
  (`sync.py listen`), polling the event feed every `interval` seconds (default 30).
  `"unmonitored": false` turns off unmonitored-event records and keeps the feed on the
  three handled types.
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
| `inbox.json` | the script | the line cursor of `pending-comments.jsonl` delivered to the inbox, and request ids whose failure was logged |
| `messages.json` | the script | per agent message: comment cursor, owner comments recorded, acknowledgement boosts queued and done, comments replied to; boost counts and boosts seen on the messages and their comments |
| `feed.json` | the listener | the event feed's position, the last event id handled and the filters they belong to |
| `unmonitored.json` | the listener | unmonitored-event key -> the first event and recording recorded, when, how many were seen, and the owner's decision once handled |
| `sync.lock` | the script | the shared state lock held by a timer run, a listener cycle or a command |
| `todos.json` | the script | to-do key -> to-do id, title, URL, created and completed times, comment cursor, owner comments recorded, acknowledgement boosts queued and done, comments replied to |
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

`tests/test_sync.py` covers the existing behaviors and is unchanged by the layer split;
`tests/test_tools.py` covers the to-do and message tools, the to-do comment reader, the
boost readers on every surface, inbox delivery (with `fm-inbox.sh` stubbed), the
new commands and the layer boundary; `tests/test_listen.py` covers the listener with
stubbed `/events.json` pages: entry and resume, `next`, position saved only after a page
is handled, 409/410/400 re-entry, dispatch to each reader, no duplicate record when a
timer sweep runs during a listener cycle, and unmonitored events: detection per event
kind, one record per key until forgotten, handled events recording nothing, and the
narrow feed when it is off. The `basecamp`, `gh` and `lavish-axi` CLIs are stubbed, and `init`'s systemd and check
registration sit behind a fake; tests make no network calls and touch no real home.

## Pending records

Each line of `pending-comments.jsonl` is one JSON object with a `kind`. Records written
before `kind` existed have none; treat a missing `kind` as `"comment"`.

- `comment`: `task`, `repo`, `card`, `comment` (id), `at`, `text`.
- `question`: the same fields as `comment`, for a comment containing `?`.
- `chat-question`: `chat` (id), `line` (id), `url`, `text`, `at`.
- `checkin`: `questionnaire`, `question` (ids), `date` (local, `YYYY-MM-DD`), `title`,
  `url`, `at`. A check-in question came due today; answer it with `sync.py answer`.
- `approval`: `task`, `repo`, `card`, `url` (card URL), `boost` (id), `text`, `at`. The captain
  gave the card a 👍: approve every recommendation on it as recommended.
- `todo-comment`: `key`, `todo` (id), `comment` (id), `question` (true when it contains
  `?`), `url` (the comment), `text`, `at`. The owner commented on a tracked to-do; answer
  with `sync.py reply --recording <comment>`.
- `message-comment`: `message` (id), `subject`, `comment` (id), `question`, `url` (the
  comment), `text`, `at`. The owner commented on a post the agent made, usually feedback
  or an instruction on a report; act on it and answer with `sync.py reply --recording <comment>`.
- `boost`: `surface` (`chat`, `card`, `card-comment`, `todo`, `todo-comment`,
  `checkin-answer`, `message`, `message-comment`), the surface's ids (`chat`; `task`,
  `repo`, `card`; `key`, `todo`; `question`; `message`, `subject`), `recording` (the
  boosted recording), `boost` (id), `text` (the boost's text), `url`, `at`. The owner's
  boost is an answer to what was boosted, like a comment.
- `unmonitored`: `key` (`<event type>/<recording type>`), `event_type`, `recording_type`,
  `event` (id), `recording` (id), `title` (for a comment, of what it is on), `text` (an
  excerpt), `creator` (`id`, `name`), `url`, `at`. The owner did something no enabled
  behavior handles; put it to them as a decision to-do asking how events like it should be
  handled, act on the answer, then `sync.py unmonitored handle --key <key> --decision <text>`.

## License

MIT. See [LICENSE](LICENSE).
