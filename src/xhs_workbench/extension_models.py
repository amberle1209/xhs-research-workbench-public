"""Strict public wire contracts for the Chrome extension native host.

These models deliberately retain only bounded public page facts.  They are a
trust boundary: unknown fields, coercible numbers, media source URLs, and
credentials fail validation before a job can see them.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from typing import Annotated, Literal, Self, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from xhs_workbench.media import MediaMime, MediaMissingReason, MediaRole
from xhs_workbench.models import is_canonical_xhs_search_route, is_safe_retained_text
from xhs_workbench.security import parse_xhs_url

PROTOCOL_VERSION = "1.0"
MAX_PROTOCOL_ID_LENGTH = 128
MAX_CANONICAL_URL_LENGTH = 512
MAX_TITLE_CHARACTERS = 200
MAX_BODY_CHARACTERS = 20_000
MAX_AUTHOR_NAME_CHARACTERS = 100
MAX_TAGS = 100
MAX_TAG_CHARACTERS = 100
MAX_TIME_EVIDENCE_CHARACTERS = 200
MAX_CANDIDATE_SNAPSHOTS = 100
MAX_REQUESTED_COUNT = 20
MAX_DECLARED_MEDIA_SLOTS = 22
MAX_MEDIA_TRANSFERS_PER_JOB = MAX_REQUESTED_COUNT * MAX_DECLARED_MEDIA_SLOTS
MAX_MEDIA_CHUNK_BYTES = 256 * 1024
MAX_MEDIA_CHUNK_BASE64_CHARACTERS = ((MAX_MEDIA_CHUNK_BYTES + 2) // 3) * 4
MAX_HOST_VERSION_CHARACTERS = 64
MAX_SAFE_JAVASCRIPT_INTEGER = 9_007_199_254_740_991

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_OFFSET_ISO_8601 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?[+-][0-9]{2}:[0-9]{2}$"
)
_SEMVER = re.compile(r"^\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_DECIMAL_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")
_URL_LIKE_RETAINED_TEXT = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|//)")
_SRT_TIMESTAMP = re.compile(
    r"^([0-9]{2}):([0-5][0-9]):([0-5][0-9]),([0-9]{3}) --> "
    r"([0-9]{2}):([0-5][0-9]):([0-5][0-9]),([0-9]{3})$"
)

MetricPrecision = Literal["exact", "display_rounded", "not_exposed"]
TimeEvidenceKind = Literal["published", "edited", "unknown"]
MetricEvidenceSource = Literal[
    "detail_visible_count",
    "search_card_interface",
    "account_card_interface",
]
CollectionSurface = Literal["extension_current", "extension_search", "extension_account"]
ProgressPhase = Literal["started", "scanning", "ranking", "downloading", "saving", "stopped"]
PublicErrorCode = Literal[
    "invalid_request",
    "unsupported_protocol",
    "message_limit_exceeded",
    "invalid_frame",
    "invalid_state",
    "identity_mismatch",
    "media_rejected",
    "internal_error",
    "job_in_progress",
]


def _exact_integer(value: object, field_name: str) -> object:
    if type(value) is not int:
        raise ValueError(f"{field_name} must be an exact integer")
    return value


def _safe_unicode_text(value: str, *, maximum: int, field_name: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field_name} must be Unicode normalized")
    if (
        len(value) > maximum
        or _URL_LIKE_RETAINED_TEXT.search(value) is not None
        or not is_safe_retained_text(value)
    ):
        raise ValueError(f"{field_name} is outside the retained-text boundary")
    return value


def _safe_id(value: str, field_name: str) -> str:
    if len(value) > MAX_PROTOCOL_ID_LENGTH or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded opaque identifier")
    return value


def _canonical_xhs_url(value: str, expected_type: Literal["note", "profile"]) -> str:
    if len(value) > MAX_CANONICAL_URL_LENGTH:
        raise ValueError("canonical URL is too long")
    parsed_input = urlsplit(value)
    if parsed_input.query or parsed_input.fragment:
        raise ValueError("canonical URL must not contain a query or fragment")
    parsed = parse_xhs_url(value)
    if parsed.object_type != expected_type or parsed.canonical_url != value:
        raise ValueError("URL is not the declared canonical Xiaohongshu identity")
    return value


def _validate_slot_role_position(role: MediaRole, position: int) -> None:
    if role != "image" and position != 1:
        raise ValueError("video roles use position one")


class StrictWireModel(BaseModel):
    """A no-coercion, no-extra-field model for every native wire object."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ProtocolMessage(StrictWireModel):
    protocol_version: Literal["1.0"]


