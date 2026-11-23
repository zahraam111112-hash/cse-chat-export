"""Optional HTTP Basic auth for the dashboard.

Enabled by setting CSE_AUTH_USER / CSE_AUTH_PASS. When unset the app is open
(useful behind a trusted reverse proxy with its own auth), so the operator
always decides explicitly rather than inheriting a silently-open service.
"""

from __future__ import annotations

import base64
import hmac
import os

from fastapi import HTTPException, Request

USER = os.environ.get("CSE_AUTH_USER", "").strip()
PASSWORD = os.environ.get("CSE_AUTH_PASS", "")

PUBLIC_PATHS = ("/healthz",)


def enabled() -> bool:
    return bool(USER and PASSWORD)


def check(request: Request) -> None:
    if not enabled() or public():
        return
    if request.url.path in PUBLIC_PATHS:
        return
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise _challenge()
    try:
        raw = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
    except Exception:  # noqa: BLE001
        raise _challenge() from None
    user, _, password = raw.partition(":")
    ok_user = hmac.compare_digest(user.encode(), USER.encode())
    ok_pass = hmac.compare_digest(password.encode(), PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise _challenge()


def is_admin(request: Request) -> bool:
    """True when the caller logged in as the configured admin user.

    In public mode (`CSE_PUBLIC=1`) the Basic wall is off and anonymous
    visitors share the service — this returns False for all of them, so each
    one only ever sees jobs submitted from their own address.
    """
    if enabled():
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        user, _, password = raw.partition(":")
        return (hmac.compare_digest(user.encode(), USER.encode())
                and hmac.compare_digest(password.encode(), PASSWORD.encode()))
    return not public()


def public() -> bool:
    return os.environ.get("CSE_PUBLIC", "") == "1"


def _challenge() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="authentication required",
        headers={"WWW-Authenticate": 'Basic realm="chatgpt-share-export"'},
    )
