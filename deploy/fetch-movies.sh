#!/bin/sh
# Runs in the "fetcher" service (alpine) on Easypanel.
# Downloads the starter public-domain films into /media/Movies.
# Partial downloads stay in /media/incoming so ErsatzTV never schedules a broken file.
apk add --no-cache curl >/dev/null

D=/media/Movies
I=/media/incoming
mkdir -p "$D" "$I"

get() {
  t="$1"; src="$2"
  [ -f "$D/$t/$t.mp4" ] && { echo "have $t"; return; }
  mkdir -p "$I/$t"
  # exit 33 = range not satisfiable, i.e. the partial file is already complete
  until curl -sL -C - --retry 5 --retry-all-errors -o "$I/$t/$t.mp4" "https://archive.org/download/$src" || [ $? -eq 33 ]; do
    sleep 5
  done
  mv "$I/$t" "$D/$t" && echo "done $t"
}

get "Carnival of Souls (1962)"          CarnivalofSouls/CarnivalOfSouls.mp4
get "His Girl Friday (1940)"            his_girl_friday/his_girl_friday.mp4
get "House on Haunted Hill (1959)"      house_on_haunted_hill_ipod/house_on_haunted_hill.mp4
get "The Little Shop of Horrors (1960)" TheLittleShopOfHorrors1960/The-Little-Shop-of-Horrors.mp4
get "Scarlet Street (1945)"             ScarletStreet/Scarlet_Street.mp4
get "The General (1926)"                TheGeneral/The_General_512kb.mp4
get "The Phantom of the Opera (1925)"   ThePhantomoftheOpera/Phantom_of_the_Opera_512kb.mp4

echo "ALL DONE"
# keep the container alive so Easypanel doesn't restart-loop it; safe to stop/delete afterwards
sleep infinity
