#!/bin/zsh
# Resumable download that reconnects if the transfer stalls.
# usage: fetch.sh <url> <output-file>
url="$1"; out="$2"
for attempt in {1..100}; do
  curl -sL -C - --speed-limit 20000 --speed-time 90 -o "$out" "$url" && { echo "OK $out"; exit 0; }
  rc=$?
  [ $rc -eq 33 ] && { echo "OK (already complete) $out"; exit 0; }   # range not satisfiable = done
  /bin/sleep 3
done
echo "FAIL $out"; exit 1
