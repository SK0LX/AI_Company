"""Unit tests for token/key encryption. No network.

    python tests/test_crypto.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.crypto import decrypt, encrypt


def main() -> None:
    secret = "123456:ABC-DEF_telegram-token.value"
    enc = encrypt(secret)
    assert enc and enc != secret  # actually encrypted, not plaintext
    assert decrypt(enc) == secret  # round-trips

    # different ciphertext than the plaintext, decrypts back
    enc2 = encrypt("sk-ant-another-secret")
    assert decrypt(enc2) == "sk-ant-another-secret"

    # empty stays empty (no crash)
    assert encrypt("") == ""
    assert decrypt("") == ""

    # garbage / non-token input doesn't raise (best-effort decrypt)
    try:
        decrypt("not-a-valid-fernet-token")
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"decrypt should not raise on garbage: {exc}")

    print("crypto tests: OK")


if __name__ == "__main__":
    main()
