"""Tokenisation (HMAC-SHA256) and encryption at rest (AES-256-GCM).

Both use the `cryptography` package / Python stdlib, which call OpenSSL. On IBM Z, OpenSSL can use
the CPACF instructions for AES and SHA; whether it does on a given VM is shown by deploy/evidence.sh,
not assumed here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BLOB_VERSION = b"\x01"
NONCE_BYTES = 12


def hmac_token(secret: bytes, value: str) -> str:
    """Deterministic pseudonym: HMAC-SHA256(key=secret, msg=value) as 64 lowercase hex chars."""
    if len(secret) < 32:
        raise ValueError("HMAC secret must be at least 32 bytes")
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def parse_secret(raw: str) -> bytes:
    """EDGE_SECRET is hex (64+ chars) as written by run_all.sh."""
    raw = raw.strip()
    try:
        secret = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError("EDGE_SECRET must be hex") from exc
    if len(secret) < 32:
        raise ValueError("EDGE_SECRET must be at least 32 bytes (64 hex characters)")
    return secret


def load_edge_secret() -> bytes:
    raw = os.environ.get("EDGE_SECRET", "")
    if not raw:
        raise RuntimeError("EDGE_SECRET is not set; it must only be present in the edge's environment")
    return parse_secret(raw)


def generate_data_key() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


class Cipher:
    """AES-256-GCM with a random 96-bit nonce per message. Blob = version | nonce | ciphertext+tag."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256 key must be 32 bytes")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: bytes, aad: str = "") -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return BLOB_VERSION + nonce + self._aead.encrypt(nonce, plaintext, aad.encode("utf-8") or None)

    def decrypt(self, blob: bytes, aad: str = "") -> bytes:
        blob = bytes(blob)
        if blob[:1] != BLOB_VERSION:
            raise ValueError("unknown ciphertext version")
        nonce, ct = blob[1:1 + NONCE_BYTES], blob[1 + NONCE_BYTES:]
        return self._aead.decrypt(nonce, ct, aad.encode("utf-8") or None)

    def encrypt_json(self, obj: Any, aad: str = "") -> bytes:
        return self.encrypt(json.dumps(obj, default=str, separators=(",", ":")).encode("utf-8"), aad)

    def decrypt_json(self, blob: bytes, aad: str = "") -> Any:
        return json.loads(self.decrypt(blob, aad))

    def encrypt_text(self, text: str, aad: str = "") -> bytes:
        return self.encrypt(text.encode("utf-8"), aad)

    def decrypt_text(self, blob: bytes, aad: str = "") -> str:
        return self.decrypt(blob, aad).decode("utf-8")


def cipher_from_settings() -> Cipher:
    from common.config import get_settings

    key = get_settings().data_key
    if key is None:
        raise RuntimeError("DATA_KEY is not set; run deploy/run_all.sh to generate secrets/")
    return Cipher(key)
