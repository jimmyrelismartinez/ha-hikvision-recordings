"""Short-lived HMAC tokens for external clip links.

Token = b64url(payload) "." b64url(HMAC-SHA256(key, payload_bytes)).
The HMAC is verified on the raw segment before JSON-decoding. Cap 512 chars.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

TOKEN_MAX_LEN = 512
TOKEN_VERSION = 1


def new_signing_key() -> bytes:
    return secrets.token_bytes(32)


def new_entry_id() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def mint(key: bytes, entry_id: str, exp: int) -> str:
    payload = json.dumps(
        {"v": TOKEN_VERSION, "e": entry_id, "x": int(exp)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    body = _b64url_encode(payload)
    sig = _b64url_encode(hmac.new(key, payload, hashlib.sha256).digest())
    token = f"{body}.{sig}"
    if len(token) > TOKEN_MAX_LEN:
        raise ValueError("token exceeds length cap")
    return token


def verify(key: bytes, token: str) -> dict[str, Any]:
    """Return the payload dict, or raise ValueError on any malformation/tamper/expiry."""
    if not isinstance(token, str) or not token or len(token) > TOKEN_MAX_LEN:
        raise ValueError("malformed")
    if token.count(".") != 1:
        raise ValueError("malformed")
    body, sig = token.split(".", 1)
    if not body or not sig:
        raise ValueError("malformed")
    try:
        payload_bytes = _b64url_decode(body)
        sig_bytes = _b64url_decode(sig)
    except (ValueError, TypeError) as exc:
        raise ValueError("malformed") from exc
    expected = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig_bytes):
        raise ValueError("bad_sig")
    try:
        data = json.loads(payload_bytes.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed") from exc
    if not isinstance(data, dict):
        raise ValueError("malformed")
    if data.get("v") != TOKEN_VERSION:
        raise ValueError("malformed")
    entry_id = data.get("e")
    exp = data.get("x")
    if not isinstance(entry_id, str) or not entry_id:
        raise ValueError("malformed")
    if not isinstance(exp, int):
        raise ValueError("malformed")
    return {"v": TOKEN_VERSION, "e": entry_id, "x": exp}
