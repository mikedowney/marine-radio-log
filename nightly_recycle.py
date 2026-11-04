#!/usr/bin/env python3
"""Restart the watchkeeper overnight, but only if it needs it and only if
nothing is happening.

ONNX Runtime's CPU arena sizes itself to the largest encoder activations it
has ever allocated and never returns them, so RSS climbs with the longest
clip decoded rather than with uptime: 2.4 GB at load, 5.5 GB a day later.
Nothing breaks, but the headroom goes, and a full swap is how two lockouts
started. A restart resets it in about 15 seconds.

Three guards, because a watchkeeper that restarts itself during a distress
call is worse than one using too much memory:

  1. RSS below the threshold        -> nothing to reclaim, skip.
  2. Any transmission in the last N -> the band is live, skip.
  3. Transcriptions still pending   -> work in flight, skip.
  4. Already restarted today        -> once a night is enough, skip.

Run it every few minutes across the quiet hours rather than once: it then
fires on the FIRST genuinely quiet window it finds instead of giving up
because the one moment it looked happened to be busy.

Audio lives on tmpfs, which survives a service restart, and watchkeeper
re-queues interrupted transcriptions on startup -- so a restart between
transmissions costs nothing but the dead air.

--dry-run reports the decision and changes nothing.
"""
import argparse, getpass, os, sqlite3, subprocess, sys, time

# The account the watchkeeper services run under. Under cron as root neither
# the login name nor SUDO_USER is any use, so set WATCHKEEPER_USER there.
USER = (os.environ.get("WATCHKEEPER_USER")
        or os.environ.get("SUDO_USER")
        or getpass.getuser())

DB = os.path.expanduser("~%s/watchkeeper/watchkeeper.db" % USER)
UNIT = "watchkeeper@%s" % USER
PATTERN = "watchkeeper.py --preset"


def log(msg):
    print(msg)
    subprocess.run(["logger", "-t", "nightly-recycle", msg], check=False)


def rss_mb():
    try:
        pid = subprocess.run(["pgrep", "-f", PATTERN],
                             capture_output=True, text=True).stdout.split()
        if not pid:
            return None
        for line in open("/proc/%s/status" % pid[0]):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def db_state(idle_s):
    """(seconds since the last transmission, pending count) or (None, None)."""
    if not os.path.exists(DB):
        return None, None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
        try:
            age = con.execute(
                "SELECT (julianday('now') - julianday(MAX(started_at)))*86400.0"
                " FROM history").fetchone()[0]
            pend = con.execute(
                "SELECT COUNT(*) FROM history WHERE status='pending'"
            ).fetchone()[0]
        finally:
            con.close()
        return (None if age is None else float(age)), int(pend)
    except sqlite3.Error as exc:
        log("database unreadable (%s) -- not restarting" % exc)
        return -1.0, -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold-mb", type=float, default=4000.0)
    ap.add_argument("--idle-s", type=float, default=1200.0,
                    help="required quiet period, seconds (default 20 min)")
    ap.add_argument("--stamp", default="/var/lib/watchkeeper-recycle.stamp")
    ap.add_argument("--any-time", action="store_true",
                    help="ignore the once-a-day stamp")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    today = time.strftime("%Y-%m-%d")
    if not a.any_time and not a.dry_run:
        try:
            if open(a.stamp).read().strip() == today:
                return      # already recycled today; stay silent in cron
        except OSError:
            pass

    rss = rss_mb()
    if rss is None:
        log("watchkeeper not running -- nothing to do")
        return
    age, pend = db_state(a.idle_s)

    why = []
    if rss < a.threshold_mb:
        why.append("RSS %.0f MB is under the %.0f MB threshold"
                   % (rss, a.threshold_mb))
    if age is not None and age >= 0 and age < a.idle_s:
        why.append("last transmission %.0f s ago, need %.0f s quiet"
                   % (age, a.idle_s))
    if pend is not None and pend > 0:
        why.append("%d transcription(s) still pending" % pend)
    if age == -1.0:
        why.append("database unreadable")

    if why:
        log("skipping restart: %s  [RSS %.0f MB]" % ("; ".join(why), rss))
        return

    if a.dry_run:
        log("WOULD restart %s  [RSS %.0f MB, quiet %.0f s, %d pending]"
            % (UNIT, rss, age if age is not None else -1, pend or 0))
        return

    log("restarting %s  [RSS %.0f MB, quiet %.0f s]"
        % (UNIT, rss, age if age is not None else -1))
    r = subprocess.run(["systemctl", "restart", UNIT], capture_output=True,
                       text=True)
    if r.returncode != 0:
        log("restart FAILED rc=%d %s" % (r.returncode, r.stderr.strip()[:200]))
        return
    try:                                  # only after a restart succeeds
        os.makedirs(os.path.dirname(a.stamp), exist_ok=True)
        with open(a.stamp, "w") as fh:
            fh.write(today + "\n")
    except OSError as exc:
        log("could not write stamp %s (%s) -- may retry tonight"
            % (a.stamp, exc))
    time.sleep(20)
    new = rss_mb()
    log("restarted; RSS %.0f MB -> %s MB"
        % (rss, "%.0f" % new if new else "unknown"))


if __name__ == "__main__":
    main()
