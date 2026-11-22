#!/usr/bin/env python3
"""AIS ingest for Radio Log.

Holds a connection to the PredictWind DataHub and keeps a table of
current vessel targets. Runs as its own service, writing its own
database (~/watchkeeper/ais.db), deliberately: the watch must not be
able to fall over because an AIS feed misbehaved. watchkeeper.db is
never opened by this process, so no amount of AIS traffic can contend
for its write lock or change its journal mode. Consumers ATTACH ais.db
and join on their own terms.

Only CURRENT state is kept -- one row per vessel, overwritten. The feed
carries ~700 vessels updating roughly once a minute, which as history
would be about 300 MB a day and almost entirely redundant: 89% of those
vessels are moored and repeating one position. What the log actually
needs is "where is this vessel now", so that is all that is stored.

  python3 ais_ingest.py              run until stopped
  python3 ais_ingest.py --selftest   check the decoder and DB, then exit
  python3 ais_ingest.py --once 60    collect for 60 s and report
"""

import argparse, logging, math, os, re, socket, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ais_decode as A
import ais_match as M

DB = os.path.expanduser("~/watchkeeper/ais.db")
WK = os.path.expanduser("~/watchkeeper/watchkeeper.db")
UDP_PORT, TCP_PORT = 11101, 11102
# Drop a target this long after we last heard ANYTHING about it. The
# DataHub stops sending a vessel once its data passes its own Target
# Age (45 min here), so two hours is well clear of anything live while
# clearing departed boats within the hour. Six, the first guess, left
# vessels on the page five hours after they had gone.
STALE_HOURS = 2
COMMIT_EVERY = 10.0       # seconds; ~150 upserts a commit
PRUNE_EVERY = 900.0
# How long ais_track keeps position reports. Two weeks at the rate
# this water produces them; see the header for what that costs.
# Raising it is a straight multiple -- the table has no other growth
# term. Measured 18 Nov: 19,733 fixes an hour at 36.7 bytes a row, so
# 0.7 MB per hour kept, and 336 hours is about 235 MB. It lives on the
# audio tmpfs, which is resident memory, so this number is spent out
# of the same pocket as the recordings.
TRACK_HOURS = 336
# If a commit fails the pending points stay queued for the next one.
# This bounds that queue so a database that is unwritable for an hour
# cannot take the process out with it. Dropping the oldest is the right
# way round: the newest points are the ones the map is about to want.
TRACK_MAX = 100000
# The track lives in RAM, in its own database on the audio mount.
#
# Not in ais.db: ais_targets, message_vessel and dsc_vessel are the
# only record of where a vessel was when it transmitted and must
# survive a reboot. The track need not -- it refills on its own, which
# is what makes it the one table here that can go in volatile storage.
#
# Sharing the audio mount rather than creating a second tmpfs: the RAM
# cost is identical either way, and refresh.sh removes only *.wav, so
# this file is left alone. The trade is that the WAV ring and the track
# now compete for one mount, and whichever fills it stops both.
TRACK_DB = os.path.expanduser("~/watchkeeper/audio/ais_track.db")
TAG_EVERY = 5.0
WORD_CACHE_S = 6 * 3600   # the vocabulary of this water moves slowly
# The fleet changes slowly; compiling 700 name patterns took half of every
# pass. Cached, a 5 s cadence costs less than the old 30 s one did.
VESSEL_CACHE_S = 60.0
# Never reach back further than this. A transmission older than the
# oldest AIS we hold cannot be tagged honestly -- we would be pairing
# October speech with tonight's positions.
TAG_WINDOW_S = 3600
# Transcripts are written asynchronously: a row appears with no text and
# is filled in a second or two later, sometimes much later when a re-roll
# runs. So the watermark lags, and every pass re-examines the last few
# minutes. Writes are INSERT OR REPLACE, so re-tagging costs nothing and
# a slow transcript is no longer lost because a faster one overtook it.
RESCAN_LAG_S = 300

log = logging.getLogger("ais")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ais_targets (
    mmsi        INTEGER PRIMARY KEY,
    name        TEXT,
    ship_type   INTEGER,
    lat         REAL,
    lon         REAL,
    sog         REAL,
    cog         REAL,
    source      TEXT,      -- INTERNET (DataHub repeat) or VHF
    pos_at      TEXT,      -- when the POSITION was acquired, not received
    seen_at     TEXT       -- when we last heard anything about it
);
CREATE INDEX IF NOT EXISTS idx_ais_name ON ais_targets(name);
CREATE INDEX IF NOT EXISTS idx_ais_pos  ON ais_targets(pos_at);

