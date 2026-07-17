#!/usr/bin/env python3
"""Generate the SUPERNOTE_PASSWORD_MD5 value from a plaintext password.

Usage: uv run scripts/hash_password.py
Prompts for the password (no echo) and prints the md5 hex to set in the
server environment. The plaintext itself is never stored anywhere.
"""

import getpass
import hashlib


def main() -> None:
    plain = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm:  ")
    if plain != confirm:
        raise SystemExit("passwords do not match")
    print(f"SUPERNOTE_PASSWORD_MD5={hashlib.md5(plain.encode()).hexdigest()}")


if __name__ == "__main__":
    main()
