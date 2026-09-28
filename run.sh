#!/bin/sh
# Hourly entry point: refresh the Basecamp OAuth token when it is within three days
# of expiry, then mirror the backlog. No model calls; see sync.py for the bounds.
# Usage: run.sh <firstmate home> <config.json>   (extra args, e.g. --dry-run, pass through)
[ $# -ge 2 ] || { echo "usage: run.sh <home> <config.json> [--dry-run]" >&2; exit 2; }
home=$1 config=$2; shift 2
here=$(cd "$(dirname "$0")" && pwd)
log="$(dirname "$config")/sync.log"
left=$(basecamp auth status --json 2>/dev/null | python3 -c 'import json,sys,re; e=json.load(sys.stdin)["data"].get("expires_in","0h"); m=re.match(r"(\d+)h",e); print(m.group(1) if m else 0)' 2>/dev/null)
if [ "${left:-0}" -lt 72 ]; then
  basecamp auth refresh --json </dev/null >/dev/null 2>&1 || echo "$(date -u +%FT%TZ) FAILED token refresh; the captain must run basecamp auth login" >> "$log"
fi
exec timeout 1500 python3 "$here/sync.py" --home "$home" --config "$config" "$@"
