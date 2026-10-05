"""`basecamp-mate doctor`: check everything the sync needs and say, in plain words, what to fix.

Checks, in order: Python, the basecamp CLI and its version, systemd user services, the
firstmate home, the config, the Basecamp sign-in (every call under a timeout and the
file credential store, so a locked system keyring cannot hang it), the project and the
dock tools the config uses, each mirrored card table and its columns, the background
services, the last sync, and the wake check; across the homes checked, that only one per
Basecamp login takes to-do requests account-wide. A check whose prerequisite failed is
skipped. Each problem is printed with the exact fix; the exit status is 1 when anything
is broken, 0 otherwise.

Without --home it checks every home that has basecamp-sync services installed (found
from the unit files), else $FM_HOME or ~/firstmate. Nothing is written, except that an
expired sign-in is renewed the way each sync run renews it.
"""
import argparse, calendar, os, re, shlex, shutil, subprocess, sys, time

import init_home
from behaviors import COLUMNS
from init_home import CHECK, CHECK_ID, CHECK_INBOX, unit_name
from setup_home import Basecamp, INSTALL_CLI, default_home

MIN_BASECAMP = (0, 11, 0)
STALE_MINUTES = 20  # the timer runs every 5 minutes; this many without a log line means it is not running
SETUP = "basecamp-mate setup"


