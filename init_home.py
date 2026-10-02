"""`sync.py init`: set a firstmate home up to mirror its backlog into one Basecamp project.

Reads the project through the `basecamp` CLI login and discovers every id the sync
needs: account and project from the URL, each card table in the dock (matched to a
backlog repo by its title, case-insensitively, against the home's data/projects.md, or
by --repo-map), each table's columns by their exact names, the login's own identity,
the captain, the chats in the dock, and, when the dock has one message board, the
GitHub repos behind the mapped repos' <home>/projects/<repo> origins, whose releases
the sync announces there. Any missing or ambiguous table, column or
identity is refused with a message; nothing is guessed. A card table whose title
matches no registered repo, and that --repo-map does not name, is skipped: it is left
out of the config and its columns are never read.

It then writes <home>/data/basecamp-sync/config.json and the empty hand-kept side files,
installs and enables a per-home systemd user timer (every 5 minutes, 240s cap), and
registers the comment wake check through the home's own bin/fm-check-register.sh.
Re-running with the same inputs changes nothing; --dry-run prints the discovered config
and the planned installs and writes nothing, locally or in Basecamp.

With --no-cards no card table is read and the config has no "tables" or "repos"; every
registered repo with a GitHub origin becomes a release source.

The only Basecamp write is creating a missing regular column, and only with
--create-missing-columns.
"""
import argparse, hashlib, json, os, re, subprocess, sys

from sync import COLUMNS

HERE = os.path.dirname(os.path.abspath(__file__))
CREATABLE = ("Figuring it out", "In progress", "Ready for QA")  # Triage, Not now and Done are built in
CHECK_ID = "basecamp-sync"
SIDE_FILES = {"extra-repos.json": {}, "figuring.json": {}, "not-now.json": {}, "skip.json": [],
              "boards.json": {}, "decisions.json": {}, "pending-comments.jsonl": None}

CHECK = """#!/usr/bin/env bash
# Wakes this home's firstmate when the Basecamp sync records a new captain comment,
# question or approval (data/basecamp-sync/pending-comments.jsonl) or a failed run,
# once per change. Installed by firstmate-basecamp-sync `sync.py init`.
set -u
STATE_DIR="$(cd "$(dirname "$0")" && pwd)"
D="$STATE_DIR/../data/basecamp-sync"
MARK="$STATE_DIR/.basecamp-sync.seen"
n=$(cat "$D/pending-comments.jsonl" 2>/dev/null | wc -l)
f=$(grep -c "FAILED" "$D/sync.log" 2>/dev/null); f=${f:-0}
cur="$n $f"
[ "$(cat "$MARK" 2>/dev/null)" = "$cur" ] && exit 0
printf '%s\\n' "$cur" > "$MARK"
[ "$n" -gt 0 ] || [ "$f" -gt 0 ] && echo "basecamp sync: $n pending record(s), $f failed run(s) - follow the basecamp-sync skill: read data/basecamp-sync/pending-comments.jsonl and sync.log; relay comments and approvals to the main firstmate and wait for its answer before acting; answer informational questions in this home's scope with sync.py reply"
exit 0
"""


class Refuse(Exception):
    pass


def parse_url(url):
    m = re.search(r"basecamp(?:api)?\.com/(\d+)/(?:projects|buckets)/(\d+)", url)
    if not m:
        raise Refuse(f"not a Basecamp project URL: {url} (expected https://app.basecamp.com/<account>/projects/<project>)")
    return m.group(1), m.group(2)


def registered_projects(home):
    path = os.path.join(home, "data", "projects.md")
    if not os.path.exists(path):
        return []
    return [m.group(1) for m in (re.match(r"- ([A-Za-z0-9._-]+)", ln) for ln in open(path)) if m]


