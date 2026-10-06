# ---- Data.search ------------------------------------------------------
    def search(self, query, channel=None, limit=200):
        """Full-text-ish search over the permanent history table.

        history, not messages: messages is a 1000-row ring that turns over in
        about half a day and is wiped by refresh.sh, so searching it would
        answer "what did I hear recently" rather than "what did I ever hear".
        history keeps every row since the table was created.

        Rows whose WAV is still in the ring get a msg_id so the result can
        offer playback; older ones are text only, which is the honest state
        of affairs -- the audio really is gone.

        Terms are ANDed. Anything in double quotes is one phrase. LIKE is
        case-insensitive for ASCII in SQLite, and on a table of this size a
        scan is well under a millisecond, so there is no index to maintain
        and no FTS table to keep in step with the writer.
        """
        terms = [t for t in re.findall(r'"([^"]*)"|(\S+)', query or "")
                 for t in (t[0] or t[1],) if t.strip()]
        if not terms:
            return {"query": query or "", "terms": [], "total": 0, "rows": []}

        # % and _ are LIKE wildcards; a user searching for "22A_USCG" means
        # the literal underscore.
        def esc(t):
            return (t.replace("\\", "\\\\").replace("%", "\\%")
                     .replace("_", "\\_"))

        where = ["h.transcript IS NOT NULL", "h.transcript <> ''"]
        args = []
        for t in terms:
            where.append("h.transcript LIKE ? ESCAPE '\\'")
            args.append("%" + esc(t) + "%")
        if channel:
            where.append("h.channel = ?")
            args.append(channel)
        clause = " AND ".join(where)

        with self._conn() as c:
            total = c.execute(
                "SELECT count(*) FROM history h WHERE " + clause,
                args).fetchone()[0]
            rows = c.execute(
                "SELECT h.hid, h.channel, h.freq_hz, h.started_at,"
                " h.duration_s, h.snr_db, h.quality, h.status, h.transcript,"
                " m.id AS msg_id"
                "  FROM history h"
                "  LEFT JOIN messages m"
                "    ON m.started_at = h.started_at AND m.channel = h.channel"
                " WHERE " + clause +
                " ORDER BY h.started_at DESC LIMIT ?",
                args + [max(1, min(int(limit), 1000))]).fetchall()

        out = []
        for r in rows:
            try:
                started = datetime.fromisoformat(r["started_at"])
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                epoch = started.timestamp()
            except (ValueError, TypeError):
                epoch = 0.0
            out.append({
                "hid": r["hid"],
                "id": r["msg_id"],          # null once the WAV has aged out
                "channel": r["channel"],
                "mhz": round((r["freq_hz"] or 0) / 1e6, 3),
                "started": epoch,
                "duration": round(r["duration_s"] or 0, 1),
                "snr": round(r["snr_db"], 1) if r["snr_db"] is not None else None,
                "q": (round(r["quality"]) if r["quality"] is not None else None),
                "status": r["status"],
                "text": (r["transcript"] or "").strip(),
                "pos": wk.positions_in(r["transcript"] or ""),
            })
        return {"query": query or "", "terms": terms,
                "total": total, "shown": len(out), "rows": out}

    def search_channels(self):
        """Channels that appear in history, for the filter dropdown."""
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT DISTINCT channel FROM history"
                " WHERE transcript IS NOT NULL AND transcript <> ''"
                " ORDER BY channel")]
