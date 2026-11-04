#!/usr/bin/env bash
#
# Clear the live message log for a clean test run.
#
#   ./refresh.sh          messages + audio.  history and thermal are KEPT
#   ./refresh.sh --all    also wipes history and the thermal series
#
# The distinction matters. `messages` is the live log -- capped at
# max_messages and emptied of anything whose WAV has gone. `history` and
# `thermal` are the permanent record that the analysis queries read, and the
# reason they exist is that two days of gain experiments were lost when the
# only copy lived in `messages`.
#
# So: plain refresh for a clean-looking log, --all only when you genuinely
# want to discard the measurements too.
set -eu

DB="${DB:-$HOME/watchkeeper/watchkeeper.db}"
AUDIO="${AUDIO:-$HOME/watchkeeper/audio}"
[ -r "$DB" ] || { echo "no database at $DB"; exit 1; }

count() { sqlite3 "$DB" "select count(*) from $1;" 2>/dev/null || echo "-"; }

printf 'before:  messages %s   history %s   thermal %s\n' \
  "$(count messages)" "$(count history)" "$(count thermal)"

sqlite3 "$DB" "delete from messages;"
# -f so an already-empty directory is not an error, and a glob that matches
# nothing is ignored rather than passed through literally.
rm -f "$AUDIO"/*.wav

if [ "${1:-}" = "--all" ]; then
    sqlite3 "$DB" "delete from history; delete from thermal;"
    # Reclaim the pages rather than leaving the file at its high-water mark.
    sqlite3 "$DB" "vacuum;"
    echo "history and thermal wiped as well"
elif [ -n "${1:-}" ]; then
    echo "unknown option: $1  (only --all is understood)" >&2
    exit 2
fi

sudo systemctl restart watchkeeper@$USER watchkeeper-web@$USER

printf 'after:   messages %s   history %s   thermal %s\n' \
  "$(count messages)" "$(count history)" "$(count thermal)"
echo "Ready to run"
