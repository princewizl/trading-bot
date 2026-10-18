"""
Trading bot PWA — dashboard + web push, served at https://bot.collxct.ng.

Reads the same SQLite database the bot service writes (data/db.py), so there
is no Gist, no token and no polling of third parties. Runs as its own container
(`trading-bot-web`) behind the Collxct nginx proxy.

Env:
  DASHBOARD_PASSWORD  required — login password
  WEB_SECRET_KEY      required — signs the session cookie (any long random string)
  VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_SUBJECT   web push (notifications/push.py)

Run:  gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 web_app:app
      (one worker: the offline watchdog thread must run exactly once)
"""

import contextlib
import hmac
import io
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from dashboard_stats import compute_stats
from data import db
from notifications import push

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.Formatter.converter = time.gmtime
logger = logging.getLogger("web")

PASSWORD   = os.getenv("DASHBOARD_PASSWORD", "")
SECRET_KEY = os.getenv("WEB_SECRET_KEY", "")

OFFLINE_AFTER_SECS = 300      # heartbeat older than this = bot offline
MAX_LOGIN_FAILS    = 8
LOCKOUT_SECS       = 900

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=SECRET_KEY or secrets.token_hex(32),
    SESSION_COOKIE_SECURE=os.getenv("WEB_INSECURE_COOKIES", "") != "1",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
if not SECRET_KEY:
    logger.warning("WEB_SECRET_KEY not set — sessions reset on every restart")

db.init()

PUBLIC_PATHS = {"/login", "/manifest.webmanifest", "/sw.js", "/health"}
_fails: dict[str, list] = {}   # ip -> [count, first_fail_ts]


# ── Auth ──────────────────────────────────────────────────────────────────

@app.before_request
def _require_login():
    if request.path in PUBLIC_PATHS or request.path.startswith("/static/"):
        return None
    if not PASSWORD:
        return "DASHBOARD_PASSWORD is not set on the server.", 503
    if session.get("ok"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "login required"}), 401
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    ip = request.remote_addr or "?"
    count, first = _fails.get(ip, [0, 0.0])
    if time.time() - first > LOCKOUT_SECS:
        count = 0
    if request.method == "POST":
        if count >= MAX_LOGIN_FAILS:
            error = "Too many attempts — try again in 15 minutes."
        elif PASSWORD and hmac.compare_digest(request.form.get("password", ""), PASSWORD):
            _fails.pop(ip, None)
            session.clear()
            session.permanent = True
            session["ok"] = True
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        else:
            _fails[ip] = [count + 1, first if count else time.time()]
            time.sleep(1)
            error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Pages / data ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html", stats=compute_stats(db.journal_rows()),
                           bot=_bot_status(), open_trades=_open_trades(),
                           fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                           vapid_public_key=push.VAPID_PUBLIC_KEY)


@app.route("/refresh")
def refresh():
    return redirect(url_for("index"))


@app.route("/api/data")
def api_data():
    return jsonify({"stats": compute_stats(db.journal_rows()), "bot": _bot_status(),
                    "open_trades": _open_trades(), "fetched_at": time.time()})


@app.route("/api/report")
def api_report():
    import shadow_report
    days = request.args.get("days", type=int) or 0
    horizon = request.args.get("horizon", type=int) or 5
    if horizon not in shadow_report.HORIZONS:
        abort(400)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        shadow_report.main(["--days", str(days), "--horizon", str(horizon)])
    return jsonify({"text": buf.getvalue()})


def _bot_status() -> dict:
    st = db.get_status()
    hb = st.get("heartbeat")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds() if hb else None
    return {
        "mode":          st.get("mode") or "—",
        "online":        age is not None and age < OFFLINE_AFTER_SECS,
        "heartbeat_age": int(age) if age is not None else None,
        "iq_connected":  st.get("iq_connected") == "1",
        "balance":       st.get("balance"),
        "last_scan":     st.get("last_scan"),
        "next_scan":     st.get("next_scan"),
        "last_signals":  st.get("last_scan_signals"),
    }


def _open_trades() -> list[dict]:
    return [{
        "pair": config.PAIR_DISPLAY.get(t["pair"], t["pair"]), "direction": t["direction"],
        "stake": t["stake"], "expires_at": t["expires_at"], "order_id": t["order_id"],
        "payout": t["payout"],
    } for t in db.open_trades()]


# ── PWA plumbing ──────────────────────────────────────────────────────────

@app.route("/manifest.webmanifest")
def manifest():
    return jsonify({
        "name": "Trading Bot", "short_name": "Trading Bot",
        "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#0f1117", "theme_color": "#0f1117",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }), 200, {"Content-Type": "application/manifest+json"}


@app.route("/sw.js")
def service_worker():
    # Served from the root so its scope covers the whole app.
    resp = send_from_directory("static", "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    sub = request.get_json(silent=True) or {}
    keys = sub.get("keys") or {}
    if not (sub.get("endpoint", "").startswith("https://") and keys.get("p256dh") and keys.get("auth")):
        return jsonify({"error": "bad subscription"}), 400
    db.add_subscription(sub["endpoint"], keys["p256dh"], keys["auth"], request.headers.get("User-Agent", ""))
    return jsonify({"ok": True, "devices": len(db.subscriptions())})


@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    endpoint = (request.get_json(silent=True) or {}).get("endpoint", "")
    db.remove_subscription(endpoint)
    return jsonify({"ok": True})


@app.route("/api/push/test", methods=["POST"])
def push_test():
    n = push.send("🔔 Test notification", "Push is working on this device.", tag="test")
    return jsonify({"delivered": n})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot_online": _bot_status()["online"]})


# ── Offline watchdog (the bot can't announce its own death) ───────────────

def _watchdog():
    alerted = False
    while True:
        time.sleep(60)
        try:
            st = _bot_status()
            if st["heartbeat_age"] is None:
                continue   # bot has never run
            if not st["online"] and not alerted:
                push.send("⚠️ Bot offline", f"No heartbeat for {st['heartbeat_age'] // 60} min. "
                          "Check: docker compose logs bot", tag="offline", urgent=True)
                alerted = True
            elif st["online"] and alerted:
                push.send("✅ Bot back online", "Heartbeat restored.", tag="offline")
                alerted = False
        except Exception as e:
            logger.error(f"Watchdog error: {e}")


if os.getenv("WEB_WATCHDOG", "1") == "1":
    threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()


if __name__ == "__main__":
    app.run(debug=True, port=5000)