class Doctor:
    def __init__(self, runner=subprocess.run, system=None, which=shutil.which, out=print, now=time.time):
        self.run, self.which, self.out, self.now = runner, which, out, now
        self.system = system or init_home.System(runner=runner)
        self.problems = 0

    def ok(self, text):
        self.out(f"  ✓ {text}")

    def bad(self, text, fix):
        self.problems += 1
        self.out(f"  ✗ {text}")
        for line in fix.splitlines():
            self.out(f"      {line}")

    def skip(self, text):
        self.out(f"  - {text}")

    # --- discovery ---

    def homes(self):
        """(home, config path) for each installed sync unit; else the default home's config."""
        found, unit_dir = [], self.system.unit_dir
        for name in sorted(os.listdir(unit_dir)) if os.path.isdir(unit_dir) else ():
            if not (name.startswith("basecamp-sync-") and name.endswith(".service")) or name.endswith("-listen.service"):
                continue
            argv = self.exec_start(os.path.join(unit_dir, name))
            if argv and len(argv) >= 3 and argv[0].endswith("run.sh"):
                found.append((argv[1], argv[2]))
        if found:
            return found
        home = default_home()
        return [(home, os.path.join(home, "data", "basecamp-sync", "config.json"))] if home else []

    @staticmethod
    def exec_start(path):
        try:
            for line in open(path):
                if line.startswith("ExecStart="):
                    return shlex.split(line[len("ExecStart="):])
        except OSError:
            pass
        return None

    # --- checks ---

    def tools(self):
        if sys.version_info < (3, 11):
            self.bad(f"Python {sys.version.split()[0]} is too old; basecamp-mate needs 3.11 or newer.",
                     "Fix: install Python 3.11 or newer from https://www.python.org/downloads/ or your package manager.")
        else:
            self.ok(f"Python {sys.version.split()[0]}")
        good = True
        if not self.which("basecamp"):
            self.bad("The Basecamp command-line tool is not installed.", f"Fix: run  {INSTALL_CLI}")
            good = False
        else:
            try:
                r = self.run(["basecamp", "--version"], capture_output=True, text=True, timeout=15)
                m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout)
            except subprocess.TimeoutExpired:
                m = None
            if not m:
                self.bad("The Basecamp command-line tool did not say its version.",
                         f"Fix: reinstall it with  {INSTALL_CLI}")
                good = False
            elif tuple(int(g) for g in m.groups()) < MIN_BASECAMP:
                self.bad(f"The Basecamp command-line tool is version {m.group(0)}; basecamp-mate needs "
                         f"{'.'.join(map(str, MIN_BASECAMP))} or newer.", f"Fix: update it with  {INSTALL_CLI}")
                good = False
            else:
                self.ok(f"Basecamp command-line tool {m.group(0)}")
        try:
            r = self.system.systemctl("show-environment")
            sd = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            sd = False
        if sd:
            self.ok("systemd user services")
        else:
            self.bad("systemd user services are not available, so the sync cannot run in the background.",
                     "Fix: basecamp-mate needs a Linux computer with systemd; on one, log in to a desktop or run\n"
                     "     loginctl enable-linger \"$USER\"")
        return good, sd

    def home(self, home):
        missing = [d for d in ("data", "state") if not os.path.isdir(os.path.join(home, d))]
        if missing:
            self.bad(f"{home} is not a firstmate home (no {' or '.join(d + '/' for d in missing)}).",
                     f"Fix: set firstmate up there, or run  {SETUP}  and give the right folder.")
            return False
        if not os.access(os.path.join(home, "bin", "fm-check-register.sh"), os.X_OK):
            self.bad(f"The firstmate home at {home} has no bin/fm-check-register.sh, so the agent cannot be woken.",
                     "Fix: update firstmate in that home, then run  " + SETUP)
            return False
        self.ok(f"firstmate home {home}")
        return True

    def config(self, home, config):
        if not os.path.exists(config):
            self.bad("This home is not connected to Basecamp yet (no settings file).", f"Fix: run  {SETUP}")
            return None
        try:
            import sync
            s = sync.Sync(home, config, dry=True, runner=None)
        except Exception as e:
            self.bad(f"The settings file {config} is broken: {e}",
                     f"Fix: run  {SETUP}  again and answer yes to replacing the settings.")
            return None
        self.ok(f"settings {config}")
        return s.cfg

    def login(self, cfg):
        profile = cfg.get("profile")
        bc = Basecamp(self.run, login=profile, account=cfg["account"])
        name = f"the Basecamp sign-in {profile!r}" if profile else "the default Basecamp sign-in"
        relogin = (f"Fix: run  {SETUP}  and sign in again when it asks, or run\n"
                   f"     BASECAMP_NO_KEYRING=1 basecamp auth login{' -P ' + profile if profile else ''} --device-code")
        st = bc.call("auth", "status", timeout=20, account=False)
        if st.get("code") == "timeout":
            self.bad(f"Checking {name} got no answer in 20 seconds. This usually means the computer's keyring "
                     "(password store) is locked and Basecamp is waiting on it.",
                     "Fix: unlock the keyring, or use the file store instead: run  " + SETUP +
                     "  again (its services use BASECAMP_NO_KEYRING=1),\n     or sign in with  "
                     f"BASECAMP_NO_KEYRING=1 basecamp auth login{' -P ' + profile if profile else ''} --device-code")
            return None
        if st.get("code") == "missing":
            return None
        d = st.get("data") or {}
        if not (st.get("ok") and d.get("authenticated")):
            self.bad(f"{name.capitalize()} is not signed in (or its saved sign-in is in the keyring, which the "
                     "background services do not read).", relogin)
            return None
        if d.get("expired"):
            if not bc.call("auth", "refresh", account=False).get("ok"):
                self.bad(f"{name.capitalize()} has expired and could not be renewed.", relogin)
                return None
            self.ok(f"{name} (it had expired; renewed it)")
        else:
            self.ok(name)
        return bc

    def project(self, bc, cfg):
        out = bc.call("api", "get", f"/projects/{cfg['project']}.json")
        if not out.get("ok"):
            if out.get("code") == "timeout":
                self.bad("Basecamp did not answer in 30 seconds.", "Fix: check the internet connection and try again.")
            else:
                self.bad(f"The sign-in cannot open Basecamp project {cfg['project']}: {out.get('error')}",
                         "Fix: in Basecamp, invite the agent's person to the project (or check it was not archived),\n"
                         f"     then run  {SETUP}  again.")
            return None
        proj = out.get("data") or {}
        self.ok(f"Basecamp project {proj.get('name')!r}")
        dock = {str(d.get("id")): d for d in proj.get("dock") or []}
        wants = [(c.get("chat") if isinstance(c, dict) else c, "chat") for c in cfg.get("chats") or []]
        if cfg.get("message_board"):
            wants.append((cfg["message_board"], "Message Board"))
        if (cfg.get("todos") or {}).get("todoset"):
            wants.append((cfg["todos"]["todoset"], "To-dos"))
        for q in (cfg.get("checkins") or {}).get("questionnaires", []):
            wants.append((q, "Automatic Check-ins"))
        for tid, what in wants:
            d = dock.get(str(tid))
            if not d or not d.get("enabled"):
                self.bad(f"The project's {what} tool ({tid}) is {'turned off' if d else 'gone'}.",
                         f"Fix: turn it back on in the project's settings in Basecamp, or run  {SETUP}  again.")
        return proj

    def card_tables(self, bc, cfg):
        if cfg.get("cards") is False or not cfg.get("tables"):
            return
        for board, t in sorted(cfg["tables"].items()):
            out = bc.call("api", "get", f"/buckets/{cfg['project']}/card_tables/{t['table']}.json")
            if not out.get("ok"):
                self.bad(f"The card table {board!r} cannot be opened: {out.get('error')}",
                         f"Fix: restore it in Basecamp (Trash or the project's settings), or run  {SETUP}  again.")
                continue
            have = {str(lst.get("id")): (lst.get("title") or "").strip() for lst in out["data"].get("lists", [])}
            missing = [c for c in COLUMNS if str(t.get(c)) not in have]
            renamed = [(c, have[str(t[c])]) for c in COLUMNS if str(t.get(c)) in have and have[str(t[c])] != c]
            for c in missing:
                fix = (f"Fix: run  {SETUP}  again; it puts the column back." if c in init_home.CREATABLE
                       else f"Fix: {c!r} is a built-in column; restore it in Basecamp, then run  {SETUP}  again.")
                self.bad(f"The card table {board!r} is missing its {c!r} column.", fix)
            for c, now in renamed:
                self.bad(f"The card table {board!r} column {c!r} was renamed to {now!r}.",
                         f"Fix: rename it back to {c!r} in Basecamp.")
            if not (missing or renamed):
                self.ok(f"card table {board!r} and its columns")

    def services(self, home, config, cfg):
        name = unit_name(home)
        units = [(f"{name}.timer", "the 5-minute sync timer", f"{name}.service")]
        if cfg.get("listen") not in (None, False):
            units.append((f"{name}-listen.service", "the event listener", f"{name}-listen.service"))
        for unit, what, svc in units:
            path = os.path.join(self.system.unit_dir, unit)
            if not os.path.exists(path):
                self.bad(f"{what.capitalize()} is not installed.", f"Fix: run  {SETUP}  again.")
                continue
            argv = self.exec_start(os.path.join(self.system.unit_dir, svc)) or []
            gone = [a for a in argv if a.endswith(("run.sh", "sync.py")) and not os.path.exists(a)]
            if gone:
                self.bad(f"{what.capitalize()} runs {gone[0]}, which is gone (was basecamp-mate moved?).",
                         f"Fix: run  {SETUP}  again from where basecamp-mate is now.")
                continue
            enabled = self.system.systemctl("is-enabled", unit).stdout.strip()
            active = self.system.systemctl("is-active", unit).stdout.strip()
            if enabled != "enabled" or active != "active":
                self.bad(f"{what.capitalize()} is not running ({enabled or 'unknown'}, {active or 'unknown'}).",
                         f"Fix: run  systemctl --user enable --now {unit}\n"
                         f"     and if it stops again, look at  journalctl --user -u {svc} -n 50")
            else:
                self.ok(f"{what} is running")

    def last_sync(self, config):
        log = os.path.join(os.path.dirname(config), "sync.log")
        lines = [ln.strip() for ln in open(log)] if os.path.exists(log) else []
        lines = [ln for ln in lines if ln]
        if not lines:
            self.bad("The sync has not run yet.", "Fix: wait five minutes, or run  " + SETUP + "  again for a test sync.")
            return
        last = lines[-1]
        when = None
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)", last)
        if m:
            when = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ"))
        if "FAILED" in last:
            self.bad(f"The last sync failed: {last}", self.failure_fix(last))
        elif when and self.now() - when > STALE_MINUTES * 60:
            self.bad(f"The sync has not run for {int((self.now() - when) // 60)} minutes.",
                     "Fix: check the sync timer above; it should run every 5 minutes.")
        else:
            self.ok("the last sync worked")

    @staticmethod
    def failure_fix(line):
        low = line.lower()
        if "token refresh" in low or "unauthorized" in low or "401" in low or "auth" in low:
            return f"Fix: the Basecamp sign-in needs renewing; run  {SETUP}  and sign in again."
        if "timeout" in low or "timed out" in low:
            return "Fix: Basecamp was slow or the keyring is locked; run  " + SETUP + "  again to use the file store."
        if "tasks-axi" in low:
            return "Fix: install tasks-axi (the card mirror reads the task list with it), or turn the card mirror off with  " + SETUP
        return "Fix: read the lines before it in sync.log; if it keeps failing, run  " + SETUP + "  again."

    def wake_check(self, home, cfg):
        script = CHECK_INBOX if cfg.get("inbox") is not None else CHECK
        try:
            todo = self.system.register_check(home, CHECK_ID, script, dry=True)
        except Exception as e:
            todo = [str(e)]
        if todo:
            self.bad("The agent's wake check is not registered, so new Basecamp messages will not wake it.",
                     f"Fix: run  {SETUP}  again, or run\n     FM_HOME={shlex.quote(home)} "
                     f"{shlex.quote(os.path.join(home, 'bin', 'fm-check-register.sh'))} {CHECK_ID}")
        else:
            self.ok("the wake check is registered")

    def main(self, home=None, config=None):
        self.out("basecamp-mate doctor")
        cli_ok, sd_ok = self.tools()
        homes = [(os.path.abspath(home), config or os.path.join(home, "data", "basecamp-sync", "config.json"))] if home \
            else self.homes()
        if not homes:
            self.bad("No basecamp-mate setup was found on this computer.", f"Fix: run  {SETUP}")
        anywhere = {}  # (account, profile) -> the homes taking to-do requests account-wide with that login
        for h, c in homes:
            self.out(f"\n{h}")
            if not self.home(h):
                continue
            cfg = self.config(h, c)
            if cfg is None:
                continue
            opts = cfg.get("assigned_todos")
            if isinstance(opts, dict) and opts.get("scope") == "account":
                anywhere.setdefault((str(cfg.get("account")), cfg.get("profile")), []).append(h)
            bc = self.login(cfg) if cli_ok else None
            if bc and self.project(bc, cfg):
                self.card_tables(bc, cfg)
            elif cli_ok:
                self.skip("skipped the project and card table checks until the problems above are fixed")
            if sd_ok:
                self.services(h, c, cfg)
            self.last_sync(c)
            self.wake_check(h, cfg)
        self.account_wide(anywhere)
        self.out("\n" + (f"{self.problems} problem(s) found; fix them in order, then run  basecamp-mate doctor  again."
                         if self.problems else "Everything looks good."))
        return 1 if self.problems else 0


    def account_wide(self, anywhere):
        """Warn when more than one home here takes to-do requests account-wide with the same Basecamp login."""
        for (account, profile), hs in sorted(anywhere.items(), key=str):
            if len(hs) < 2:
                continue
            login = f"the sign-in {profile!r}" if profile else "the default sign-in"
            self.out("")
            self.bad(f"{len(hs)} homes take to-dos assigned anywhere in account {account} with {login}, so each such "
                     f"to-do would reach all of them: {', '.join(hs)}",
                     'Fix: keep "assigned_todos": {"scope": "account"} only in the main home\'s settings, and set the '
                     'others to "assigned_todos": {} (their own project only).')


def cli(argv, **kw):
    ap = argparse.ArgumentParser(prog="basecamp-mate doctor",
                                 description="Check the Basecamp sync and explain any problem with its fix.")
    ap.add_argument("--home", help="the firstmate home (default: every home with sync services installed)")
    ap.add_argument("--config", help="its config.json (default: <home>/data/basecamp-sync/config.json)")
    a = ap.parse_args(argv)
    return Doctor(**kw).main(a.home, a.config)