class JobMessage(ProtocolMessage):
    job_id: str

    @field_validator("job_id")
    @classmethod
    def validate_job_id(cls, value: str) -> str:
        return _safe_id(value, "job_id")


class ExtensionHealth(ProtocolMessage):
    kind: Literal["health"]


class ExtensionBeginJob(JobMessage):
    kind: Literal["begin_job"]
    collection_surface: CollectionSurface
    source_page_url: str
    requested_count: int = Field(ge=1, le=MAX_REQUESTED_COUNT)
    candidate_scan_limit: int = Field(ge=1, le=MAX_CANDIDATE_SNAPSHOTS)
    publication_cutoff: str | None = None
    selection_order: Literal["exact_likes_desc", "page_order"] = "exact_likes_desc"

    @field_validator("requested_count", "candidate_scan_limit", mode="before")
    @classmethod
    def validate_integer_fields(cls, value: object, info: object) -> object:
        field_name = getattr(info, "field_name", "count")
        return _exact_integer(value, field_name)

    @field_validator("source_page_url")
    @classmethod
    def validate_source_page_url(cls, value: str) -> str:
        if len(value) > MAX_CANONICAL_URL_LENGTH or not is_safe_retained_text(value):
            raise ValueError("source_page_url is unsafe")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.xiaohongshu.com", "xiaohongshu.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("source_page_url must be query-free Xiaohongshu HTTPS")
        return value

    @field_validator("publication_cutoff")
    @classmethod
    def validate_publication_cutoff(cls, value: str | None) -> str | None:
        if value is not None and not _OFFSET_ISO_8601.fullmatch(value):
            raise ValueError("publication_cutoff must be an offset-bearing ISO-8601 string")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.requested_count > self.candidate_scan_limit:
            raise ValueError("requested_count cannot exceed candidate_scan_limit")
        if self.collection_surface == "extension_current" and self.requested_count != 1:
            raise ValueError("extension_current must request exactly one note")
        if self.selection_order == "page_order" and (
            self.collection_surface != "extension_search"
            or self.requested_count not in {5, 10}
            or self.publication_cutoff is not None
            or not is_canonical_xhs_search_route(self.source_page_url)
        ):
            raise ValueError("page-order search request is invalid")
        return self


