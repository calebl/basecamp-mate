# People the sync listens to

Who gets through is explicit, and enforced before the agent: **what anyone else writes
never reaches it.** Every reader checks the person first, so someone not on the lists below
makes no pending record, no inbox note and no 👀, whatever they write and wherever. When
they address the agent directly, the block is logged and, with `blocked` on, they get one
short automatic reply (see "Blocked senders" below).

A config with no lists listens only to the captain. For a new home, `init` (and so
`setup`) picks the default from the account: on a **personal account** (no one but the
captain and the agent's login in the captain's Basecamp company) it listens only to the
captain; on a **team account** (others in that company, clients aside) it also writes
`"participants": {"company": true}`, so everyone in the company may ask the agent things,
while only the captain (and operators) decide. It reads the account's people
(`/people.json`, page by page, stopping at the first colleague) to tell, says which it
found after the printed config, and listens only to the captain when that read fails.
`--participants-company` or `--no-participants-company` overrides the default; a re-run
of `init` or `setup` on an existing home keeps its config's setting, so an existing home
never widens on its own. To change it later, edit `participants` in the config, or ask the
agent to.

The agent acts with the captain's authority, so who else reaches it, and whose word
counts, is widened only deliberately, in the config (or with `init`'s flags), in four ways:

- **Operators** (`operators`: person ids, `init --operator <person>`) are named people
  whose word authorizes the agent as the captain's does. They are listened to everywhere
  the captain is.
- **People** (`people`: person ids, `init --listen-to <person>`) are named people the agent
  listens to as **participants**: every reader that relays the captain (chat lines,
  every-line chats, card, to-do, message and thread comments, mentions, boosts on every
  surface, Pings, to-do requests and unmonitored input) relays them too.
- **People in one project** (`project_people`: `{"<project id>": [person ids]}`, by hand)
  are named participants heard only where they write in that project: an outside person
  invited to one project, say. Elsewhere, including Pings (a Ping is a bucket of its own,
  not a project), they are not heard. With account-wide to-do requests (`"scope":
  "account"`), a to-do they assign the agent in their project is a request; in any other
  project it is not. With account-wide mentions (`"mentions": {"scope": "account"}`), the
  same holds for their @mentions and comments.
- **Participants by rule** (`participants`: `{"project": true}`, `init
  --participants-project`, `{"company": true}`, `init --participants-company`, and/or
  `{"domains": ["example.com"]}`, `init --participants-domain <domain>`): anyone on the
  project who is not a client, anyone whose Basecamp profile is in the captain's company
  (`{"company": <id>}` names another company), or anyone whose email address is at a
  listed domain. They may ask the agent things: their chat
  lines (in a relayed chat), Pings, comments and @mentions are relayed as a participant's.
  Their boosts, to-do assignments and unmonitored input are not: a boost or an assignment
  reads as a decision, which a participant cannot make, and a whole project's unrelated
  activity is not something to ask the captain about.

The captain always counts, whether or not a list names them, and the acting login never
does: its own lines only move cursors. "Owner" below means anyone the sync listens to.

**One caveat decides which to use.** Basecamp shows a person's email address only to
themselves and to account admins; everyone else sees it masked (`r••••@•••.•••`). Unless
the agent's login is an account admin, a domain matches no one and an email passed to
`--operator` or `--listen-to` matches no one (`init` says why). Operators and people need
not be on the project: `init` looks a person it cannot find there up among the account's
people, and `sync.py access` lists them like anyone else. `"project": true` and the
named lists are keyed on person ids, which every login sees. `init` warns, and `doctor`
flags, domains that can admit nobody. The project's people are read once a run, only when
someone not named writes, and kept in `participants.json`, whose last list stands in when
that read fails.

`"company"` works for every login: a person's company is on their Basecamp profile. It
comes from the person object when Basecamp includes it, and otherwise from their person
record (`/people/<id>.json`), read at most once a run per person; `companies.json` keeps
the last company read for each person, and that stands in when a read fails. When there is
none to fall back on, the company admits no one it cannot check, and that person gets no
automatic reply either (a person with an unknown company is not known to be blocked).

`sync.py access --home <home> --config <config.json>` prints who gets through, and where:
the captain, operators, people, people per project, the rules, and whether blocked senders
get the automatic reply. It only reads (each person's name).

Every record says who wrote it (`author`: `id`, `name`), whether that is the captain
(`captain`: true or false) and their `role` (`captain`, `operator` or `participant`), and
so does its inbox note ("from the captain", "from <name> (an operator: their word counts
as the captain's)" or "from <name> (not the captain)"). A record **authorizes** when its
`role` is `captain` or `operator`; a participant's never does:

- only the captain's or an operator's 👍 on an assigned card is an `approval`; a
  participant's is a `boost`;
- only the captain's or an operator's comment or boost on a decision to-do is the
  decision; a participant's is input to weigh, and the to-do stays open;
- a check-in question's instruction counts only when the captain or an operator wrote it;
- a to-do request from the captain or an operator is captain work; a participant's is a
  request to weigh and route;
- decision to-dos, assigned cards and `sync.py ask` mentions are still the captain's alone.

A config with only `captain` behaves exactly as before; its records gain only `role`.

Operators are the trusted group for risky actions: name in `operators` the people whose
word may authorize what only the captain could otherwise (a merge, a force-push, a
credential), and keep everyone else a participant.

## Blocked senders

Someone the sync does not listen to **addresses** the agent when they @mention it in a chat
line or a comment, write in a Ping with it (with `pings` on), or assign it a to-do or card
(with `assigned_todos` or `assigned_cards` on, as a notification reports it). What they wrote
is dropped before the agent, with a `blocked <name> (<id>): ... not relayed` line in
`sync.log`. With `"blocked": {}` the sync also posts one short reply where they wrote:
a new line in that chat or Ping, or a comment on that thread, to-do or card:

> Sorry, I can't act on this: I only take requests from the people I work for. Please ask
> one of them to pass it on.

`{"reply": "<text>"}` posts your text instead. The reply goes out at most once per person
and conversation a day, and never for anything written before automatic replies started
(`blocked.json`'s `since`, set on the first run with `blocked` on). The captain, the
acting login and Basecamp's own senders are never answered, and lines and comments that do
not address the agent get no reply. A to-do the assignment sweep finds is never answered:
the sweep only guesses who assigned it (its creator); only a notification names them. Nor is
a participant admitted by rule whose assignment is ignored: they are listened to, just not
for assignments. With
`blocked` on, the Ping reader reads every Ping the login is in, not only those with someone
listened to, so a stranger's Ping can be answered. A failed reply is logged and not retried.
Like every post, nothing is posted without a `profile`, as the owner, or in a dry run.

`init` turns it on for a new home; `--no-blocked-reply` leaves it off and `--blocked-reply
<text>` sets the text. An existing config without `blocked` keeps today's behavior (the
silent drop), and re-running `init` or `setup` keeps whatever the config has. To change it,
edit `blocked` in the config, or ask the agent to.