def unit_name(home):
    """basecamp-sync-<home path under ~, as [a-z0-9-]>: one unit pair per home."""
    rel = os.path.relpath(home, os.path.expanduser("~"))
    if rel.startswith(".."):
        rel = home
    return "basecamp-sync-" + (re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-") or "home")


def sd_quote(s):
    return f'"{s}"' if re.search(r"\s", s) else s


def diff_fields(old, new):
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


class System:
    """The local installs, behind a seam so tests never touch systemd or a real home.

    Each method returns what it changed (or, with dry, would change); [] means nothing.
    """

    def __init__(self, runner=subprocess.run, unit_dir=None):
        self.run = runner
        self.unit_dir = unit_dir or os.path.expanduser("~/.config/systemd/user")

    def preflight(self, home):
        reg = os.path.join(home, "bin", "fm-check-register.sh")
        if not os.access(reg, os.X_OK):
            raise Refuse(f"{reg} is missing or not executable; cannot register the wake check")

    def systemctl(self, *args):
        return self.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)

    def install_timer(self, name, service, timer, dry):
        changed = []
        for fname, text in ((f"{name}.service", service), (f"{name}.timer", timer)):
            path = os.path.join(self.unit_dir, fname)
            if os.path.exists(path) and open(path).read() == text:
                continue
            changed.append(f"write {path}")
            if not dry:
                os.makedirs(self.unit_dir, exist_ok=True)
                with open(path, "w") as f:
                    f.write(text)
        enabled = self.systemctl("is-enabled", f"{name}.timer").stdout.strip() == "enabled"
        active = self.systemctl("is-active", f"{name}.timer").stdout.strip() == "active"
        if changed:
            changed.append("systemctl --user daemon-reload")
        if changed or not (enabled and active):
            changed.append(f"systemctl --user enable --now {name}.timer")
        if not dry:
            for step in changed:
                if step.startswith("systemctl"):
                    r = self.systemctl(*step.split()[2:])
                    if r.returncode != 0:
                        raise RuntimeError(f"{step}: {(r.stdout + r.stderr)[:300]}")
        return changed

    def register_check(self, home, cid, script, dry):
        state = os.path.join(home, "state")
        path = os.path.join(state, f"{cid}.check.sh")
        trust = os.path.join(state, f"{cid}.check-trust")
        want = hashlib.sha256(script.encode()).hexdigest()
        same = (os.path.exists(path) and open(path).read() == script
                and (os.stat(path).st_mode & 0o777) == 0o700)
        trusted = os.path.exists(trust) and open(trust).read().split() == ["fm-custom-check-v1", want]
        if same and trusted:
            return []
        changed = ([] if same else [f"write {path} (mode 700)"]) + [f"{home}/bin/fm-check-register.sh {cid}"]
        if dry:
            return changed
        if not same:
            old = os.umask(0o077)
            try:
                with open(path, "w") as f:
                    f.write(script)
            finally:
                os.umask(old)
            os.chmod(path, 0o700)
        env = {k: v for k, v in os.environ.items() if k not in ("FM_ROOT_OVERRIDE", "FM_STATE_OVERRIDE")}
        env["FM_HOME"] = home
        r = self.run([os.path.join(home, "bin", "fm-check-register.sh"), cid],
                     capture_output=True, text=True, timeout=60, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"fm-check-register.sh {cid}: {(r.stdout + r.stderr)[:300]}")
        return changed


