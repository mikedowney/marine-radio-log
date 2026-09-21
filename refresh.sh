#!/bin/sh
# Wipe the message log and recordings for a clean test after a software change.
# Stop the watchkeeper first, or a recording deleted while the transcriber is
# reading it comes back as an 'error' row.
echo "Deleting message log and audio files"

sqlite3 ~/watchkeeper/watchkeeper.db "delete from messages;"
rm -f ~/watchkeeper/audio/*

echo "Ready to run"
