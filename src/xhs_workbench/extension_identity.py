"""Stable Chrome extension identity from a checked-in public DER key."""

from __future__ import annotations

import hashlib


def derive_extension_id(public_key_der: bytes) -> str:
    """Return Chrome's a--p identity derived from the first SHA-256 bytes."""
    if not isinstance(public_key_der, bytes) or not public_key_der:
        raise ValueError("public_key_der must be non-empty bytes")
    return "".join(chr(ord("a") + int(nibble, 16)) for nibble in hashlib.sha256(public_key_der).hexdigest()[:32])