-- WHO a vessel is, as opposed to where she is. Never pruned.
--
-- ais_targets above is deliberately short-lived: a target is dropped
-- two hours after we last hear of it, because a stale range and
-- bearing on the log page is worse than none. That is right for
-- position and wrong for identity -- a ship that sailed yesterday has
-- four days of track in ais_track and, without this table, no name to
-- put on it. Identity does not expire the way a fix does.
--
-- Written only when a report actually carries static data (AIS types
-- 5 and 24), which is a small fraction of traffic, so this costs the
-- SD card almost nothing. Position reports do not touch it.
--
-- first_seen and last_seen are identity sightings, not position ones:
-- when we first and last learned who this MMSI is. For when she was
-- last anywhere, ask ais_track.
CREATE TABLE IF NOT EXISTS ais_vessel (
    mmsi        INTEGER PRIMARY KEY,
    name        TEXT,
    ship_type   INTEGER,
    first_seen  TEXT,
    last_seen   TEXT
);
CREATE INDEX IF NOT EXISTS idx_vessel_name ON ais_vessel(name);

-- Vessels the operator has pinned. Kept here rather than in a browser
-- so the same list appears on the phone and at the chart table.
CREATE TABLE IF NOT EXISTS ais_favourite (
    mmsi        INTEGER PRIMARY KEY,
    added_at    TEXT
);


-- One ROW PER VESSEL, not per transmission: "Seaways Athens, Seaways
-- Athens, this is Jamie Lee" names two boats, and both are worth having.
CREATE TABLE IF NOT EXISTS message_vessel (
    started_at  TEXT    NOT NULL,   -- joins messages/history on
    channel     TEXT    NOT NULL,   -- (started_at, channel)
    mmsi        INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    role        TEXT,               -- to = being called, from = speaking
    seq         INTEGER,            -- order spoken
    -- The fix AS IT WAS when this was said. The live row in ais_targets
    -- moves; a map link built from it later would point somewhere the
    -- vessel no longer was, which is worse than no link.
    lat         REAL,
    lon         REAL,
    own_lat     REAL,               -- and where we were, to frame the map
    own_lon     REAL,
    distance_nm REAL,
    bearing_deg REAL,
    rule        TEXT,               -- which test accepted it
    pos_age_s   INTEGER,            -- how stale the fix was when matched
    tagged_at   TEXT,
    PRIMARY KEY (started_at, channel, mmsi)
);
CREATE INDEX IF NOT EXISTS idx_mv_started ON message_vessel(started_at DESC);

-- One ROW PER VESSEL, not per call. A DSC exchange names two ships --
-- "from <sender> to <addressee>" and the acknowledgement back -- and both
-- are usually in the fleet with positions. Keyed on dsc_id alone this
-- could only ever hold the sender.
CREATE TABLE IF NOT EXISTS dsc_vessel (
    dsc_id      INTEGER NOT NULL,
    mmsi        INTEGER NOT NULL,
    role        TEXT,               -- from = sender, to = addressed
    name        TEXT,
    lat         REAL,
    lon         REAL,
    own_lat     REAL,
    own_lon     REAL,
    distance_nm REAL,
    bearing_deg REAL,
    pos_age_s   INTEGER,
    sog         REAL,
    tagged_at   TEXT,
    PRIMARY KEY (dsc_id, mmsi)
);

CREATE TABLE IF NOT EXISTS tagger_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    watermark   TEXT
);

