"""Boundaries for retaining only safe Xiaohongshu evidence."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

_ALLOWED_HOSTS = frozenset({"www.xiaohongshu.com", "xiaohongshu.com"})
_SENSITIVE_KEYS = frozenset(
    {
        "a1",
        "access_token",
        "authorization",
        "cookie",
        "cookies",
        "refresh_token",
        "sign",
        "signature",
        "token",
        "web_session",
        "xsec_token",
    }
)
_KEY_SEPARATOR = re.compile(r"[\s_-]+")
_URL_LIKE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:|^//")
_OBJECT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class UnsafeUrlError(ValueError):
    """Raised when a URL is outside the safe Xiaohongshu evidence boundary."""


_SAFE_ERROR_TYPES: dict[type[BaseException], str] = {
    TypeError: "TypeError",
    UnsafeUrlError: "UnsafeUrlError",
    ValueError: "ValueError",
}


class CanonicalXhsUrl(BaseModel):
    """A parsed XHS object without any raw URL query or fragment data."""

    object_type: Literal["note", "profile"]
    object_id: str
    canonical_url: str


def parse_xhs_url(url: str) -> CanonicalXhsUrl:
    """Parse an allowlisted HTTPS XHS URL into its non-sensitive canonical form."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise UnsafeUrlError("URL is not an approved Xiaohongshu HTTPS URL") from error

    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise UnsafeUrlError("URL is not an approved Xiaohongshu HTTPS URL")

    path_parts = parsed.path.split("/")
    object_type: Literal["note", "profile"]
    is_explore = len(path_parts) == 3 and path_parts[:2] == ["", "explore"]
    is_discovery_item = len(path_parts) == 4 and path_parts[:3] == ["", "discovery", "item"]
    if is_explore or is_discovery_item:
        object_type = "note"
    elif len(path_parts) == 4 and path_parts[:3] == ["", "user", "profile"]:
        object_type = "profile"
    else:
        raise UnsafeUrlError("URL path is not an approved Xiaohongshu object path")

    object_id = path_parts[-1]
    if not _OBJECT_ID.fullmatch(object_id):
        raise UnsafeUrlError("URL path is not an approved Xiaohongshu object path")

    return CanonicalXhsUrl(
        object_type=object_type,
        object_id=object_id,
        canonical_url=f"https://{host}{parsed.path}",
    )


def sha256_text(value: str) -> str:
    """Return the SHA-256 digest for text that must not be retained raw."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sanitize_evidence(value: object) -> object:
    """Recursively remove credentials and unsafe URL strings from evidence."""
    return _sanitize(value, active_containers=set())


def _sanitize(value: object, active_containers: set[int]) -> object:
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_containers:
            return "[REDACTED]"
        active_containers.add(container_id)
        try:
            sanitized: dict[str, object] = {}
            for key, item in value.items():
                sanitized_key, redact_value = _sanitize_key(key)
                unique_key = _deduplicate_key(sanitized_key, sanitized)
                sanitized[unique_key] = (
                    "[REDACTED]" if redact_value else _sanitize(item, active_containers)
                )
            return sanitized
        except Exception:  # noqa: BLE001 - untrusted evidence must fail closed.
            return "[REDACTED]"
        finally:
            active_containers.remove(container_id)

    if isinstance(value, list):
        container_id = id(value)
        if container_id in active_containers:
            return "[REDACTED]"
        active_containers.add(container_id)
        try:
            return [_sanitize(item, active_containers) for item in value]
        except Exception:  # noqa: BLE001 - untrusted evidence must fail closed.
            return "[REDACTED]"
        finally:
            active_containers.remove(container_id)

    if isinstance(value, tuple):
        container_id = id(value)
        if container_id in active_containers:
            return "[REDACTED]"
        active_containers.add(container_id)
        try:
            return tuple(_sanitize(item, active_containers) for item in value)
        except Exception:  # noqa: BLE001 - untrusted evidence must fail closed.
            return "[REDACTED]"
        finally:
            active_containers.remove(container_id)

    if isinstance(value, str):
        if not _URL_LIKE.match(value):
            return value
        try:
            return parse_xhs_url(value).canonical_url
        except UnsafeUrlError:
            return "[REDACTED_URL]"

    if value is None or isinstance(value, bool | int | float):
        return value

    return "[REDACTED]"


def is_safe_evidence_key(key: object) -> bool:
    """Return whether a mapping key is not a credential or session identifier."""
    if not isinstance(key, str):
        return False
    normalized_key = _KEY_SEPARATOR.sub("_", key.strip().casefold())
    return normalized_key not in _SENSITIVE_KEYS


def _sanitize_key(key: object) -> tuple[str, bool]:
    if not is_safe_evidence_key(key):
        return "[REDACTED_KEY]", True

    sanitized_key = _sanitize(key, active_containers=set())
    if not isinstance(sanitized_key, str):
        return "[REDACTED_KEY]", True
    return sanitized_key, False


def _deduplicate_key(key: str, sanitized: Mapping[str, object]) -> str:
    if key not in sanitized:
        return key

    duplicate_number = 2
    while f"{key} [duplicate {duplicate_number}]" in sanitized:
        duplicate_number += 1
    return f"{key} [duplicate {duplicate_number}]"


def safe_error(exc: BaseException) -> dict[str, str]:
    """Summarize an exception without retaining its potentially sensitive message."""
    return {"type": _SAFE_ERROR_TYPES.get(type(exc), "error"), "message": "[REDACTED]"}
