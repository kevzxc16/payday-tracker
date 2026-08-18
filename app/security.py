"""
Security primitives: password hashing, random token generation, HMAC signatures.

We use `hashlib.scrypt` (in the stdlib since Python 3.6) for password hashing.
scrypt is a memory-hard KDF designed to resist brute force.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from app.config import settings

# scrypt parameters. These values give roughly 100-200ms hashing time on
# modern hardware, which is the right ballpark for a login flow.
_SCRYPT_N = 2 ** 14   # CPU/memory cost
_SCRYPT_R = 8         # block size
_SCRYPT_P = 1         # parallelisation
_SCRYPT_DKLEN = 64    # derived key length in bytes


def hash_password(password: str) -> tuple[str, str]:
    """
    Hash a password with a fresh random salt.

    Returns (hash_hex, salt_hex). Store both in the users table.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return digest.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    """Constant-time check of a plaintext password against stored hash + salt."""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    actual = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    # hmac.compare_digest is constant-time; protects against timing attacks.
    return hmac.compare_digest(actual, expected)


def random_token(nbytes: int = 32) -> str:
    """URL-safe random token (used for session IDs, password reset tokens, CSRF)."""
    return secrets.token_urlsafe(nbytes)


def sign(value: str) -> str:
    """HMAC-SHA256 sign a string with SECRET_KEY. Returns hex digest."""
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_signature(value: str, signature: str) -> bool:
    """Constant-time check of an HMAC signature."""
    return hmac.compare_digest(sign(value), signature)
