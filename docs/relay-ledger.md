# Relay ledger and stats

Everything the sync relays costs the agent a turn, so the sync keeps a record of what it
relayed and what came of it, in `relay.jsonl` beside the config. It is append-only, one
JSON line per event, and stays on this computer: the backup never uploads it and nothing
else sends it anywhere.

- `relayed`: written with each pending record (so each inbox note): `item` (the note's
  request id, `basecamp-<kind>-<id>`), `kind`, `why` (the record's own `why`, `awaiting` or
  `yours`, else what the kind means: `addressed` for a mention, `chat`, `ping`, `card`,
  `own-message`, `tracked-todo`, `own-recording` for a boost, `assigned` for a request),
  `recording` (the id `reply --recording` and `dismiss --recording` take: the comment, line or
  boost, or the to-do of a request), `key`, `surface`, `bucket`, `url`, `title`, `text` (what
  the agent saw), `author`, `role` and `created_at` (Basecamp's time).
- `skipped`: an item the relay filter dropped: a comment on a followed thread with no
  @mention or answer awaited, a group Ping line not addressed to the agent, an emoji-only
  boost there and a boost on someone else's recording. Each keeps its one "skipped" line in
  `sync.log`; the ledger adds its `kind`, `recording`, `reason`, `bucket`, `text`, `url` and
  `author`. People the sync does not listen to are blocked before the filter and are not
  counted here.
- `answered`: `sync.py reply` posted the answer (`via` `reply` or `reply --boost`, with the
  `recording`), or `todo comment`, `todo complete` or `todo subtask` acted on a tracked to-do
  (with its `key`: every item relayed under that key before it counts as answered).
- `dismissed`: the agent ran `sync.py dismiss --recording <id> [--reason <text>]`: the
  item was not for it. The command finds the item by the id stored when it was recorded
  (the ledger, else `pending-comments.jsonl` for an item relayed before the ledger began),
  takes the agent's read boost off it as `reply` would, and records the outcome with the
  reason. It is refused (raised) for an id nothing relayed, leaves an item already answered
  (by `reply`, or by a to-do command on its key, since it was relayed) as it is, and records a recording Basecamp no longer finds (`not_found`) as dismissed all
  the same. A boost carries no read boost, so only its outcome is recorded. The inbox note
  of every comment, line or boost names the command ("Not for you: ...").

Every line carries `at` (when it was written, UTC) and `agent` (the `home`, the `profile`
and, once known, the login's person `id`). Together they make each relayed item a labeled
example: what the agent saw and why, where to refetch it (`bucket`, `recording`, `url`),
which agent it was for, and the label (`answered`, `dismissed`, or none yet).

`sync.py stats --home <home> --config <config.json> [--since YYYY-MM-DD] [--until
YYYY-MM-DD] [--by day|week|month|all] [--json]` reports it. Per period (by week, from
Monday, unless `--by`), and in total: the items relayed, answered, dismissed and still open,
the items skipped, and the noise rate, the dismissed share of the items decided (answered
or dismissed). Open items are counted as open, never guessed; the dismissed share of
everything relayed is in the JSON too. Then the breakdown by kind and why, most dismissed
first, so the top noise sources show, and the skipped items by kind and reason. An item's
outcome is `answered` once an answer written after it was relayed names its recording or
its to-do key (an answer wins over a dismissal), else `dismissed` once a dismissal written
after it names its recording, else open: an outcome never reaches an item relayed later,
such as a request's edit, which shares the request's to-do id and key. The ledger starts with the version that writes it; nothing before
is counted.

**Tokens.** `stats` also reads the agent's Claude Code transcripts, by default those of
sessions run in `--home` (under `CLAUDE_CONFIG_DIR`, default `~/.claude`, in `projects/`,
subagent transcripts included), or the directories given with `--transcripts <dir>`
(repeatable); `--no-tokens` skips them. It reports the period's session total from each
assistant message's `usage` (input, cache write, cache read and output, each message counted
once however many lines it spans), and an estimate per item: the assistant messages from
the first transcript line that shows the item's note (the line holding its `--recording
<id>` or `--todo <key>` command) to the agent's own `reply`, `dismiss` or to-do command
naming it, each message shared evenly among the items whose spans hold it. Totals and the
per-item average are given for answered and dismissed items, with how many were
attributed; an item handled in another session, or with no transcript, is not attributed.
It is an estimate: the span holds whatever else the agent did meanwhile, and each message's
usage is mostly the cached context of the whole session, so read it as the cost of the turns
an item took, not of the item alone.
