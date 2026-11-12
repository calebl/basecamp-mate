# To-do requests

With `"assigned_todos": {}` (`init --assigned-todos`), a to-do a listened-to person
creates or reassigns in the project so that the agent's login is an assignee is a request
to the agent: it is recorded once as a `todo-request` (title, description, link, assignees
and who assigned it), acknowledged with a 👀 on the to-do, and tracked in `todos.json` as
`request-<to-do id>`. From then on it is read like a tracked to-do: comments by the people
listened to arrive as `todo-comment` records and their boosts as `boost` records, both
with `"request": true` and the to-do's `title`; an edit to its name or description arrives as
`todo-request-update` with the new text (and a moved due date or changed repetition rule, with
the new schedule); and its closing as `todo-request-closed`, with a
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

**Cards.** With `"assigned_cards": {}` (`init --assigned-cards`; off by default, the same
`limit` and `scope` as `assigned_todos`), a card a listened-to person assigns to the agent's
login is a request read the same way: a `card-request` record (key `request-<card id>`,
tracked in `todos.json` with `"type": "card"`), then `card-request-update` and
`card-request-closed`, and its comments and boosts as `todo-comment` and `boost` records
with `"request": true` and `"type": "card"`. Basecamp raises an `Event` notification on the
card when it is assigned, which the notification reader takes as the signal to read it; the
5-minute sweep of `/my/assignments.json` is the repair sweep. That listing also has a card
assigned to someone else when one of its steps is assigned to the agent; the sweep skips any
card entry whose own `assignees` leave out the agent's login, so such a card is never fetched
nor counted against `limit`. `sync.py reply`, `todo comment`,
`todo show` and `todo complete` work on it; `todo complete` completes the card, which
Basecamp shows by moving it to its table's Done column. A card the card mirror made from the
backlog (in `map.json`) is never a request, and `assigned_todos` is unchanged by it.

**Card steps.** With `"assigned_steps": {}` (`init --assigned-steps`; off by default, the
same `limit` and `scope` as `assigned_todos`), a card step (one of a card's checklist items,
a `Kanban::Step`) a listened-to person assigns to the agent's login is a request: a
`step-request` record (key `request-<step id>`, tracked in `todos.json` with `"type":
"step"` and the `card` it is on), acknowledged with a 👀 on the step, then
`step-request-update` (its text or due date changed) and `step-request-closed` (`reason`
`completed`, `unassigned` or `trashed`). Every record of it carries `card`: the `id`,
`title` and `url` of the card the step is on. Basecamp lists a step in `/my/assignments.json`
only inside its card's entry, as one of its `children` of type `"step"` (the card itself is
listed there whoever it is assigned to); the 5-minute sweep reads it from there, and the
step's own notifications (`Assignment`, the `Event` of a step added already assigned, and
`Completion`), which subscribe to the step and link to its card, run the reader within
about 30 seconds. The step is read at `/buckets/<project>/card_tables/steps/<id>.json`, and
its events (`/buckets/<project>/recordings/<id>/events.json`) say when it was assigned to the
agent and by whom: the latest `assignment_changed` event that adds the agent's login. When
the sweep finds the step, without a notification naming who assigned it, that event's creator
did. Someone not listened to who assigns one gets the automatic reply on its card.

A step has no comments of its own, and its card is someone else's, so of the card's
comments only those that concern the agent are part of the request, as `todo-comment`
records with `"request": true`, `"type": "step"` and a `why`: one that @mentions the agent
or is a Basecamp Reply to its comment (`addressed`; a Reply has no @mention in its text, so
the mention notification that names it decides, even after the sweep skipped it), and the
first comment from someone listened to after the agent's own comment on the card
(`awaiting`); a Reply read late, behind a newer comment of the agent's, leaves that comment
awaiting its answer. Comments made on the card up to the step's assignment (that event's
time, else the step's making) are never relayed, while one made after it counts even when
the sweep finds the step later. Every other comment is skipped (a `skipped comment` line in
`sync.log` and the relay ledger). With several of the agent's steps open on one card, the one
with the smallest key reads its comments, so each is relayed once, under that key; the next
goes on from where it stopped. A worded boost on the step, or any boost on one of those comments or the agent's
own comment there, is a `boost` record of the request. While another reader that is on reads
that card (the card mirror, for a card made from the backlog; an open card request with
`assigned_cards` on; a decision card with `todos`), its comments are left to that reader. `sync.py todo comment --todo request-<id>` comments on the card, `sync.py reply`
answers one of its comments there, `todo show` reads the step, and `sync.py todo complete`
checks the step off (`basecamp subtasks complete`), leaving the card as it is. Every record
and outcome goes through the relay ledger, so `sync.py stats` counts step requests like any
other.

**Scheduled to-dos.** A request with a due date is scheduled: every request record carries
`due_on` (null when undated), and the agent does the work on that date, not before when
the description says so, leaving the to-do open until then. Some scheduled to-dos also
repeat on a schedule Basecamp keeps (set in Basecamp's *Repeat* block): their records carry
`repeats`, Basecamp's `repetition_schedule` without its empty fields (`frequency`, `days`
with 0 for Sunday, `start_date`, `end_date`, intervals), and null for one that does not
repeat. Basecamp alone owns the schedule; the sync and the agent keep no copy of it as
authority. A record shows the due date and rule as read then, and a change in Basecamp
(the due date moved, the rule changed or removed) arrives as a `todo-request-update`
carrying the new `due_on` and `repeats`. Before acting on the day, the agent re-reads the
to-do with `sync.py todo show --todo request-<id>` (read-only: whether it is open and
assigned to the agent, its due date and rule now) rather than trusting a date it saved.

When a repeating to-do is completed (by the agent with `todo complete`, or by anyone),
Basecamp makes the next occurrence itself: a **new to-do** (a new id) in the same list with
the same name, rule and assignees, due on the next date. That making raises no event or
notification, so the 5-minute sweep finds it in `/my/assignments.json`. It is recorded once
as a new `todo-request` with `follows` set to the to-do id of the occurrence it continues,
and attributed to whoever asked for that occurrence: Basecamp's copy does not say who asked,
so its creator does not decide whether it is heard. The match is the completed repeating
request in the same project and list with the same name that no other to-do has followed
yet; one still open here (completed since the last sweep) has its last comments and its
closing read first. A to-do Basecamp reuses (completed, then open again with a new due date)
is its own next occurrence: a new request under the same key, with `follows` its own id and
`reopened` false. The agent comments the results, completes the to-do, and never creates the
next occurrence. Other repeating Basecamp objects (check-ins, schedule events) are not
to-do requests.

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