CREATE TABLE IF NOT EXISTS ais_self (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    lat   REAL,
    lon   REAL,
    at    TEXT
);
"""

RMC = re.compile(r"^\$..RMC,")
GGA = re.compile(r"^\$..GGA,")


def iso(epoch=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.gmtime(epoch if epoch else time.time()))


def nmea_latlon(f, i):
    """NMEA ddmm.mmmm + hemisphere -> signed degrees."""
    try:
        raw, hemi = f[i], f[i + 1]
        if not raw or not hemi:
            return None
        deg = int(float(raw) / 100)
        val = deg + (float(raw) - deg * 100) / 60.0
        return -val if hemi in ("S", "W") else val
    except (ValueError, IndexError):
        return None


def migrate(db):
    """message_vessel was keyed one-row-per-transmission at first, which
    cannot hold both boats in a hail. Rebuild it if that old shape is
    still there; it holds only derived data, so nothing is lost."""
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(message_vessel)")}
    except sqlite3.Error:
        return
    try:
        dv = {r[1] for r in db.execute("PRAGMA table_info(dsc_vessel)")}
    except sqlite3.Error:
        dv = set()
    # ALTERed, never rebuilt. These tables hold the only record of where
    # a vessel was when it transmitted; ais_targets is overwritten in
    # place, so a dropped tag cannot be reconstructed. That has already
    # cost ten of them.
    #
    # Declared rather than inferred from the CREATE TABLE above: that is
    # CREATE TABLE IF NOT EXISTS, so a column added to the text never
    # reaches a database that already exists. message_vessel carried sog
    # in the schema and not in the file, and every tag pass failed on it.
    for _tab, _want in (("message_vessel", (("sog", "REAL"),)),
                        ("dsc_vessel", (("sog", "REAL"),))):
        try:
            _got = {r[1] for r in db.execute("PRAGMA table_info(%s)" % _tab)}
        except sqlite3.Error:
            continue
        if not _got:
            continue
        for _col, _typ in _want:
            if _col not in _got:
                log.info("adding %s to %s", _col, _tab)
                db.execute("ALTER TABLE %s ADD COLUMN %s %s"
                           % (_tab, _col, _typ))
                db.commit()
    if dv and "role" not in dv:
        log.info("rebuilding dsc_vessel: a DSC exchange names two vessels")
        db.execute("DROP TABLE dsc_vessel")
        db.execute("DELETE FROM tagger_state")
        db.commit()
    if cols and not {"role", "lat", "own_lat"} <= cols:
        log.info("rebuilding message_vessel for multi-vessel tagging")
        db.execute("DROP TABLE message_vessel")
        db.execute("DROP TABLE IF EXISTS dsc_vessel")
        db.execute("DELETE FROM tagger_state")
        db.commit()


TRACK_SCHEMA = """
-- One ROW PER POSITION REPORT, which is what ais_targets can never be:
-- it is keyed on mmsi and every report overwrites the row.
--
-- Normalised against it rather than repeating itself. Name, ship type
-- and source belong to the vessel and change almost never; only
-- position, speed and course change per report, so only those are
-- here, joined back on mmsi. That join now crosses databases, which
-- SQLite does not mind.
--
-- Scaled integers, not REAL. 1e-6 degrees is about 11 cm, finer than
-- anything AIS carries, and it measured 29.3 bytes a row against 44.6
-- for REAL. sog and cog are tenths; both are NULL when the report did
-- not carry them, which is not the same as zero.
--
-- WITHOUT ROWID with the key as the primary key: an ordinary table
-- plus an index on (mmsi, t) measured 65.7 bytes a row to store the
-- same thing twice. There is no index on t -- see the sweep in
-- prune(), which is a sequential scan and is fast enough.
CREATE TABLE IF NOT EXISTS trk.ais_track (
    mmsi  INTEGER NOT NULL,
    t     INTEGER NOT NULL,   -- unix seconds UTC, from the tag block
                              -- when it carries one, else arrival
    lat   INTEGER NOT NULL,   -- degrees x 1e6
    lon   INTEGER NOT NULL,   -- degrees x 1e6
    sog   INTEGER,            -- knots x 10, NULL if not reported
    cog   INTEGER,            -- degrees x 10, NULL if not reported
    PRIMARY KEY (mmsi, t)
) WITHOUT ROWID;
"""


def fstype(path):
    """The filesystem type holding path: the longest mount point in
    /proc/mounts that is a prefix of it."""
    p = os.path.realpath(path)
    best, kind = "", ""
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for ln in fh:
                f = ln.split()
                if len(f) < 3:
                    continue
                mp = f[1].replace("\\040", " ")
                if (p == mp or p.startswith(mp.rstrip("/") + "/")) \
                        and len(mp) >= len(best):
                    best, kind = mp, f[2]
    except OSError:
        return ""
    return kind


def attach_track(db):
    """Open the in-RAM track database beside the main one.

    Idempotent and self-healing: a reboot empties the audio mount, so
    the file is simply absent and is recreated empty.
    """
    d = os.path.dirname(TRACK_DB)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        log.warning("cannot create %s: %s", d, e)
    kind = fstype(d)
    if kind != "tmpfs":
        # Loud, because this failure is otherwise invisible. If the
        # audio mount did not come up, the track lands on the SD card
        # and quietly writes tens of megabytes a day to it -- the one
        # thing moving it here was meant to stop.
        log.warning("%s is on %s, not tmpfs: the AIS track will be "
                    "written to the SD CARD", d, kind or "an unknown "
                    "filesystem")
    db.execute("ATTACH DATABASE ? AS trk", (TRACK_DB,))
    db.execute("PRAGMA trk.journal_mode=WAL")
    # Nothing to survive a crash: the file is in RAM, so a crash loses
    # it whether or not it was flushed. The fsyncs buy nothing.
    db.execute("PRAGMA trk.synchronous=OFF")
    db.executescript(TRACK_SCHEMA)
    db.commit()

    # One-time move of whatever the on-disk table collected before this
    # patch, so the history already recorded is not thrown away.
    try:
        present = db.execute(
            "SELECT COUNT(*) FROM main.sqlite_master"
            " WHERE type='table' AND name='ais_track'").fetchone()[0]
    except sqlite3.Error:
        present = 0
    if present:
        n = db.execute("SELECT COUNT(*) FROM main.ais_track").fetchone()[0]
        db.execute("INSERT OR REPLACE INTO trk.ais_track"
                   " SELECT mmsi, t, lat, lon, sog, cog FROM main.ais_track")
        db.execute("DROP TABLE main.ais_track")
        db.commit()
        log.info("moved %d track point(s) out of ais.db into %s", n, TRACK_DB)
        try:
            db.execute("VACUUM main")
        except sqlite3.Error as e:
            log.warning("could not reclaim the space in ais.db: %s", e)
    return db


def seed_vessels(db):
    """Fill ais_vessel once, from everything already on disk.

    Runs only when the table is empty, so it is a one-off on the first
    start after this change and a no-op for ever after.

    Four sources, poorest first so the better ones overwrite:
    ais_track knows every MMSI we have ever had a position for and
    nothing else about them; message_vessel and dsc_vessel keep the
    name of anything ever tagged; ais_targets has name and type for
    whatever is on the water right now.
    """
    try:
        if db.execute("SELECT 1 FROM ais_vessel LIMIT 1").fetchone():
            return
    except sqlite3.Error:
        return

    n = 0
    try:
        rows = db.execute(
            "SELECT mmsi, MIN(t) a, MAX(t) b FROM trk.ais_track"
            " GROUP BY mmsi").fetchall()
        db.executemany(
            "INSERT OR IGNORE INTO ais_vessel"
            " (mmsi, first_seen, last_seen) VALUES (?,?,?)",
            [(r[0], iso(r[1]), iso(r[2])) for r in rows])
        n += len(rows)
    except sqlite3.Error as exc:
        log.warning("seeding from the track: %s", exc)

    for tab, when in (("message_vessel", "tagged_at"),
                      ("dsc_vessel", "tagged_at"),
                      ("ais_targets", "seen_at")):
        try:
            rows = db.execute(
                "SELECT mmsi, name, %s, %s FROM %s"
                " WHERE name IS NOT NULL AND name <> ''"
                % ("ship_type" if tab == "ais_targets" else "NULL",
                   when, tab)).fetchall()
        except sqlite3.Error:
            continue            # a box that has never tagged anything
        for mmsi, name, styp, seen in rows:
            db.execute(
                "INSERT INTO ais_vessel"
                " (mmsi, name, ship_type, first_seen, last_seen)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(mmsi) DO UPDATE SET"
                "   name = COALESCE(excluded.name, ais_vessel.name),"
                "   ship_type = COALESCE(excluded.ship_type,"
                "                        ais_vessel.ship_type),"
                "   first_seen = MIN(COALESCE(ais_vessel.first_seen,"
                "                             excluded.first_seen),"
                "                    excluded.first_seen),"
                "   last_seen = MAX(COALESCE(ais_vessel.last_seen,"
                "                            excluded.last_seen),"
                "                   excluded.last_seen)",
                (mmsi, name, styp, seen or iso(), seen or iso()))
    db.commit()
    named = db.execute("SELECT COUNT(*) FROM ais_vessel"
                       " WHERE name IS NOT NULL").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM ais_vessel").fetchone()[0]
    log.info("seeded ais_vessel: %d vessel(s), %d named", total, named)


def open_db(path):
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")      # do not block the watch
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    db.commit()
    migrate(db)
    db.executescript(SCHEMA)
    db.commit()
    attach_track(db)
    seed_vessels(db)
    return db


class Ingest:
    def __init__(self, db):
        self.db = db
        self.r = A.Reassembler()
        self.pend = {}          # mmsi -> merged fields awaiting commit
        # Not a dict: every report is kept, so this cannot be keyed on
        # mmsi the way pend is.
        self.track = []         # (mmsi, t, lat, lon, sog, cog) awaiting commit
        self.selfpos = None
        self.stats = {"lines": 0, "vdm": 0, "pos": 0, "name": 0, "vhf": 0,
                      "track": 0, "dropped": 0}

    def feed(self, line):
        self.stats["lines"] += 1
        tag, sent = A.split_tag(line.strip())
        if not sent:
            return
        if RMC.match(sent) or GGA.match(sent):
            f = sent.split(",")
            ll = nmea_latlon(f, 3) if RMC.match(sent) else nmea_latlon(f, 2)
            lo = nmea_latlon(f, 5) if RMC.match(sent) else nmea_latlon(f, 4)
            if ll is not None and lo is not None:
                self.selfpos = (ll, lo)
            return
        got = self.r.feed(sent)
        if not got:
            return
        talker, payload = got
        self.stats["vdm"] += 1
        d = A.decode(payload)
        if not d or not d.get("mmsi"):
            return
        src = "VHF" if talker == "AI" else (A.tag_source(tag) or "INTERNET")
        if src == "VHF":
            self.stats["vhf"] += 1
        acq = A.tag_time(tag)
        rec = self.pend.setdefault(d["mmsi"], {})
        rec["source"] = src
        rec["seen_at"] = iso()
        if d.get("name"):
            rec["name"] = d["name"]
            self.stats["name"] += 1
        if d.get("ship_type") is not None:
            rec["ship_type"] = d["ship_type"]
        if d.get("lat") is not None:
            rec.update(lat=d["lat"], lon=d["lon"], sog=d.get("sog"),
                       cog=d.get("cog"), pos_at=iso(acq) if acq else iso())
            self.stats["pos"] += 1
            # Appended here and not in commit(). pend is a merge buffer
            # keyed by mmsi, flushed every COMMIT_EVERY seconds; a
            # Class A vessel reporting at 2 s has four of every five
            # fixes overwritten in it before anything is written. The
            # track has to be taken at arrival or it is a sample.
            self.track.append((
                d["mmsi"], int(acq or time.time()),
                int(round(d["lat"] * 1e6)), int(round(d["lon"] * 1e6)),
                None if d.get("sog") is None
                else int(round(d["sog"] * 10)),
                None if d.get("cog") is None
                else int(round(d["cog"] * 10))))
            self.stats["track"] += 1
            if len(self.track) > TRACK_MAX:
                drop = len(self.track) - TRACK_MAX
                del self.track[:drop]
                self.stats["dropped"] += drop

    def commit(self):
        if self.selfpos:
            self.db.execute(
                "INSERT INTO ais_self(id,lat,lon,at) VALUES(1,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET lat=excluded.lat,"
                " lon=excluded.lon, at=excluded.at",
                (self.selfpos[0], self.selfpos[1], iso()))
        for mmsi, r in self.pend.items():
            cols = ["mmsi"] + list(r)
            vals = [mmsi] + [r[k] for k in r]
            sets = ", ".join("%s=excluded.%s" % (c, c) for c in r)
            self.db.execute(
                "INSERT INTO ais_targets(%s) VALUES(%s)"
                " ON CONFLICT(mmsi) DO UPDATE SET %s"
                % (", ".join(cols), ", ".join("?" * len(cols)), sets), vals)
            # Identity, which outlives the target row. Only when this
            # cycle actually learned something: a position report
            # carries neither name nor type, and writing on every one
            # would multiply this table's traffic to the card for
            # nothing. COALESCE so a later sighting that carries only a
            # type cannot blank a name we already have.
            if r.get("name") or r.get("ship_type") is not None:
                self.db.execute(
                    "INSERT INTO ais_vessel"
                    " (mmsi, name, ship_type, first_seen, last_seen)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT(mmsi) DO UPDATE SET"
                    "   name = COALESCE(excluded.name, ais_vessel.name),"
                    "   ship_type = COALESCE(excluded.ship_type,"
                    "                        ais_vessel.ship_type),"
                    "   last_seen = excluded.last_seen",
                    (mmsi, r.get("name"), r.get("ship_type"),
                     r.get("seen_at") or iso(), r.get("seen_at") or iso()))
        if self.track:
            # OR REPLACE, not IGNORE: the same report reaches us twice
            # when the DataHub repeats something also heard on VHF, and
            # both land on the same (mmsi, t). Either is correct; taking
            # the later one is at least consistent.
            #
            # One executemany inside the transaction the upserts above
            # are already in, so a cycle is still one commit. Per-row
            # commits here would put several fsyncs a second onto the
            # SD card, which is the whole reason audio lives in tmpfs.
            self.db.executemany(
                "INSERT OR REPLACE INTO trk.ais_track"
                " (mmsi, t, lat, lon, sog, cog) VALUES (?,?,?,?,?,?)",
                self.track)
            self.track.clear()
        n = len(self.pend)
        self.pend.clear()
        self.db.commit()
        return n

    def prune(self):
        cut = iso(time.time() - STALE_HOURS * 3600)
        c = self.db.execute("DELETE FROM ais_targets WHERE seen_at < ?", (cut,))
        self.db.commit()
        if c.rowcount:
            log.info("pruned %d target(s) unseen for %dh", c.rowcount,
                     STALE_HOURS)
        # The track outlives the target by a long way: a vessel dropped
        # from ais_targets two hours after her last report still has a
        # track answering where she went. Deliberately a separate
        # window, not a join.
        #
        # TRACK_HOURS is two weeks, and webui's MAP_MAX_HOURS must
        # match it -- the map offers windows out to whatever this keeps.
        # At the rate actually observed here, about 20,000 fixes an
        # hour, two weeks is ~6.8 million rows and ~400 MB. This table
        # lives in tmpfs, so that is 400 MB of the Pi's RAM at steady
        # state. Shortening the window is the lever if that is ever
        # needed; MAP_MAX_HOURS has to come down with it.
        #
        # No index on t, so this walks the primary-key btree. Measured
        # at 0.07 s over two million rows, against 0.02 s with an index
        # that would have cost 48% more disk on every row. Every 15
        # minutes, that is not a trade worth making.
        cut_t = int(time.time() - TRACK_HOURS * 3600)
        c = self.db.execute("DELETE FROM trk.ais_track WHERE t < ?",
                            (cut_t,))
        self.db.commit()
        if c.rowcount:
            log.info("pruned %d track point(s) older than %dh",
                     c.rowcount, TRACK_HOURS)



class Tagger:
    """Attach a vessel to a transmission, but only when we are sure.

    watchkeeper.db is opened READ-ONLY and never written; results go to
    ais.db keyed on (started_at, channel), which both messages and
    history carry. The watch cannot be affected by anything here.
    """

    def __init__(self, db, wk_path=WK):
        self.db = db
        self.wk_path = wk_path
        self._ves = None
        self._ves_at = 0.0
        self._words_at = 0.0
        self._conn = None

    def _wk(self):
        """One read-only connection, kept open.

        Reopening per pass re-reads the schema every time, and at a 5 s
        cadence that is 17,000 opens a day against the database the watch
        is writing to. Held open it is idle between queries and takes no
        lock."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                "file:%s?mode=ro" % self.wk_path, uri=True, timeout=10)
        return self._conn

    def _drop_conn(self):
        try:
            if self._conn:
                self._conn.close()
        except sqlite3.Error:
            pass
        self._conn = None

    def watermark(self):
        r = self.db.execute("SELECT watermark FROM tagger_state"
                            " WHERE id=1").fetchone()
        if r:
            return r[0]
        # First run: start from now. Backfilling would pair old speech
        # with today's positions, which is a fabrication, not a gap.
        now = iso()
        self.db.execute("INSERT INTO tagger_state(id,watermark)"
                        " VALUES(1,?)", (now,))
        self.db.commit()
        return now

    def vessels(self):
        """Named targets whose position is under an hour old.

        Cached for VESSEL_CACHE_S: compiling the patterns dominates the
        pass, and a vessel list up to a minute stale changes nothing --
        the positions in it are already minutes old by design."""
        now = time.time()
        if self._ves is not None and now - self._ves_at < VESSEL_CACHE_S:
            return self._ves
        cut = iso(now - M.MAX_POS_AGE_S)
        rows = self.db.execute(
            "SELECT mmsi, name, lat, lon,"
            " CAST((julianday('now') - julianday(pos_at)) * 86400 AS INTEGER),"
            " sog"
            " FROM ais_targets WHERE name IS NOT NULL AND lat IS NOT NULL"
            " AND pos_at >= ?", (cut,)).fetchall()
        self._ves = M.compile_vessels(rows)
        self._ves_at = now
        return self._ves

    def own(self):
        return self.db.execute("SELECT lat, lon FROM ais_self"
                               " WHERE id=1").fetchone()

    def _fix(self, v, own):
        d = b = None
        if own and own[0] is not None:
            d = M.haversine_nm(own[0], own[1], v["lat"], v["lon"])
            b = M.bearing_deg(own[0], own[1], v["lat"], v["lon"])
        return d, b

    def words(self):
        """Count every word in the log, for ais_match's rarity gate.

        The corpus is its own dictionary. Rebuilt a few times a day:
        29,000 transcripts is about a second of work and the
        distribution does not move quickly. A failure here leaves the
        table as it was, which disables fuzzy matching rather than
        letting it run unguarded.
        """
        now = time.time()
        if self._words_at and now - self._words_at < WORD_CACHE_S:
            return
        try:
            rows = self._wk().execute(
                "SELECT transcript FROM history"
                " WHERE transcript IS NOT NULL"
                " AND trim(transcript) <> ''").fetchall()
        except sqlite3.Error as exc:
            log.warning("could not build the word table: %s", exc)
            self._drop_conn()
            return
        freq = {}
        for (t,) in rows:
            for w in M.PLAINWORD.findall(t):
                w = w.lower()
                freq[w] = freq.get(w, 0) + 1
        M.WORD_FREQ.clear()
        M.WORD_FREQ.update(freq)
        self._words_at = now
        log.info("word table: %d transcripts, %d distinct words",
                 len(rows), len(freq))

    def pass_once(self, verbose=False):
        self.words()
        mark = self.watermark()
        floor = iso(time.time() - TAG_WINDOW_S)
        since = max(mark, floor)
        ves = self.vessels()
        own = self.own()
        if not ves:
            return 0, 0
        tagged = scanned = 0
        try:
            wk = self._wk()
            rows = wk.execute(
                "SELECT started_at, channel, transcript FROM messages"
                " WHERE started_at > ? AND transcript IS NOT NULL"
                " AND trim(transcript) <> '' ORDER BY started_at", (since,)
            ).fetchall()
            dsc = wk.execute(
                "SELECT id, self_id, address FROM dsc WHERE started_at > ?"
                " AND self_id IS NOT NULL AND ok = 1", (since,)).fetchall()
        except sqlite3.Error as exc:
            log.warning("could not read watchkeeper.db: %s", exc)
            self._drop_conn()
            return 0, 0

        for started, chan, tr in rows:
            scanned += 1
            hits = M.match_all(tr, ves)
            if not hits:
                continue
            self.db.execute("DELETE FROM message_vessel WHERE started_at=?"
                            " AND channel=?", (started, chan))
            for i, hit in enumerate(hits):
                d, b = self._fix(hit, own)
                self.db.execute(
                    "INSERT OR REPLACE INTO message_vessel(started_at,"
                    " channel, mmsi, name, role, seq, lat, lon, own_lat,"
                    " own_lon, distance_nm, bearing_deg, rule, pos_age_s,"
                    " sog, tagged_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (started, chan, hit["mmsi"], hit["name"], hit["role"], i,
                     hit["lat"], hit["lon"],
                     own[0] if own else None, own[1] if own else None,
                     d, b, hit["rule"], hit["pos_age_s"],
                     hit.get("sog"), iso()))
                tagged += 1
                log.info("vessel %s%s on %s%s", hit["name"],
                         "" if not hit["role"] else " (%s)" % hit["role"],
                         chan,
                         "" if d is None else " -- %.1f nm, %03.0f deg" % (d, b))

        by_mmsi = {str(v["mmsi"]): v for v in ves}
        for dsc_id, self_id, address in dsc:
            sid = str(self_id or "").strip()
            aid = str(address or "").strip()
            # Both ends of the call, when we can place them. A call
            # addressed to itself is a self-test and yields one row.
            ends = [("from", sid)]
            if aid and aid != sid:
                ends.append(("to", aid))
            for role, mmsi in ends:
                v = by_mmsi.get(mmsi)
                if not v:
                    continue
                d, b = self._fix(v, own)
                # Written once. The rescan window is 300 s wide and turns
                # every 5 s, so without this each call was re-tagged about
                # sixty times -- sixty WARNING lines, and each pass
                # replacing the fix taken seconds after the transmission
                # with a later, more distant one. An existing tag is
                # overwritten only to give it a position it did not have.
                prior = self.db.execute(
                    "SELECT lat FROM dsc_vessel WHERE dsc_id=? AND mmsi=?",
                    (dsc_id, v["mmsi"])).fetchone()
                if prior is not None and (prior[0] is not None
                                          or v["lat"] is None):
                    continue
                self.db.execute(
                    "INSERT OR REPLACE INTO dsc_vessel(dsc_id, mmsi, role,"
                    " name, lat, lon, own_lat, own_lon, distance_nm,"
                    " bearing_deg, pos_age_s, sog, tagged_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dsc_id, v["mmsi"], role, v["name"], v["lat"], v["lon"],
                     own[0] if own else None, own[1] if own else None,
                     d, b, v["pos_age_s"], v.get("sog"), iso()))
                tagged += 1
                log.warning("DSC %s %s is %s%s%s", role, mmsi, v["name"],
                            "" if d is None
                            else " -- %.1f nm, %03.0f deg" % (d, b),
                            "" if prior is None else " (position added)")

        # Lag the watermark rather than advancing it to the newest row
        # seen; see RESCAN_LAG_S.
        self.db.execute("UPDATE tagger_state SET watermark=? WHERE id=1",
                        (iso(time.time() - RESCAN_LAG_S),))
        self.db.commit()
        if verbose:
            print("vessels in range with a fresh fix: %d" % len(ves))
            print("transmissions scanned: %d, tagged: %d" % (scanned, tagged))
        return scanned, tagged


