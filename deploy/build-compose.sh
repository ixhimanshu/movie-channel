#!/bin/zsh
# Generates deploy/docker-compose.yml for Easypanel (Compose service).
# The player page, nginx config, downloader script and submitter are embedded as base64 so the
# compose file is self-contained (Easypanel has no way to upload extra files).
set -e
cd "${0:A:h}"

CRED=admin-credentials.txt
if [ ! -f $CRED ]; then
  pass=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)
  print -r -- "user: admin
password: $pass" > $CRED
  chmod 600 $CRED
fi
pass=$(awk '/^password:/{print $2}' $CRED)
# fixed salt (derived from the password) so rebuilding doesn't change the committed compose file
salt=$(print -rn -- "$pass" | shasum -a 256 | cut -c1-8)
htpasswd_line="admin:$(openssl passwd -apr1 -salt "$salt" "$pass")"

b64() { base64 -i "$1" | tr -d '\n'; }
PAGE=$(b64 ../web/index.html)
CONF=$(b64 nginx.conf)
AUTH=$(print -rn -- "$htpasswd_line" | base64 | tr -d '\n')
FETCH=$(b64 fetch-movies.sh)
SUBMIT=$(b64 submitter.py)

cat > docker-compose.yml <<EOF
services:
  ersatztv:
    image: ghcr.io/ersatztv/legacy:latest
    restart: unless-stopped
    environment:
      TZ: Asia/Kolkata
    volumes:
      - etv-config:/config
      - media:/media
    tmpfs:
      - /transcode

  player:
    image: nginx:1.27-alpine
    restart: unless-stopped
    depends_on:
      - ersatztv
    environment:
      PAGE_B64: "$PAGE"
      CONF_B64: "$CONF"
      AUTH_B64: "$AUTH"
    command: ["/bin/sh", "-c", "echo \$\$PAGE_B64 | base64 -d > /usr/share/nginx/html/index.html && echo \$\$CONF_B64 | base64 -d > /etc/nginx/conf.d/default.conf && echo \$\$AUTH_B64 | base64 -d > /etc/nginx/admin.htpasswd && exec nginx -g 'daemon off;'"]

  fetcher:
    image: alpine:3.20
    restart: unless-stopped
    environment:
      FETCH_B64: "$FETCH"
    command: ["/bin/sh", "-c", "echo \$\$FETCH_B64 | base64 -d > /fetch.sh && exec sh /fetch.sh"]
    volumes:
      - media:/media

  submitter:
    image: python:3.12-alpine
    restart: unless-stopped
    environment:
      SUBMIT_B64: "$SUBMIT"
    # yt-dlp is installed fresh on every start so YouTube changes are picked up by a restart
    command: ["/bin/sh", "-c", "apk add --no-cache ffmpeg nodejs >/dev/null && pip install -q --no-cache-dir --root-user-action=ignore 'yt-dlp[default]' && mkdir -p /app && echo \$\$SUBMIT_B64 | base64 -d > /app/submitter.py && exec python -u /app/submitter.py"]
    volumes:
      - media:/media
      - etv-config:/etv:ro

volumes:
  etv-config:
  media:
EOF
echo "wrote deploy/docker-compose.yml ($(wc -c < docker-compose.yml) bytes); admin login in deploy/$CRED"
