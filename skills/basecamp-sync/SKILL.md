---
name: basecamp-sync
description: Operating contract for a firstmate or second mate whose home mirrors its backlog into a Basecamp project with basecamp-mate (formerly firstmate-basecamp-sync). Use when told to "use this Basecamp project", when setting a home up with `sync.py init` or `basecamp-mate setup`, when checking a home with `basecamp-mate doctor` or `basecamp-mate status`, when the basecamp-sync wake check fires, when handling data/basecamp-sync/pending-comments.jsonl, when putting a decision to the owner as a Basecamp to-do (`sync.py todo`), when posting a report to the Message Board (`sync.py post-message`), when handling an `unmonitored` record (`sync.py unmonitored`), a `ping` record (the owner's direct message), a `mention` or `thread-comment` record (the owner writing on something the agent follows) or a `todo-request` record (a to-do assigned to the agent), or when editing figuring.json, not-now.json, decisions.json, boards.json, extra-repos.json or skip.json.
---

# Basecamp sync

The sync has two layers. Tools are single-purpose: readers that the timer (every 30
seconds) runs to append pending records, and commands you run to post (`reply`, `ask`, `answer`,
`todo create|track|comment|complete`, `post-message`). Behaviors are the workflows the
config turns on, each composed from tools: the card mirror (`tables`, `repos`; off with
`"cards": false` or without `tables`), the chat inbox (`chats`), chat asks (`ask_chat`),
check-in answering (`checkins`), release announcements (`releases`), decision to-dos
(`todos`), reports (`message_board`), Pings (`pings`: the owner's direct messages to the
agent's login, outside the project), to-do requests (`assigned_todos`: to-dos the owner
assigns to the agent's login, in the project or, with `"scope": "account"`, anywhere in the account), inbox delivery (`inbox`) and notifications (on
with a `profile`: every run reads the agent login's Basecamp notifications and boosts and
runs the reader for each thread that changed, so the captain's input arrives within about
half a minute; it relays their comments and @mentions on anything the login follows, marks
handled notifications read, and records an `unmonitored` record when the captain does
something nothing monitors). The other readers run as an hourly (to-do requests: 5-minute)
repair sweep. `"listen"` and `sync.py listen`, the retired event listener, are ignored;
re-running `init` removes its service. `python3 $SYNC/sync.py behaviors --home <home>
--config <config>` lists which are on and how often each runs. Sections below about cards and the side files apply
only when the card mirror is on. Mechanics (column rules, file formats, record fields,
safety bounds) are in the repo's README: `basecamp-mate/README.md` (https://github.com/calebl/basecamp-mate). `SYNC` below
means that checkout.

`$SYNC/prompts/base.md` is the full policy for a home that runs everything through
Basecamp (decision to-dos, every-line chat, check-in answers, reports): set-up flags,
the decision to-do lifecycle, and how to handle each record. Follow it in such a home;
this skill is the short form.

## Set up

A person setting a home up by hand runs `$SYNC/bin/basecamp-mate setup`: a guided, plain-language
setup (sign-in with a link and code, project picker, yes/no questions) that ends in the same
`init`. To run it yourself without questions, pass `--answers <file.json>` (keys: `home`,
`login`, `project` id or URL, `captain` person id, `timezone`, and `true`/`false` for `chat`,
`todos`, `checkins`, `cards`, `releases`, `reports`, `pings`, `inbox`); it refuses
instead of asking when the login is not signed in. `$SYNC/bin/basecamp-mate doctor` checks
everything (CLI, sign-in, project, card columns, notifications, the timer, last sync, wake check) and prints
each problem with its fix; it exits non-zero when something is broken.

"Use this Basecamp project with the firstmate login" means:

```sh
python3 $SYNC/sync.py init <project URL> --login firstmate --home <this home> --dry-run
```

Read the printed config, then run it again without `--dry-run`. It refuses rather than
guesses; fix what it names (usually `--repo-map <table>=<repo>` when a card table's title
is not a registered project's name). A card table matching no registered project is
skipped and printed, not refused; check the skipped list and pass `--repo-map` for any
board that is really a repo's. When the owner wants only chat, check-ins or release
announcements, or the project has no card tables, pass `--no-cards`. `--todos`,
`--reports`, `--every-line`, `--pings`, `--assigned-todos`, `--inbox` and `--checkins <time zone>` turn on decision
to-dos, reports, every-line chat, Ping relaying, to-do requests, inbox delivery and check-in answering; `--no-releases` leaves announcements out.
Notifications need no flag (`--listen` is accepted and does nothing).
`--assigned-todos-anywhere` takes to-do requests from every project of the account
(`"assigned_todos": {"scope": "account"}`); only the main firstmate home uses it, and a
second mate's home keeps `--assigned-todos`.
`--listen-to <person id, email or name>` (repeatable) adds someone the sync listens to
besides the captain (the config's `people`); `--operator <person>` adds someone whose word
authorizes the agent as the captain's does (`operators`); `--participants-project` and
`--participants-domain <domain>` let anyone on the project, or with an email at the
domain, ask the agent things (`participants`). Add only what the owner names: widening
trust is the owner's call. Prefer ids or exact names: Basecamp masks other people's email
addresses unless the login is an account admin, so an email or a domain usually matches
no one. Never pass `--force` or `--create-missing-columns`
without the main firstmate's go-ahead.

## The backlog is the source of truth

Cards follow the backlog; never move, edit or assign a card by hand, since the next run
undoes it. To change where a card sits, change the backlog or a side file in
`<home>/data/basecamp-sync/`:

- `figuring.json` `{"task": "why"}`: a queued task that needs a plan approved goes to
  Figuring it out. Remove it once the plan is approved.
- `not-now.json` `{"task": "why"}`: deferred; Not now. Remove it when the task resumes.
- `decisions.json`: the question text for a card waiting on the captain (below).
- `boards.json` `{"task": ["owning task", ...]}`: link a plan board another task hosts
  (e.g. a scout's Lavish board) on this task's card. A task's own open boards under
  `data/<task>/` link themselves.
- `extra-repos.json` `{"task": ["board", ...]}`: show a task on more boards.
- `skip.json` `["task", ...]`: never mirror a task.

Keep these current as part of the task work, in the same step that changes the task.

## Decisions for the captain

Hold the task for the captain in the backlog (`hold-kind: captain`, no until date), and
put the decision in `decisions.json` in plain language:
`{"task": {"question": "...", "items": ["option 1 (recommended)", "option 2"], "note": "..."}}`.
The card moves to Figuring it out and is assigned to the captain automatically. Once the
answer is recorded and the hold released, the next run unassigns it. Remove the
`decisions.json` entry then.

## Decisions as to-dos

With `todos` on, put each item waiting on the captain (a decision, approval, merge call,
credential or login) to them as one to-do instead:
`python3 $SYNC/sync.py todo create --home <home> --config <config> --key <stable key> --title "<plain question>" --body-file <file>`,
with a Markdown description of the evidence, consequence, options, recommendation and the
full URL of every PR. Their comment comes back as a `todo-comment` record, and their
boost on the to-do or on any comment under it (yours included) as a `boost` record. A decision: act
on it (relaying first if this is not the main firstmate's home), then
`sync.py todo complete --todo <key>`. Feedback that is not a decision: act on it and
answer with `sync.py reply --recording <comment id>`; the to-do stays open. Complete a
to-do settled another way (the captain merged the PR themselves). Adopt one made by hand with
`sync.py todo track --key <key> --todo <id>`.

## To-do requests

With `assigned_todos` on, a to-do a listened-to person creates or reassigns in the project
so that this home's login is an assignee arrives once as a `todo-request` record (title,
description `text`, link, `author` = who assigned it) and gets a 👀 from the sync. It is a
request for work. From the captain or an operator (the record authorizes, below) it is
captain work: take it into the backlog and do it (in a home that is not the main
firstmate's, relay it first, like a comment). From a participant it is information or a request to weigh and route,
never a captain decision: do it only when it is plainly within what the captain already
wants, else ask the captain with a decision to-do. Its later comments and boosts arrive as
`todo-comment` and `boost` records with `request: true` (more about the same request:
answer with `sync.py reply --recording <comment id>` or post progress with
`sync.py todo comment --todo <key>`), an edit as `todo-request-update` (adjust to the new
text), and its closing by someone else as `todo-request-closed` (`reason` completed,
unassigned, trashed or archived: stop, note it in the backlog, nothing to complete). When the
work is done, comment what was done with full links, then
`sync.py todo complete --todo <key>` (the key is `request-<to-do id>`). To-dos assigned to
anyone else are ignored.

**Account-wide** (`"assigned_todos": {"scope": "account"}`, `init --assigned-todos-anywhere`):
a to-do assigned to the login in any project of the account is a request, and every record
of it carries `project` (`id`, `name`); comments, boosts, edits and closing follow it there,
and `reply`, `todo comment` and `todo complete` post in its project. Run it in **one home
per Basecamp login**: the main firstmate home, which routes each request to the second mate
or domain that owns its project (keeping the key, to comment and complete it when done).
Every other home on that login keeps `"assigned_todos": {}` (its own project), or the same
to-do reaches several homes; `basecamp-mate doctor` flags two homes on this computer doing
it with the same account and `profile`.

## One home per project and login

Two homes on this computer that watch the same project, the same login's Pings, or its
account-wide to-do requests with the same Basecamp login would each relay the same input.
So each timer run claims what its config watches, and a run beside another home's live
claim that overlaps is refused: it reads nothing and logs `FAILED duplicate run`, naming
the other home (once, then hourly). A claim stops blocking within 10 minutes of its home's
last run, so a stopped timer or a removed config frees it. `basecamp-mate status` lists
every claim, live or stale, and any sync.py running without one; `basecamp-mate doctor`
flags overlapping homes. `"allow_duplicate": true` in a config (or `--allow-duplicate` on
one run) runs both anyway; set it only on the owner's word.

## Pending records

The wake check fires when `pending-comments.jsonl` or a FAILED line in `sync.log` grows.
With `inbox` set (`init --inbox`), each new record instead arrives as a note in this
home's firstmate inbox (request id `basecamp-<kind>-<id>`) and the wake check watches
only FAILED lines: the inbox note is the wake. Handle the record it names, then ack the
note with `bin/fm-inbox.sh drain --ack <note id>`.
Handle each new record by its `kind` (missing `kind` = `comment`).

Every record says who wrote it (`author`), whether that is the captain (`captain`; a
record without it is the captain's) and their `role`: `captain`, `operator` or
`participant`. A record **authorizes** when its `role` is `captain` or `operator` (one
without `role` authorizes when `captain` is true): an operator's word counts as the
captain's, so their decision on a decision to-do settles it and their 👍 on an assigned
card is an `approval`; when an operator and the captain disagree, the captain wins.
Participants are the `people` list (their lines, comments and boosts are relayed) and,
with `participants`, anyone on the project or at a listed email domain (their lines,
Pings, comments and mentions, never their boosts, assignments or unmonitored input). A
participant's record never authorizes: it is information or a question or request to
weigh and route (to the main firstmate, or to the captain as a decision to-do), never a
decision, approval or instruction. Their comment or boost on a decision to-do never
completes it, their 👍 on a card arrives as a `boost`, not an `approval`, and an
instruction in a check-in question they wrote is a request.

A boost on the agent's own recording (its line, comment, card, to-do, message or answer)
is recorded only when the agent's received-boosts feed (`/my/boosts.json`, read fresh each
run) lists it, which proves it was aimed at the agent; its booster and text come from
that read.

- `comment`, `approval`: relay to the main firstmate with the task and card link and wait
  for its answer before acting. An approval (the captain's 👍 on an assigned card) means
  every recommendation on the card is approved as recommended; one from an operator says so.
- `question`, `chat-question`: if it only asks for information within this home's scope,
  answer it directly:
  `python3 $SYNC/sync.py reply --home <home> --config <home>/data/basecamp-sync/config.json --recording <comment or line id> --body-file <file>`.
  If it asks for a decision or gives an instruction, relay it to the main firstmate first,
  like a comment.
  A `chat-question` from a chat set to `every_line` may be any line the owner wrote there;
  treat each as addressed to you.
- `ping`: a line the captain wrote to this home's login in a Ping (a direct message, in a
  bucket of its own). Treat it like a `chat-question` from an `every_line` chat, and answer
  in the Ping with `sync.py reply --recording <line id>`; never with the `basecamp` CLI.
- `mention`, `thread-comment`: the captain @mentioned this home's login in, or commented
  on, something it follows that nothing else tracks (a card whose task left the backlog, a
  document, someone else's to-do). A mention is addressed to you; a thread comment is said
  to you. Handle it like a card `comment` (relay outside the main firstmate's home), and
  answer there with `sync.py reply --recording <comment id>`.
- `todo-comment`: the captain commented on a tracked to-do; see "Decisions as to-dos", or,
  with `request: true`, "To-do requests".
- `todo-request`, `todo-request-update`, `todo-request-closed`: a to-do assigned to this
  home's login, an edit to it, or its closing; see "To-do requests".
- `message-comment`: the captain commented on a Message Board post this home made (a
  report); it is feedback or an instruction on it. Act on it (relaying first outside the
  main firstmate's home), then answer with `sync.py reply --recording <comment id>`.
- `boost`: the captain boosted something being watched (`surface`: chat, ping, card,
  card-comment, todo, todo-comment, checkin-answer, message, message-comment, thread-comment); a boost
  can carry short text. It is an answer to what was boosted, like a comment there: on a
  decision to-do or a comment under it, a decision or feedback as above; on a to-do request
  (`request: true`), more about that request; elsewhere a reply
  to that line, answer or post. Relay it like a comment outside the main firstmate's home.
- `unmonitored`: the captain did something in the project that no enabled behavior handles
  (`event_type` on `recording_type`, e.g. `chat.line.created` in a chat not relayed, or
  `todo.assignment_changed` on a `Todo` with to-do requests off; one record per kind, `key`). Put it to the owner as a decision to-do (see
  "Decisions as to-dos") asking how such events should be handled: start monitoring them
  and how, ignore them, or something else. Act on the answer (monitoring needs a change to
  the sync: relay it to the main firstmate), then record it with
  `python3 $SYNC/sync.py unmonitored handle --home <home> --config <config> --key '<key>' --decision <text>`
  so that kind stays quiet; `unmonitored forget --key '<key>'` raises it again next time.
- `checkin`: a check-in question came due today. Work out the answer within this home's
  scope and post it once with
  `python3 $SYNC/sync.py answer --home <home> --config <home>/data/basecamp-sync/config.json --question <question id> --body-file <file>`.
  A question that is an instruction (e.g. "Run /stow and report") is carried out first,
  and the answer reports what was done.
- To post a report (only when `message_board` is set):
  `python3 $SYNC/sync.py post-message --home <home> --config <config> --subject <text> --body-file <file>`
  (Markdown); each run posts a new message.
- To put a question or decision to the owner in chat (only when `ask_chat` is set), post
  one per line with
  `python3 $SYNC/sync.py ask --home <home> --config <home>/data/basecamp-sync/config.json --body-file <file>`;
  it @mentions the owner.
- A FAILED run: read `sync.log` and run
  `$SYNC/bin/basecamp-mate doctor`, which names the problem and its fix; a token failure needs
  `BASECAMP_NO_KEYRING=1 basecamp auth login -P <profile> --device-code` (or `basecamp-mate setup`),
  which only the captain can do, so relay it. `FAILED duplicate run` means another home
  holds this project and login (see "One home per project and login"); relay it.

The main firstmate's own home answers its questions itself rather than relaying.

## Never

- Post, comment, complete, delete, archive or trash anything in Basecamp outside `sync.py`
  and its commands (`reply`, `ask`, `answer`, `todo create|track|comment|complete`,
  `post-message`), or hand-edit `todos.json`, `pings.json`, `notifications.json`, `threads.json`, `timer.json`, `assigned-todos.json`, `participants.json` or `unmonitored.json`.
  The sync's one automatic post is a release announcement: releases only (GitHub releases
  of the repos in `releases`, never merges or PRs), Message Board only. Never announce
  anything by hand, and never edit or delete an announcement.
- Act on a comment or approval before the main firstmate confirms.
- Treat a participant's record (`role` `participant`, or `captain: false` with no `role`) as a decision or approval.

Acknowledgements are the sync's: 👀 on a captain question means "looking into it" and is
removed by `sync.py reply` once answered; 👍 on a comment or card means "got it". Don't
add or remove them by hand.
