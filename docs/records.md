# Pending records

Each line of `pending-comments.jsonl` is one JSON object with a `kind`. Records written
before `kind` existed have none; treat a missing `kind` as `"comment"`. Every record but
`checkin` is something a listened-to person did, and carries `author` (`id`, `name`),
`captain` (true when the captain wrote it) and `role` (`captain`, `operator` or
`participant`; null for a check-in question's writer nobody listens to); a `checkin`
carries the question's writer the same way. A record written before the `people` list has
none of them and is the captain's; one written before operators has no `role`. Only a
record whose `role` is `captain` or `operator` (without `role`: `captain` true) can be a
decision or an approval.

Times stay as Basecamp or GitHub wrote them (`at`, `created_at`); a record with a readable
time also carries it in the people's local time as `at_local` or `created_at_local`, like
`2026-10-21 09:14 CDT`. Quote the local one when telling a person a time. The zone is the
config `timezone`, else the captain's Basecamp time zone, else UTC. Logs and state files
stay in UTC.

- `comment`: `task`, `repo`, `card`, `comment` (id), `at`, `text`.
- `question`: the same fields as `comment`, for a comment containing `?`.
- `chat-question`: `chat` (id), `line` (id), `url`, `text`, `at`; `mention: true` when it
  @mentions the agent (or replies to its line) in a chat nothing else relays; `project`
  (`id`, `name`) when that chat is in another project (`"mentions": {"scope": "account"}`,
  or a chat watched with `sync.py watch`).
- `thread-comment`: `bucket`, `parent` (the thread's id), `parent_type` (e.g. `Document`,
  `Kanban::Card`, `Todo`), `title`, `comment` (id), `question`, `url`, `text`, `at`, `why`.
  A listened-to person commented on a thread the agent follows that no other reader tracks
  (a card nothing mirrors, a document, someone else's to-do), and either it is the first
  comment after the agent's `reply --awaiting` there (`"why": "awaiting"`) or the agent
  made the thread (`"why": "yours"`); see "Followed threads" in [notifications.md](notifications.md).
  Answer with `sync.py reply
  --recording <comment>`, which comments on the thread.
- `mention`: the same fields but `why`, for a comment that @mentions the agent or is a
  Basecamp Reply to its comment: addressed to it.
  Either carries `project` (`id`, `name`) when the thread is in another project of the
  account (`"mentions": {"scope": "account"}`).
- `ping`: `bucket` (the Ping's circle), `chat`, `line` (ids), `title` (the Ping's name),
  `url`, `text`, `at`, and `attachments` (`filename`, `url` each) when the line had uploads
  or uploads right after it were folded in. The owner wrote to the agent's login in a Ping;
  answer there with `sync.py reply --recording <line>`. A `mention` or `thread-comment` can
  carry `attachments` the same way.
- `checkin`: `questionnaire`, `question` (ids), `date` (local, `YYYY-MM-DD`), `title`,
  `url`, `at`. A check-in question came due today; answer it with `sync.py answer`. An
  instruction in it is the captain's only when `captain` is true.
- `approval`: `task`, `repo`, `card`, `url` (card URL), `boost` (id), `text`, `at`. The captain
  (or an operator) gave the card a 👍: approve every recommendation on it as recommended.
- `todo-comment`: `key`, `todo` (id), `comment` (id), `question` (true when it contains
  `?`), `url` (the comment), `text`, `at`. The owner commented on a tracked to-do; answer
  with `sync.py reply --recording <comment>`.
- `todo-request`: `key` (`request-<to-do id>`), `todo` (id), `n` (how many times it was
  assigned; `reopened` when more than once), `title`, `text` (the description), `assignees`
  (names), `url`, `at`, `project` (`{"id", "name"}`, the project it is in), `due_on` (its due
  date, or null), `repeats` (Basecamp's repetition rule, or null) and `follows` (the to-do id
  of the repeating to-do's occurrence this one is next after, or null); `author` is who
  assigned it (for a next occurrence, whoever asked for the one before). A to-do assigned to the agent's login is
  a request: captain work when `captain` is true, a request to weigh otherwise. Complete it
  with `sync.py todo complete --todo <key>` when the work is done. Its later comments and
  boosts are `todo-comment` and `boost` records with `request: true`, `title` and `project`.
  A scheduled one is done on its due date as Basecamp shows it then (`sync.py todo show`);
  a repeating one is completed when done, and Basecamp makes the next occurrence.
- `todo-request-update`: `key`, `todo`, `n`, `title`, `project`, `text` (the new description), `url`,
  `at`, `due_on`, `repeats`; `author` is null (Basecamp does not say who edited it). The request's
  name, description, due date or repetition rule changed.
- `todo-request-closed`: `key`, `todo`, `n`, `title`, `project`, `reason` (`completed`, `trashed`,
  `archived` or `unassigned`), `url`, `at`, `due_on`, `repeats`; `author` is who completed it,
  when known, else null. The request is closed and no longer read; nothing is left to
  complete. When it repeats, Basecamp's next occurrence arrives as a new `todo-request`.
- `card-request`, `card-request-update`, `card-request-closed`: the same fields, for a card
  assigned to the agent's login (`assigned_cards`); `todo` is the card id, `title` the card's
  title and `text` its content. Handled like the to-do request records.
- `message-comment`: `message` (id), `subject`, `comment` (id), `question`, `url` (the
  comment), `text`, `at`. The owner commented on a post the agent made, usually feedback
  or an instruction on a report; act on it and answer with `sync.py reply --recording <comment>`.
- `boost`: `surface` (`chat`, `ping`, `card`, `card-comment`, `todo`, `todo-comment`,
  `checkin-answer`, `message`, `message-comment`, `thread-comment`), the surface's ids (`chat`; `bucket`, `chat`; `task`,
  `repo`, `card`; `key`, `todo`; `question`; `message`, `subject`; `bucket`, `parent`, `title`), `recording` (the
  boosted recording), `boost` (id), `text` (the boost's text), `url`, `at`. The owner's
  boost is an answer to what was boosted, like a comment (on a `thread-comment` surface an
  emoji-only boost is skipped unless it is on the reply that awaits an answer, when it
  carries `"why": "awaiting"`); a 👍 on an assigned card by
  anyone but the captain is a `boost`, not an `approval`.
- `unmonitored`: `key` (`<what happened>/<recording type>`), `event_type`, `recording_type`,
  `notification` (the notification's or boost's id; `event` in records from the retired
  listener), `recording` (id), `title`, `text` (an excerpt), `creator` (`id`, `name`), `url`,
  `at`. The owner did something no enabled behavior handles; put it to them as a decision to-do asking how events like it should be
  handled, act on the answer, then `sync.py unmonitored handle --key <key> --decision <text>`.
- `timesheet-unmapped` (`timesheet` on): `key`, `days` (day -> hours not posted), `hours`
  (their total), `at`. The key has no to-do or card; name one with `sync.py timesheet start
  --key <key> --on <URL>`. Recorded once per key.
- `timesheet-refused` (`timesheet` on): `bucket` (the project), `key`, `date`, `hours`, `url`
  (the to-do or card), `error`, `code` (the CLI's), `at`. That project's timesheet refused
  the entry: timesheet tracking may be off there. It is tried again with one entry at most
  once a day. Recorded once per project until an entry there posts again.
- `timesheet-missing` (`timesheet` on): `key`, `bucket`, `date`, `hours`, `url`, `error`,
  `code`, `at`. The key's to-do or card was not found (deleted, or not visible to the login).
  Only that key is tried again, once a day; name another with `sync.py timesheet start --key
  <key> --on <URL>`. Recorded once per key until an entry for it posts again.
