"""Password-hashing helpers for the Supernote login scheme.

The spec documents the login password as either ``SHA256(MD5(plain) + randomCode)``
or ``MD5(MD5(plain) + randomCode)`` — which one real firmware uses is unknown until
captured, so the server verifies against both and the client picks one to send.

The server only ever needs ``MD5(plain)`` (never the plaintext), so all helpers
take the md5 digest as their starting point.
"""

import hashlib


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def password_md5(plain: str) -> str:
    """First-stage digest of the plaintext password."""
    return md5_hex(plain.encode())


def login_hash_sha256(pw_md5: str, random_code: str) -> str:
    """Primary documented scheme: SHA256(MD5(plain) + randomCode)."""
    return hashlib.sha256((pw_md5 + random_code).encode()).hexdigest()


def login_hash_md5(pw_md5: str, random_code: str) -> str:
    """Alternate documented scheme: MD5(MD5(plain) + randomCode)."""
    return hashlib.md5((pw_md5 + random_code).encode()).hexdigest()
