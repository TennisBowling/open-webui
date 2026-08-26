"""Web Push delivery (VAPID + pywebpush).

The only notification channel that reaches a user with no tab open, which is
what makes a scheduled automation useful on a phone. Everything here is
best-effort: a push service being down must never affect the run that produced
the notification.

pywebpush is `requests`-based (blocking), so every send is dispatched through
`anyio.to_thread.run_sync` rather than blocking the event loop — a handful of
dead endpoints would otherwise stall the whole worker on TCP timeouts.
"""

from __future__ import annotations

import base64
import json
import logging

import anyio.to_thread
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.push import PushSubscriptions

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


def _vapid_claims(app) -> dict:
    """The `sub` claim identifies us to the push service so it can contact the
    sender about a misbehaving application server. Prefer the admin's address;
    fall back to a generic mailto rather than omitting it (some services 400 on
    a missing `sub`)."""
    email = str(getattr(app.state.config, "ADMIN_EMAIL", "") or "").strip()
    return {"sub": f"mailto:{email}" if email else "mailto:admin@open-webui.local"}


async def send_web_push_to_user(app, user_id: str, payload: dict) -> int:
    """Push ``payload`` to every subscription of ``user_id``. Returns the number
    of endpoints that accepted it.

    The payload is kept small on purpose — iOS caps push payload size, and a
    notification only needs a title, a body, and where to go on tap."""
    private_key = str(getattr(app.state.config, "WEBPUSH_VAPID_PRIVATE_KEY", "") or "")
    if not private_key:
        return 0

    subscriptions = await PushSubscriptions.get_by_user_id(user_id)
    if not subscriptions:
        return 0

    from pywebpush import WebPushException, webpush

    claims = _vapid_claims(app)
    body = json.dumps(payload)
    delivered = 0

    for subscription in subscriptions:
        info = {"endpoint": subscription.endpoint, "keys": subscription.keys or {}}

        def _send() -> None:
            webpush(
                subscription_info=info,
                data=body,
                vapid_private_key=private_key,
                vapid_claims=dict(claims),
            )

        try:
            await anyio.to_thread.run_sync(_send)
            delivered += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                # The push service says this endpoint is permanently gone (app
                # uninstalled, permission revoked). Drop it — retrying it every
                # run is the classic way these tables rot.
                await PushSubscriptions.delete_by_endpoint(subscription.endpoint)
                log.info("dropped expired push subscription (%s)", status)
            else:
                log.warning("web push failed (%s): %s", status, e)
        except Exception:
            log.exception("web push failed for user %s", user_id)

    return delivered


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_vapid_keys() -> tuple[str, str]:
    """Mint a VAPID (P-256) keypair as the unpadded url-safe base64 strings both
    ends expect: the uncompressed public point for the browser's
    ``applicationServerKey``, and the raw private value for pywebpush's
    ``vapid_private_key``. Returns ``(public, private)``."""
    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(public_raw), _b64url(private_raw)