class ExtensionMetricValue(StrictWireModel):
    raw_value: str | None = None
    normalized_value: int | None = Field(default=None, ge=0, le=MAX_SAFE_JAVASCRIPT_INTEGER)
    precision: MetricPrecision

    @field_validator("normalized_value", mode="before")
    @classmethod
    def validate_normalized_value(cls, value: object) -> object:
        if value is not None:
            return _exact_integer(value, "normalized_value")
        return value

    @field_validator("raw_value")
    @classmethod
    def validate_raw_value(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_unicode_text(value, maximum=100, field_name="raw_value")
        return value

    @model_validator(mode="after")
    def validate_precision(self) -> Self:
        if self.precision == "not_exposed":
            if self.raw_value is not None or self.normalized_value is not None:
                raise ValueError("not_exposed metrics cannot retain values")
        elif self.precision == "exact" and self.normalized_value is None:
            raise ValueError("exact metrics require normalized_value")
        elif self.precision == "exact" and self.raw_value is not None and (
            not _CANONICAL_DECIMAL_INTEGER.fullmatch(self.raw_value)
            or int(self.raw_value) != self.normalized_value
        ):
            raise ValueError("exact metric raw_value must canonically equal normalized_value")
        elif self.precision == "display_rounded" and (
            self.raw_value is None or self.normalized_value is None
        ):
            raise ValueError("display_rounded metrics require both values")
        return self


class ExtensionNoteMetrics(StrictWireModel):
    likes: ExtensionMetricValue | None = None
    collects: ExtensionMetricValue | None = None
    comments: ExtensionMetricValue | None = None
    shares: ExtensionMetricValue | None = None


class ExtensionTimeEvidence(StrictWireModel):
    kind: TimeEvidenceKind
    raw_text: str

    @field_validator("raw_text")
    @classmethod
    def validate_raw_text(cls, value: str) -> str:
        return _safe_unicode_text(
            value, maximum=MAX_TIME_EVIDENCE_CHARACTERS, field_name="time_evidence.raw_text"
        )


class BoundMediaSlotDeclaration(StrictWireModel):
    note_id: str
    role: MediaRole
    position: int = Field(ge=1, le=20)

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        return _safe_id(value, "note_id")

    @field_validator("position", mode="before")
    @classmethod
    def validate_position(cls, value: object) -> object:
        return _exact_integer(value, "position")

    @model_validator(mode="after")
    def validate_role_position(self) -> Self:
        _validate_slot_role_position(self.role, self.position)
        return self


class ExtensionCandidateSnapshot(JobMessage):
    kind: Literal["candidate_snapshot"]
    source_position: int = Field(ge=1, le=MAX_CANDIDATE_SNAPSHOTS)
    note_id: str
    canonical_url: str
    title: str | None = None
    body: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    note_type: str | None = None
    published_at: str | None = None
    time_evidence: ExtensionTimeEvidence | None = None
    author_id: str | None = None
    author_name: str | None = None
    author_profile_url: str | None = None
    metrics: ExtensionNoteMetrics
    metric_provenance: dict[
        Literal["likes", "collects", "comments", "shares"], MetricEvidenceSource
    ] = Field(default_factory=dict)
    media_slots: list[BoundMediaSlotDeclaration] = Field(
        default_factory=list, max_length=MAX_DECLARED_MEDIA_SLOTS
    )

    @field_validator("source_position", mode="before")
    @classmethod
    def validate_source_position(cls, value: object) -> object:
        return _exact_integer(value, "source_position")

    @field_validator("note_id", "author_id")
    @classmethod
    def validate_ids(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return value
        return _safe_id(value, getattr(info, "field_name", "id"))

    @field_validator("canonical_url")
    @classmethod
    def validate_canonical_url(cls, value: str) -> str:
        return _canonical_xhs_url(value, "note")

    @field_validator("author_profile_url")
    @classmethod
    def validate_author_profile_url(cls, value: str | None) -> str | None:
        if value is not None:
            return _canonical_xhs_url(value, "profile")
        return value

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_unicode_text(value, maximum=MAX_TITLE_CHARACTERS, field_name="title")
        return value

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_unicode_text(value, maximum=MAX_BODY_CHARACTERS, field_name="body")
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        return [_safe_unicode_text(value, maximum=MAX_TAG_CHARACTERS, field_name="tag") for value in values]

    @field_validator("note_type")
    @classmethod
    def validate_note_type(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_unicode_text(value, maximum=100, field_name="note_type")
        return value

    @field_validator("published_at")
    @classmethod
    def validate_published_at(cls, value: str | None) -> str | None:
        if value is not None and not _OFFSET_ISO_8601.fullmatch(value):
            raise ValueError("published_at must be an offset-bearing ISO-8601 string")
        return value

    @field_validator("author_name")
    @classmethod
    def validate_author_name(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_unicode_text(value, maximum=MAX_AUTHOR_NAME_CHARACTERS, field_name="author_name")
        return value

    @model_validator(mode="after")
    def validate_identity_and_slots(self) -> Self:
        canonical = parse_xhs_url(self.canonical_url)
        if canonical.object_id != self.note_id:
            raise ValueError("note_id must match canonical_url")
        if self.author_profile_url is not None and self.author_id is not None:
            profile = parse_xhs_url(self.author_profile_url)
            if profile.object_id != self.author_id:
                raise ValueError("author_id must match author_profile_url")
        for metric_name in self.metric_provenance:
            metric = getattr(self.metrics, metric_name)
            if metric is None or metric.precision == "not_exposed":
                raise ValueError("metric provenance requires an exposed metric")
        slot_keys = {(slot.role, slot.position) for slot in self.media_slots}
        if len(slot_keys) != len(self.media_slots):
            raise ValueError("media slot declarations must be unique")
        if any(slot.note_id != self.note_id for slot in self.media_slots):
            raise ValueError("media slot note_id must match candidate note_id")
        image_slots = [slot for slot in self.media_slots if slot.role == "image"]
        video_slots = [slot for slot in self.media_slots if slot.role != "image"]
        if image_slots and video_slots:
            raise ValueError("image and video slots cannot be mixed")
        image_positions = sorted(slot.position for slot in image_slots)
        if image_positions != list(range(1, len(image_positions) + 1)):
            raise ValueError("image slot positions must be contiguous")
        return self


class ExtensionCandidateUnavailable(JobMessage):
    """A bounded scan exclusion or legacy detail failure for one candidate."""

    kind: Literal["candidate_unavailable"]
    source_position: int = Field(ge=1, le=MAX_CANDIDATE_SNAPSHOTS)
    note_id: str
    reason: Literal["detail_unavailable", "sponsored", "invalid_card"]

    @field_validator("source_position", mode="before")
    @classmethod
    def validate_source_position(cls, value: object) -> object:
        return _exact_integer(value, "source_position")

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        return _safe_id(value, "note_id")


class ExtensionFinishScan(JobMessage):
    kind: Literal["finish_scan"]
    scroll_rounds: int | None = Field(default=None, ge=0, le=2)
    sort_label: str | None = None

    @field_validator("scroll_rounds", mode="before")
    @classmethod
    def validate_scroll_rounds(cls, value: object) -> object:
        if value is None:
            return value
        return _exact_integer(value, "scroll_rounds")

    @field_validator("sort_label")
    @classmethod
    def validate_sort_label(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _safe_unicode_text(value, maximum=100, field_name="sort_label")


class ExtensionMediaBegin(JobMessage):
    kind: Literal["media_begin"]
    note_id: str
    role: MediaRole
    position: int = Field(ge=1, le=20)
    sequence: int = Field(ge=1, le=MAX_MEDIA_TRANSFERS_PER_JOB)
    size_limit_bytes: int = Field(ge=1, le=100 * 1024 * 1024)

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        return _safe_id(value, "note_id")

    @field_validator("position", "sequence", "size_limit_bytes", mode="before")
    @classmethod
    def validate_integers(cls, value: object, info: object) -> object:
        return _exact_integer(value, getattr(info, "field_name", "integer"))

    @model_validator(mode="after")
    def validate_role_position(self) -> Self:
        _validate_slot_role_position(self.role, self.position)
        return self


class ExtensionMediaChunk(JobMessage):
    kind: Literal["media_chunk"]
    note_id: str
    role: MediaRole
    position: int = Field(ge=1, le=20)
    sequence: int = Field(ge=1, le=MAX_MEDIA_TRANSFERS_PER_JOB)
    chunk_index: int = Field(ge=0, le=MAX_SAFE_JAVASCRIPT_INTEGER)
    data_base64: str = Field(min_length=4, max_length=MAX_MEDIA_CHUNK_BASE64_CHARACTERS)

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        return _safe_id(value, "note_id")

    @field_validator("position", "sequence", "chunk_index", mode="before")
    @classmethod
    def validate_integers(cls, value: object, info: object) -> object:
        return _exact_integer(value, getattr(info, "field_name", "integer"))

    @field_validator("data_base64")
    @classmethod
    def validate_base64_chunk(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("data_base64 is not valid base64") from error
        if not decoded or len(decoded) > MAX_MEDIA_CHUNK_BYTES:
            raise ValueError("data_base64 exceeds the chunk boundary")
        return value

    @model_validator(mode="after")
    def validate_role_position(self) -> Self:
        _validate_slot_role_position(self.role, self.position)
        return self


class ExtensionMediaEnd(JobMessage):
    kind: Literal["media_end"]
    note_id: str
    role: MediaRole
    position: int = Field(ge=1, le=20)
    sequence: int = Field(ge=1, le=MAX_MEDIA_TRANSFERS_PER_JOB)
    mime_type: MediaMime
    sha256: str

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        return _safe_id(value, "note_id")

    @field_validator("position", "sequence", mode="before")
    @classmethod
    def validate_integers(cls, value: object, info: object) -> object:
        return _exact_integer(value, getattr(info, "field_name", "integer"))

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_role_position(self) -> Self:
        _validate_slot_role_position(self.role, self.position)
        return self


class ExtensionMediaMissing(JobMessage):
    kind: Literal["media_missing"]
    note_id: str
    role: MediaRole
    position: int = Field(ge=1, le=20)
    reason: MediaMissingReason

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        return _safe_id(value, "note_id")

    @field_validator("position", mode="before")
    @classmethod
    def validate_position(cls, value: object) -> object:
        return _exact_integer(value, "position")

    @model_validator(mode="after")
    def validate_role_position(self) -> Self:
        _validate_slot_role_position(self.role, self.position)
        return self


class VideoMetadata(StrictWireModel):
    note_id: str
    duration_ms: int | None = Field(default=None, ge=1, le=86_400_000)
    subtitle_srt: str | None = None
    subtitle_status: Literal["available", "not_exposed", "failed"] | None = None

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        return _safe_id(value, "note_id")

    @field_validator("duration_ms", mode="before")
    @classmethod
    def validate_duration(cls, value: object) -> object:
        return value if value is None else _exact_integer(value, "duration_ms")

    @field_validator("subtitle_srt")
    @classmethod
    def validate_subtitle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\x00" in value or len(value.encode("utf-8")) > 512 * 1024 or not is_safe_retained_text(value):
            raise ValueError("subtitle is outside the retained-text boundary")
        blocks = re.split(r"\r?\n\r?\n", value.strip())
        if not blocks or len(blocks) > 10_000:
            raise ValueError("invalid SRT")
        for index, block in enumerate(blocks, start=1):
            lines = block.splitlines()
            if len(lines) < 3 or lines[0] != str(index) or not _SRT_TIMESTAMP.fullmatch(lines[1]):
                raise ValueError("invalid SRT cue")
            if not any(line.strip() for line in lines[2:]):
                raise ValueError("empty SRT cue")
            match = _SRT_TIMESTAMP.fullmatch(lines[1])
            assert match is not None
            start = tuple(int(part) for part in match.groups()[:4])
            end = tuple(int(part) for part in match.groups()[4:])
            if start >= end:
                raise ValueError("invalid SRT cue duration")
        return value

    @model_validator(mode="after")
    def validate_subtitle_status(self) -> Self:
        if (self.subtitle_status == "available") != (self.subtitle_srt is not None):
            raise ValueError("available subtitles require SRT content")
        return self


class ExtensionFinishJob(JobMessage):
    kind: Literal["finish_job"]
    video_metadata: VideoMetadata | None = None


class ExtensionVideoStatus(JobMessage):
    kind: Literal["video_status"]


class ExtensionVideoStop(JobMessage):
    kind: Literal["video_stop"]


class ExtensionStopJob(JobMessage):
    kind: Literal["stop_job"]
    terminal_cause: Literal[
        "stopped", "login_required", "challenge_detected",
        "structural_error", "route_mismatch", "identity_mismatch",
    ] | None = None


class ExtensionOpenReport(JobMessage):
    kind: Literal["open_report"]


NativeRequest: TypeAlias = Annotated[
    ExtensionHealth
    | ExtensionBeginJob
    | ExtensionCandidateSnapshot
    | ExtensionCandidateUnavailable
    | ExtensionFinishScan
    | ExtensionMediaBegin
    | ExtensionMediaChunk
    | ExtensionMediaEnd
    | ExtensionMediaMissing
    | ExtensionFinishJob
    | ExtensionStopJob
    | ExtensionOpenReport
    | ExtensionVideoStatus
    | ExtensionVideoStop,
    Field(discriminator="kind"),
]
NATIVE_REQUEST_ADAPTER: TypeAdapter[NativeRequest] = TypeAdapter(NativeRequest)


class HealthResult(ProtocolMessage):
    kind: Literal["health_result"]
    status: Literal["ready"]
    host_version: str

    @field_validator("host_version")
    @classmethod
    def validate_host_version(cls, value: str) -> str:
        if len(value) > MAX_HOST_VERSION_CHARACTERS or not _SEMVER.fullmatch(value):
            raise ValueError("host_version must be a bounded semantic version")
        return value


class JobStarted(JobMessage):
    kind: Literal["job_started"]
    status: Literal["started"]


class CandidateResult(JobMessage):
    kind: Literal["candidate_result"]
    note_id: str
    source_position: int = Field(ge=1, le=MAX_CANDIDATE_SNAPSHOTS)
    outcome: Literal["recorded", "unavailable"]

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        return _safe_id(value, "note_id")

    @field_validator("source_position", mode="before")
    @classmethod
    def validate_source_position(cls, value: object) -> object:
        return _exact_integer(value, "source_position")


class SelectedNote(StrictWireModel):
    note_id: str
    selection_rank: int = Field(ge=1, le=MAX_REQUESTED_COUNT)

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        return _safe_id(value, "note_id")

    @field_validator("selection_rank", mode="before")
    @classmethod
    def validate_selection_rank(cls, value: object) -> object:
        return _exact_integer(value, "selection_rank")


class SelectionResult(JobMessage):
    kind: Literal["selection_result"]
    scanned_count: int = Field(ge=0, le=MAX_CANDIDATE_SNAPSHOTS)
    eligible_count: int = Field(ge=0, le=MAX_CANDIDATE_SNAPSHOTS)
    selected_count: int = Field(ge=0, le=MAX_REQUESTED_COUNT)
    status: Literal["complete", "partial"]
    selected: list[SelectedNote] = Field(default_factory=list, max_length=MAX_REQUESTED_COUNT)

    @field_validator("scanned_count", "eligible_count", "selected_count", mode="before")
    @classmethod
    def validate_counts(cls, value: object, info: object) -> object:
        return _exact_integer(value, getattr(info, "field_name", "count"))

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if not self.selected_count == len(self.selected):
            raise ValueError("selected_count must equal selected item count")
        if not self.selected_count <= self.eligible_count <= self.scanned_count:
            raise ValueError("selection counts are inconsistent")
        ids = [item.note_id for item in self.selected]
        ranks = sorted(item.selection_rank for item in self.selected)
        if len(set(ids)) != len(ids) or ranks != list(range(1, self.selected_count + 1)):
            raise ValueError("selected items must have unique IDs and contiguous ranks")
        return self


class Progress(JobMessage):
    kind: Literal["progress"]
    phase: ProgressPhase
    discovered: int = Field(ge=0, le=MAX_CANDIDATE_SNAPSHOTS)
    inspected: int = Field(ge=0, le=MAX_CANDIDATE_SNAPSHOTS)
    eligible: int = Field(ge=0, le=MAX_CANDIDATE_SNAPSHOTS)
    selected: int = Field(ge=0, le=MAX_CANDIDATE_SNAPSHOTS)
    saved: int = Field(ge=0, le=MAX_MEDIA_TRANSFERS_PER_JOB)
    current_source_position: int | None = Field(default=None, ge=1, le=MAX_CANDIDATE_SNAPSHOTS)

    @field_validator(
        "discovered", "inspected", "eligible", "selected", "saved", "current_source_position", mode="before"
    )
    @classmethod
    def validate_counts(cls, value: object, info: object) -> object:
        if value is None:
            return value
        return _exact_integer(value, getattr(info, "field_name", "count"))

    @model_validator(mode="after")
    def validate_progress_counts(self) -> Self:
        if not self.selected <= self.eligible <= self.inspected <= self.discovered:
            raise ValueError("progress counts are inconsistent")
        return self


class MediaResult(JobMessage):
    kind: Literal["media_result"]
    note_id: str
    role: MediaRole
    position: int = Field(ge=1, le=20)
    outcome: Literal["downloaded", "missing", "rejected"]
    reason: MediaMissingReason | None = None

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        return _safe_id(value, "note_id")

    @field_validator("position", mode="before")
    @classmethod
    def validate_position(cls, value: object) -> object:
        return _exact_integer(value, "position")

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        _validate_slot_role_position(self.role, self.position)
        if (self.outcome == "downloaded") != (self.reason is None):
            raise ValueError("media reason is required only for unavailable outcomes")
        return self


VideoStatus = Literal[
    "none", "preparing_model", "running", "complete", "skipped_too_long",
    "skipped_no_audio", "no_speech", "failed", "not_started",
]
VideoReason = Literal[
    "video_not_saved", "duration_unknown", "audio_unreadable", "dependencies_unavailable",
    "model_preparation_failed", "processing_timeout", "task_interrupted", "stopped",
    "worker_start_failed", "transcript_too_large", "report_update_failed", "job_in_progress",
]


class VideoProcessingSummary(StrictWireModel):
    status: VideoStatus
    reason: VideoReason | None = None
    report_update_failed: bool | None = None


class JobResult(JobMessage):
    kind: Literal["job_result"]
    status: Literal["complete", "partial", "stopped", "failed"]
    retained_count: int = Field(ge=0, le=MAX_REQUESTED_COUNT)
    report_available: bool
    report_file: Literal["index.html"] | None = None
    video_processing: VideoProcessingSummary | None = None

    @field_validator("retained_count", mode="before")
    @classmethod
    def validate_retained_count(cls, value: object) -> object:
        return _exact_integer(value, "retained_count")

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.report_available != (self.report_file == "index.html"):
            raise ValueError("report_file is present only for an available report")
        return self


class ReportResult(JobMessage):
    kind: Literal["report_result"]
    opened: Literal[True]
    terminal_status: Literal["complete", "partial", "stopped", "failed"] | None = None


class VideoResult(JobMessage):
    kind: Literal["video_result"]
    processing: VideoProcessingSummary


class ErrorResponse(ProtocolMessage):
    kind: Literal["error"]
    job_id: str | None = None
    code: PublicErrorCode
    fatal: bool

    @field_validator("job_id")
    @classmethod
    def validate_job_id(cls, value: str | None) -> str | None:
        if value is not None:
            return _safe_id(value, "job_id")
        return value


NativeResponse: TypeAlias = Annotated[
    HealthResult
    | JobStarted
    | CandidateResult
    | SelectionResult
    | Progress
    | MediaResult
    | JobResult
    | ReportResult
    | VideoResult
    | ErrorResponse,
    Field(discriminator="kind"),
]
NATIVE_RESPONSE_ADAPTER: TypeAdapter[NativeResponse] = TypeAdapter(NativeResponse)

REQUEST_TO_ALLOWED_RESPONSE_KINDS: dict[str, frozenset[str]] = {
    "health": frozenset({"health_result", "error"}),
    "begin_job": frozenset({"job_started", "error"}),
    "candidate_snapshot": frozenset({"candidate_result", "error"}),
    "candidate_unavailable": frozenset({"candidate_result", "error"}),
    "finish_scan": frozenset({"selection_result", "error"}),
    "media_begin": frozenset({"progress", "media_result", "error"}),
    "media_chunk": frozenset({"progress", "error"}),
    "media_end": frozenset({"media_result", "error"}),
    "media_missing": frozenset({"media_result", "error"}),
    "finish_job": frozenset({"job_result", "error"}),
    "stop_job": frozenset({"job_result", "error"}),
    "open_report": frozenset({"report_result", "error"}),
    "video_status": frozenset({"video_result", "error"}),
    "video_stop": frozenset({"video_result", "error"}),
}


def validate_response_for_request(request: NativeRequest, response: NativeResponse) -> NativeResponse:
    """Bind a host response to one extension request before it crosses the wire."""
    if response.kind not in REQUEST_TO_ALLOWED_RESPONSE_KINDS[request.kind]:
        raise ValueError("response kind is not allowed for request kind")
    request_job_id = getattr(request, "job_id", None)
    response_job_id = getattr(response, "job_id", None)
    if request_job_id is None:
        if response_job_id is not None:
            raise ValueError("health responses cannot carry job_id")
    elif response_job_id != request_job_id:
        raise ValueError("job-scoped response must carry the matching job_id")
    if (
        isinstance(request, (ExtensionCandidateSnapshot, ExtensionCandidateUnavailable))
        and isinstance(response, CandidateResult)
        and (
            response.note_id != request.note_id
            or response.source_position != request.source_position
            or (
                isinstance(request, ExtensionCandidateSnapshot)
                and response.outcome != "recorded"
            )
            or (
                isinstance(request, ExtensionCandidateUnavailable)
                and response.outcome != "unavailable"
            )
        )
    ):
        raise ValueError("candidate response must echo identity and match request outcome")
    if (
        isinstance(request, (ExtensionMediaBegin, ExtensionMediaEnd, ExtensionMediaMissing))
        and isinstance(response, MediaResult)
        and (
            response.note_id != request.note_id
            or response.role != request.role
            or response.position != request.position
        )
    ):
        raise ValueError("media response must echo note_id, role, and position")
    return response
