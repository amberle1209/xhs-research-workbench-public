"""One-shot read-only bridge for the optional upstream client."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Literal, Protocol, Self, cast, get_args
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from xhs_workbench import media
from xhs_workbench.isolated_login import acquire_project_login
from xhs_workbench.media import (
    IMAGE_MIME_TYPES,
    MAX_DISCOVERED_IMAGE_SLOTS,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_NOTE,
    MAX_NOTE_MEDIA_BYTES,
    MAX_RUN_MEDIA_BYTES,
    MAX_VIDEO_BYTES,
    MEDIA_IO_TIMEOUT_SECONDS,
    NOTE_MEDIA_BUDGET_SECONDS,
    VIDEO_MIME_TYPES,
    MediaMime,
    MediaMissingReason,
    MediaRole,
    MediaStatus,
    mime_extension,
    sniff_media_mime,
    validate_media_container,
)
from xhs_workbench.models import (
    AccountFieldKey,
    AccountFieldStatus,
    AccountRecord,
    CandidateAttempt,
    MetricEvidenceSource,
    MetricProvenanceKey,
    MetricValue,
    NoteMetrics,
    PacingSummary,
    TimeEvidence,
    validate_metric_provenance,
)
from xhs_workbench.models import TimeEvidenceKind as _TimeEvidenceKind
from xhs_workbench.models import is_safe_retained_text as _is_safe_retained_text
from xhs_workbench.persistent_client import DetailReadFailure, PersistentXhsClient
from xhs_workbench.pinned_https import open_pinned_https
from xhs_workbench.security import is_safe_evidence_key, parse_xhs_url, sanitize_evidence

TimeEvidenceKind = _TimeEvidenceKind


def is_safe_retained_text(value: str) -> bool:
    """Retain the bridge's existing public text-safety boundary."""
    return _is_safe_retained_text(value)


