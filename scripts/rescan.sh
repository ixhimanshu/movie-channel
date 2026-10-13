#!/bin/zsh
# Rescan media/Movies after adding or removing movies, then rebuild the channel schedule.
DIR=${0:A:h}
echo "Scanning Movies library..."
"$DIR/etv-api.sh" POST /api/libraries/1/scan >/dev/null
# give the scanner time to probe new files before rebuilding the schedule
/bin/sleep 20
# deleted movies are only flagged as missing by the scan; purge them so they leave the schedule
"$DIR/etv-api.sh" POST /api/maintenance/empty_trash >/dev/null
echo "Rebuilding Movie Channel schedule..."
"$DIR/etv-api.sh" POST /api/channels/1/playout/reset >/dev/null
echo "Done. Guide refreshes in your IPTV app on its next EPG update."
