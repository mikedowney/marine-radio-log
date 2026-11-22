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
import argparse, os, sqlite3, subprocess, sys, time

DB = os.path.expanduser("~downeym/watchkeeper/watchkeeper.db")
UNIT = "watchkeeper@downeym"
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


def meminfo():
    """(MemAvailable MB, swap used MB) from /proc/meminfo, or (None, None).

    RSS alone does not say whether the machine is in trouble. A 5 GB
    watchkeeper on a quiet box is fine; the same process with 900 MB
    available and swap in use is the state that preceded both lockouts.
    """
    try:
        v = {}
        for line in open("/proc/meminfo"):
            k, _, rest = line.partition(":")
            v[k] = float(rest.split()[0]) / 1024.0      # kB -> MB
        swap_used = v.get("SwapTotal", 0.0) - v.get("SwapFree", 0.0)
        return v.get("MemAvailable"), swap_used
    except Exception:                                    # noqa: BLE001
        return None, None


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
    ap.add_argument("--urgent-idle-s", type=float, default=300.0,
                    help="required quiet period when the machine is under "
                         "memory pressure (default 5 min)")
    ap.add_argument("--urgent-mb", type=float, default=5000.0,
                    help="RSS at which the shorter quiet period applies")
    ap.add_argument("--min-available-mb", type=float, default=2000.0,
                    help="MemAvailable below this counts as pressure")
    ap.add_argument("--max-swap-mb", type=float, default=256.0,
                    help="swap in use above this counts as pressure")
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
    avail, swap = meminfo()

    # Why the shorter window applies, in words, so the journal says what the
    # machine was doing rather than only what the script decided.
    pressure = []
    if rss >= a.urgent_mb:
        pressure.append("RSS %.0f MB" % rss)
    if avail is not None and avail < a.min_available_mb:
        pressure.append("only %.0f MB available" % avail)
    if swap is not None and swap > a.max_swap_mb:
        pressure.append("%.0f MB of swap in use" % swap)
    need_idle = a.urgent_idle_s if pressure else a.idle_s

    age, pend = db_state(need_idle)

    why = []
    if rss < a.threshold_mb:
        why.append("RSS %.0f MB is under the %.0f MB threshold"
                   % (rss, a.threshold_mb))
    if age is not None and age >= 0 and age < need_idle:
        why.append("last transmission %.0f s ago, need %.0f s quiet%s"
                   % (age, need_idle,
                      " (shortened: %s)" % ", ".join(pressure)
                      if pressure else ""))
    if pend is not None and pend > 0:
        why.append("%d transcription(s) still pending" % pend)
    if age == -1.0:
        why.append("database unreadable")

    state = "RSS %.0f MB" % rss
    if avail is not None:
        state += ", %.0f MB available" % avail
    if swap is not None:
        state += ", %.0f MB swap" % swap

    if why:
        log("skipping restart: %s  [%s]" % ("; ".join(why), state))
        return

    if a.dry_run:
        log("WOULD restart %s  [RSS %.0f MB, quiet %.0f s, %d pending]"
            % (UNIT, rss, age if age is not None else -1, pend or 0))
        return

    log("restarting %s  [%s, quiet %.0f s%s]"
        % (UNIT, state, age if age is not None else -1,
           "; under pressure: " + ", ".join(pressure) if pressure else ""))
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
    # The decoder finishes loading at about 31 s, so a sample at 20 s read
    # the memory before the thing being measured had been allocated.
    time.sleep(45)
    new = rss_mb()
    new_avail, new_swap = meminfo()
    log("restarted; RSS %.0f -> %s MB%s"
        % (rss, "%.0f" % new if new else "unknown",
           ", %.0f MB available, %.0f MB swap" % (new_avail, new_swap)
           if new_avail is not None else ""))


if __name__ == "__main__":
    main()
