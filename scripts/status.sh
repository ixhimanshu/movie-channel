#!/bin/zsh
# Show whether the channel is up, the URLs to put in your IPTV app, and what's on now.
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
if ! curl -s -o /dev/null --max-time 3 http://localhost:8409/iptv/channels.m3u; then
  echo "ErsatzTV is NOT running. Start it with: scripts/start.sh"; exit 1
fi
echo "ErsatzTV is running."
echo
echo "Watch in any browser (with movie counter): http://$IP:8410"
echo
echo "Add these to your IPTV app (TV must be on the same Wi-Fi):"
echo "  Playlist (M3U): http://$IP:8409/iptv/channels.m3u"
echo "  Guide (EPG):    http://$IP:8409/iptv/xmltv.xml"
echo "  Admin UI:       http://$IP:8409"
echo
curl -s http://localhost:8409/iptv/xmltv.xml | python3 -c '
import sys, datetime, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.read().lstrip("﻿"))
now = datetime.datetime.now(datetime.timezone.utc)
parse = lambda s: datetime.datetime.strptime(s, "%Y%m%d%H%M%S %z")
progs = sorted(root.findall("programme"), key=lambda p: parse(p.get("start")))
upcoming = [p for p in progs if parse(p.get("stop")) > now][:4]
if not upcoming:
    print("Guide is empty - run scripts/rescan.sh after adding movies to media/Movies")
for i, p in enumerate(upcoming):
    start = parse(p.get("start")).astimezone()
    label = "NOW " if i == 0 else "NEXT" if i == 1 else "    "
    title = p.findtext("title")
    print(f"  {label} {start:%a %H:%M}  {title}")
'
