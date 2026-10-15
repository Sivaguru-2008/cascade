import base64
import os

import pytest
from cryptography.exceptions import InvalidTag

from common.crypto import Cipher, generate_data_key, hmac_token, parse_secret


def test_hmac_deterministic_and_keyed():
    k1, k2 = os.urandom(32), os.urandom(32)
    a = hmac_token(k1, "P000001")
    assert a == hmac_token(k1, "P000001")
    assert a != hmac_token(k1, "P000002")
    assert a != hmac_token(k2, "P000001")
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_hmac_rejects_short_key():
    with pytest.raises(ValueError):
        hmac_token(b"short", "x")


def test_parse_secret():
    assert parse_secret("ab" * 32) == bytes.fromhex("ab" * 32)
    with pytest.raises(ValueError):
        parse_secret("zz")
    with pytest.raises(ValueError):
        parse_secret("ab" * 8)


def test_aes_gcm_round_trip_and_ciphertext():
    c = Cipher(base64.b64decode(generate_data_key()))
    obj = {"patient_token": "a" * 64, "index_drug": "amlodipine"}
    blob = c.encrypt_json(obj, aad="AL-000001")
    assert b"amlodipine" not in blob and b"aaaa" not in blob
    assert c.decrypt_json(blob, aad="AL-000001") == obj
    assert c.encrypt_json(obj, aad="AL-000001") != blob  # random nonce


def test_aes_gcm_tamper_and_wrong_aad_fail():
    c = Cipher(os.urandom(32))
    blob = bytearray(c.encrypt_text("hello", aad="x"))
    with pytest.raises(InvalidTag):
        c.decrypt_text(bytes(blob), aad="y")
    blob[-1] ^= 1
    with pytest.raises(InvalidTag):
        c.decrypt_text(bytes(blob), aad="x")


def test_wrong_key_fails():
    blob = Cipher(os.urandom(32)).encrypt_text("secret")
    with pytest.raises(InvalidTag):
        Cipher(os.urandom(32)).decrypt_text(blob)
