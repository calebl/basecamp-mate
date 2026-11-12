"""The relay report (`sync.py stats`): how much of what the sync relays to the agent was for it, and what it cost.

It reads the relay ledger (tools.RELAY, relay.jsonl beside the config), which the sync and the agent's commands append to:

  - "relayed": an item recorded for the agent (pending-comments.jsonl, and an inbox note with "inbox" on): its kind,
    why it was relayed, the recording `reply --recording` and `dismiss --recording` take, its link and text;
  - "skipped": an item the relay filter dropped (it keeps its "skipped" line in sync.log too), with the reason;
  - "answered": the agent answered a recording with `sync.py reply`, or acted on a tracked to-do's key with
    `todo comment`, `todo complete` or `todo subtask`;
  - "dismissed": the agent said a recording was not for it with `sync.py dismiss`.

An item's outcome is "answered" once an answer written after it was relayed names its recording or, for one with a
to-do key, its key (tools.outcome_applies), else "dismissed" once a dismissal written after it names its recording, else
"open". The dismissed share of the items decided
(answered or dismissed) is the noise rate; open items are counted, never guessed.

Tokens, when a Claude Code transcript directory is readable (default: the sessions run in the home): the period's
session total, from each assistant message's usage (counted once per message id), and an estimate per item. An item's
turns run from the first transcript line that shows its note to the agent (the line holding its `--recording <id>`, or
`--todo <key>`, command) to the agent's command that answered or dismissed it; each assistant message in that span is
shared evenly among the items whose spans hold it. An item whose span is not found (no transcript of that session, or
answered in another session) is counted as not attributed. Only local files are read; nothing is sent anywhere.
"""
import json, os, re
from datetime import date, datetime, timedelta, timezone

from tools import RELAY, claude_transcript_dirs, outcome_applies, read_ledger

PERIODS = ("day", "week", "month", "all")
PARTS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
ANSWER = re.compile(r"sync\.py\b.*\b(reply|dismiss|todo\s+(comment|complete|subtask))\b", re.S)


def when(at):
    """ISO time `at` as an aware datetime (UTC when it names no zone), or None when unreadable."""
    try:
        d = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def day_of(at):
    """The UTC day (a date) of ISO time `at`, or None when unreadable."""
    d = when(at)
    return d and d.astimezone(timezone.utc).date()


def period_of(day, period):
    """The label of the `period` holding `day`: the day, the Monday of its ISO week, its month, or "all"."""
    if period == "day":
        return day.isoformat()
    if period == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    if period == "month":
        return day.strftime("%Y-%m")
    return "all"


def fold(events):
    """(items, skipped) from ledger `events`: each relayed item (in relay order) with its "outcome", "answered_at" or
    "dismissed_at" and "via"; and the skipped lines."""
    items, skipped = [], []
    for ev in events:
        if ev.get("event") == "relayed":
            items.append(dict(ev, outcome="open"))
        elif ev.get("event") == "skipped":
            skipped.append(ev)
    by_rec, by_key = {}, {}
    for it in items:
        if it.get("recording") is not None:
            by_rec.setdefault(str(it["recording"]), []).append(it)
        if it.get("key") is not None:
            by_key.setdefault(it["key"], []).append(it)
    for ev in events:
        kind = ev.get("event")
        if kind not in ("answered", "dismissed"):
            continue
        near = by_rec.get(str(ev["recording"]), []) if ev.get("recording") is not None else by_key.get(ev.get("key"), [])
        for it in (it for it in near if outcome_applies(ev, it)):
            if kind == "answered" and it["outcome"] != "answered":
                it.update(outcome="answered", answered_at=ev.get("at"), via=ev.get("via"))
            elif kind == "dismissed" and it["outcome"] == "open":
                it.update(outcome="dismissed", dismissed_at=ev.get("at"), reason=ev.get("reason"))
    return items, skipped


def counts(items, skipped=()):
    out = {"relayed": len(items), "answered": 0, "dismissed": 0, "open": 0, "skipped": len(skipped)}
    for it in items:
        out[it["outcome"]] += 1
    decided = out["answered"] + out["dismissed"]
    out["dismissed_share"] = round(out["dismissed"] / decided, 3) if decided else None
    out["dismissed_of_relayed"] = round(out["dismissed"] / len(items), 3) if items else None
    return out


def in_range(at, since, until):
    d = day_of(at)
    return d is not None and (since is None or d >= since) and (until is None or d <= until)


