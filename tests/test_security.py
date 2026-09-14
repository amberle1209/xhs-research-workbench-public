import json

import pytest

from xhs_workbench import security
from xhs_workbench.security import (
    UnsafeUrlError,
    parse_xhs_url,
    safe_error,
    sanitize_evidence,
    sha256_text,
)


def test_parse_note_url_removes_all_query_and_fragment() -> None:
    parsed = parse_xhs_url(
        "https://www.xiaohongshu.com/explore/abc123?xsec_token=secret&source=feed#x"
    )

    assert parsed.object_type == "note"
    assert parsed.object_id == "abc123"
    assert parsed.canonical_url == "https://www.xiaohongshu.com/explore/abc123"
    assert "secret" not in parsed.model_dump_json()


@pytest.mark.parametrize(
    "url",
    [
        "http://xiaohongshu.com/explore/abc123",
        "https://evilxiaohongshu.com/explore/abc123",
        "https://www.xiaohongshu.com.evil.example/explore/abc123",
        "https://www.xiaohongshu.com/explore/abc123/extra",
        "https://www.xiaohongshu.com/search/abc123",
    ],
)
def test_parse_xhs_url_rejects_unsafe_hosts_schemes_and_paths(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        parse_xhs_url(url)


@pytest.mark.parametrize(
    ("url", "object_type", "object_id", "canonical_url"),
    [
        (
            "https://xiaohongshu.com/discovery/item/note-id?from=web",
            "note",
            "note-id",
            "https://xiaohongshu.com/discovery/item/note-id",
        ),
        (
            "https://www.xiaohongshu.com/user/profile/user-id#profile",
            "profile",
            "user-id",
            "https://www.xiaohongshu.com/user/profile/user-id",
        ),
    ],
)
def test_parse_xhs_url_canonicalizes_allowed_paths(
    url: str, object_type: str, object_id: str, canonical_url: str
) -> None:
    parsed = parse_xhs_url(url)

    assert parsed.object_type == object_type
    assert parsed.object_id == object_id
    assert parsed.canonical_url == canonical_url


def test_sha256_text_returns_stable_digest() -> None:
    assert sha256_text("xhs") == "fc1dba5f8f246d497302ad9fab1b3ce87e26668b4907d62b5c7e767efb3fa491"


@pytest.mark.parametrize(
    "key",
    [
        "access_token",
        "access-token",
        "ACCESS-TOKEN",
        "refresh_token",
        "refresh-token",
        "REFRESH-TOKEN",
        "Cookies",
        "COOKIES",
        " WEB SESSION ",
    ],
)
def test_safe_evidence_key_rejects_normalized_sensitive_keys(key: str) -> None:
    assert not security.is_safe_evidence_key(key)


def test_safe_evidence_key_allows_a_public_chinese_label() -> None:
    assert security.is_safe_evidence_key("获赞与收藏")


def test_recursive_sanitizer_removes_sensitive_keys_and_urls() -> None:
    raw = {
        "headers": {"Cookie": "secret", "AUTHORIZATION": "also-secret"},
        "url": "https://www.xiaohongshu.com/explore/a?xsec_token=secret",
        "items": [{"web_session": "nested-secret"}],
    }

    clean = sanitize_evidence(raw)
    serialized = json.dumps(clean)

    assert "secret" not in serialized
    assert clean["headers"]["[REDACTED_KEY]"] == "[REDACTED]"
    assert clean["headers"]["[REDACTED_KEY] [duplicate 2]"] == "[REDACTED]"
    assert clean["items"][0]["[REDACTED_KEY]"] == "[REDACTED]"
    assert clean["url"] == "https://www.xiaohongshu.com/explore/a"


def test_recursive_sanitizer_redacts_unparseable_url_strings() -> None:
    clean = sanitize_evidence(
        {"nested": ["https://example.com/explore/abc?token=secret", "plain text"]}
    )

    assert clean == {"nested": ["[REDACTED_URL]", "plain text"]}


@pytest.mark.parametrize(
    "url_like",
    [
        "https:example.com/explore/a?token=",
        "https:/example.com/explore/a?token=",
        "//example.com/explore/a?token=",
    ],
)
def test_recursive_sanitizer_redacts_malformed_url_like_strings(url_like: str) -> None:
    clean = sanitize_evidence({"value": url_like})

    assert clean == {"value": "[REDACTED_URL]"}
    assert "token" not in json.dumps(clean)


@pytest.mark.parametrize(
    "opaque_uri",
    [
        "mailto:person@example.com",
        "data:text/plain,example",
        "ftp://example.com/file.txt",
    ],
)
def test_recursive_sanitizer_redacts_opaque_uri_values(opaque_uri: str) -> None:
    assert sanitize_evidence({"value": opaque_uri}) == {"value": "[REDACTED_URL]"}


def test_recursive_sanitizer_redacts_opaque_uri_mapping_keys() -> None:
    clean = sanitize_evidence({"mailto:person@example.com": "safe value"})

    assert clean == {"[REDACTED_URL]": "safe value"}


def test_recursive_sanitizer_keeps_non_url_text_and_time_strings() -> None:
    raw = {"label": "ordinary text", "time": "12:30"}

    assert sanitize_evidence(raw) == raw


def test_recursive_sanitizer_normalizes_string_keys_without_leaking_urls_or_sensitive_names() -> None:
    clean = sanitize_evidence(
        {
            "Cookie": "secret",
            "https://www.xiaohongshu.com/explore/a?xsec_token=": "safe value",
        }
    )
    serialized = json.dumps(clean)

    assert clean == {
        "[REDACTED_KEY]": "[REDACTED]",
        "https://www.xiaohongshu.com/explore/a": "safe value",
    }
    assert "xsec_token" not in serialized
    assert "Cookie" not in serialized


def test_recursive_sanitizer_preserves_values_when_normalized_keys_collide() -> None:
    clean = sanitize_evidence(
        {
            "https://www.xiaohongshu.com/explore/a": "first",
            "https://www.xiaohongshu.com/explore/a?source=feed": "second",
        }
    )

    assert clean == {
        "https://www.xiaohongshu.com/explore/a": "first",
        "https://www.xiaohongshu.com/explore/a [duplicate 2]": "second",
    }


def test_safe_error_returns_a_non_sensitive_exception_summary() -> None:
    summary = safe_error(ValueError("request failed with token=secret"))

    assert summary == {"type": "ValueError", "message": "[REDACTED]"}
    assert "secret" not in json.dumps(summary)


def test_safe_error_does_not_expose_a_dynamic_exception_class_name() -> None:
    custom_exception = type("custom_exception_type", (Exception,), {})

    summary = safe_error(custom_exception(""))

    assert summary == {"type": "error", "message": "[REDACTED]"}
    assert "custom_exception_type" not in json.dumps(summary)
