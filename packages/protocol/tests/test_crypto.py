import hashlib

from noted_protocol.crypto import (
    login_hash_md5,
    login_hash_sha256,
    md5_hex,
    password_md5,
)


def test_password_md5_matches_stdlib() -> None:
    assert password_md5("hunter2") == hashlib.md5(b"hunter2").hexdigest()


def test_md5_hex() -> None:
    assert md5_hex(b"") == "d41d8cd98f00b204e9800998ecf8427e"


def test_login_hash_sha256() -> None:
    pw_md5 = password_md5("secret")
    rc = "abc123"
    expected = hashlib.sha256((pw_md5 + rc).encode()).hexdigest()
    assert login_hash_sha256(pw_md5, rc) == expected


def test_login_hash_md5() -> None:
    pw_md5 = password_md5("secret")
    rc = "abc123"
    expected = hashlib.md5((pw_md5 + rc).encode()).hexdigest()
    assert login_hash_md5(pw_md5, rc) == expected


def test_schemes_differ() -> None:
    pw_md5 = password_md5("secret")
    assert login_hash_sha256(pw_md5, "rc") != login_hash_md5(pw_md5, "rc")