class Init:
    def __init__(self, url, login, home, captain=None, repo_map=(), create_missing=False, dry=False,
                 force=False, cards=True, runner=subprocess.run, system=None, sync_dir=HERE, out=print):
        self.account, self.project = parse_url(url)
        self.login, self.home = login, os.path.abspath(home)
        self.captain_arg = captain
        self.repo_map = {}
        for pair in repo_map:
            name, sep, repo = pair.partition("=")
            if not sep or not name.strip() or not repo.strip():
                raise Refuse(f"--repo-map takes <table name>=<repo>, got {pair!r}")
            self.repo_map[name.strip().lower()] = repo.strip()
        self.create_missing, self.dry, self.force, self.cards = create_missing, dry, force, cards
        if not cards and (self.repo_map or create_missing):
            raise Refuse("--no-cards cannot be combined with --repo-map or --create-missing-columns")
        self.run = runner
        self.system = system or System()
        self.sync_dir = sync_dir
        self.out = out
        self.dir = os.path.join(self.home, "data", "basecamp-sync")

    def bc(self, *args):
        cmd = ["basecamp", "-a", self.account, "-P", self.login, *args, "--json"]
        r = self.run(cmd, capture_output=True, text=True, timeout=120)
        try:
            out = json.loads(r.stdout)
        except ValueError:
            out = {"ok": False, "error": (r.stdout + r.stderr)[:300]}
        if not out.get("ok"):
            raise Refuse(f"basecamp {' '.join(args[:3])} as login {self.login}: {out.get('error')}")
        return out.get("data")

    def discover(self):
        """The config the project implies, plus the columns still to create: (config, [(board, column)])."""
        problems = []
        proj = self.bc("api", "get", f"/projects/{self.project}.json") or {}
        dock = [d for d in proj.get("dock", []) if d.get("enabled")]
        tables = {}
        for d in dock if self.cards else ():
            if d.get("name") != "kanban_board":
                continue
            board = (d.get("title") or "").strip().lower()
            if board in tables:
                problems.append(f"two card tables are titled {board!r}; rename one")
            tables[board] = str(d["id"])
        if self.cards and not tables:
            problems.append("the project has no card tables in its dock (pass --no-cards to set up without the card mirror)")
        for name in self.repo_map:
            if name not in tables:
                problems.append(f"--repo-map names {name!r}, but the project has no card table titled that "
                                f"(tables: {', '.join(sorted(tables)) or 'none'})")
        registered = registered_projects(self.home)
        repos, self.skipped = {}, []
        for board in sorted(tables):
            if board in self.repo_map:
                repo = self.repo_map[board]
                if repo not in registered:
                    problems.append(f"--repo-map maps {board!r} to {repo!r}, which is not registered in data/projects.md")
                    continue
            else:
                hits = [p for p in registered if p.lower() == board]
                if not hits:
                    self.skipped.append(board)
                    continue
                if len(hits) != 1:
                    problems.append(f"card table {board!r} matches several registered projects in data/projects.md "
                                    f"({', '.join(hits)}); pass --repo-map {board}=<repo>")
                    continue
                repo = hits[0]
            if repo in repos:
                problems.append(f"repo {repo!r} is mapped to both {repos[repo]!r} and {board!r}")
                continue
            repos[repo] = board
        if tables and not repos and not problems:
            problems.append("no card table matches a registered project in data/projects.md, so there is nothing to sync "
                            f"(skipped: {', '.join(self.skipped)}); pass --repo-map <table name>=<repo>")
        included = set(repos.values())
        cfg_tables, to_create = {}, []
        for board, tid in sorted(tables.items()):
            if board not in included:
                continue
            t = self.bc("api", "get", f"/buckets/{self.project}/card_tables/{tid}.json") or {}
            cols = {}
            for lst in t.get("lists", []):
                cols.setdefault((lst.get("title") or "").strip(), []).append(str(lst["id"]))
            entry = {"table": tid}
            for col in COLUMNS:
                ids = cols.get(col, [])
                if len(ids) > 1:
                    problems.append(f"card table {board!r} has {len(ids)} columns named {col!r}; rename all but one")
                elif ids:
                    entry[col] = ids[0]
                elif self.create_missing and col in CREATABLE:
                    to_create.append((board, col))
                else:
                    hint = (" (pass --create-missing-columns to create it)" if col in CREATABLE
                            else " (a built-in column; restore it in Basecamp)")
                    problems.append(f"card table {board!r} has no column named exactly {col!r}{hint}")
            cfg_tables[board] = entry
        me = self.bc("api", "get", "/my/profile.json") or {}
        acting = me.get("id")
        people = self.bc("api", "get", f"/projects/{self.project}/people.json") or []
        captain = self.find_captain(people, acting, problems)
        if problems:
            raise Refuse("init refused, nothing was written:\n  - " + "\n  - ".join(problems))
        cfg = {"account": self.account, "project": self.project, "captain": captain, "profile": self.login}
        if self.cards:
            cfg.update(repos=dict(sorted(repos.items())), tables=cfg_tables)
        chats = [d["id"] for d in dock if d.get("name") == "chat"]
        if chats:
            cfg["chats"] = chats
        boards = [d["id"] for d in dock if d.get("name") == "message_board"]
        gh_repos = {}
        # Without cards, every registered repo is a release source, named after itself.
        sources = repos if self.cards else {r: r for r in registered}
        for repo, board in sorted(sources.items()):
            full = self.github_repo(repo)
            if full:
                gh_repos[full] = board
        if len(boards) == 1 and gh_repos:
            cfg["releases"] = {"board": str(boards[0]), "repos": gh_repos}
        return cfg, to_create

    def side_files(self):
        """The hand-kept files to create; without cards only the pending file applies."""
        return SIDE_FILES if self.cards else {"pending-comments.jsonl": None}

    def github_repo(self, repo):
        """owner/name of <home>/projects/<repo>'s GitHub origin, following renames through gh; None if not GitHub."""
        try:
            r = self.run(["git", "-C", os.path.join(self.home, "projects", repo), "remote", "get-url", "origin"],
                         capture_output=True, text=True, timeout=30)
        except Exception:
            return None
        m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", r.stdout.strip()) if r.returncode == 0 else None
        if not m:
            return None
        full = f"{m.group(1)}/{m.group(2)}"
        try:
            v = self.run(["gh", "repo", "view", full, "--json", "nameWithOwner"], capture_output=True, text=True, timeout=30)
            if v.returncode == 0:
                return json.loads(v.stdout).get("nameWithOwner") or full
        except Exception:
            pass
        return full

    def find_captain(self, people, acting, problems):
        if self.captain_arg:
            arg = str(self.captain_arg).strip()
            if arg.isdigit():
                hits = [p for p in people if p.get("id") == int(arg)]
            else:
                hits = [p for p in people if (p.get("email_address") or "").lower() == arg.lower()]
            if len(hits) != 1:
                problems.append(f"--captain {arg} matches no person on the project"
                                + ("" if arg.isdigit() else " (emails can be hidden from this login; pass the person id)"))
                return None
        else:
            hits = [p for p in people if p.get("owner") and p.get("id") != acting]
            if len(hits) != 1:
                names = ", ".join(f"{p.get('name')} ({p.get('id')})" for p in hits) or "none"
                problems.append(f"cannot tell who the captain is: account owners on the project other than the "
                                f"login: {names}; pass --captain <person id or email>")
                return None
        if hits[0].get("id") == acting:
            problems.append(f"the captain {hits[0].get('name')} ({acting}) is the login {self.login} itself; "
                            "sign the login in as a separate user (docs/firstmate-account.md)")
            return None
        return hits[0]["id"]

    def units(self, config_path):
        name = unit_name(self.home)
        service = ("[Unit]\n"
                   f"Description=Mirror the firstmate backlog at {self.home} into Basecamp project {self.project}\n\n"
                   "[Service]\nType=oneshot\nTimeoutStartSec=240\n"
                   f"ExecStart={sd_quote(os.path.join(self.sync_dir, 'run.sh'))} {sd_quote(self.home)} {sd_quote(config_path)}\n")
        timer = ("[Unit]\n"
                 f"Description=Basecamp sync for the firstmate backlog at {self.home}, every 5 minutes\n\n"
                 "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\nAccuracySec=15s\n\n"
                 "[Install]\nWantedBy=timers.target\n")
        return name, service, timer

    def main(self):
        for sub in ("data", "state"):
            if not os.path.isdir(os.path.join(self.home, sub)):
                raise Refuse(f"{self.home} is not a firstmate home (no {sub}/)")
        if os.path.realpath(self.dir).startswith(os.path.realpath(self.sync_dir) + os.sep):
            raise Refuse(f"{self.dir} is inside this repo; the config lives in the home, never here")
        self.system.preflight(self.home)
        cfg, to_create = self.discover()
        config_path = os.path.join(self.dir, "config.json")
        existing = json.load(open(config_path)) if os.path.exists(config_path) else None
        plan = []
        for board in self.skipped:
            self.out(f"skipped card table {board!r}: no matching repo; pass --repo-map {board}=<repo> to include it")
        for board, col in to_create:
            plan.append(f"create column {col!r} in card table {board!r}")
            cfg["tables"][board][col] = "<new>"
        if existing is None:
            plan.append(f"write {config_path}")
        elif existing != cfg:
            fields = diff_fields(existing, cfg)
            if not self.force:
                msg = (f"{config_path} already exists and differs in: {', '.join(fields)}. "
                       "Check the discovered config; pass --force to replace it.")
                if not self.dry:
                    raise Refuse(msg + "\n" + json.dumps(cfg, indent=1))
                plan.append("REFUSE: " + msg)
            else:
                plan.append(f"replace {config_path} (differs in: {', '.join(fields)})")
        for name in self.side_files():
            if not os.path.exists(os.path.join(self.dir, name)):
                plan.append(f"create empty {os.path.join(self.dir, name)}")
        self.out(json.dumps(cfg, indent=1))
        if self.dry:
            plan += self.system.install_timer(*self.units(config_path), dry=True)
            plan += self.system.register_check(self.home, CHECK_ID, CHECK, dry=True)
            self.out("dry run, nothing written. Would:" if plan else "dry run: nothing to change")
            for p in plan:
                self.out(f"  {p}")
            for board in self.skipped:
                self.out(f"  skip card table {board!r} (no matching repo)")
            return plan + [f"skip card table {b!r}" for b in self.skipped]
        for board, col in to_create:
            t = cfg["tables"][board]
            data = self.bc("cards", "column", "create", col, "--card-table", t["table"], "-p", self.project) or {}
            t[col] = str(data["id"])
            self.out(f"created column {col!r} in {board!r}: {t[col]}")
        os.makedirs(self.dir, exist_ok=True)
        if existing != cfg:
            tmp = config_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cfg, f, indent=1)
            os.replace(tmp, config_path)
        for name, empty in self.side_files().items():
            path = os.path.join(self.dir, name)
            if not os.path.exists(path):
                with open(path, "w") as f:
                    f.write("" if empty is None else json.dumps(empty) + "\n")
        done = [p for p in plan if not p.startswith("create column")]
        done += self.system.install_timer(*self.units(config_path), dry=False)
        done += self.system.register_check(self.home, CHECK_ID, CHECK, dry=False)
        self.out("done:" if done else "nothing to change")
        for p in done:
            self.out(f"  {p}")
        return done


