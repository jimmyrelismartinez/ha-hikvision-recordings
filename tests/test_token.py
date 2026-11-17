"""HMAC token round-trip / tamper / expiry / malformed / length cap."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from hikvision_recordings.app.token import (
    TOKEN_MAX_LEN,
    mint,
    new_entry_id,
    new_signing_key,
    verify,
)


def test_round_trip():
    key = new_signing_key()
    eid = new_entry_id()
    exp = int(time.time()) + 300
    token = mint(key, eid, exp)
    payload = verify(key, token)
    assert payload == {"v": 1, "e": eid, "x": exp}


def test_tampered_payload_rejected():
    key = new_signing_key()
    token = mint(key, new_entry_id(), int(time.time()) + 60)
    body, sig = token.split(".", 1)
    raw = base64.urlsafe_b64decode(body + "==")
    data = json.loads(raw)
    data["e"] = "tampered"
    bad_body = base64.urlsafe_b64encode(
        json.dumps(data, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    with pytest.raises(ValueError):
        verify(key, f"{bad_body}.{sig}")


def test_wrong_key_rejected():
    token = mint(new_signing_key(), new_entry_id(), int(time.time()) + 60)
    with pytest.raises(ValueError):
        verify(new_signing_key(), token)


def test_malformed_tokens():
    key = new_signing_key()
    for bad in ("", "no-dot", "a.", ".b", "a.b.c", "!!!.!!!"):
        with pytest.raises(ValueError):
            verify(key, bad)


def test_length_cap():
    key = new_signing_key()
    # A normal token is well under the cap.
    token = mint(key, new_entry_id(), int(time.time()) + 60)
    assert len(token) < TOKEN_MAX_LEN
    with pytest.raises(ValueError):
        verify(key, "a" * (TOKEN_MAX_LEN + 1))


def test_hmac_checked_before_json():
    """Wrong HMAC over a non-JSON payload must fail as bad_sig before JSON parse."""
    key = new_signing_key()
    garbage = base64.urlsafe_b64encode(b"not-json{{{").decode().rstrip("=")
    wrong_sig = base64.urlsafe_b64encode(
        hmac.new(key, b"different-bytes", hashlib.sha256).digest()
    ).decode().rstrip("=")
    with pytest.raises(ValueError, match="bad_sig"):
        verify(key, f"{garbage}.{wrong_sig}")