def discover(timeout=60):
    """Learn the DataHub's address from its UDP broadcast."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", UDP_PORT))
        s.settimeout(timeout)
        _, addr = s.recvfrom(4096)
        return addr[0]
    except (OSError, socket.timeout):
        return None
    finally:
        s.close()


def run(host, db, duration=None, tag=True):
    ing = Ingest(db)
    tagger = Tagger(db) if tag else None
    end = time.time() + duration if duration else None
    backoff = 1
    bad_lines = 0          # counted across reconnects, not reset by one
    last_c = last_p = last_t = time.time()
    while end is None or time.time() < end:
        try:
            if host is None:
                host = discover(30)
                if host is None:
                    log.warning("no DataHub broadcast on UDP %d; retrying",
                                UDP_PORT)
                    continue
                log.info("DataHub discovered at %s", host)
            with socket.create_connection((host, TCP_PORT), timeout=15) as s:
                log.info("connected to %s:%d", host, TCP_PORT)
                backoff = 1
                s.settimeout(30)
                buf = b""
                while end is None or time.time() < end:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise OSError("DataHub closed the connection")
                    buf += chunk
                    while b"\n" in buf:
                        ln, buf = buf.split(b"\n", 1)
                        # One unreadable sentence must not cost the
                        # connection. Anything raised in here used to
                        # unwind to the reconnect handler below, which
                        # drops the socket, backs off, and loses the
                        # batch that had not been committed yet -- an
                        # expensive answer to a single bad line, and
                        # over the air bad lines are ordinary.
                        try:
                            ing.feed(ln.decode("ascii", "replace"))
                        except Exception as exc:        # noqa: BLE001
                            bad_lines += 1
                            if bad_lines in (1, 10, 100) or bad_lines % 1000 == 0:
                                log.warning("unreadable sentence (%d so far): "
                                            "%s: %s", bad_lines,
                                            type(exc).__name__, exc)
                    now = time.time()
                    if now - last_c >= COMMIT_EVERY:
                        ing.commit()
                        last_c = now
                    if now - last_p >= PRUNE_EVERY:
                        ing.prune()
                        last_p = now
                    if tagger and now - last_t >= TAG_EVERY:
                        try:
                            tagger.pass_once()
                        except Exception as exc:        # noqa: BLE001
                            log.warning("tag pass: %s", exc)
                        last_t = now
        except Exception as exc:                        # noqa: BLE001
            if end is not None and time.time() >= end:
                break
            log.warning("AIS feed: %s -- reconnecting in %ds", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    ing.commit()
    return ing


def selftest(db):
    ok = True
    d = A.decode("15MwvSP000000000000000000000")
    print("decoder: %s" % ("ok" if d and d.get("mmsi") else "FAILED"))
    ok &= bool(d and d.get("mmsi"))
    n = db.execute("SELECT count(*) FROM ais_targets").fetchone()[0]
    print("ais_targets table present, %d row(s)" % n)
    print("ais_self:", db.execute("SELECT lat,lon,at FROM ais_self").fetchone()
          or "(not yet seen)")
    tr = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT mmsi), MIN(t), MAX(t)"
        "  FROM trk.ais_track").fetchone()
    if tr[0]:
        print("ais_track:    %d point(s), %d vessel(s), %.1f h of history"
              % (tr[0], tr[1], (tr[3] - tr[2]) / 3600.0))
    else:
        print("ais_track:    empty (nothing recorded yet)")
    try:
        mb = os.path.getsize(TRACK_DB) / 1048576.0
        wal = TRACK_DB + "-wal"
        if os.path.exists(wal):
            mb += os.path.getsize(wal) / 1048576.0
    except OSError:
        mb = 0.0
    print("track store:  %s  (%s, %.1f MB incl. WAL)"
          % (TRACK_DB, fstype(os.path.dirname(TRACK_DB)) or "?", mb))
    try:
        v = db.execute("SELECT COUNT(*), COUNT(name), COUNT(ship_type)"
                       "  FROM ais_vessel").fetchone()
        f = db.execute("SELECT COUNT(*) FROM ais_favourite").fetchone()[0]
        print("ais_vessel:   %d known, %d named, %d typed; %d favourite(s)"
              % (v[0], v[1], v[2], f))
    except sqlite3.Error as exc:
        print("ais_vessel:   unavailable (%s)" % exc)
    jm = db.execute("PRAGMA journal_mode").fetchone()[0]
    print("journal_mode: %s" % jm)
    print("database:     %s" % db.execute("PRAGMA database_list").fetchone()[2])
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", help="DataHub IP (default: discover via UDP)")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--once", type=int, metavar="SECONDS")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--tagonce", action="store_true",
                    help="run one tagging pass and exit")
    ap.add_argument("--no-tag", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    db = open_db(a.db)
    if a.selftest:
        sys.exit(0 if selftest(db) else 1)
    if a.tagonce:
        Tagger(db).pass_once(verbose=True)
        return
    ing = run(a.host, db, a.once, tag=not a.no_tag)
    if a.once:
        print("\nstats: %s" % ing.stats)
        for r in db.execute(
                "SELECT count(*), count(name), count(lat),"
                " sum(source='VHF') FROM ais_targets").fetchall():
            print("targets=%s named=%s positioned=%s vhf=%s" % r)
        print("self:", db.execute("SELECT lat,lon,at FROM ais_self").fetchone())


if __name__ == "__main__":
    main()
