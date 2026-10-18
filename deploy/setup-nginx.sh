#!/usr/bin/env bash
# Adds https://bot.collxct.ng to the main Collxct nginx (collxct-nginx-1) with ZERO downtime:
#   1. back up nginx.conf
#   2. add a port-80 block that serves ACME challenges from /etc/letsencrypt/acme-webroot
#      (already mounted into the container) → nginx -t → graceful reload
#   3. issue the certificate with certbot --webroot (nginx keeps running; renewals too)
#   4. add the 443 block proxying to trading-bot-web:8000 → nginx -t → graceful reload
# Any failed `nginx -t` restores the backup before anything is reloaded. Safe to re-run.
#
# Run on the server as root:  bash /opt/trading-bot/deploy/setup-nginx.sh
set -euo pipefail

CONF=/opt/collxct/nginx/nginx.conf
NGINX=collxct-nginx-1
DOMAIN=bot.collxct.ng
WEBROOT=/etc/letsencrypt/acme-webroot
MARKER="    # Per-app server blocks, each mounted in from its own repo."
BACKUP="$CONF.bak-$(date +%Y%m%d-%H%M%S)"

cp -p "$CONF" "$BACKUP"
echo "Backup: $BACKUP"
mkdir -p "$WEBROOT/.well-known/acme-challenge"

# Insert text before $MARKER, rewriting the file IN PLACE: the container bind-mounts this
# exact inode, so tools that replace the file (sed -i, editors) would be invisible to nginx.
insert_block() {
  python3 - "$CONF" "$MARKER" "$1" <<'PY'
import sys
path, marker, block = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "r+") as f:
    s = f.read()
    if marker not in s:
        sys.exit(f"marker not found in {path}")
    f.seek(0); f.write(s.replace(marker, block + marker, 1)); f.truncate()
PY
}

test_and_reload() {
  if docker exec "$NGINX" nginx -t; then
    docker exec "$NGINX" nginx -s reload
    echo "nginx reloaded"
  else
    cat "$BACKUP" > "$CONF"
    echo "nginx -t FAILED — restored $BACKUP, nothing was reloaded" >&2
    exit 1
  fi
}

# ── 1+2. HTTP block (ACME + redirect) ──────────────────────────────────────
if grep -q "server_name $DOMAIN;" "$CONF"; then
  echo "HTTP block for $DOMAIN already present"
else
  insert_block "    # ── $DOMAIN (trading bot PWA, /opt/trading-bot) ──
    server {
        listen 80;
        listen [::]:80;
        server_name $DOMAIN;

        # ACME HTTP-01 from the mounted /etc/letsencrypt: issue/renew without stopping nginx.
        location /.well-known/acme-challenge/ {
            root $WEBROOT;
        }
        location / {
            return 301 https://\$host\$request_uri;
        }
    }

"
  test_and_reload
fi

# ── 3. Certificate ─────────────────────────────────────────────────────────
if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
  echo "Certificate for $DOMAIN already exists"
else
  certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --non-interactive --agree-tos \
    --deploy-hook "docker exec $NGINX nginx -s reload"
fi

# ── 4. HTTPS block ─────────────────────────────────────────────────────────
if grep -q "live/$DOMAIN/fullchain.pem" "$CONF"; then
  echo "HTTPS block for $DOMAIN already present"
else
  insert_block "    server {
        listen 443 ssl;
        listen [::]:443 ssl;
        http2 on;
        server_name $DOMAIN;

        ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_ciphers HIGH:!aNULL:!MD5;
        ssl_prefer_server_ciphers on;
        ssl_session_cache shared:SSL:10m;
        ssl_session_timeout 1d;

        add_header Strict-Transport-Security \"max-age=31536000; includeSubDomains\" always;
        add_header X-Content-Type-Options \"nosniff\" always;
        add_header Referrer-Policy \"strict-origin-when-cross-origin\" always;

        client_max_body_size 1m;

        # Resolved per request via Docker DNS: nginx still starts/reloads if the bot's
        # web container is down, so a bot outage can never take the other sites offline.
        resolver 127.0.0.11 valid=30s ipv6=off;
        set \$bot_web http://trading-bot-web:8000;

        location / {
            proxy_pass \$bot_web;
            proxy_set_header Host              \$host;
            proxy_set_header X-Real-IP         \$remote_addr;
            proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header X-Forwarded-Host  \$host;
        }
    }

"
  test_and_reload
fi

echo
curl -sS -o /dev/null -w "https://$DOMAIN/health → HTTP %{http_code}\n" "https://$DOMAIN/health" || true
echo "Done. Backup kept at $BACKUP"
