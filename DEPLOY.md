# Deploying the trading bot to the Contabo server

Two containers from one image, on the same Contabo VPS as Collxct and Recbot
(`158.220.84.21`), sharing a SQLite volume:

| Container | What it does | Network |
|---|---|---|
| `trading-bot` | Trading loop (`bot_service.py`): scans every M5 candle, places and settles trades, sends push notifications | outbound only |
| `trading-bot-web` | PWA dashboard + push subscriptions (`web_app.py`) at **https://bot.collxct.ng** | joins `collxct_collxct_net`; proxied by `collxct-nginx-1` |

This follows Recbot's pattern: the app joins the main proxy's network, the
main nginx gets one extra `server` block, and the hostname gets its own Let's
Encrypt certificate. No Collxct or Recbot config is edited. Resource caps:
bot 512 MB / 0.5 CPU, web 256 MB / 0.25 CPU.

---

## 0. Before deploying (one time)

1. **Delete the cron-job.org job** that dispatches the GitHub workflow. Its
   schedule is removed, but cron-job.org calls it through the API, and a
   re-enabled workflow would trade the same IQ Option account in parallel.
   Leave the workflow disabled.
2. **Rotate the SMTP password** of the bot's sender mailbox. It was in the
   `.env.example` committed to this public repo. Email is now off by default
   (`EMAIL_ENABLED=false`), but the password is still exposed.
3. `git push` this code. The server pulls from GitHub.

## 1. Code

```bash
ssh root@158.220.84.21
git clone https://github.com/princewizl/trading-bot.git /opt/trading-bot
cd /opt/trading-bot
```

## 2. `.env` (never via git)

Start from `.env.example`. Required:

```
IQ_EMAIL=...            IQ_PASSWORD=...         IQ_DEMO=true
DASHBOARD_PASSWORD=<long password>
WEB_SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">
VAPID_PUBLIC_KEY=...    VAPID_PRIVATE_KEY=...   VAPID_SUBJECT=mailto:<your email>
TRADE_LOG_GIST_ID=<existing gist id, only for the one-time import>
```

Generate the VAPID pair once with `python -m notifications.push --gen-keys`
(locally, or on the server after step 3 with
`docker compose run --rm bot python -m notifications.push --gen-keys`). Keep
the same keys forever: changing them silently invalidates every phone's
subscription.

## 3. Build, smoke-test, import history

```bash
docker compose build
# One dry-run scan: fetches all pairs, runs the strategy, writes the shadow log. No trades, no pushes.
docker compose run --rm bot python bot_service.py --dry-run --once
# Copy the old Gist journal (207 rows) into SQLite — read via the gist's raw URL, no token.
docker compose run --rm bot python import_history.py
```

Expect `Fetched 9 pairs in ~1-3s` from the scan and `Imported 203 trade(s)`
from the import (safe to re-run; duplicates are ignored).

## 4. Start the containers

```bash
docker compose up -d
docker compose logs -f bot      # expect: IQ Option connected (DEMO) … Next scan at HH:M4:35 UTC
```

## 5. Certificate for bot.collxct.ng

Issue it the way Recbot's was issued (standalone, with the main nginx briefly
stopped). **Collxct and Recbot are offline for roughly 10-20 seconds** while
certbot validates, so do this at a quiet time:

```bash
certbot certonly --standalone -d bot.collxct.ng \
  --pre-hook  "docker stop collxct-nginx-1" \
  --post-hook "docker start collxct-nginx-1"
```

The hooks are saved in `/etc/letsencrypt/renewal/bot.collxct.ng.conf`, so
renewals repeat the same short stop/start automatically. Check with
`certbot renew --dry-run`.

## 6. Add the nginx server block

Find where the main nginx reads its config (the same place the
`recbot.collxct.ng` block lives):

```bash
docker inspect collxct-nginx-1 --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
grep -rl "recbot.collxct.ng" /opt/collxct 2>/dev/null
```

Add `deploy/nginx-bot.collxct.ng.conf` there. Either copy it as its own file
if nginx includes a `conf.d/` directory, or paste it next to the recbot block.
Then test **before** reloading:

```bash
docker exec collxct-nginx-1 nginx -t && docker exec collxct-nginx-1 nginx -s reload
```

The block resolves `trading-bot-web` at request time, so nginx still starts and
reloads even if the bot's web container is down. A bot outage can never take
Collxct or Recbot offline.

## 7. Phone setup

1. Open **https://bot.collxct.ng** and sign in.
2. **Android (Chrome):** tap **🔔 Enable alerts** and allow. Optionally use
   ⋮ → *Install app*.
   **iPhone (iOS 16.4+):** Share → **Add to Home Screen**, open it from the
   Home Screen, *then* tap 🔔. iOS only allows push for installed web apps.
3. Tap 🔔 again → **OK** to send a test notification.

You'll get: trade placed / signal-only, every result (WIN / LOSS / TIE /
UNKNOWN), circuit breaker tripped, IQ Option unreachable > 5 min and back,
bot offline > 5 min and back (sent by the web container's watchdog), and the
Sunday 20:00 UTC weekly summary.

## 8. Retire the old pieces

- PythonAnywhere dashboard: delete the web app there.
- Gist: no longer written or read after the import. Keep it as an archive or delete it.
- `GIST_TOKEN`: revoke it on GitHub. Nothing uses it any more.

---

## Day-to-day

| Task | Command (in `/opt/trading-bot`) |
|---|---|
| Live logs | `docker compose logs -f bot` / `docker compose logs -f web` |
| Health | `docker compose ps` |
| Shadow-log report | in the app (Paper Log → Run report), or `docker compose exec bot python shadow_report.py --days 14` |
| Deploy an update | `git pull && docker compose up -d --build` |
| Stop trading | `docker compose stop bot` (open trades stay OPEN and settle on next start) |
| Back up the DB | `docker compose exec bot python -c "import sqlite3; sqlite3.connect('/data/bot.db').backup(sqlite3.connect('/data/backup.db'))" && docker cp trading-bot:/data/backup.db ./bot-$(date +%F).db` |

## Settings (`.env`, then `docker compose up -d`)

- `MIN_PAYOUT=0.80`: skip trades when IQ's payout is below 80%.
- `ALLOW_OTC=true`: trade IQ's synthetic OTC feed when the real market is
  closed. Not recommended, because the strategy reads real-market prices.
- `EMAIL_ENABLED=true`: also send emails (needs the `EMAIL_*` / `SMTP_*` values).
- `IQ_DEMO=false`: **real money.** Only after the paper log and live demo
  results both show a win-rate CI clearly above break-even over 200+ trades.
