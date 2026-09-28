---
name: basecamp-sync
description: Operating contract for a firstmate or second mate whose home mirrors its backlog into a Basecamp project with firstmate-basecamp-sync. Use when told to "use this Basecamp project", when setting a home up with `sync.py init`, when the basecamp-sync wake check fires, when handling data/basecamp-sync/pending-comments.jsonl, or when editing figuring.json, not-now.json, decisions.json, boards.json, extra-repos.json or skip.json.
---

# Basecamp sync

The sync mirrors this home's backlog onto the project's card tables every 5 minutes.
Mechanics (column rules, file formats, record fields, safety bounds) are in the repo's
README: `firstmate-basecamp-sync/README.md`. `SYNC` below means that checkout.

## Set up

"Use this Basecamp project with the firstmate login" means:

```sh
python3 $SYNC/sync.py init <project URL> --login firstmate --home <this home> --dry-run
```

Read the printed config, then run it again without `--dry-run`. It refuses rather than
guesses; fix what it names (usually `--repo-map <table>=<repo>` when a card table's title
is not a registered project's name). A card table matching no registered project is
skipped and printed, not refused; check the skipped list and pass `--repo-map` for any
board that is really a repo's. Never pass `--force` or `--create-missing-columns`
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

## Pending records

The wake check fires when `pending-comments.jsonl` or a FAILED line in `sync.log` grows.
Handle each new record by its `kind` (missing `kind` = `comment`):

- `comment`, `approval`: relay to the main firstmate with the task and card link and wait
  for its answer before acting. An approval (the captain's 👍 on an assigned card) means
  every recommendation on the card is approved as recommended.
- `question`, `chat-question`: if it only asks for information within this home's scope,
  answer it directly:
  `python3 $SYNC/sync.py reply --home <home> --config <home>/data/basecamp-sync/config.json --recording <comment or line id> --body-file <file>`.
  If it asks for a decision or gives an instruction, relay it to the main firstmate first,
  like a comment.
- A FAILED run: read `sync.log`; a token failure needs `basecamp auth login -P <profile>`,
  which only the captain can do, so relay it.

The main firstmate's own home answers its questions itself rather than relaying.

## Never

- Post, delete, archive or trash anything in Basecamp outside `sync.py` and `sync.py reply`.
- Act on a comment or approval before the main firstmate confirms.

Acknowledgements are the sync's: 👀 on a captain question means "looking into it" and is
removed by `sync.py reply` once answered; 👍 on a comment or card means "got it". Don't
add or remove them by hand.
