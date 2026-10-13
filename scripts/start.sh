#!/bin/zsh
# Start ErsatzTV and keep the Mac awake so the channel stays on.
open -a ErsatzTV-Legacy
# web player with the movie counter (port 8410)
if ! lsof -nP -iTCP:8410 -sTCP:LISTEN >/dev/null 2>&1; then
  nohup node "${0:A:h}/../web/server.js" > /Users/Shared/MovieChannel/player.log 2>&1 &!
fi
/bin/sleep 3
# -i: no idle sleep, -s: no system sleep on AC power. Runs until you close this terminal or press Ctrl+C.
echo "ErsatzTV started. Keeping the Mac awake (Ctrl+C to stop)..."
"${0:A:h}/status.sh"
exec caffeinate -is
