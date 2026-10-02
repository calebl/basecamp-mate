---
name: basecamp-sync
description: Operating contract for a firstmate or second mate whose home mirrors its backlog into a Basecamp project with firstmate-basecamp-sync. Use when told to "use this Basecamp project", when setting a home up with `sync.py init`, when the basecamp-sync wake check fires, when handling data/basecamp-sync/pending-comments.jsonl, when putting a decision to the owner as a Basecamp to-do (`sync.py todo`), when posting a report to the Message Board (`sync.py post-message`), or when editing figuring.json, not-now.json, decisions.json, boards.json, extra-repos.json or skip.json.
---

# Basecamp sync

The sync has two layers. Tools are single-purpose: readers that the 5-minute timer runs
to append pending records, and commands you run to post (`reply`, `ask`, `answer`,
`todo create|track|comment|complete`, `post-message`). Behaviors are the workflows the
config turns on, each composed from tools: the card mirror (`tables`, `repos`; off with
`"cards": false` or without `tables`), the chat inbox (`chats`), chat asks (`ask_chat`),
check-in answering (`checkins`), release announcements (`releases`), decision to-dos
(`todos`), reports (`message_board`), inbox delivery (`inbox`) and the owner-event
listener (`listen`: a `sync.py listen` service beside the timer that polls Basecamp's event
feed and runs the same readers within about a minute of the captain posting; the records
are the same). `python3 $SYNC/sync.py behaviors --home <home>
--config <config>` lists which are on. Sections below about cards and the side files apply
only when the card mirror is on. Mechanics (column rules, file formats, record fields,
safety bounds) are in the repo's README: `firstmate-basecamp-sync/README.md`. `SYNC` below
means that checkout.

`$SYNC/prompts/base.md` is the full policy for a home that runs everything through
Basecamp (decision to-dos, every-line chat, check-in answers, reports): set-up flags,
the decision to-do lifecycle, and how to handle each record. Follow it in such a home;
this skill is the short form.

## Set up

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
`--reports`, `--every-line`, `--inbox`, `--listen` and `--checkins <time zone>` turn on decision
to-dos, reports, every-line chat, inbox delivery, the listener service and check-in answering; `--no-releases` leaves announcements out. Never pass `--force` or `--create-missing-columns`
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

## Pending records

The wake check fires when `pending-comments.jsonl` or a FAILED line in `sync.log` grows.
With `inbox` set (`init --inbox`), each new record instead arrives as a note in this
home's firstmate inbox (request id `basecamp-<kind>-<id>`) and the wake check watches
only FAILED lines: the inbox note is the wake. Handle the record it names, then ack the
note with `bin/fm-inbox.sh drain --ack <note id>`.
Handle each new record by its `kind` (missing `kind` = `comment`):

- `comment`, `approval`: relay to the main firstmate with the task and card link and wait
  for its answer before acting. An approval (the captain's 👍 on an assigned card) means
  every recommendation on the card is approved as recommended.
- `question`, `chat-question`: if it only asks for information within this home's scope,
  answer it directly:
  `python3 $SYNC/sync.py reply --home <home> --config <home>/data/basecamp-sync/config.json --recording <comment or line id> --body-file <file>`.
  If it asks for a decision or gives an instruction, relay it to the main firstmate first,
  like a comment.
  A `chat-question` from a chat set to `every_line` may be any line the owner wrote there;
  treat each as addressed to you.
- `todo-comment`: the captain commented on a tracked to-do; see "Decisions as to-dos".
- `boost`: the captain boosted something being watched (`surface`: chat, card,
  card-comment, todo, todo-comment, checkin-answer, message, message-comment); a boost
  can carry short text. It is an answer to what was boosted, like a comment there: on a
  decision to-do or a comment under it, a decision or feedback as above; elsewhere a reply
  to that line, answer or post. Relay it like a comment outside the main firstmate's home.
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
- A FAILED run (or `FAILED listen`, three listener cycles in a row): read `sync.log`; a token failure needs `basecamp auth login -P <profile>`,
  which only the captain can do, so relay it.

The main firstmate's own home answers its questions itself rather than relaying.

## Never

- Post, comment, complete, delete, archive or trash anything in Basecamp outside `sync.py`
  and its commands (`reply`, `ask`, `answer`, `todo create|track|comment|complete`,
  `post-message`), or hand-edit `todos.json` or `feed.json`.
  The sync's one automatic post is a release announcement: releases only (GitHub releases
  of the repos in `releases`, never merges or PRs), Message Board only. Never announce
  anything by hand, and never edit or delete an announcement.
- Act on a comment or approval before the main firstmate confirms.

Acknowledgements are the sync's: 👀 on a captain question means "looking into it" and is
removed by `sync.py reply` once answered; 👍 on a comment or card means "got it". Don't
add or remove them by hand.
