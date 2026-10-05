# basecamp-mate

> Formerly `firstmate-basecamp-sync`. GitHub redirects the old repository URL; existing
> installs keep working unchanged (the `basecamp-sync` skill, config keys and systemd unit
> names are the same).

Source: <https://github.com/calebl/basecamp-mate>

## Getting started

You need a Linux computer where your firstmate agent runs, and a Basecamp project. Open a
terminal and run:

```sh
git clone https://github.com/calebl/basecamp-mate.git
basecamp-mate/bin/basecamp-mate setup
```

Setup walks you through everything, one question at a time:

1. It checks for what it needs and offers to install the Basecamp command-line tool if it
   is missing.
2. It asks where your firstmate home is.
3. It signs the agent in to Basecamp: you open a link and type a code. Sign in as the
   agent's **own** Basecamp person (invite one to your project first, with an email you
   control), not as yourself. [More on the agent's account](docs/firstmate-account.md).
4. It lists your projects; pick one by its number. Then pick yourself from the people on it.
5. It asks a few yes/no questions. Press Enter to take the suggested answer: the agent
   reads your chat questions, asks you for decisions as to-dos and answers check-ins. It
   reads its own Basecamp notifications every 30 seconds, so it notices what you write to
   it within about half a minute. Card tables, release announcements, Message Board
   reports and Pings stay off unless you say yes.
6. It turns on what the project needs, starts the background services, runs one test sync
   and tells you what is on.

To change your answers later, run `setup` again. If something stops working, run:

```sh
basecamp-mate/bin/basecamp-mate doctor
```

It checks each piece (the Basecamp tool, the sign-in, the project, the card tables, the
background services, the last sync, the agent's wake-up) and prints each problem in plain
words with the exact fix.

Everything below is the detailed reference.

## What it is

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
| notification reader | reader | reads the agent login's notifications (`/my/readings.json`: every project, one call) and hands each one that changed to a behavior; records nothing itself |
| boost reader | reader | reads the boosts on the agent login's own recordings (`/my/boosts.json`, one call) and hands each new one to a behavior; records nothing itself |
| mark read | primitive | marks the agent login's notifications read (`basecamp notifications read`, i.e. `PUT /my/unreads.json`) |
| Ping reader | reader | finds the Pings (direct messages) the agent's login is in with the owner through `/my/readings.json` and records each new line the owner writes there as `ping` |
| thread reader | reader | records the owner's new comments on any other thread the agent's login follows or is @mentioned in (a card whatever its backlog state, a document, an upload, someone else's to-do or message) as `thread-comment` or `mention`, and a mention in a chat nothing relays as `chat-question` |
| to-do request readers | reader | find the open to-dos in the project (or, with `"scope": "account"`, any project of the account) assigned to the agent's login (`/my/assignments.json`, or a to-do event) and record each one a listened-to person assigned as `todo-request`, once; then its edits and closing as `todo-request-update` and `todo-request-closed` |
| message comment reader | reader | records the owner's new comments on the agent's own recent Message Board posts as `message-comment` |
| boost readers | reader | record the owner's new boosts, with their text, on every monitored surface as `boost` (below) |
| unmonitored recorder | reader | records owner input that no behavior handles as `unmonitored`, once per kind of thing (below) |
| `sync.py reply` | command | answers a recorded comment, chat line, Ping line, to-do comment, thread comment or mention where it was made, then removes the 👀 |
| `sync.py ask` | command | posts a new chat line @mentioning the owner |
| `sync.py answer` | command | answers a check-in question, once per question per day |
| `sync.py todo create` | command | creates a to-do assigned to the owner, with a description, and tracks it under a key |
| `sync.py todo track` | command | tracks an existing to-do under a key, without relaying its old comments |
| `sync.py todo comment` | command | comments on a tracked to-do |
| `sync.py todo complete` | command | completes a tracked to-do (a decision to-do, or a to-do request when the work is done) |
| `sync.py post-message` | command | posts a message (subject and body) on the configured message board |
| `sync.py unmonitored list\|handle\|forget` | command | lists the unmonitored-event keys, marks one handled with the owner's decision, or drops one so it is raised again |
| inbox note | primitive | queues a note in a firstmate home's inbox through its `bin/fm-inbox.sh note --request-id` |
| card, Message Board and boost primitives | primitive | create, update, move, assign and unassign a card; post a board message; add an acknowledgement boost |

A reader polls and appends what it finds to `pending-comments.jsonl`, once each, keeping a
cursor or seen-list beside the config; it never acts on what it reads. A command posts
exactly what the agent hands it, and only when the agent runs it.

**Behaviors** ([`behaviors.py`](behaviors.py)) are opt-in workflows a home turns on in its
config, each composed from tools. A behavior whose keys are absent makes no calls at all.
Most have a timer step; the others are carried out by the agent with the commands. The
agent's side of each is in [`prompts/base.md`](prompts/base.md).

The timer runs `run.sh` every 30 seconds, and each behavior's step runs when it is due
(`timer.json` keeps when each last ran): `notifications` and `inbox-delivery` every run;
`card-mirror`, `release-announcements`, `checkin-answering` and `assigned-todos` every 5
minutes; and `chat-inbox`, `decision-todos`, `reports` and `pings` hourly, as the repair
sweep behind `notifications`, which reads their surfaces as soon as something changes.
Without `notifications` (no `profile`, or `"notifications": false`) those four run every
5 minutes instead. `sync.py --all` runs every step at once.

| Behavior | Config keys | `init` flag | Timer step | Agent side |
| --- | --- | --- | --- | --- |
| `notifications` | `profile` (off with `"notifications": false`) | default | every run: the notification and boost readers, then the reader for each thread that changed; mark read | records arrive within about 30 seconds; an `unmonitored` record becomes a decision to-do |
| `card-mirror` | `tables`, `repos` (off with `"cards": false`) | default (`--no-cards` to leave off) | mirror the backlog; card comment and 👍 readers (5 min) | relay comments and approvals; `reply` |
| `chat-inbox` | `chats` | default; `--every-line` | chat reader (hourly sweep) | `reply` to each line |
| `chat-asks` | `ask_chat` | by hand | none | `ask` |
| `release-announcements` | `releases` | default, when the dock has one message board | post one message per new GitHub release | none |
| `checkin-answering` | `checkins` | `--checkins <time zone>` | check-in reader (5 min) | `answer` |
| `decision-todos` | `todos` | `--todos` | to-do comment reader (hourly sweep) | `todo create`, `reply`/`todo comment`, `todo complete` |
| `assigned-todos` | `assigned_todos` | `--assigned-todos` (`--assigned-todos-anywhere`: account-wide) | to-do request readers, then the to-do comment reader for the requests (5 min) | take each request on; `todo comment`/`reply`; `todo complete` when done |
| `reports` | `message_board` | `--reports` | comment and boost readers on the agent's messages (hourly sweep) | `post-message`; `reply` to feedback |
| `pings` | `pings` | `--pings` | Ping reader (hourly sweep) | `reply` to each line, in the Ping |
| `inbox-delivery` | `inbox` | `--inbox` | deliver each new pending record as a firstmate inbox note | handle the note, then `fm-inbox.sh drain --ack` |

The boundary: behaviors never run a CLI themselves (every Basecamp, GitHub, backlog or
Lavish call goes through a tool), and tools never decide whether to run or what to do
with what they read. `sync.py behaviors --home <home> --config <config.json>` lists which
behaviors a config turns on and how often each runs.

Every piece shares `account`, `project`, `captain` (the owner's person id) and the optional
`profile`. A config without `tables` (or with `"cards": false`) never reads the backlog,
never calls a card or card-table endpoint, and never writes `map.json`; the other
behaviors run the same either way. Set such a home up with `sync.py init --no-cards`.

## People the sync listens to

By default the sync listens only to the captain. The optional `people` list (Basecamp
person ids, set with `sync.py init --listen-to <person>`) adds others: every reader that
relays the captain (chat lines, every-line chats, card, to-do, message and thread
comments, mentions, boosts on every surface, Pings, to-do requests and unmonitored input)
then relays them too. The captain is always on the list, whether or not
`people` names them, and the acting login never is: its own lines only move cursors.
"Owner" below means anyone on the list.

Every record says who wrote it (`author`: `id`, `name`) and whether that is the captain
(`captain`: true or false), and so does its inbox note ("from the captain" or "from
<name> (not the captain)"). Authority stays with the captain alone:

- only the captain's 👍 on an assigned card is an `approval`; anyone else's is a `boost`;
- only the captain's comment or boost on a decision to-do is the decision; another
  person's is input to weigh, and the to-do stays open for the captain;
- a check-in question's instruction is the captain's only when they wrote it;
- decision to-dos, assigned cards and `sync.py ask` mentions are still the captain's alone.

A config with only `captain` behaves exactly as before.

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

## Notifications

The agent hears the owner through its own Basecamp login's notifications, the same list
as the bell in Basecamp's web app. They are durable: an item stays until it is read, so a
missed run loses nothing, and one call covers every project. With a `profile` (unless
`"notifications": false`), every timer run, every 30 seconds, makes two reads as the
acting login:

- `GET /my/readings.json`: the unread notifications and the first page of read ones.
- `GET /my/boosts.json`: the boosts on the login's own recordings (its chat lines,
  comments, to-dos, cards, messages and check-in answers). This endpoint works but is not
  in Basecamp's API docs.

Without a `profile`, or with one that signs in as the owner, nothing is read (the
notifications would be the owner's own) and the readers that `notifications` takes over
run every 5 minutes, as before.

A notification is a pointer to a whole thread ("this to-do has 2 new comments", "this
chat has a new line"), not to each comment or line. `notifications.json` keeps a cursor
per item (its id and `unread_at`). For each item whose cursor moved, the existing reader
for that thread runs once, records what is new by its own cursor and acknowledges it,
exactly as on the timer, so records, 👀/👍, seen-lists and inbox notes are the readers'
own:

| Notification | Reader run |
| --- | --- |
| a project chat in `chats` has a new line | the chat reader, for that chat; an @mention of the agent there, or a Campfire reply to one of its lines (which carries no mention), is recorded even without `?` |
| a Ping with an owner in it (with `pings` on) | the Ping reader, for that Ping |
| a comment or @mention on a mirrored card | that card's comment and 👍 readers, also for a card whose task has left the backlog (the mirror itself leaves such a card as it is) |
| a comment or @mention on an open tracked to-do (a decision to-do or a request) | that to-do's comment reader |
| a comment on one of the agent's own messages (with `reports` on) | that message's reader, whatever its age |
| a comment or @mention on any other thread in the project (a document, an upload, someone else's to-do or message, a card nothing mirrors) | the thread reader: `thread-comment` or `mention` records |
| an @mention of the agent in a chat nothing relays | a `chat-question` for that line |
| `Assignment` or `Completion` of a to-do (with `assigned_todos` on; in any project with `"scope": "account"`) | the to-do request reader, or the request's edit and closing check; the notification says who assigned or completed it |
| a check-in `Reminder` (with `checkins` on) | the check-in reader: a faster signal than the 5-minute schedule check |

The agent's login hears what it is subscribed to: what it created, commented on or was
assigned, plus anything it was @mentioned in, replied to or Pinged in. To have it hear
another thread without mentions (another chat, a check-in's answers), subscribe its
person to it once in Basecamp.

Each new owner boost in `/my/boosts.json` runs the reader whose state holds the boosted
recording (the chat, Ping, mirrored card, tracked to-do, message, check-in answers or
followed thread), which records it as a `boost` (or the captain's 👍 on an assigned card as
an `approval`). A boost on a to-do request, which the owner created, is not listed there;
the `assigned-todos` sweep reads those every 5 minutes.

**Mark read.** Once an item is handled with no failed Basecamp call, it is marked read
(`basecamp notifications read <ids>`), after its records are written; `"notifications":
{"mark_read": false}` leaves everything unread. This keeps the unread list under
Basecamp's cap of 100 and the bell honest. The cursor, never the read flag, decides what is
new, so someone marking everything read in a browser loses nothing. An item whose reader
had a failed call keeps its old cursor and is not marked: the next run retries it, and the
readers' own cursors make that harmless. A failed mark is retried next run.

**Scope.** Items in other projects are another home's: they are never read, acted on or
marked read here, except a Ping (with `pings` on) and a to-do request with `"scope":
"account"`. Basecamp's own items (`BoostReport`, `Bulletin`, `Onboarding`) are ignored.

The first run only seeds the cursors and the boosts seen, so no history is replayed; the
repair sweep reads the configured surfaces the same run. A dry run reads and logs, and
records, boosts, saves and marks nothing.

**The repair sweep.** Notifications cannot see edits, card or to-do moves, or a thread the
login stopped following, so the readers they take over still run on the timer: the chat,
decision to-do, report and Ping readers hourly, and the `assigned-todos` sweep (new
assignments, every open request's comments, boosts, edits and closing) every 5 minutes.

### Unmonitored input

Unless `"notifications": {"unmonitored": false}`, owner input in the project that no
enabled behavior handles is recorded so the agent can ask the owner what to do about it:

| Notification or boost | Key |
| --- | --- |
| a line in a project chat not in `chats` | `chat.line.created/Chat::Lines` |
| a to-do assigned to the agent, or completed, with `assigned_todos` off | `todo.assignment_changed/Todo`, `todo.completed/Todo` |
| an owner boost on one of the agent's recordings nothing monitors | `boost.created/<type>`, e.g. `boost.created/Comment on Document` |
| a kind of notification Basecamp adds later | `<kind>/<recording type>` |

Comments and @mentions are never unmonitored: whatever they are on, a reader records them.
Each key is recorded once as an `unmonitored` record (fields below) and delivered like any
other record; the keys keep the event feed's vocabulary, so decisions recorded before
notifications replaced it still apply. `unmonitored.json` beside the config keeps the keys
seen, the first notification (or boost), a count of later ones and, once the agent runs
`sync.py unmonitored handle --key <key> --decision <text>`, the owner's decision; the key
then stays quiet. `sync.py unmonitored forget --key <key>` drops it, so the next one is
recorded (and put to the owner) again; `sync.py unmonitored list` prints the file. The
notification's own title, excerpt and link fill the record; the recording is refetched only
when its type is unknown.

The timer run and each command hold one shared lock (`sync.lock` beside the config)
throughout, so two of them never read and write the state at once.

### Upgrading from the event listener

Earlier versions polled Basecamp's event feed from a `sync.py listen` service beside the
timer (`"listen"` in the config). Notifications replaced it: they reached the agent in
0.5-11 seconds in the owner's tests, against 31-40 seconds for the feed, and never need
re-entering. A config that still has `"listen"` keeps working: the key is ignored, with one
note in `sync.log` (its `"unmonitored": false` still applies; move it to
`"notifications"`). `sync.py listen` only logs that and exits 0, so a listener service
left running stays down. Re-run `init` (or `basecamp-mate setup`) to remove the service and
drop `"listen"` from the config; `init` does both without `--force`. `basecamp-mate doctor`
flags a listener service still installed. The old `feed.json`, `pings-feed.json` and
`requests-feed.json` are no longer read and can be deleted.

## Pings

A Ping (a direct message) is a chat in a bucket of its own (a "circle"), not in the
project. With `"pings": {}` (`init --pings`), a new line in a Ping makes its notification
change, and the Ping reader runs for that Ping in the same run. The reader's own sweep
(hourly with notifications) reads `GET /my/readings.json` as the
acting login: its `pings` section lists each recently active Ping (read or unread) with
the bucket and chat ids in its `subscription_url`. Only Pings whose participants or
creator include the owner are read, at most `limit` a run (`{"limit": <n>}`, default 10),
the most recently active first; a quiet Ping drops out of the readings and a new line
brings it back. For each, the chat's lines are read
(`/buckets/<circle>/chats/<chat>/lines.json`) and every new owner line is recorded once
as a `ping` record and acknowledged with a 👀, like a chat question; the owner's boosts
on lines there are `boost` records with surface `ping`. `pings.json` keeps `since` (the
first run) and a line cursor per Ping chat: history from before `since` is never relayed,
while a Ping that starts later is relayed from its first line. The agent answers with
`sync.py reply --recording <line>`, which posts a new line in that Ping (the `basecamp
chat` commands refuse circle buckets, so it posts to the lines endpoint) and removes the
👀, under the same refusal rules as every reply. Without a `profile`, or with one that
signs in as the owner, nothing is read: the readings would be the owner's own.

## To-do requests

With `"assigned_todos": {}` (`init --assigned-todos`), a to-do a listened-to person
creates or reassigns in the project so that the agent's login is an assignee is a request
to the agent: it is recorded once as a `todo-request` (title, description, link, assignees
and who assigned it), acknowledged with a 👀 on the to-do, and tracked in `todos.json` as
`request-<to-do id>`. From then on it is read like a tracked to-do: comments by the people
listened to arrive as `todo-comment` records and their boosts as `boost` records, both
with `"request": true` and the to-do's `title`; an edit to its name or description arrives as
`todo-request-update` with the new text; and its closing as `todo-request-closed`, with a
`reason`: `completed` (by anyone; who, when Basecamp says), `trashed`, `archived` or
`unassigned` (no longer assigned to the agent). A closed request is no longer read. The
agent answers with `sync.py reply --recording <comment>` or `sync.py todo comment --todo
request-<id>`, and closes the request itself with `sync.py todo complete --todo
request-<id>` when the work is done; those to-do commands work on a request with or without
`todos` in the config. A closed request assigned to the agent again is a new request.
To-dos assigned to anyone else are not requests, and with `assigned_todos` on an
assignment or completion is never unmonitored.

Who assigned it decides how the agent treats it: from the captain it is captain work; from
another listed person it is information or a request to weigh, never a captain decision.

**Anywhere in the account.** With `"assigned_todos": {"scope": "account"}` (`init
--assigned-todos-anywhere`), a to-do assigned to the agent's login in *any* project of the
account is a request, not just one in the configured project. Every record of a request
(`todo-request`, `todo-request-update`, `todo-request-closed`, and its `todo-comment` and
`boost` records) carries `project`: `{"id", "name"}` of the project the to-do is in, and
the inbox note names a project other than the configured one, so the agent can route the
work. Everything later read or posted on it (comments, boosts, 👀, `reply`, `todo
comment`, `todo complete`) goes to that project. Notifications act on an assignment or
completion in any project, and on comments on tracked requests there (nothing else in
another project); the timer's sweep reads every project's assignments. The default scope,
`"project"`, is unchanged: only the configured project's to-dos.

Run the account-wide scope in **one home per Basecamp login**: the main home (firstmate's
own), which routes each request to the home or domain that owns its project. Every other
home on that login keeps `"assigned_todos": {}`, or leaves it off; two homes running it
would each relay the same to-do. `basecamp-mate doctor` flags two homes on this computer
that run it with the same account and `profile`; it cannot see homes on other computers.

Notifications see the assignment (`Assignment`), comments and completion (`Completion`)
within about 30 seconds, and say who assigned or completed it. The timer's sweep, every 5
minutes, is the backup and the only reader of a request's edits and of the owner's boosts
on it, which raise no notification: one read of `GET
/my/assignments.json` as the acting login (the agent's open assignments in every project,
filtered to this one unless the scope is `account`) finds a to-do the notifications missed, attributed to its creator, since
Basecamp does not say who assigned it; at most `limit` new to-dos are fetched a sweep
(`{"limit": <n>}`, default 10), and a to-do whose creator is not listened to is fetched
once and remembered in `assigned-todos.json`. Each sweep then reads every open request's
comments, boosts and the to-do itself, for edits and closing. Without a `profile`, or with
one that signs in as the owner, nothing is read: the assignments would be the owner's own.

## Safety bounds

- Only the configured account, project, card tables, chats, check-ins, to-do set and
  message board are touched, and, with `pings` on, the Pings the acting login is in with
  the owner; with `assigned_todos` on, the project's to-dos assigned to the acting login are
  read and acknowledged with a 👀 (with `"scope": "account"`, those in any project of the
  account, and the comments and boosts on them). Notifications and boosts are read for the
  whole account, but only this project's items (and those Pings and requests) are acted on;
  in the project, any thread the acting login follows or is @mentioned in is read for the
  owner's comments, and `reply` answers there. Their only writes are the readers' 👀/👍
  acknowledgements and marking handled notifications read; never another home's.
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
login. `--listen-to <person>` (repeatable: a person id, email or exact name on the
project) adds someone the sync listens to besides the captain, written as `people` (the
captain first); a name or email matching no one or several people, or the login itself,
is refused. `--repo-map <table>=<repo>` (repeatable) maps a table whose title is not a
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
  `.timer` (every 30 seconds, 240s cap), running this checkout's `run.sh`;
- stops, disables and removes `basecamp-sync-<home path>-listen.service`, the retired
  event listener, if an earlier version installed it;
- writes `<home>/state/basecamp-sync.check.sh` and registers it with the home's own
  `bin/fm-check-register.sh`, so the home wakes on new pending records and failed runs.

Four opt-in flags turn on more behaviors from the project's dock, each refusing when the
dock lacks what it needs: `--todos` (`todos` with the one enabled to-do set),
`--reports` (`message_board`, the one enabled message board), `--every-line` (every
discovered chat as `{"chat": <id>, "every_line": true}`) and `--checkins <IANA time zone>`
(`checkins` with the one enabled Automatic Check-ins questionnaire). Without them the
config is the same as before they existed, so re-running `init` on an older home changes
nothing. `--no-releases` leaves `releases` out. `--inbox` adds `"inbox": {}` and installs
the failures-only wake check. `--listen` is still accepted and does nothing (notifications
replaced the event listener); a config that differs from the discovered one only in a
`"listen"` key is rewritten without it, no `--force` needed. `--pings` adds
`"pings": {}`: the owner's Pings to the `--login` are relayed (see Pings).
`--assigned-todos` adds `"assigned_todos": {}`: a to-do the owner assigns to the
`--login` is a request (see To-do requests). `--assigned-todos-anywhere` adds
`"assigned_todos": {"scope": "account"}` instead: one in any project of the account is (only
in the main home; see To-do requests).

`--no-cards` sets a home up without the card mirror: it reads no card tables (the project
need not have any), writes a config without `tables` or `repos`, creates only
`pending-comments.jsonl`, and takes every registered repo with a GitHub origin as a
`releases` source, named after the repo. It cannot be combined with `--repo-map` or
`--create-missing-columns`. Chats, the Message Board, the timer and the wake check are set
up as usual.

`--no-chats` leaves the dock's chats out (no chat inbox). `--no-keyring` (on by default when
`BASECAMP_NO_KEYRING` is set in the environment `init` runs in) adds
`Environment=BASECAMP_NO_KEYRING=1` to the units, so every `basecamp` call they make uses
the CLI's file credential store and never waits on a locked system keyring.

Re-running with the same inputs changes nothing. `--dry-run` prints the discovered config
and what it would write, install or refuse, and writes nothing.

### Guided setup and doctor

`bin/basecamp-mate setup` ([`setup_home.py`](setup_home.py), also `sync.py setup`) is the
guided front end to `init` for people: it checks Python, the `basecamp` CLI (offering its
installer) and systemd user services; asks for the home; signs the login in with
`basecamp profile create <login> --device-code` (or `auth login -P <login> --device-code`)
under `BASECAMP_NO_KEYRING=1`; lists the login's accounts and projects to pick from and the
project's people to pick the captain from; asks the yes/no questions (chat inbox, decision
to-dos, check-ins on; card mirror, releases, reports, Pings off by default) and the
check-in time zone; enables or adds the dock tool each chosen behavior needs (to-do set,
questionnaire, chat, message board) and, for the card mirror, a card table per registered
repo that has none; then runs `init` itself (with `--create-missing-columns` only for the card
mirror, `--force` after asking when the home already has a config, and `--no-keyring`), runs
`run.sh` once as a test sync, and prints what is on and how to change it.
`--answers <file.json>` answers by key (`home`, `login`, `account`, `project` (id or URL),
`captain` (person id), `timezone`, `replace`, `install_cli`, and booleans `chat`, `todos`,
`checkins`, `cards`, `releases`, `reports`, `pings`, `inbox`) and `--yes` takes the
defaults; without a terminal, anything that needs a person (a sign-in, an unanswered pick)
refuses with what to do instead of asking.

`bin/basecamp-mate doctor` ([`doctor.py`](doctor.py), also `sync.py doctor`) checks Python
3.11+, the `basecamp` CLI and its version, systemd user services, then per home (every home
with installed `basecamp-sync-*` units, or `--home`): the firstmate home, the config, the
login (`auth status` under a 20-second timeout and `BASECAMP_NO_KEYRING=1`, renewing an
expired token as `run.sh` would), the project and the dock tools the config names, each card
table's columns by id and title, the agent's notifications (`/my/readings.json` readable,
and fewer than Basecamp's 100 unread), the timer units (installed, pointing at files that
exist, enabled and active) and a leftover listener service, the last `sync.log` line
(FAILED, or older than 20 minutes),
and the wake check's registration. Each problem prints with its fix; it exits 1 when anything
is broken.

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
- `captain`: the captain's Basecamp person id: the assignee, and the only person whose
  word is a decision or approval.
- `people` (optional): the Basecamp person ids whose lines, comments and boosts are
  relayed; the captain is always included. Default: just the captain. See "People the
  sync listens to".
- `profile` (optional): the `basecamp` CLI login every call runs as (`-P <profile>`),
  including `run.sh`'s token refresh. Absent means the CLI's default login. It changes
  only who acts; `captain` stays the assignee and the only person whose 👍 approves, and
  the acting user's own comments and boosts are never relayed.
- `todos` (optional): `{}`, `{"todoset": "<to-do set id>"}` or `{"list": "<to-do list id>"}`:
  where `sync.py todo create` puts the owner's to-dos (loose on the set, or in the list);
  turns on the to-do comment reader.
- `message_board` (optional): the message board id `sync.py post-message` posts on; the
  owner's boosts on the agent's messages there are relayed.
- `inbox` (optional): `{}` or `{"fm_home": "<firstmate home>"}`: deliver each new pending
  record as a note in that home's inbox (default: the `--home`).
- `notifications` (optional): on whenever `profile` is set. `false` turns it off;
  `{"mark_read": false}` leaves handled notifications unread; `{"unmonitored": false}`
  turns off `unmonitored` records. See Notifications.
- `listen` (retired): ignored, with one note in `sync.log`; its `"unmonitored": false` still
  applies. See "Upgrading from the event listener".
- `pings` (optional): `{}` or `{"limit": <n>}`: relay the owner's Pings (direct messages)
  to the acting login, reading at most `n` Pings a run (default 10).
- `assigned_todos` (optional): `{}` or `{"limit": <n>, "scope": "project" | "account"}`:
  treat a to-do a listened-to person assigns to the acting login as a request, fetching at
  most `n` newly assigned to-dos a sweep (default 10); `"scope": "account"` takes them from
  every project of the account (one home per login), the default `"project"` only from this
  one. See To-do requests.
- `chats` (optional): chat (Campfire) ids whose owner questions are relayed.
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
| `chats.json` | the script | chat id -> line cursor, captured question lines, acknowledgement boosts queued and done, questions replied to (a chat nothing relays holds only the mentions recorded there) |
| `checkins.json` | the script | check-in question id -> dates recorded as due (`recorded`) and dates answered (`answered`) |
| `inbox.json` | the script | the line cursor of `pending-comments.jsonl` delivered to the inbox, and request ids whose failure was logged |
| `messages.json` | the script | per agent message: comment cursor, owner comments recorded, acknowledgement boosts queued and done, comments replied to; boost counts and boosts seen on the messages and their comments |
| `notifications.json` | the script | per notification id, its cursor (`unread_at` when last handled); the boost ids seen in `/my/boosts.json`; ids whose mark-read failed, to retry |
| `timer.json` | the script | when each behavior's timer step last ran (`ran`), for its cadence |
| `threads.json` | the script | per followed thread (`<bucket>:<recording>`): its type and title, comment cursor, owner comments recorded, acknowledgement boosts queued and done, boost counts and boosts seen, comments replied to |
| `pings.json` | the script | `since` (the Ping reader's first run) and, per Ping chat id, its bucket, title, URL, line cursor, owner lines recorded, acknowledgement boosts queued and done, boost counts and boosts seen, lines replied to |
| `unmonitored.json` | the script | unmonitored key -> the first notification (or boost) and recording recorded, when, how many were seen, and the owner's decision once handled |
| `sync.lock` | the script | the shared state lock held by a timer run or a command |
| `todos.json` | the script | to-do key -> to-do id, title, URL, created and completed times, comment cursor, owner comments recorded, acknowledgement boosts queued and done, comments replied to; for a to-do request (`request-<id>`), also who assigned it (`request`), how many times it was assigned (`requests`), a digest of its name and description, the project it is in (`bucket`, `project_name`), and why it closed (`closed`) |
| `assigned-todos.json` | the script | `ignored`: to-dos assigned to the acting login whose creator is not listened to, so the sweep fetches each once |
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

A run from the command line runs only the steps that are due, like the timer; add `--all`
to run every step now. Drop `--dry-run` to apply. `run.sh <home> <config> [--dry-run]` is
the scheduled entry point: it refreshes the Basecamp OAuth token when under three days remain (logging a
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

## A systemd timer by hand

`init` installs a 30-second timer; to set one up by hand instead,
`~/.config/systemd/user/basecamp-sync.service` (also in [`examples/`](examples)):

```ini
[Unit]
Description=Mirror a firstmate backlog into its Basecamp project

[Service]
Type=oneshot
ExecStart=%h/src/basecamp-mate/run.sh %h/path/to/firstmate-home %h/path/to/firstmate-home/data/basecamp-sync/config.json
```

`~/.config/systemd/user/basecamp-sync.timer`:

```ini
[Unit]
Description=Basecamp sync for a firstmate backlog, every 30 seconds

[Timer]
OnBootSec=2min
OnUnitActiveSec=30s
AccuracySec=5s

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
new commands and the layer boundary; `tests/test_notifications.py` covers notifications
with stubbed `/my/readings.json` and `/my/boosts.json`: the first run seeding without
replaying, each kind of notification running only its thread's reader (chats, mentions and
Campfire replies, tracked to-dos, mirrored cards, the agent's messages, check-in Reminders),
followed threads and mentions with their replies, a comment on a card whose task has left
the backlog, boosts, marking read only after the record is written and not after a failed
read, a failed mark retried, other projects left alone, unmonitored input once per kind
(old listener keys still applying), the timer's cadences and the upgrade from a `"listen"`
config; `tests/test_people.py` covers the `people` list: relaying
several people with who wrote each record, captain-only approvals and decisions, an
unchanged single-captain config and `init --listen-to`; `tests/test_pings.py` covers the Ping reader with a stubbed
`/my/readings.json`, the reply in a Ping, and a Ping's notification and boosts;
`tests/test_assigned_todos.py` covers to-do requests with a stubbed `/my/assignments.json`:
the sweep, who assigned it, comments, boosts, edits and closing, reassignment, the agent
completing it, the `Assignment` and `Completion` notifications, and their notes, plus the account-wide
scope (a request in another project carrying its project, its comments, boosts, replies,
edits and closing in that project) and the unchanged project default. The `basecamp`, `gh` and `lavish-axi` CLIs are stubbed, and `init`'s systemd and check
registration sit behind a fake; tests make no network calls and touch no real home.
`tests/test_init.py` also covers removing the retired listener service and `"listen"` key.
`tests/test_setup.py` covers `setup` (defaults, a project URL, interactive picks, the card
mirror creating tables and columns, the device-code sign-in, refusals, re-runs and a failed
test sync) and `doctor` (a healthy home and each failure: no config, a locked keyring, an
expired login, a missing column, a stopped timer, a leftover listener, the unread cap, a
failed or stale sync, an unregistered wake check, a missing home, two homes taking to-dos
account-wide with one login) with the CLIs, `systemctl` and `run.sh` stubbed.

## Pending records

Each line of `pending-comments.jsonl` is one JSON object with a `kind`. Records written
before `kind` existed have none; treat a missing `kind` as `"comment"`. Every record but
`checkin` is something a listened-to person did, and carries `author` (`id`, `name`) and
`captain` (true when the captain wrote it); a `checkin` carries the question's writer the
same way. A record written before the `people` list has neither and is the captain's.
Only a record with `captain: true` can be a decision or an approval.

- `comment`: `task`, `repo`, `card`, `comment` (id), `at`, `text`.
- `question`: the same fields as `comment`, for a comment containing `?`.
- `chat-question`: `chat` (id), `line` (id), `url`, `text`, `at`; `mention: true` when it
  @mentions the agent (or replies to its line) in a chat nothing else relays.
- `thread-comment`: `bucket`, `parent` (the thread's id), `parent_type` (e.g. `Document`,
  `Kanban::Card`, `Todo`), `title`, `comment` (id), `question`, `url`, `text`, `at`. A
  listened-to person commented on a thread the agent follows that no other reader tracks
  (a card nothing mirrors, a document, someone else's to-do). Answer with `sync.py reply
  --recording <comment>`, which comments on the thread.
- `mention`: the same fields, for a comment that @mentions the agent: addressed to it.
- `ping`: `bucket` (the Ping's circle), `chat`, `line` (ids), `title` (the Ping's name),
  `url`, `text`, `at`. The owner wrote to the agent's login in a Ping; answer there with
  `sync.py reply --recording <line>`.
- `checkin`: `questionnaire`, `question` (ids), `date` (local, `YYYY-MM-DD`), `title`,
  `url`, `at`. A check-in question came due today; answer it with `sync.py answer`. An
  instruction in it is the captain's only when `captain` is true.
- `approval`: `task`, `repo`, `card`, `url` (card URL), `boost` (id), `text`, `at`. The captain
  gave the card a 👍: approve every recommendation on it as recommended.
- `todo-comment`: `key`, `todo` (id), `comment` (id), `question` (true when it contains
  `?`), `url` (the comment), `text`, `at`. The owner commented on a tracked to-do; answer
  with `sync.py reply --recording <comment>`.
- `todo-request`: `key` (`request-<to-do id>`), `todo` (id), `n` (how many times it was
  assigned; `reopened` when more than once), `title`, `text` (the description), `assignees`
  (names), `url`, `at`, `project` (`{"id", "name"}`, the project it is in); `author` is who
  assigned it. A to-do assigned to the agent's login is
  a request: captain work when `captain` is true, a request to weigh otherwise. Complete it
  with `sync.py todo complete --todo <key>` when the work is done. Its later comments and
  boosts are `todo-comment` and `boost` records with `request: true`, `title` and `project`.
- `todo-request-update`: `key`, `todo`, `n`, `title`, `project`, `text` (the new description), `url`,
  `at`; `author` is null (Basecamp does not say who edited it). The request's
  name or description changed.
- `todo-request-closed`: `key`, `todo`, `n`, `title`, `project`, `reason` (`completed`, `trashed`,
  `archived` or `unassigned`), `url`, `at`; `author` is who completed it, when known, else
  null. The request is closed and no longer read; nothing is left to complete.
- `message-comment`: `message` (id), `subject`, `comment` (id), `question`, `url` (the
  comment), `text`, `at`. The owner commented on a post the agent made, usually feedback
  or an instruction on a report; act on it and answer with `sync.py reply --recording <comment>`.
- `boost`: `surface` (`chat`, `ping`, `card`, `card-comment`, `todo`, `todo-comment`,
  `checkin-answer`, `message`, `message-comment`, `thread-comment`), the surface's ids (`chat`; `bucket`, `chat`; `task`,
  `repo`, `card`; `key`, `todo`; `question`; `message`, `subject`; `bucket`, `parent`, `title`), `recording` (the
  boosted recording), `boost` (id), `text` (the boost's text), `url`, `at`. The owner's
  boost is an answer to what was boosted, like a comment; a 👍 on an assigned card by
  anyone but the captain is a `boost`, not an `approval`.
- `unmonitored`: `key` (`<what happened>/<recording type>`), `event_type`, `recording_type`,
  `notification` (the notification's or boost's id; `event` in records from the retired
  listener), `recording` (id), `title`, `text` (an excerpt), `creator` (`id`, `name`), `url`,
  `at`. The owner did something no enabled behavior handles; put it to them as a decision to-do asking how events like it should be
  handled, act on the answer, then `sync.py unmonitored handle --key <key> --decision <text>`.

## License

MIT. See [LICENSE](LICENSE).