def report(t, since=None, until=None, period="week", transcripts=None, tokens=True):
    """The relay report for Tools `t`'s home as a dict: per period and in total, the relayed / answered / dismissed /
    open / skipped counts and the dismissed share; in total, the breakdown by kind and why (most dismissed first) and the
    skipped items by kind and reason; with `tokens`, the transcripts' usage (see the module doc). `since` and `until`
    are dates, both days included; `transcripts` the directories to read (default: the home's Claude Code sessions)."""
    items, skipped = fold(read_ledger(t.path(RELAY)))
    items = [it for it in items if in_range(it.get("at"), since, until)]
    skipped = [s for s in skipped if in_range(s.get("at"), since, until)]
    groups = {}
    for it in items:
        groups.setdefault(period_of(day_of(it["at"]), period), ([], []))[0].append(it)
    for s in skipped:
        groups.setdefault(period_of(day_of(s["at"]), period), ([], []))[1].append(s)
    out = {"ledger": t.path(RELAY), "since": since and since.isoformat(), "until": until and until.isoformat(),
           "period": period, "periods": [dict(counts(*groups[p]), period=p) for p in sorted(groups)],
           "total": counts(items, skipped)}
    by = {}
    for it in items:
        by.setdefault((it.get("kind"), it.get("why")), []).append(it)
    out["by"] = sorted(({"kind": k, "why": w, **counts(v)} for (k, w), v in by.items()),
                       key=lambda r: (-r["dismissed"], -r["relayed"], str(r["kind"]), str(r["why"])))
    sk = {}
    for s in skipped:
        sk[(s.get("kind"), s.get("reason"))] = sk.get((s.get("kind"), s.get("reason")), 0) + 1
    out["skipped_by"] = [{"kind": k, "reason": r, "count": n} for (k, r), n in sorted(sk.items(), key=lambda x: (-x[1], str(x[0])))]
    if tokens:
        dirs = list(transcripts or claude_transcript_dirs(t.home))
        out["tokens"] = token_report(dirs, items, since, until, period)
        for row in out["by"]:
            got = [it for it in items if (it.get("kind"), it.get("why")) == (row["kind"], row["why"]) and "tokens" in it]
            row["tokens_attributed"] = round(sum(it["tokens"] for it in got))
    return out


def total(usage):
    return sum(int(usage.get(k) or 0) for k in PARTS)