def cli(argv, **kw):
    ap = argparse.ArgumentParser(prog="sync.py init", description="Set a firstmate home up to mirror into one Basecamp project.")
    ap.add_argument("url", help="the Basecamp project URL, e.g. https://app.basecamp.com/<account>/projects/<project>")
    ap.add_argument("--login", required=True, help="the basecamp CLI profile the sync acts as")
    ap.add_argument("--home", required=True, help="the firstmate home")
    ap.add_argument("--captain", help="the captain's person id or email (default: the project's account owner)")
    ap.add_argument("--repo-map", action="append", default=[], metavar="TABLE=REPO",
                    help="map a card table to a backlog repo when their names differ (repeatable)")
    ap.add_argument("--create-missing-columns", action="store_true",
                    help="create a missing Figuring it out, In progress or Ready for QA column")
    ap.add_argument("--no-cards", action="store_true",
                    help="set up without the card-table mirror: no card tables are read or written")
    ap.add_argument("--force", action="store_true", help="replace an existing config.json that differs")
    ap.add_argument("--dry-run", action="store_true", help="print the discovered config and planned installs; write nothing")
    a = ap.parse_args(argv)
    try:
        Init(a.url, a.login, a.home, captain=a.captain, repo_map=a.repo_map,
             create_missing=a.create_missing_columns, dry=a.dry_run, force=a.force,
             cards=not a.no_cards, **kw).main()
    except Refuse as e:
        print(e, file=sys.stderr)
        return 2
    return 0
