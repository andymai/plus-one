import base64
import json

from .store import now


def _key_path(cfg):
    return cfg.state_dir / "vapid_private.pem"


def _vapid(cfg):
    from py_vapid import Vapid01

    path = _key_path(cfg)
    if not path.exists():
        vapid = Vapid01()
        vapid.generate_keys()
        vapid.save_key(str(path))
        path.chmod(0o600)
    return Vapid01.from_file(str(path))


def public_key(cfg):
    from cryptography.hazmat.primitives import serialization

    key = _vapid(cfg).private_key.public_key()
    raw = key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def subscribe(store, endpoint, p256dh, auth, label):
    if not endpoint.startswith("https://"):
        raise ValueError("push endpoint must be https")
    store.db.execute(
        """
        INSERT INTO push_subs (endpoint, keys, label, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET keys = excluded.keys, label = excluded.label, failures = 0
    """,
        (endpoint, json.dumps({"p256dh": p256dh, "auth": auth}), label[:80], now()),
    )


def send_all(cfg, store, title, body, url):
    # Push services want an https contact in the signed claims, which only the tailscale address gives.
    if cfg.web.mode != "tailscale":
        return 0
    from pywebpush import WebPushException, webpush

    _vapid(cfg)
    sent = 0
    for sub in store.query("SELECT * FROM push_subs"):
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": json.loads(sub["keys"])},
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=str(_key_path(cfg)),
                vapid_claims={"sub": cfg.web.public_url},
                ttl=3600,
                timeout=10,
            )
            store.db.execute(
                "UPDATE push_subs SET last_ok = ?, failures = 0 WHERE id = ?", (now(), sub["id"])
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                store.db.execute("DELETE FROM push_subs WHERE id = ?", (sub["id"],))
            else:
                store.db.execute(
                    "UPDATE push_subs SET failures = failures + 1 WHERE id = ?", (sub["id"],)
                )
    return sent
