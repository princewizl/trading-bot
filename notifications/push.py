"""
Web Push (VAPID) to every browser that enabled notifications in the PWA.

Keys come from env (generate once, put in .env on the server):
    python -m notifications.push --gen-keys

    VAPID_PUBLIC_KEY   base64url uncompressed P-256 point (also sent to browsers)
    VAPID_PRIVATE_KEY  base64url raw 32-byte private key
    VAPID_SUBJECT      mailto: contact the push services can reach
"""

import base64
import json
import logging
import os
import sys

from data import db

logger = logging.getLogger(__name__)

VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT     = os.getenv("VAPID_SUBJECT", "mailto:admin@collxct.ng")


def configured() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def send(title: str, body: str, tag: str = "", url: str = "/", urgent: bool = False) -> int:
    """Push to all subscriptions. Drops expired ones. Returns how many were delivered. Never raises."""
    if not configured():
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.error("pywebpush not installed — push disabled")
        return 0

    payload = json.dumps({"title": title, "body": body, "tag": tag, "url": url})
    delivered = 0
    for sub in db.subscriptions():
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=3600,
                headers={"Urgency": "high" if urgent else "normal"},
                timeout=10,
            )
            delivered += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):   # browser unsubscribed / expired
                db.remove_subscription(sub["endpoint"])
                logger.info("Push subscription expired — removed")
            else:
                logger.warning(f"Push failed ({status}): {e}")
        except Exception as e:
            logger.warning(f"Push failed: {e}")
    return delivered


def generate_keys() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01
    v = Vapid01()
    v.generate_keys()
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    private = b64(v.private_key.private_numbers().private_value.to_bytes(32, "big"))
    public = b64(v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    return public, private


if __name__ == "__main__" and "--gen-keys" in sys.argv:
    pub, priv = generate_keys()
    print(f"VAPID_PUBLIC_KEY={pub}\nVAPID_PRIVATE_KEY={priv}\nVAPID_SUBJECT=mailto:you@example.com")