def read_transcripts(dirs, since=None):
    """The Claude Code transcripts under `dirs` changed on or after day `since`: {file: [entry, ...]} in line order, each
    entry {"i", "at", "type", "raw", "id" (assistant message id), "usage", "commands" (its tool_use inputs as text)}. An
    assistant message written over several lines keeps one entry, its last line's usage, at its last line. Every file
    is keyed by its real path, so one seen under two spellings is read once. A missing directory has none."""
    files = set()
    for d in dirs:
        for root, _, names in os.walk(d, followlinks=True):
            for n in names:
                if n.endswith(".jsonl") and not n.startswith("."):
                    files.add(os.path.realpath(os.path.join(root, n)))
    out = {}
    for f in sorted(files):
        try:
            if since is not None and datetime.fromtimestamp(os.path.getmtime(f), timezone.utc).date() < since:
                continue
            with open(f, errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        entries, last = [], {}
        for raw in lines:
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            msg = o.get("message") if isinstance(o.get("message"), dict) else {}
            content = msg.get("content") if isinstance(msg.get("content"), list) else []
            cmds = [json.dumps(c.get("input"), ensure_ascii=False) for c in content
                    if isinstance(c, dict) and c.get("type") == "tool_use"]
            e = {"at": o.get("timestamp"), "type": o.get("type"), "raw": raw, "id": msg.get("id"),
                 "usage": msg.get("usage") if o.get("type") == "assistant" else None, "commands": cmds}
            if e["usage"] is not None and e["id"] is not None and e["id"] in last:
                prev = last.pop(e["id"])
                e["commands"] = prev["commands"] + cmds
                prev["gone"] = True
            entries.append(e)
            if e["usage"] is not None and e["id"] is not None:
                last[e["id"]] = e
        entries = [e for e in entries if not e.get("gone")]
        for i, e in enumerate(entries):
            e["i"] = i
        out[f] = entries
    return out


def marker(it):
    """The pattern that names item `it` in a note and in the agent's command: its recording (`--recording <id>`) or its
    to-do key (`--todo <key>`), whichever is used: a request's note and its answers name only the key, and a comment on
    a tracked to-do may be answered either way."""
    ways = []
    if it.get("recording") is not None:
        ways.append(r"--recording[ =]" + re.escape(str(it["recording"])) + r"(?!\d)")
    if it.get("key") is not None:
        ways.append(r"--todo[ =]'?" + re.escape(str(it["key"])) + r"(?![\w.-])")
    return re.compile("|".join(ways)) if ways else None


def span(entries, it):
    """The assistant entries (by index) that handled item `it` in one transcript: after the first line shown to the
    agent (not its own message) that names the item, at or after it was relayed, up to and including the agent's
    command that answered or dismissed it; None when either end is missing."""
    m = marker(it)
    if m is None:
        return None
    start, relayed = None, when(it.get("at"))
    for e in entries:
        at = when(e["at"])
        if e["type"] != "assistant" and (relayed is None or at is None or at >= relayed) and m.search(e["raw"]):
            start = e["i"]
            break
    if start is None:
        return None
    for e in entries[start + 1:]:
        if e["usage"] is not None and any(ANSWER.search(c) and m.search(c) for c in e["commands"]):
            return [x["i"] for x in entries[start + 1:e["i"] + 1] if x["usage"] is not None]
    return None


def token_report(dirs, items, since, until, period):
    """The token part of the report (see the module doc); sets each attributed item's "tokens" (its estimated share)."""
    found = read_transcripts(dirs, since)
    seen, totals, per = set(), dict.fromkeys(PARTS, 0), {}
    for entries in found.values():
        for e in entries:
            if e["usage"] is None or e["id"] in seen or not in_range(e["at"], since, until):
                continue
            seen.add(e["id"])
            for k in PARTS:
                totals[k] += int(e["usage"].get(k) or 0)
            p = period_of(day_of(e["at"]), period)
            per[p] = per.get(p, 0) + total(e["usage"])
    decided = [it for it in items if it["outcome"] in ("answered", "dismissed")]
    holders = {}  # (file, entry index) -> the items whose spans hold it
    spans = {}
    for f, entries in found.items():
        for n, it in enumerate(decided):
            if n in spans:
                continue
            s = span(entries, it)
            if s:
                spans[n] = (f, s)
                for i in s:
                    holders.setdefault((f, i), []).append(n)
    for n, (f, s) in spans.items():
        decided[n]["tokens"] = sum(total(found[f][i]["usage"]) / len(holders[(f, i)]) for i in s)
    out = {"transcripts": dirs, "files": len(found), "session_total": sum(totals.values()), "session_parts": totals,
           "session_by_period": [{"period": p, "tokens": per[p]} for p in sorted(per)]}
    for outcome in ("answered", "dismissed"):
        got = [it for it in decided if it["outcome"] == outcome]
        att = [it for it in got if "tokens" in it]
        tok = round(sum(it["tokens"] for it in att))
        out[outcome] = {"items": len(got), "attributed": len(att), "tokens": tok,
                        "per_item": round(tok / len(att)) if att else None}
    return out


def parse_day(s):
    return date.fromisoformat(s) if s else None


def human(n):
    if n is None:
        return "-"
    for unit, size in (("M", 1_000_000), ("k", 1_000)):
        if abs(n) >= size:
            return f"{n / size:.1f}{unit}"
    return str(round(n))


def pct(x):
    return "-" if x is None else f"{x * 100:.0f}%"


def render(r):
    """The report as text."""
    span_ = f"{r['since'] or 'start'} to {r['until'] or 'now'}"
    out = [f"Relay report ({r['ledger']}), {span_}, by {r['period']}",
           f"{'period':<12}{'relayed':>8}{'answered':>9}{'dismissed':>10}{'open':>6}{'skipped':>8}  noise (dismissed of decided)"]
    for p in r["periods"] + [dict(r["total"], period="total")]:
        out.append(f"{p['period']:<12}{p['relayed']:>8}{p['answered']:>9}{p['dismissed']:>10}{p['open']:>6}{p['skipped']:>8}  "
                   f"{pct(p['dismissed_share'])}")
    if r["by"]:
        out += ["", "By kind and why (most dismissed first):"]
        for b in r["by"]:
            tok = f", ~{human(b['tokens_attributed'])} tokens attributed" if b.get("tokens_attributed") else ""
            out.append(f"  {b['kind']}/{b['why']}: {b['relayed']} relayed, {b['answered']} answered, {b['dismissed']} "
                       f"dismissed, {b['open']} open (noise {pct(b['dismissed_share'])}){tok}")
    if r["skipped_by"]:
        out += ["", "Skipped by the relay filter:"]
        out += [f"  {s['kind']}: {s['reason']}: {s['count']}" for s in r["skipped_by"]]
    tk = r.get("tokens")
    if tk is not None:
        out += ["", f"Tokens ({tk['files']} Claude Code transcript(s) under {', '.join(tk['transcripts'])}):"]
        parts = tk["session_parts"]
        out.append(f"  session total {human(tk['session_total'])} (input {human(parts['input_tokens'])}, cache write "
                   f"{human(parts['cache_creation_input_tokens'])}, cache read {human(parts['cache_read_input_tokens'])}, "
                   f"output {human(parts['output_tokens'])})")
        for o in ("answered", "dismissed"):
            x = tk[o]
            out.append(f"  {o}: {x['items']} item(s), {x['attributed']} attributed, ~{human(x['tokens'])} tokens"
                       + (f" (~{human(x['per_item'])} per item)" if x["per_item"] is not None else ""))
        out.append("  per item is an estimate: the turns from its note to its reply or dismiss, shared among the items "
                   "handled in the same turns")
    return "\n".join(out)