MAX_MEDIA_BYTES = MAX_IMAGE_BYTES
MIME_EXTENSIONS = media.MIME_EXTENSIONS
MAX_REQUEST_BYTES = 64 * 1024
MEDIA_TIMEOUT_SECONDS = MEDIA_IO_TIMEOUT_SECONDS
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
EXACT_NUMBER = re.compile(r"^(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)$")
ROUNDED_NUMBER = re.compile(r"^(\d+(?:\.\d+)?)(万|w|W|k|K)$")
_BRIDGE_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$"
)
_FOLLOWER_LABELS = frozenset({"fan", "fans", "follower", "followers", "粉丝"})
_NOTE_LABELS = frozenset({"note", "notes", "笔记"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


_DATETIME_JSON_ADAPTER = TypeAdapter(datetime)


def _bridge_datetime_json_value(value: datetime) -> str:
    """Match Pydantic's datetime JSON form used by this bridge process."""
    rendered = _DATETIME_JSON_ADAPTER.dump_python(value, mode="json")
    if type(rendered) is not str:
        raise ValueError("datetime JSON serialization failed")
    return rendered


class BridgeErrorCode(str, Enum):
    AUTH_ACTION_REQUIRED = "auth_action_required"
    AUTH_INVALID = "auth_invalid"
    AUTH_FAILED = "auth_failed"
    INVALID_REQUEST = "invalid_request"
    DETAIL_ID_MISMATCH = "detail_id_mismatch"
    DETAIL_UNAVAILABLE = "detail_unavailable"
    PROFILE_AMBIGUOUS = "profile_ambiguous"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    UPSTREAM_ERROR = "upstream_error"
    BRIDGE_TIMEOUT = "bridge_timeout"
    BRIDGE_NONZERO_EXIT = "bridge_nonzero_exit"
    BRIDGE_LAUNCH_FAILED = "bridge_launch_failed"
    PROFILE_BUSY = "profile_busy"
    INVALID_BRIDGE_OUTPUT = "invalid_bridge_output"
    UNSAFE_BRIDGE_RESPONSE = "unsafe_bridge_response"
    UNSAFE_STAGING_ASSET = "unsafe_staging_asset"
    MEDIA_PARTIAL = "media_partial"
    MEDIA_SCOPE_INVALID = "media_scope_invalid"


class BridgeRequest(StrictModel):
    operation: Literal["status", "login", "search", "account"]
    keyword: str | None = None
    account_id: str | None = None
    limit: int | None = Field(default=None, ge=1, le=10)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> BridgeRequest:
        if self.operation == "search":
            if not self.keyword or self.account_id is not None or self.limit is None:
                raise ValueError("invalid search request")
        elif self.operation == "account":
            if not self.account_id or self.keyword is not None or self.limit is None:
                raise ValueError("invalid account request")
        elif self.keyword is not None or self.account_id is not None or self.limit is not None:
            raise ValueError("invalid authentication request")
        return self


class BridgeResponse(StrictModel):
    status: Literal["complete", "partial", "failed"]
    payload: dict[str, object] = Field(default_factory=dict)
    error_code: BridgeErrorCode | None = None

    @field_validator("error_code", mode="before")
    @classmethod
    def validate_error_code(cls, value: object) -> BridgeErrorCode | None:
        if value is None or isinstance(value, BridgeErrorCode):
            return value
        if type(value) is str:
            return BridgeErrorCode(value)
        raise ValueError("invalid error code")

    @model_validator(mode="after")
    def validate_failure_shape(self) -> BridgeResponse:
        if self.status == "failed" and (self.payload or self.error_code is None):
            raise ValueError("failed responses need an empty payload and safe code")
        if self.status == "complete" and self.error_code is not None:
            raise ValueError("complete responses cannot carry an error code")
        return self


class AuthPayload(StrictModel):
    auth_status: Literal["authenticated", "action_required"]


class _LoginAcquisitionFailed(Exception):
    """Keep an upstream login failure distinct from an absent saved session."""


class CoverCandidate(StrictModel):
    staging_name: str
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    size_bytes: int = Field(gt=0, le=MAX_MEDIA_BYTES)
    sha256: str

    @field_validator("staging_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if Path(value).name != value or not value or "\x00" in value:
            raise ValueError("unsafe staging name")
        return value

    @field_validator("size_bytes", mode="before")
    @classmethod
    def validate_exact_size(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("size must be an exact integer")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        if not SHA256.fullmatch(value):
            raise ValueError("invalid sha256")
        return value

    @model_validator(mode="after")
    def validate_extension(self) -> CoverCandidate:
        if not self.staging_name.endswith(MIME_EXTENSIONS[self.mime_type]):
            raise ValueError("MIME extension mismatch")
        return self


@dataclass
class MediaBudget:
    remaining_bytes: int

    def consume(self, size_bytes: int) -> bool:
        if type(size_bytes) is not int or size_bytes < 0 or size_bytes > self.remaining_bytes:
            return False
        self.remaining_bytes -= size_bytes
        return True


class StagedMediaCandidate(StrictModel):
    """A URL-free, verified-or-explicitly-unavailable bridge media slot."""

    note_id: str
    role: MediaRole
    position: int = Field(ge=1)
    status: MediaStatus
    staging_name: str | None = None
    mime_type: MediaMime | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    duration_ms: int | None = Field(default=None, ge=1)
    missing_reason: MediaMissingReason | None = None

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        if SAFE_ID.fullmatch(value) is None:
            raise ValueError("unsafe note ID")
        return value

    @field_validator("staging_name")
    @classmethod
    def validate_staging_name(cls, value: str | None) -> str | None:
        if value is not None and (Path(value).name != value or not value or "\x00" in value):
            raise ValueError("unsafe staging name")
        return value

    @field_validator("size_bytes", mode="before")
    @classmethod
    def validate_exact_size(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("size must be an exact integer")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha(cls, value: str | None) -> str | None:
        if value is not None and SHA256.fullmatch(value) is None:
            raise ValueError("invalid sha256")
        return value

    @model_validator(mode="after")
    def validate_slot(self) -> StagedMediaCandidate:
        downloaded_fields = (self.staging_name, self.mime_type, self.size_bytes, self.sha256)
        if self.status == "downloaded":
            if any(value is None for value in downloaded_fields) or self.missing_reason is not None:
                raise ValueError("downloaded media requires only asset metadata")
            assert self.staging_name is not None
            assert self.mime_type is not None
            assert self.size_bytes is not None
            expected_mime = VIDEO_MIME_TYPES if self.role == "video" else IMAGE_MIME_TYPES
            if self.mime_type not in expected_mime:
                raise ValueError("media role and MIME disagree")
            limit = MAX_VIDEO_BYTES if self.role == "video" else MAX_IMAGE_BYTES
            if self.size_bytes <= 0 or self.size_bytes > limit:
                raise ValueError("media role exceeds its byte limit")
            extension = mime_extension(self.mime_type)
            assert extension is not None
            if not self.staging_name.endswith(extension):
                raise ValueError("MIME extension mismatch")
            expected_name = _media_staging_base(self.note_id, self.role, self.position) + extension
            if self.staging_name != expected_name:
                raise ValueError("staging name does not bind its media slot")
        elif any(value is not None for value in downloaded_fields) or self.missing_reason is None:
            raise ValueError("unavailable media requires only a finite reason")
        if self.role != "video" and self.duration_ms is not None:
            raise ValueError("duration is video-only")
        return self


class BridgeNote(StrictModel):
    media_manifest_version: Literal[2]
    note_id: str
    canonical_url: str
    title: str | None = None
    body: str | None = None
    tags: list[str] = Field(default_factory=list)
    note_type: str | None = None
    published_at: datetime | None = None
    time_evidence: TimeEvidence | None = None
    author_id: str | None = None
    author_name: str | None = None
    author_profile_url: str | None = None
    metrics: NoteMetrics
    metric_provenance: dict[MetricProvenanceKey, MetricEvidenceSource] = Field(default_factory=dict)
    source_position: int
    media_discovered_count: int = Field(ge=0)
    media_discovery_truncated: bool = False
    media_candidates: list[StagedMediaCandidate] = Field(default_factory=list)
    cover: CoverCandidate | None = None

    @field_validator("published_at", mode="before")
    @classmethod
    def parse_serialized_published_at(cls, value: object) -> datetime | None:
        """Accept only this bridge's canonical JSON datetime representation."""
        if value is None:
            return value
        if type(value) is datetime:
            offset = value.utcoffset()
            if offset is not None and offset % timedelta(minutes=1) != timedelta():
                raise ValueError("published_at requires a whole-minute UTC offset")
            return value
        if type(value) is not str or _BRIDGE_DATETIME.fullmatch(value) is None:
            raise ValueError("published_at must be a canonical bridge datetime")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("published_at must be a canonical bridge datetime") from error
        if _bridge_datetime_json_value(parsed) != value:
            raise ValueError("published_at must be a canonical bridge datetime")
        return parsed

    @field_validator("canonical_url")
    @classmethod
    def validate_note_canonical_url(cls, value: str) -> str:
        parsed = parse_xhs_url(value)
        if parsed.object_type != "note" or parsed.canonical_url != value:
            raise ValueError("canonical_url must be a query-free Xiaohongshu note URL")
        return value

    @field_validator("author_profile_url")
    @classmethod
    def validate_author_profile_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = parse_xhs_url(value)
        if parsed.object_type != "profile" or parsed.canonical_url != value:
            raise ValueError("author_profile_url must be a query-free Xiaohongshu profile URL")
        return value

    @model_validator(mode="after")
    def validate_media_manifest(self) -> BridgeNote:
        if self.media_candidates and self.cover is not None:
            raise ValueError("note covers cannot coexist with a V2 media manifest")
        by_role: dict[MediaRole, list[StagedMediaCandidate]] = {
            "image": [],
            "video_cover": [],
            "video": [],
        }
        seen: set[tuple[MediaRole, int]] = set()
        downloaded_bytes = 0
        downloaded_images = 0
        for candidate in self.media_candidates:
            if candidate.note_id != self.note_id:
                raise ValueError("media candidate note ID must match note")
            key = (candidate.role, candidate.position)
            if key in seen:
                raise ValueError("media candidates must be unique by role and position")
            seen.add(key)
            by_role[candidate.role].append(candidate)
            if candidate.status == "downloaded":
                assert candidate.size_bytes is not None
                downloaded_bytes += candidate.size_bytes
                if candidate.role == "image":
                    downloaded_images += 1
        image_slots = by_role["image"]
        if len(image_slots) > MAX_DISCOVERED_IMAGE_SLOTS:
            raise ValueError("too many represented image slots")
        if downloaded_images > MAX_IMAGES_PER_NOTE:
            raise ValueError("too many downloaded images")
        if len(by_role["video"]) > 1 or len(by_role["video_cover"]) > 1:
            raise ValueError("only one represented video slot is allowed")
        if image_slots and (by_role["video"] or by_role["video_cover"]):
            raise ValueError("image and video media cannot be mixed")
        if downloaded_bytes > MAX_NOTE_MEDIA_BYTES:
            raise ValueError("media exceeds the per-note byte limit")
        for slots in by_role.values():
            positions = sorted(item.position for item in slots)
            if positions != list(range(1, len(positions) + 1)):
                raise ValueError("media positions must be contiguous within a role")
        if self.media_discovered_count < len(image_slots):
            raise ValueError("media discovered count cannot be below represented image slots")
        if self.media_discovery_truncated != (self.media_discovered_count > len(image_slots)):
            raise ValueError("media discovery truncation must match represented image slots")
        return self

    @model_validator(mode="after")
    def validate_metric_provenance(self) -> BridgeNote:
        validate_metric_provenance(self.metrics, self.metric_provenance)
        return self


class SearchPayload(StrictModel):
    notes: list[BridgeNote]


class AccountPayload(StrictModel):
    account: AccountRecord | None
    notes: list[BridgeNote]
    requested_count: int = Field(ge=1, le=10)
    candidate_attempts: list[CandidateAttempt]
    pacing_summary: PacingSummary
    avatar: CoverCandidate | None = None

    @model_validator(mode="after")
    def validate_run_media_budget(self) -> AccountPayload:
        if self.account is None:
            raise ValueError("non-failed account payload requires a bound account")
        positions = [attempt.position for attempt in self.candidate_attempts]
        if positions != list(range(1, self.requested_count + 1)):
            raise ValueError("attempt positions must match requested_count")
        complete_attempts = [
            attempt for attempt in self.candidate_attempts if attempt.outcome == "complete"
        ]
        if len(self.notes) != len(complete_attempts):
            raise ValueError("complete attempts must match notes")
        expected_delays = sum(
            attempt.note_id is not None and attempt.stage != "candidate"
            for attempt in self.candidate_attempts
        )
        if len(self.pacing_summary.detail_delay_ms) != expected_delays:
            raise ValueError("detail pacing must match attempted candidates")
        if set(self.account.field_statuses) != set(get_args(AccountFieldKey)):
            raise ValueError("account field statuses must contain the exact finite keys")
        for attempt in complete_attempts:
            assert attempt.note_id is not None
            matches = [
                note
                for note in self.notes
                if (note.note_id, note.source_position) == (attempt.note_id, attempt.position)
            ]
            if len(matches) != 1:
                raise ValueError("complete attempt must match exactly one note")
        downloaded_bytes = self.avatar.size_bytes if self.avatar is not None else 0
        for note in self.notes:
            for candidate in note.media_candidates:
                if candidate.status == "downloaded":
                    assert candidate.size_bytes is not None
                    downloaded_bytes += candidate.size_bytes
        if downloaded_bytes > MAX_RUN_MEDIA_BYTES:
            raise ValueError("media exceeds the run byte budget")
        return self


class _ReadOnlyClient(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool: ...

    def search_notes(self, keyword: str) -> object: ...

    def get_note_detail(self, note_id: str, xsec_token: str = "") -> object: ...

    def get_user_info(self, user_id: str) -> object: ...

    def get_user_posts(self, user_id: str) -> object: ...

    def account_pacing_summary(self) -> PacingSummary: ...


class _AuthStorage(Protocol):
    CONFIG_DIR: Path
    COOKIE_FILE: Path
    TOKEN_CACHE_FILE: Path
    REQUIRED_COOKIES: frozenset[str]

    def get_saved_cookie_string(self) -> str | None: ...


class _ProjectAuthStorage:
    CONFIG_DIR: Path
    COOKIE_FILE: Path
    TOKEN_CACHE_FILE: Path
    REQUIRED_COOKIES: frozenset[str] = frozenset({"a1", "web_session"})

    def __init__(self, auth_dir: Path) -> None:
        configure_auth_directory(self, auth_dir)

    def get_saved_cookie_string(self) -> str | None:
        try:
            raw = json.loads(self.COOKIE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if type(raw) is not dict or type(raw.get("cookies")) is not dict:
            return None
        cookies = raw["cookies"]
        assert isinstance(cookies, dict)
        allowed = {
            key: value
            for key, value in cookies.items()
            if key in self.REQUIRED_COOKIES and isinstance(value, str) and value
        }
        if not self.REQUIRED_COOKIES.issubset(allowed):
            return None
        return "; ".join(f"{key}={allowed[key]}" for key in sorted(allowed))


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constant")


def parse_bridge_request(raw: bytes) -> BridgeRequest:
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request too large")
    try:
        value = json.loads(
            raw, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant
        )
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid bridge request") from exc
    if type(value) is not dict:
        raise ValueError("bridge request must be an object")
    try:
        return BridgeRequest.model_validate(value)
    except Exception as exc:
        raise ValueError("invalid bridge request") from exc


def configure_auth_directory(auth_module: _AuthStorage, auth_dir: Path) -> None:
    resolved = _prepare_private_directory(auth_dir, 0o700)
    auth_module.CONFIG_DIR = resolved
    auth_module.COOKIE_FILE = resolved / "cookies.json"
    auth_module.TOKEN_CACHE_FILE = resolved / "token_cache.json"


def run_bridge(
    request: BridgeRequest,
    client_factory: Callable[[Path], _ReadOnlyClient],
    *,
    staging_dir: Path | None = None,
    auth_module: _AuthStorage | None = None,
    login_provider: Callable[[Path], dict[str, str] | None] | None = None,
) -> BridgeResponse:
    try:
        if request.operation == "status":
            if _auth_storage_state(auth_module) == "invalid":
                return _failure(BridgeErrorCode.AUTH_INVALID)
            return _auth_response(_saved_cookie_dict(auth_module))
        if request.operation == "login":
            try:
                return _auth_response(_login_cookie_dict(auth_module, login_provider))
            except _LoginAcquisitionFailed:
                return _failure(BridgeErrorCode.AUTH_FAILED)
        if staging_dir is None:
            return _failure(BridgeErrorCode.INVALID_REQUEST)
        staging_dir = _require_existing_private_directory(staging_dir)
        if _auth_storage_state(auth_module) == "invalid":
            return _failure(BridgeErrorCode.AUTH_INVALID)
        cookies = _saved_cookie_dict(auth_module)
        if cookies is None:
            return _failure(BridgeErrorCode.AUTH_ACTION_REQUIRED)
        profile_dir = _fixed_profile_directory(auth_module)
        with client_factory(profile_dir) as client:
            if request.operation == "search":
                return _collect_search(client, request, staging_dir)
            return _collect_account(client, request, staging_dir)
        return _failure(BridgeErrorCode.INVALID_REQUEST)
    except Exception:  # noqa: BLE001
        return _failure(BridgeErrorCode.UPSTREAM_ERROR)


def _auth_response(cookies: dict[str, str] | None) -> BridgeResponse:
    return BridgeResponse(
        status="complete",
        payload={"auth_status": "authenticated" if cookies is not None else "action_required"},
    )


def _collect_search(
    client: _ReadOnlyClient, request: BridgeRequest, staging_dir: Path
) -> BridgeResponse:
    assert request.keyword is not None and request.limit is not None
    notes, code, all_scope_invalid = _read_note_details(
        client, _note_candidates(client.search_notes(request.keyword), request.limit), staging_dir
    )
    if all_scope_invalid:
        return _failure(BridgeErrorCode.MEDIA_SCOPE_INVALID)
    return BridgeResponse(
        status="partial" if code else "complete",
        payload=SearchPayload(notes=notes).model_dump(exclude_none=True),
        error_code=code,
    )


def _collect_account(
    client: _ReadOnlyClient, request: BridgeRequest, staging_dir: Path
) -> BridgeResponse:
    assert request.account_id is not None and request.limit is not None
    run_budget = MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES)
    account, avatar, profile_code = _project_profile(
        client.get_user_info(request.account_id), request.account_id, staging_dir, run_budget
    )
    notes, candidate_attempts, detail_code, all_scope_invalid = _read_account_note_details(
        client,
        _account_candidate_slots(client.get_user_posts(request.account_id), request.limit),
        staging_dir,
        run_budget,
    )
    if all_scope_invalid:
        return _failure(BridgeErrorCode.MEDIA_SCOPE_INVALID)
    if account is None:
        return _failure(profile_code or BridgeErrorCode.PROFILE_UNAVAILABLE)
    code = profile_code or detail_code
    payload = AccountPayload(
        account=account,
        notes=notes,
        requested_count=request.limit,
        candidate_attempts=candidate_attempts,
        pacing_summary=client.account_pacing_summary(),
        avatar=avatar,
    ).model_dump(exclude_none=True)
    return BridgeResponse(
        status="partial" if code else "complete",
        payload=payload,
        error_code=code,
    )


def _read_note_details(
    client: _ReadOnlyClient,
    candidates: list[tuple[str, str, int]],
    staging_dir: Path,
    run_budget: MediaBudget | None = None,
) -> tuple[list[BridgeNote], BridgeErrorCode | None, bool]:
    notes: list[BridgeNote] = []
    code: BridgeErrorCode | None = None
    media_budget = run_budget or MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES)
    scope_rejections = 0
    for note_id, token, position in candidates:
        try:
            detail = client.get_note_detail(note_id, token)
        except Exception:  # noqa: BLE001
            code = BridgeErrorCode.DETAIL_UNAVAILABLE
            continue
        if not _detail_media_scope_valid(detail):
            scope_rejections += 1
            code = BridgeErrorCode.MEDIA_SCOPE_INVALID
            continue
        note = _project_note(detail, note_id, position, staging_dir, media_budget)
        if note is None:
            code = BridgeErrorCode.DETAIL_ID_MISMATCH
            continue
        if _has_unavailable_media(note) and code is None:
            code = BridgeErrorCode.MEDIA_PARTIAL
        notes.append(note)
    if scope_rejections:
        code = BridgeErrorCode.MEDIA_SCOPE_INVALID
    return notes, code, bool(candidates) and scope_rejections == len(candidates)


@dataclass(frozen=True)
class _AccountCandidateSlot:
    position: int
    note_id: str | None
    token: str
    rejection_reason: Literal["candidate_shortfall", "candidate_invalid"] | None


def _account_candidate_slots(raw_items: object, limit: int) -> list[_AccountCandidateSlot]:
    items = raw_items if isinstance(raw_items, list) else []
    return [_project_account_slot(items, position) for position in range(1, limit + 1)]


def _project_account_slot(items: list[object], position: int) -> _AccountCandidateSlot:
    if position > len(items):
        return _AccountCandidateSlot(position, None, "", "candidate_shortfall")
    item = items[position - 1]
    note_id = _first_string(item, "id", "noteId", "note_id")
    retained_note_id = note_id if note_id is not None and SAFE_ID.fullmatch(note_id) else None
    raw_token = _first_value(item, "xsec_token", "xsecToken")
    if (
        not isinstance(item, dict)
        or retained_note_id is None
        or (raw_token is not None and not isinstance(raw_token, str))
    ):
        return _AccountCandidateSlot(position, retained_note_id, "", "candidate_invalid")
    return _AccountCandidateSlot(
        position,
        retained_note_id,
        raw_token if isinstance(raw_token, str) else "",
        None,
    )


def _read_account_note_details(
    client: _ReadOnlyClient,
    slots: list[_AccountCandidateSlot],
    staging_dir: Path,
    run_budget: MediaBudget,
) -> tuple[list[BridgeNote], list[CandidateAttempt], BridgeErrorCode | None, bool]:
    notes: list[BridgeNote] = []
    candidate_attempts: list[CandidateAttempt] = []
    detail_attempts: list[CandidateAttempt] = []
    code: BridgeErrorCode | None = None
    for slot in slots:
        if slot.rejection_reason == "candidate_shortfall":
            candidate_attempts.append(
                CandidateAttempt(
                    position=slot.position,
                    outcome="unavailable",
                    stage="candidate",
                    reason=slot.rejection_reason,
                )
            )
            if code is None:
                code = BridgeErrorCode.DETAIL_UNAVAILABLE
            continue
        if slot.rejection_reason == "candidate_invalid":
            candidate_attempts.append(
                CandidateAttempt(
                    position=slot.position,
                    note_id=slot.note_id,
                    outcome="rejected",
                    stage="candidate",
                    reason=slot.rejection_reason,
                )
            )
            if code is None:
                code = BridgeErrorCode.DETAIL_UNAVAILABLE
            continue
        assert slot.note_id is not None
        try:
            detail = client.get_note_detail(slot.note_id, slot.token)
        except DetailReadFailure as error:
            attempt = _detail_read_failure_attempt(slot, error)
            candidate_attempts.append(attempt)
            detail_attempts.append(attempt)
            if code is None:
                code = BridgeErrorCode.DETAIL_UNAVAILABLE
            continue
        except Exception:  # noqa: BLE001 - upstream details are not trusted.
            attempt = CandidateAttempt(
                position=slot.position,
                note_id=slot.note_id,
                outcome="unavailable",
                stage="detail_navigation",
                reason="upstream_unavailable",
            )
            candidate_attempts.append(attempt)
            detail_attempts.append(attempt)
            if code is None:
                code = BridgeErrorCode.DETAIL_UNAVAILABLE
            continue
        if not _detail_media_scope_valid(detail):
            attempt = CandidateAttempt(
                position=slot.position,
                note_id=slot.note_id,
                outcome="rejected",
                stage="detail_projection",
                reason="media_scope_invalid",
            )
            candidate_attempts.append(attempt)
            detail_attempts.append(attempt)
            code = BridgeErrorCode.MEDIA_SCOPE_INVALID
            continue
        note = _project_note(detail, slot.note_id, slot.position, staging_dir, run_budget)
        if note is None:
            attempt = CandidateAttempt(
                position=slot.position,
                note_id=slot.note_id,
                outcome="unavailable",
                stage="detail_projection",
                reason="detail_projection_unavailable",
            )
            candidate_attempts.append(attempt)
            detail_attempts.append(attempt)
            if code is None:
                code = BridgeErrorCode.DETAIL_ID_MISMATCH
            continue
        attempt = CandidateAttempt(
            position=slot.position,
            note_id=slot.note_id,
            outcome="complete",
            stage="detail_projection",
        )
        candidate_attempts.append(attempt)
        detail_attempts.append(attempt)
        if _has_unavailable_media(note) and code is None:
            code = BridgeErrorCode.MEDIA_PARTIAL
        notes.append(note)
    all_scope_invalid = (
        bool(detail_attempts)
        and not notes
        and all(attempt.reason == "media_scope_invalid" for attempt in detail_attempts)
    )
    return notes, candidate_attempts, code, all_scope_invalid


def _detail_read_failure_attempt(
    slot: _AccountCandidateSlot, error: DetailReadFailure
) -> CandidateAttempt:
    outcome: Literal["unavailable", "rejected"] = (
        "rejected" if error.reason in {"card_unsafe", "media_scope_invalid"} else "unavailable"
    )
    return CandidateAttempt(
        position=slot.position,
        note_id=slot.note_id,
        outcome=outcome,
        stage=error.stage,
        reason=error.reason,
    )


def _note_candidates(raw_items: object, limit: int) -> list[tuple[str, str, int]]:
    if not isinstance(raw_items, list):
        return []
    result: list[tuple[str, str, int]] = []
    for position, item in enumerate(raw_items[:limit], start=1):
        note_id = _first_string(item, "id", "noteId", "note_id")
        if note_id is None or not SAFE_ID.fullmatch(note_id):
            continue
        result.append((note_id, _first_string(item, "xsec_token", "xsecToken") or "", position))
    return result


def _project_note(
    raw_detail: object,
    requested_note_id: str,
    source_position: int,
    staging_dir: Path,
    run_budget: MediaBudget | None = None,
) -> BridgeNote | None:
    if not SAFE_ID.fullmatch(requested_note_id) or not isinstance(raw_detail, dict):
        return None
    raw_note = raw_detail.get("note", raw_detail)
    if not isinstance(raw_note, dict) or not _detail_media_scope_valid(raw_detail):
        return None
    internal_id = _first_string(raw_note, "id", "noteId", "note_id")
    if (
        internal_id != requested_note_id
        or internal_id is None
        or not SAFE_ID.fullmatch(internal_id)
    ):
        return None
    user = _dict_value(raw_note, "user")
    raw_author_id = _first_string(user, "userId", "user_id", "id")
    author_id = raw_author_id if raw_author_id and SAFE_ID.fullmatch(raw_author_id) else None
    interactions = _dict_value(raw_note, "interactInfo", "interact_info")
    try:
        discovered_count = _detail_media_discovered_count(raw_detail, raw_note)
        staging_note = dict(raw_note)
        staging_note["media_discovered_count"] = discovered_count
        staging_note["author_id"] = author_id
        candidates = _stage_note_media(
            staging_note,
            internal_id,
            staging_dir,
            run_budget or MediaBudget(remaining_bytes=MAX_RUN_MEDIA_BYTES),
        )
        metrics = NoteMetrics(
            likes=_metric_value(interactions, "likedCount", "liked_count"),
            collects=_metric_value(interactions, "collectedCount", "collected_count"),
            comments=_metric_value(interactions, "commentCount", "comment_count"),
            shares=_metric_value(interactions, "shareCount", "share_count", "shared_count"),
        )
        return BridgeNote(
            media_manifest_version=2,
            note_id=internal_id,
            canonical_url=parse_xhs_url(
                f"https://www.xiaohongshu.com/explore/{internal_id}"
            ).canonical_url,
            title=_safe_text(_first_string(raw_note, "title")),
            body=_safe_text(_first_string(raw_note, "desc", "description")),
            tags=_project_tags(raw_note.get("tagList", raw_note.get("tag_list"))),
            note_type=_safe_text(_first_string(raw_note, "type")),
            published_at=_published_at(
                _first_value(raw_note, "time", "publishTime", "publish_time")
            ),
            time_evidence=_time_evidence(raw_note.get("time_evidence")),
            author_id=author_id,
            author_name=_safe_text(_first_string(user, "nickname", "nick_name")),
            author_profile_url=(
                parse_xhs_url(f"https://www.xiaohongshu.com/user/profile/{author_id}").canonical_url
                if author_id
                else None
            ),
            metrics=metrics,
            metric_provenance=_metric_provenance(raw_note.get("metric_provenance"), metrics),
            source_position=source_position,
            media_discovered_count=discovered_count,
            media_discovery_truncated=discovered_count
            > len([item for item in candidates if item.role == "image"]),
            media_candidates=candidates,
        )
    except Exception:  # noqa: BLE001
        return None


def _project_profile(
    raw_profile: object,
    requested_account_id: str,
    staging_dir: Path,
    run_budget: MediaBudget | None = None,
) -> tuple[AccountRecord | None, CoverCandidate | None, BridgeErrorCode | None]:
    if not SAFE_ID.fullmatch(requested_account_id) or not isinstance(raw_profile, dict):
        return None, None, BridgeErrorCode.PROFILE_AMBIGUOUS
    page_data = _dict_value(raw_profile, "userPageData", "user_page_data")
    basic = _dict_value(page_data, "basicInfo", "basic_info")
    account_id = _first_string(basic, "userId", "user_id", "id")
    if (
        account_id != requested_account_id
        or account_id is None
        or not SAFE_ID.fullmatch(account_id)
    ):
        return None, None, BridgeErrorCode.PROFILE_AMBIGUOUS
    field_statuses = _profile_field_statuses(page_data)
    if field_statuses is None:
        return None, None, BridgeErrorCode.PROFILE_UNAVAILABLE
    interaction_note_count, interaction_follower_count, platform_metrics, interactions_discarded = (
        _project_profile_interactions(raw_profile, page_data)
    )
    name = (
        _safe_text(_first_string(basic, "nickname", "nick_name"))
        if field_statuses["name"] == "exposed"
        else None
    )
    bio = (
        ""
        if field_statuses["bio"] == "exposed_empty"
        else _safe_text(_first_string(basic, "desc", "description"))
        if field_statuses["bio"] == "exposed"
        else None
    )
    note_count = interaction_note_count if field_statuses["note_count"] == "exposed" else None
    follower_count = (
        interaction_follower_count if field_statuses["follower_count"] == "exposed" else None
    )
    metrics = platform_metrics if field_statuses["platform_metrics"] == "exposed" else {}
    if field_statuses["name"] == "exposed" and name is None:
        field_statuses["name"] = "unavailable"
    if field_statuses["bio"] == "exposed" and bio is None:
        field_statuses["bio"] = "unavailable"
    if field_statuses["note_count"] == "exposed" and (
        note_count is None or note_count.precision == "not_exposed"
    ):
        field_statuses["note_count"] = "unavailable"
    if field_statuses["follower_count"] == "exposed" and (
        follower_count is None or follower_count.precision == "not_exposed"
    ):
        field_statuses["follower_count"] = "unavailable"
    if field_statuses["platform_metrics"] == "exposed" and (
        not metrics
        or interactions_discarded
        or any(metric.precision == "not_exposed" for metric in metrics.values())
    ):
        field_statuses["platform_metrics"] = "unavailable"
    avatar = _stage_cover(
        _avatar_source(basic) if field_statuses["avatar"] == "exposed" else None,
        account_id,
        staging_dir,
        suffix="avatar",
        run_budget=run_budget,
    )
    if field_statuses["avatar"] == "exposed" and avatar is None:
        field_statuses["avatar"] = "unavailable"
    profile_unavailable = any(status == "unavailable" for status in field_statuses.values())
    try:
        return (
            AccountRecord(
                account_id=account_id,
                profile_url=parse_xhs_url(
                    f"https://www.xiaohongshu.com/user/profile/{account_id}"
                ).canonical_url,
                name=name,
                bio=bio,
                note_count=note_count,
                follower_count=follower_count,
                platform_metrics=metrics,
                field_statuses=field_statuses,
            ),
            avatar,
            BridgeErrorCode.PROFILE_UNAVAILABLE if profile_unavailable else None,
        )
    except Exception:  # noqa: BLE001
        return None, None, BridgeErrorCode.PROFILE_UNAVAILABLE


def _profile_field_statuses(
    page_data: dict[str, object],
) -> dict[AccountFieldKey, AccountFieldStatus] | None:
    values = page_data.get("fieldStatuses")
    expected_keys = {
        "name", "bio", "note_count", "follower_count", "avatar", "platform_metrics"
    }
    valid_statuses = {"exposed", "exposed_empty", "not_exposed", "unavailable"}
    if not isinstance(values, dict) or set(values) != expected_keys:
        return None
    if any(not isinstance(status, str) or status not in valid_statuses for status in values.values()):
        return None
    statuses: dict[AccountFieldKey, AccountFieldStatus] = {
        "name": cast(AccountFieldStatus, values["name"]),
        "bio": cast(AccountFieldStatus, values["bio"]),
        "note_count": cast(AccountFieldStatus, values["note_count"]),
        "follower_count": cast(AccountFieldStatus, values["follower_count"]),
        "avatar": cast(AccountFieldStatus, values["avatar"]),
        "platform_metrics": cast(AccountFieldStatus, values["platform_metrics"]),
    }
    if statuses["name"] != "exposed":
        return None
    if any(
        key != "bio" and status == "exposed_empty" for key, status in statuses.items()
    ):
        return None
    return statuses


def _project_profile_interactions(
    raw_profile: dict[str, object], page_data: dict[str, object]
) -> tuple[MetricValue | None, MetricValue | None, dict[str, MetricValue], bool]:
    """Project the pinned upstream's visible profile-stat list without relabelling it."""
    interactions: list[object] = []
    for candidate in (page_data.get("interactions"), raw_profile.get("interactions")):
        if isinstance(candidate, list) and candidate:
            interactions = candidate
            break

    note_count: MetricValue | None = None
    follower_count: MetricValue | None = None
    platform_metrics: dict[str, MetricValue] = {}
    discarded = False
    for item in interactions:
        if not isinstance(item, dict):
            discarded = True
            continue
        label = _safe_text(_first_string(item, "name", "type"))
        if label is None or not is_safe_evidence_key(label):
            discarded = True
            continue
        metric = _metric_value(item, "count", "value") or MetricValue(
            raw_value=None, normalized_value=None, precision="not_exposed"
        )
        platform_metrics[label] = metric
        normalized_label = re.sub(r"[\s_-]+", "_", label.casefold())
        if note_count is None and normalized_label in _NOTE_LABELS:
            note_count = metric
        if follower_count is None and normalized_label in _FOLLOWER_LABELS:
            follower_count = metric
    return note_count, follower_count, platform_metrics, discarded


def _metric_value(container: dict[str, object], *keys: str) -> MetricValue | None:
    value = _first_value(container, *keys)
    if type(value) is int:
        return (
            MetricValue(raw_value=str(value), normalized_value=value, precision="exact")
            if value >= 0
            else MetricValue(raw_value=None, normalized_value=None, precision="not_exposed")
        )
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if EXACT_NUMBER.fullmatch(raw):
        return MetricValue(
            raw_value=raw, normalized_value=int(raw.replace(",", "")), precision="exact"
        )
    rounded = ROUNDED_NUMBER.fullmatch(raw)
    if rounded:
        return MetricValue(
            raw_value=raw,
            normalized_value=round(
                float(rounded.group(1)) * (10000 if rounded.group(2) == "万" else 1000)
            ),
            precision="display_rounded",
        )
    return MetricValue(raw_value=None, normalized_value=None, precision="not_exposed")


def _detail_media_scope_valid(raw_detail: object) -> bool:
    """Accept only the exact detail-media scope asserted by the pinned client."""
    if not isinstance(raw_detail, dict):
        return False
    if "media_scope_valid" in raw_detail:
        return raw_detail.get("media_scope_valid") is True
    raw_note = raw_detail.get("note", raw_detail)
    return isinstance(raw_note, dict) and raw_note.get("media_scope_valid") is True


def _detail_media_discovered_count(
    raw_detail: dict[str, object], raw_note: dict[str, object]
) -> int:
    value = raw_detail.get("media_discovered_count")
    if value is None:
        value = raw_note.get("media_discovered_count")
    if type(value) is not int or value < 0:
        raise ValueError("invalid media discovered count")
    return value


def _has_unavailable_media(note: BridgeNote) -> bool:
    return any(candidate.status != "downloaded" for candidate in note.media_candidates)


def _media_staging_base(note_id: str, role: MediaRole, position: int) -> str:
    if SAFE_ID.fullmatch(note_id) is None or type(position) is not int or position < 1:
        raise ValueError("unsafe media slot")
    if role == "image":
        return f"{note_id}-image-{position:03d}"
    if role == "video_cover" and position == 1:
        return f"{note_id}-video-cover"
    if role == "video" and position == 1:
        return f"{note_id}-video"
    raise ValueError("invalid media slot")


def _stage_note_media(
    raw_note: dict[str, object], note_id: str, staging_dir: Path, run_budget: MediaBudget
) -> list[StagedMediaCandidate]:
    """Stage every exposed media slot, preserving position and bounded failures."""
    if SAFE_ID.fullmatch(note_id) is None:
        raise ValueError("unsafe note ID")
    raw_discovered_count = raw_note.get("media_discovered_count", 0)
    if type(raw_discovered_count) is not int or raw_discovered_count < 0:
        raise ValueError("invalid media discovered count")
    discovered_count = raw_discovered_count
    note_budget = MediaBudget(remaining_bytes=MAX_NOTE_MEDIA_BYTES)
    deadline = time.monotonic() + NOTE_MEDIA_BUDGET_SECONDS
    if _first_string(raw_note, "type") == "video":
        return _stage_video_media(raw_note, note_id, staging_dir, note_budget, run_budget, deadline)
    return _stage_image_media(
        raw_note,
        note_id,
        staging_dir,
        note_budget,
        run_budget,
        deadline,
        discovered_count,
    )


def _stage_image_media(
    raw_note: dict[str, object],
    note_id: str,
    staging_dir: Path,
    note_budget: MediaBudget,
    run_budget: MediaBudget,
    deadline: float,
    discovered_count: int,
) -> list[StagedMediaCandidate]:
    image_list = raw_note.get("image_list", raw_note.get("imageList"))
    if not isinstance(image_list, list):
        image_list = []
    represented_count = min(discovered_count, MAX_DISCOVERED_IMAGE_SLOTS)
    sources: dict[int, str | None] = {}
    for item in image_list:
        if not isinstance(item, dict):
            raise TypeError("malformed image slot")
        position = item.get("position")
        if type(position) is not int or not 1 <= position <= represented_count:
            raise ValueError("invalid image position")
        if position in sources:
            raise ValueError("duplicate image position")
        sources[position] = _first_string(item, "url", "urlDefault", "url_default")
    if len(sources) > represented_count:
        raise ValueError("too many image slots")

    candidates: list[StagedMediaCandidate] = []
    for position in range(1, represented_count + 1):
        source_url = sources.get(position)
        if position > MAX_IMAGES_PER_NOTE:
            candidates.append(
                _unavailable_media(note_id, "image", position, "rejected", "slot_limit")
            )
        elif source_url is None:
            candidates.append(
                _unavailable_media(note_id, "image", position, "missing", "source_not_exposed")
            )
        else:
            candidates.append(
                _stage_media_slot(
                    note_id,
                    "image",
                    position,
                    source_url,
                    note_budget,
                    run_budget,
                    deadline,
                    None,
                    staging_dir,
                )
            )
    return candidates


def _stage_video_media(
    raw_note: dict[str, object],
    note_id: str,
    staging_dir: Path,
    note_budget: MediaBudget,
    run_budget: MediaBudget,
    deadline: float,
) -> list[StagedMediaCandidate]:
    author_id = _first_string(raw_note, "author_id")
    if author_id is None or SAFE_ID.fullmatch(author_id) is None:
        return [
            _unavailable_media(note_id, "video_cover", 1, "missing", "source_not_exposed"),
            _unavailable_media(note_id, "video", 1, "missing", "source_not_exposed"),
        ]
    raw_video = _dict_value(raw_note, "video")
    duration = raw_video.get("duration_ms")
    duration_ms = duration if type(duration) is int and duration >= 1 else None
    candidates: list[StagedMediaCandidate] = []
    poster = _first_string(raw_video, "poster")
    if poster is None:
        candidates.append(
            _unavailable_media(note_id, "video_cover", 1, "missing", "source_not_exposed")
        )
    else:
        candidates.append(
            _stage_media_slot(
                note_id,
                "video_cover",
                1,
                poster,
                note_budget,
                run_budget,
                deadline,
                None,
                staging_dir,
            )
        )
    video_url = _first_string(raw_video, "url")
    if video_url is None:
        candidates.append(
            _unavailable_media(note_id, "video", 1, "missing", "source_not_exposed", duration_ms)
        )
    elif not _is_supported_direct_video_source(video_url):
        candidates.append(
            _unavailable_media(note_id, "video", 1, "rejected", "unsupported_source", duration_ms)
        )
    else:
        candidates.append(
            _stage_media_slot(
                note_id,
                "video",
                1,
                video_url,
                note_budget,
                run_budget,
                deadline,
                duration_ms,
                staging_dir,
            )
        )
    return candidates


def _unavailable_media(
    note_id: str,
    role: MediaRole,
    position: int,
    status: Literal["missing", "rejected"],
    reason: MediaMissingReason,
    duration_ms: int | None = None,
) -> StagedMediaCandidate:
    return StagedMediaCandidate(
        note_id=note_id,
        role=role,
        position=position,
        status=status,
        duration_ms=duration_ms,
        missing_reason=reason,
    )


def _stage_media_slot(
    note_id: str,
    role: MediaRole,
    position: int,
    source_url: str,
    note_budget: MediaBudget,
    run_budget: MediaBudget,
    deadline: float,
    duration_ms: int | None,
    staging_dir: Path,
) -> StagedMediaCandidate:
    if not _is_supported_direct_media_source(source_url, role):
        return _unavailable_media(
            note_id, role, position, "rejected", "unsupported_source", duration_ms
        )
    role_limit = MAX_VIDEO_BYTES if role == "video" else MAX_IMAGE_BYTES
    max_bytes = min(role_limit, note_budget.remaining_bytes, run_budget.remaining_bytes)
    if max_bytes <= 0:
        reason: MediaMissingReason = (
            "note_budget" if note_budget.remaining_bytes <= 0 else "run_budget"
        )
        return _unavailable_media(note_id, role, position, "rejected", reason, duration_ms)
    base = _media_staging_base(note_id, role, position)
    provisional_name = f".{base}.{secrets.token_hex(8)}.staging"
    try:
        mime_type, size_bytes, digest = _download_media(
            source_url,
            staging_dir,
            provisional_name,
            max_bytes=max_bytes,
            deadline=deadline,
        )
        expected_mime = VIDEO_MIME_TYPES if role == "video" else IMAGE_MIME_TYPES
        if mime_type not in expected_mime:
            raise _MediaMimeMismatch
        extension = mime_extension(mime_type)
        assert extension is not None
        final_name = base + extension
        _publish_staged_media(staging_dir, provisional_name, final_name)
        if not note_budget.consume(size_bytes) or not run_budget.consume(size_bytes):
            _unlink_staged_name(staging_dir, final_name)
            raise _MediaDownloadFailed
        return StagedMediaCandidate(
            note_id=note_id,
            role=role,
            position=position,
            status="downloaded",
            staging_name=final_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=digest,
            duration_ms=duration_ms,
        )
    except _MediaMimeMismatch:
        _unlink_staged_name(staging_dir, provisional_name)
        return _unavailable_media(note_id, role, position, "rejected", "mime_mismatch", duration_ms)
    except ValueError as exc:
        _unlink_staged_name(staging_dir, provisional_name)
        reason = "size_limit" if str(exc) == "media size exceeded" else "mime_mismatch"
        if str(exc) not in {"media size exceeded", "media MIME/container mismatch"}:
            reason = "unsafe_source"
        return _unavailable_media(note_id, role, position, "rejected", reason, duration_ms)
    except Exception:  # noqa: BLE001 - details may contain signed source URLs.
        _unlink_staged_name(staging_dir, provisional_name)
        return _unavailable_media(
            note_id, role, position, "rejected", "download_failed", duration_ms
        )


class _MediaMimeMismatch(Exception):
    pass


class _MediaDownloadFailed(Exception):
    pass


def _is_supported_direct_video_source(source_url: str) -> bool:
    try:
        parsed = urlsplit(source_url)
    except ValueError:
        return False
    return parsed.path.casefold().endswith((".mp4", ".webm")) and _is_supported_direct_media_source(
        source_url, "video"
    )


def _is_supported_direct_media_source(source_url: str, _role: MediaRole) -> bool:
    try:
        parsed = urlsplit(source_url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and not parsed.username
        and not parsed.password
    )


def _publish_staged_media(directory: Path, source_name: str, destination_name: str) -> None:
    if Path(source_name).name != source_name or Path(destination_name).name != destination_name:
        raise ValueError("unsafe staging name")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    published = False
    created_destination = False
    try:
        os.link(
            source_name,
            destination_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        created_destination = True
        os.unlink(source_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        published = True
    finally:
        if created_destination and not published:
            try:
                os.unlink(destination_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _unlink_staged_name(directory: Path, name: str) -> None:
    if Path(name).name != name:
        return
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(directory_fd)


def _download_media(
    source_url: str,
    staging_dir: Path,
    final_name: str,
    *,
    max_bytes: int,
    deadline: float,
) -> tuple[MediaMime, int, str]:
    """Stream one validated asset into an atomically published staging file."""
    if Path(final_name).name != final_name or not final_name:
        raise ValueError("unsafe staging name")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("media size exceeded")
    timeout = _media_request_timeout(deadline)
    _validate_media_url(source_url)
    directory_fd = os.open(staging_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary_name = f".{final_name}.{secrets.token_hex(8)}.tmp"
    file_fd: int | None = None
    published = False
    created_final = False
    try:
        file_fd = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        digest = hashlib.sha256()
        size = 0
        head = bytearray()
        with (
            open_pinned_https(
                source_url,
                headers={
                    "User-Agent": "xhs-research-workbench/0.1",
                    "Referer": "https://www.xiaohongshu.com/",
                },
                timeout=timeout,
                validate_url=_validate_media_url,
            ) as response,
            os.fdopen(os.dup(file_fd), "wb") as stream,
        ):
            declared = (response.getheader("Content-Type") or "").split(";", 1)[0].lower()
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > max_bytes):
                raise ValueError("media size exceeded")
            while chunk := response.read(64 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError("media budget exceeded")
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("media size exceeded")
                if len(head) < 64:
                    head.extend(chunk[: 64 - len(head)])
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        actual = sniff_media_mime(bytes(head))
        with os.fdopen(os.dup(file_fd), "rb") as verified:
            if (
                actual is None
                or actual != declared
                or size == 0
                or not validate_media_container(verified, actual, size)
            ):
                raise ValueError("media MIME/container mismatch")
        os.link(
            temporary_name,
            final_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        created_final = True
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        published = True
        return actual, size, digest.hexdigest()
    finally:
        if file_fd is not None:
            os.close(file_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        if created_final and not published:
            try:
                os.unlink(final_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _media_request_timeout(deadline: float) -> int:
    """Return a positive integer timeout that cannot outlive the media deadline."""
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining < 1:
        raise TimeoutError("media budget exceeded")
    return min(MEDIA_IO_TIMEOUT_SECONDS, int(remaining))


def _stage_cover(
    source_url: str | None,
    item_id: str,
    staging_dir: Path,
    *,
    suffix: str = "cover",
    run_budget: MediaBudget | None = None,
) -> CoverCandidate | None:
    if source_url is None or not SAFE_ID.fullmatch(item_id):
        return None
    max_bytes = min(MAX_IMAGE_BYTES, run_budget.remaining_bytes) if run_budget else MAX_IMAGE_BYTES
    if max_bytes <= 0:
        return None
    base = f"{item_id}-{suffix}"
    provisional_name = f".{base}.{secrets.token_hex(8)}.staging"
    final_name: str | None = None
    try:
        mime_type, size_bytes, digest = _download_media(
            source_url,
            staging_dir,
            provisional_name,
            max_bytes=max_bytes,
            deadline=time.monotonic() + NOTE_MEDIA_BUDGET_SECONDS,
        )
        if mime_type not in ("image/jpeg", "image/png", "image/webp"):
            raise _MediaMimeMismatch
        extension = mime_extension(mime_type)
        assert extension is not None
        final_name = base + extension
        _publish_staged_media(staging_dir, provisional_name, final_name)
        if run_budget is not None and not run_budget.consume(size_bytes):
            _unlink_staged_name(staging_dir, final_name)
            raise _MediaDownloadFailed
        return CoverCandidate(
            staging_name=final_name, mime_type=mime_type, size_bytes=size_bytes, sha256=digest
        )
    except Exception:  # noqa: BLE001
        _unlink_staged_name(staging_dir, provisional_name)
        if final_name is not None:
            _unlink_staged_name(staging_dir, final_name)
        return None


def _validate_media_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("unsafe media URL") from None
    if (
        parsed.scheme != "https"
        or host is None
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise ValueError("unsafe media URL")
    host = host.casefold().rstrip(".")
    if not _is_allowed_media_host(host):
        raise ValueError("unsafe media host")


def _is_allowed_media_host(host: str) -> bool:
    return host in {"xhscdn.com", "xiaohongshu.com"} or host.endswith(
        (".xhscdn.com", ".xiaohongshu.com")
    )


def _cover_source(note: dict[str, object]) -> str | None:
    images = note.get("imageList", note.get("image_list"))
    candidates: list[object] = [note.get("cover")]
    if isinstance(images, list) and images:
        candidates.append(images[0])
    return _media_source(candidates)


def _avatar_source(info: dict[str, object]) -> str | None:
    return _media_source([info.get("image"), info.get("avatar"), info.get("avatarUrl")])


def _media_source(candidates: list[object]) -> str | None:
    for candidate in candidates:
        if (
            isinstance(candidate, str)
            and (normalized := _upgrade_plain_http_xhs_media_url(candidate)) is not None
        ):
            return normalized
        if isinstance(candidate, dict):
            value = _first_string(candidate, "url", "urlDefault", "url_default")
            if value and (normalized := _upgrade_plain_http_xhs_media_url(value)) is not None:
                return normalized
    return None


def _upgrade_plain_http_xhs_media_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "http":
        return value
    host = parsed.hostname.casefold().rstrip(".") if parsed.hostname is not None else None
    if (
        host is None
        or not _is_allowed_media_host(host)
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return None
    return urlunsplit(("https", host, parsed.path, parsed.query, parsed.fragment))


def _project_tags(value: object) -> list[str]:
    return (
        [name for item in value if (name := _safe_text(_first_string(item, "name"))) is not None]
        if isinstance(value, list)
        else []
    )


def _published_at(value: object) -> datetime | None:
    if type(value) is int:
        try:
            return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return None


def _time_evidence(value: object) -> TimeEvidence | None:
    if not isinstance(value, dict):
        return None
    try:
        return TimeEvidence.model_validate(value)
    except ValueError:
        return None


def _metric_provenance(
    value: object, metrics: NoteMetrics
) -> dict[MetricProvenanceKey, MetricEvidenceSource]:
    if not isinstance(value, dict):
        return {}
    try:
        provenance = TypeAdapter(dict[MetricProvenanceKey, MetricEvidenceSource]).validate_python(
            value
        )
    except ValueError:
        return {}
    return {
        metric_name: source
        for metric_name, source in provenance.items()
        if (metric := getattr(metrics, metric_name)) is not None and metric.precision != "not_exposed"
    }


def _safe_text(value: str | None) -> str | None:
    if value is None or not is_safe_retained_text(value):
        return None
    sanitized = sanitize_evidence(value)
    return sanitized if isinstance(sanitized, str) and sanitized != "[REDACTED_URL]" else None


def _dict_value(container: object, *keys: str) -> dict[str, object]:
    value = _first_value(container, *keys)
    return value if isinstance(value, dict) else {}


def _first_string(container: object, *keys: str) -> str | None:
    value = _first_value(container, *keys)
    return value if isinstance(value, str) and value else None


def _first_value(container: object, *keys: str) -> object | None:
    if not isinstance(container, dict):
        return None
    for key in keys:
        value: object = container.get(key)
        if value is not None:
            return value
    return None


def _saved_cookie_dict(auth_module: _AuthStorage | None) -> dict[str, str] | None:
    if auth_module is None:
        return {}
    if not _valid_auth_storage(auth_module):
        return None
    try:
        return _validated_cookie_string(auth_module.get_saved_cookie_string(), auth_module)
    except Exception:  # noqa: BLE001
        return None


def _fixed_profile_directory(auth_module: _AuthStorage | None) -> Path:
    auth_dir = Path(auth_module.CONFIG_DIR) if auth_module is not None else Path(".local") / "auth"
    return auth_dir / "browser-profile"


def _login_cookie_dict(
    auth_module: _AuthStorage | None,
    login_provider: Callable[[Path], dict[str, str] | None] | None,
) -> dict[str, str] | None:
    if auth_module is None:
        return None
    saved = _saved_cookie_dict(auth_module)
    if login_provider is None and saved is not None:
        return saved
    if _auth_storage_state(auth_module) == "invalid" or login_provider is None:
        raise _LoginAcquisitionFailed
    try:
        value = login_provider(Path(auth_module.CONFIG_DIR))
        cookies = _validated_acquired_cookies(value, auth_module)
        if cookies is None:
            raise _LoginAcquisitionFailed
        _persist_auth_cookies(auth_module, cookies)
        return cookies
    except _LoginAcquisitionFailed:
        raise
    except Exception:  # noqa: BLE001
        raise _LoginAcquisitionFailed from None


def _validated_acquired_cookies(
    value: dict[str, str] | None, auth_module: _AuthStorage
) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    required = frozenset(getattr(auth_module, "REQUIRED_COOKIES", {"a1", "web_session"}))
    if required != frozenset({"a1", "web_session"}):
        return None
    allowed = {
        key: item
        for key, item in value.items()
        if key in required and isinstance(item, str) and item
    }
    return allowed if required.issubset(allowed) else None


def _validated_cookie_string(value: str | None, auth_module: _AuthStorage) -> dict[str, str] | None:
    if not isinstance(value, str):
        return None
    cookies = _cookie_string_to_dict(value)
    required = getattr(auth_module, "REQUIRED_COOKIES", {"a1", "web_session"})
    return (
        cookies
        if {"a1", "web_session"}.issubset(required) and all(cookies.get(key) for key in required)
        else None
    )


def _cookie_string_to_dict(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split(";"):
        name, separator, item = part.strip().partition("=")
        if separator and name and item and name not in result:
            result[name] = item
    return result


def _valid_auth_storage(auth_module: _AuthStorage) -> bool:
    return _auth_storage_state(auth_module) == "valid"


def _auth_storage_state(auth_module: _AuthStorage | None) -> Literal["missing", "invalid", "valid"]:
    if auth_module is None:
        return "valid"
    auth_dir = Path(auth_module.CONFIG_DIR)
    cookie_file = Path(auth_module.COOKIE_FILE)
    if cookie_file.parent != auth_dir or not _is_private_auth_directory(auth_dir):
        return "invalid"
    try:
        info = cookie_file.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "invalid"
    return (
        "valid"
        if stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_uid == os.geteuid()
        and info.st_nlink == 1
        else "invalid"
    )


def _persist_auth_cookies(auth_module: _AuthStorage, cookies: dict[str, str]) -> None:
    auth_dir = Path(auth_module.CONFIG_DIR)
    cookie_file = Path(auth_module.COOKIE_FILE)
    state = _auth_storage_state(auth_module)
    if state == "invalid" or cookie_file.parent != auth_dir:
        raise _LoginAcquisitionFailed
    temporary = auth_dir / f".cookies-{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump({"cookies": cookies}, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, cookie_file)
        cookie_file.chmod(0o600)
        directory_descriptor = os.open(auth_dir, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        if _auth_storage_state(auth_module) != "valid":
            raise _LoginAcquisitionFailed
    except Exception as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if isinstance(exc, _LoginAcquisitionFailed):
            raise
        raise _LoginAcquisitionFailed from None


def _prepare_private_directory(path: Path, mode: int) -> Path:
    if ".." in path.parts or _has_existing_symlink(path):
        raise ValueError("unsafe directory")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if _has_existing_symlink(path) or not path.is_dir():
        raise ValueError("unsafe directory")
    path.chmod(mode)
    return path.resolve(strict=True)


def _require_existing_private_directory(path: Path) -> Path:
    if ".." in path.parts or not _is_private_directory(path):
        raise ValueError("unsafe staging directory")
    return path.resolve(strict=True)


def _is_private_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not _has_existing_symlink(path)
    )


def _is_private_auth_directory(path: Path) -> bool:
    if not _is_private_directory(path):
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.geteuid()


def _has_existing_symlink(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    for part in path.parts:
        if part in {path.anchor, "."}:
            continue
        current /= part
        if current.exists() and current.is_symlink():
            return True
    return False


def _failure(code: BridgeErrorCode) -> BridgeResponse:
    return BridgeResponse(status="failed", payload={}, error_code=code)


def validate_bridge_response(request: BridgeRequest, raw: object) -> BridgeResponse:
    if type(raw) is not dict:
        raise ValueError("response must be object")
    response = BridgeResponse.model_validate(raw)
    if response.status == "failed":
        return response
    payload_type: type[StrictModel] = (
        AuthPayload
        if request.operation in {"status", "login"}
        else SearchPayload
        if request.operation == "search"
        else AccountPayload
    )
    payload = payload_type.model_validate(response.payload)
    if request.operation == "account":
        assert isinstance(payload, AccountPayload)
        if payload.requested_count != request.limit:
            raise ValueError("account payload requested_count must match request limit")
    return BridgeResponse(
        status=response.status,
        payload=payload.model_dump(exclude_none=True),
        error_code=response.error_code,
    )


def _default_client_factory(profile_dir: Path) -> _ReadOnlyClient:
    if profile_dir.name != "browser-profile":
        raise ValueError("invalid project browser profile")
    return PersistentXhsClient(profile_dir.parent)


def main() -> None:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            request = parse_bridge_request(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
            auth_module = _ProjectAuthStorage(Path(os.environ["XHS_WORKBENCH_AUTH_DIR"]))
            staging = os.environ.get("XHS_WORKBENCH_ASSET_STAGING_DIR")
            response = run_bridge(
                request,
                _default_client_factory,
                staging_dir=Path(staging) if staging else None,
                auth_module=auth_module,
                login_provider=acquire_project_login,
            )
        except Exception:  # noqa: BLE001
            response = _failure(BridgeErrorCode.INVALID_REQUEST)
    sys.stdout.write(response.model_dump_json())


if __name__ == "__main__":
    main()
