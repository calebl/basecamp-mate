# Card mirror

The backlog is the source of truth; Basecamp is a one-way view of it. Each run reads
every backlog task, picks its board from the task's repo, picks a column, and then
creates, updates, moves, assigns or unassigns cards.

Column rules:

| Task state | Column |
| --- | --- |
| Queued | Triage |
| Queued, listed in `figuring.json` | Figuring it out |
| Held for the captain (`hold-kind: captain`, no until date) | Figuring it out, **assigned to the captain** (unless `"assign_captain": false`) |
| Parked, has an until date, or listed in `not-now.json` | Not now |
| In flight | In progress |
| In flight with `pr=` in `state/<task>.meta` | Ready for QA |
| Done | Done |

`"assign_captain": false` (`init --no-assign-captain`, or "No" to setup's card-assignment
question) keeps a held card in Figuring it out but never assigns the owner, and the next run
unassigns the owner (only the owner) from every card the mirror had assigned for a hold. Use it
in a home whose project is not where the owner takes decisions: the agent sends each decision
to its main home, which puts it to the owner as a to-do in its own project linking back here.
The 👍 approval reader reads only assigned cards, so it is quiet there too.

If an agent hosts a Lavish review board for a task, the card links it. Each run reads the
live sessions once from plain `lavish-axi` (it has no machine-readable listing, so its
`sessions[N]{file,status,url,...}` table is parsed, with a 15s cap). A session with status
`open` whose file is under `<home>/data/<task>/` belongs to that task, and its URL, exactly
as printed, is listed as **Plan board** near the top of the note. `boards.json` adds boards
owned by other tasks (e.g. a scout's plan). Once a session ends or disappears the link
drops on the next run. If `lavish-axi` is missing, fails or times out, the run logs it and
each card keeps the links it had.

Card notes are HTML blocks (`<div>`, `<ol>`/`<ul>`) with no raw newlines between them. When the captain hold is released the card is unassigned on the next run.

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
