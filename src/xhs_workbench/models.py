"""Safe, visible-result contracts for Xiaohongshu collection runs."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import Enum
from pathlib import PureWindowsPath
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xhs_workbench.media import (
    IMAGE_MIME_TYPES,
    MAX_DISCOVERED_IMAGE_SLOTS,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_NOTE,
    MAX_NOTE_MEDIA_BYTES,
    MAX_VIDEO_BYTES,
    MAX_VIDEOS_PER_NOTE,
    VIDEO_MIME_TYPES,
    MediaMime,
    MediaMissingReason,
    MediaRole,
    MediaStatus,
    mime_extension,
)
from xhs_workbench.security import is_safe_evidence_key, parse_xhs_url

_EXACT_RAW_VALUE = re.compile(r"^(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?<![a-z0-9])(?:access[\s_-]*token|refresh[\s_-]*token|authorization|cookies?|"
    r"sign(?:ature)?|token|web[\s_-]*session|xsec(?:[\s_-]*token)?|a1)(?![a-z0-9])\s*[:=]|"
    r"\bauthorization\s+bearer\s+\S+"
)
_QUERY_BEARING_HTTP_URL = re.compile(r"(?i)https?://[^\s<>\"']*[?#]")
_URL_LIKE = re.compile(r"(?i)(?:https?:)?//")

TimeEvidenceKind = Literal["published", "edited", "unknown"]
MetricEvidenceSource = Literal[
    "detail_visible_count",
    "search_card_interface",
    "account_card_interface",
]
MetricProvenanceKey = Literal["likes", "collects", "comments", "shares"]
ExtensionCollectionSurface = Literal[
    "extension_current", "extension_search", "extension_account"
]
ExtensionSelectionBasis = Literal["exact_likes_desc", "page_order"]
ExtensionSelectionOutcome = Literal["selected", "excluded", "unavailable", "rejected"]
ExtensionSelectionInclusion = Literal["included", "excluded"]
ExtensionDetailOutcome = Literal[
    "enriched", "detail_unavailable", "login_required", "challenge_detected", "stopped"
]
ExtensionSearchRunStatus = Literal["complete", "partial", "failed", "stopped"]
ExtensionSearchRunErrorCode = Literal[
    "detail_unavailable", "login_required", "challenge_detected", "stopped"
    , "structural_error", "route_mismatch", "identity_mismatch"
]
ExtensionSelectionReason = Literal[
    "candidate_invalid",
    "detail_unavailable",
    "route_mismatch",
    "identity_mismatch",
    "publication_time_unavailable",
    "likes_unavailable",
    "likes_not_exact",
    "selection_limit_reached",
    "message_limit_exceeded",
    "login_required",
    "challenge_detected",
    "sponsored",
    "invalid_card",
    "integrity_failure",
    "stopped",
]


def is_safe_retained_text(value: str) -> bool:
    """Reject secret assignments and signed URLs before retaining visible public text."""
    return _SENSITIVE_VALUE.search(value) is None and _QUERY_BEARING_HTTP_URL.search(value) is None


def is_canonical_xhs_search_route(value: str) -> bool:
    """Accept only the fixed, query-free Manual Search route."""
    parsed = urlsplit(value)
    return (
        is_safe_retained_text(value)
        and value == "https://www.xiaohongshu.com/search_result"
        and parsed.scheme == "https"
        and parsed.hostname == "www.xiaohongshu.com"
        and parsed.path == "/search_result"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and not parsed.query
        and not parsed.fragment
    )


def _is_safe_time_evidence_text(value: str) -> bool:
    """Require nonempty visible time text without any URL-like content."""
    return (
        any(unicodedata.category(character)[0] not in {"C", "M", "Z"} for character in value)
        and _URL_LIKE.search(value) is None
        and is_safe_retained_text(value)
    )


class VisibleResultModel(BaseModel):
    """Base contract that rejects fields outside the retained public evidence."""

    model_config = ConfigDict(extra="forbid")


class MetricValue(VisibleResultModel):
    """A platform-displayed metric with its retained precision stated explicitly."""

    raw_value: str | None = None
    normalized_value: int | None = None
    precision: Literal["exact", "display_rounded", "not_exposed"]

    @model_validator(mode="after")
    def validate_precision(self) -> Self:
        """Require the retained values to match their stated precision."""
        if self.precision == "not_exposed":
            if self.raw_value is not None or self.normalized_value is not None:
                raise ValueError("not_exposed metrics cannot retain raw or normalized values")
        elif self.precision == "exact":
            if self.normalized_value is None:
                raise ValueError("exact metrics require normalized_value")
            if self.raw_value is not None:
                if not _EXACT_RAW_VALUE.fullmatch(self.raw_value):
                    raise ValueError("exact metric raw_value must be a decimal integer display")
                if int(self.raw_value.replace(",", "")) != self.normalized_value:
                    raise ValueError("exact metric raw_value must match normalized_value")
        elif self.raw_value is None or self.normalized_value is None:
            raise ValueError("display_rounded metrics require raw_value and normalized_value")
        return self


class NoteMetrics(VisibleResultModel):
    """The only note interaction metrics that may be retained."""

    likes: MetricValue | None = None
    collects: MetricValue | None = None
    comments: MetricValue | None = None
    shares: MetricValue | None = None


class TimeEvidence(VisibleResultModel):
    """Visible page text and its finite, non-inferred time meaning."""

    kind: TimeEvidenceKind
    raw_text: str

    @field_validator("raw_text")
    @classmethod
    def validate_raw_text(cls, value: str) -> str:
        if not _is_safe_time_evidence_text(value):
            raise ValueError("time evidence raw_text must be safe retained public text")
        return value


def validate_metric_provenance(
    metrics: NoteMetrics,
    metric_provenance: dict[MetricProvenanceKey, MetricEvidenceSource],
) -> None:
    """Require provenance only for metrics that the source exposed."""
    for metric_name in metric_provenance:
        metric = getattr(metrics, metric_name)
        if metric is None or metric.precision == "not_exposed":
            raise ValueError("provenance requires an exposed metric")


class RunStatus(str, Enum):
    """The explicitly reported outcome of a collection run."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    STOPPED = "stopped"


class LocalAsset(VisibleResultModel):
    """Integrity metadata for a verified local media asset retained in an output bundle."""

    local_path: str
    mime_type: MediaMime
    size_bytes: int = Field(ge=1, le=MAX_VIDEO_BYTES)
    sha256: str

    @field_validator("local_path")
    @classmethod
    def validate_local_path(cls, value: str) -> str:
        """Retain only a safe relative artifact path."""
        result = _validate_local_relative_path(value)
        assert result is not None
        return result

    @field_validator("size_bytes", mode="before")
    @classmethod
    def validate_exact_size(cls, value: object) -> object:
        """Avoid coercing booleans, strings, or floats into a byte count."""
        if type(value) is not int:
            raise ValueError("size_bytes must be an exact integer")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        """Require a lowercase SHA-256 digest."""
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_extension_matches_mime(self) -> Self:
        """Keep a bundle asset's filename extension consistent with its verified MIME."""
        extension = mime_extension(self.mime_type)
        assert extension is not None
        if not self.local_path.endswith(extension):
            raise ValueError("local asset extension must match MIME type")
        return self


class NoteMediaSlot(VisibleResultModel):
    """One ordered, explicitly available-or-unavailable V2 media slot."""

    note_id: str
    role: MediaRole
    position: int = Field(ge=1)
    status: MediaStatus
    asset: LocalAsset | None = None
    duration_ms: int | None = Field(default=None, ge=1)
    missing_reason: MediaMissingReason | None = None

    @model_validator(mode="after")
    def validate_status_and_role(self) -> Self:
        """Keep each persisted slot attributable and internally consistent."""
        if self.status == "downloaded" and (
            self.asset is None or self.missing_reason is not None
        ):
            raise ValueError("downloaded media requires only an asset")
        if self.status != "downloaded" and (
            self.asset is not None or self.missing_reason is None
        ):
            raise ValueError("unavailable media requires only a finite reason")
        if self.role != "video" and self.duration_ms is not None:
            raise ValueError("duration is video-only")
        if self.asset is not None:
            expected = VIDEO_MIME_TYPES if self.role == "video" else IMAGE_MIME_TYPES
            if self.asset.mime_type not in expected:
                raise ValueError("media role and MIME disagree")
            maximum_bytes = MAX_VIDEO_BYTES if self.role == "video" else MAX_IMAGE_BYTES
            if self.asset.size_bytes > maximum_bytes:
                raise ValueError("media role exceeds its byte limit")
        return self


class NoteRecord(VisibleResultModel):
    """Publicly visible data retained for one Xiaohongshu note."""

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
    selection_rank: int | None = Field(default=None, ge=1, le=20)
    selection_basis: ExtensionSelectionBasis | None = None
    cover_local_path: str | None = None
    cover_asset: LocalAsset | None = None
    media_manifest_version: Literal[2] | None = None
    media_slots: list[NoteMediaSlot] = Field(default_factory=list)
    media_discovered_count: int | None = Field(default=None, ge=0)
    media_discovery_truncated: bool = False
    missing_fields: list[str] = Field(default_factory=list)

    @field_validator("canonical_url")
    @classmethod
    def validate_note_canonical_url(cls, value: str) -> str:
        """Require a safe, query-free canonical URL for the note itself."""
        parsed = parse_xhs_url(value)
        if parsed.object_type != "note" or parsed.canonical_url != value:
            raise ValueError("canonical_url must be a query-free Xiaohongshu note URL")
        return value

    @field_validator("author_profile_url")
    @classmethod
    def validate_author_profile_url(cls, value: str | None) -> str | None:
        """Retain an author profile URL only in its safe canonical form."""
        if value is None:
            return None
        parsed = parse_xhs_url(value)
        if parsed.object_type != "profile" or parsed.canonical_url != value:
            raise ValueError("author_profile_url must be a query-free Xiaohongshu profile URL")
        return value

    @field_validator("cover_local_path")
    @classmethod
    def validate_cover_local_path(cls, value: str | None) -> str | None:
        """Require a local cover path that remains beneath the artifact root."""
        return _validate_local_relative_path(value)

    @field_validator("selection_rank", mode="before")
    @classmethod
    def validate_exact_selection_rank(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("selection_rank must be an exact integer")
        return value

    @model_validator(mode="after")
    def validate_cover_asset_pair(self) -> Self:
        """Keep legacy and metadata paths as one consistent local-asset pair."""
        if self.cover_asset is not None and (
            self.cover_local_path is None or self.cover_asset.local_path != self.cover_local_path
        ):
            raise ValueError("cover asset path must match cover_local_path")
        if self.cover_asset is not None and (
            self.cover_asset.mime_type not in IMAGE_MIME_TYPES
            or self.cover_asset.size_bytes > MAX_IMAGE_BYTES
        ):
            raise ValueError("cover asset must be an approved image")
        self._validate_media_manifest()
        return self

    @model_validator(mode="after")
    def validate_metric_provenance(self) -> Self:
        validate_metric_provenance(self.metrics, self.metric_provenance)
        return self

    def _validate_media_manifest(self) -> None:
        if self.media_manifest_version is None:
            if (
                self.media_slots
                or self.media_discovered_count is not None
                or self.media_discovery_truncated
            ):
                raise ValueError("versionless records cannot retain a media manifest")
            return
        if self.media_discovered_count is None:
            raise ValueError("V2 records require a media discovered count")
        slots_by_role: dict[MediaRole, list[NoteMediaSlot]] = {
            "image": [],
            "video_cover": [],
            "video": [],
        }
        seen_slots: set[tuple[MediaRole, int]] = set()
        downloaded_bytes = 0
        downloaded_images = 0
        for slot in self.media_slots:
            if slot.note_id != self.note_id:
                raise ValueError("media slot note_id must match note")
            slot_key = (slot.role, slot.position)
            if slot_key in seen_slots:
                raise ValueError("media slots must be unique by role and position")
            seen_slots.add(slot_key)
            slots_by_role[slot.role].append(slot)
            if slot.status == "downloaded":
                assert slot.asset is not None
                downloaded_bytes += slot.asset.size_bytes
                if slot.role == "image":
                    downloaded_images += 1
        image_slots = slots_by_role["image"]
        if len(image_slots) > MAX_DISCOVERED_IMAGE_SLOTS:
            raise ValueError("too many represented image slots")
        if downloaded_images > MAX_IMAGES_PER_NOTE:
            raise ValueError("too many downloaded images")
        for role in ("video_cover", "video"):
            if len(slots_by_role[role]) > MAX_VIDEOS_PER_NOTE:
                raise ValueError("only one represented video slot is allowed")
        if image_slots and (slots_by_role["video"] or slots_by_role["video_cover"]):
            raise ValueError("image and video media cannot be mixed")
        if downloaded_bytes > MAX_NOTE_MEDIA_BYTES:
            raise ValueError("media exceeds the per-note byte limit")
        for slots in slots_by_role.values():
            positions = sorted(slot.position for slot in slots)
            if positions != list(range(1, len(positions) + 1)):
                raise ValueError("media slot positions must be contiguous within a role")
        if self.media_discovered_count < len(image_slots):
            raise ValueError("media discovered count cannot be below represented image slots")
        if self.media_discovery_truncated != (self.media_discovered_count > len(image_slots)):
            raise ValueError("media discovery truncation must match represented image slots")
        if self.cover_local_path is not None or self.cover_asset is not None:
            expected_cover = self._manifest_compatibility_cover(slots_by_role)
            if (
                expected_cover is None
                or self.cover_local_path != expected_cover.local_path
                or self.cover_asset != expected_cover
            ):
                raise ValueError("V2 compatibility cover must equal the first downloaded cover slot")

    @staticmethod
    def _manifest_compatibility_cover(
        slots_by_role: dict[MediaRole, list[NoteMediaSlot]],
    ) -> LocalAsset | None:
        for role in ("image", "video_cover"):
            for slot in slots_by_role[role]:
                if slot.position == 1 and slot.status == "downloaded":
                    return slot.asset
        return None


AccountFieldKey = Literal[
    "name", "bio", "note_count", "follower_count", "avatar", "platform_metrics"
]
AccountFieldStatus = Literal["exposed", "exposed_empty", "not_exposed", "unavailable"]
CandidateAttemptOutcome = Literal["complete", "unavailable", "rejected"]
CandidateAttemptStage = Literal[
    "candidate", "card_resolution", "detail_navigation", "detail_projection"
]
CandidateAttemptReason = Literal[
    "candidate_shortfall",
    "candidate_invalid",
    "card_not_found",
    "card_offscreen",
    "card_unsafe",
    "detail_timeout",
    "detail_route_mismatch",
    "detail_projection_unavailable",
    "media_scope_invalid",
    "upstream_unavailable",
]
DetailDelayMilliseconds = Annotated[int, Field(ge=3_000, le=7_000)]

_ATTEMPT_REASONS: dict[
    tuple[CandidateAttemptOutcome, CandidateAttemptStage], frozenset[CandidateAttemptReason]
] = {
    ("unavailable", "candidate"): frozenset({"candidate_shortfall"}),
    ("rejected", "candidate"): frozenset({"candidate_invalid"}),
    ("unavailable", "card_resolution"): frozenset({"card_not_found", "card_offscreen"}),
    ("rejected", "card_resolution"): frozenset({"card_unsafe"}),
    ("unavailable", "detail_navigation"): frozenset(
        {"detail_timeout", "detail_route_mismatch", "upstream_unavailable"}
    ),
    ("unavailable", "detail_projection"): frozenset({"detail_projection_unavailable"}),
    ("rejected", "detail_projection"): frozenset({"media_scope_invalid"}),
}


class PacingSummary(VisibleResultModel):
    """Finite delays applied during a conservative account collection run."""

    policy: Literal["conservative_jitter_v1"]
    profile_open_delay_ms: int = Field(ge=2_000, le=5_000)
    detail_delay_ms: list[DetailDelayMilliseconds]

    @field_validator("profile_open_delay_ms", mode="before")
    @classmethod
    def validate_profile_open_delay_ms(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("profile_open_delay_ms must be an exact integer")
        return value

    @field_validator("detail_delay_ms", mode="before")
    @classmethod
    def validate_detail_delay_ms(cls, value: object) -> object:
        if type(value) is not list or any(type(delay) is not int for delay in value):
            raise ValueError("detail_delay_ms must contain exact integers")
        return value


class CandidateAttempt(VisibleResultModel):
    """One finite attempt to project an account candidate note."""

    position: int = Field(ge=1, le=10)
    note_id: str | None = None
    outcome: CandidateAttemptOutcome
    stage: CandidateAttemptStage
    reason: CandidateAttemptReason | None = None

    @field_validator("position", mode="before")
    @classmethod
    def validate_position(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("position must be an exact integer")
        return value

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str | None) -> str | None:
        if value is not None and (
            _SAFE_ID.fullmatch(value) is None or not is_safe_evidence_key(value)
        ):
            raise ValueError("note_id must be safe")
        return value

    @model_validator(mode="after")
    def validate_attempt_combination(self) -> Self:
        if self.outcome == "complete":
            if self.stage != "detail_projection" or self.reason is not None or self.note_id is None:
                raise ValueError("complete attempt requires a projected note")
            return self
        if self.reason is None:
            raise ValueError("incomplete attempt requires a finite reason")
        reasons = _ATTEMPT_REASONS.get((self.outcome, self.stage))
        if reasons is None or self.reason not in reasons:
            raise ValueError("attempt reason is not legal for outcome and stage")
        if self.outcome == "unavailable" and self.stage == "candidate":
            if self.note_id is not None:
                raise ValueError("candidate shortfall cannot identify a note")
        elif (self.outcome, self.stage) != ("rejected", "candidate") and self.note_id is None:
            raise ValueError("attempt requires a safe note_id")
        return self


class AccountRecord(VisibleResultModel):
    """Publicly visible account data attached to a collection result."""

    account_id: str | None = None
    profile_url: str | None = None
    avatar_local_path: str | None = None
    avatar_asset: LocalAsset | None = None
    name: str | None = None
    bio: str | None = None
    note_count: MetricValue | None = None
    follower_count: MetricValue | None = None
    platform_metrics: dict[str, MetricValue] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    field_statuses: dict[AccountFieldKey, AccountFieldStatus] = Field(default_factory=dict)

    @field_validator("profile_url")
    @classmethod
    def validate_profile_url(cls, value: str | None) -> str | None:
        """Retain an account homepage only in its safe canonical form."""
        if value is None:
            return None
        parsed = parse_xhs_url(value)
        if parsed.object_type != "profile" or parsed.canonical_url != value:
            raise ValueError("profile_url must be a query-free Xiaohongshu profile URL")
        return value

    @field_validator("avatar_local_path")
    @classmethod
    def validate_avatar_local_path(cls, value: str | None) -> str | None:
        """Require a local avatar path that remains beneath the artifact root."""
        return _validate_local_relative_path(value)

    @model_validator(mode="after")
    def validate_avatar_asset_pair(self) -> Self:
        """Keep legacy and metadata paths as one consistent local-asset pair."""
        if self.avatar_asset is not None and (
            self.avatar_local_path is None or self.avatar_asset.local_path != self.avatar_local_path
        ):
            raise ValueError("avatar asset path must match avatar_local_path")
        if self.avatar_asset is not None and (
            self.avatar_asset.mime_type not in IMAGE_MIME_TYPES
            or self.avatar_asset.size_bytes > MAX_IMAGE_BYTES
        ):
            raise ValueError("avatar asset must be an approved image")
        if any(
            field != "bio" and status == "exposed_empty"
            for field, status in self.field_statuses.items()
        ):
            raise ValueError("exposed_empty is only valid for bio")
        return self

    @field_validator("platform_metrics", mode="before")
    @classmethod
    def validate_platform_metric_labels(cls, value: object) -> object:
        """Allow explicit platform labels without accepting sensitive mapping keys."""
        if type(value) is not dict:
            raise ValueError("platform_metrics must be a dictionary")
        for key in value:
            if not isinstance(key, str):
                raise ValueError("platform metric labels must be strings")  # noqa: TRY004
            _validate_platform_metric_label(key)
        return value


class ExtensionSelectionEntry(VisibleResultModel):
    """One inspected extension candidate and its finite selection outcome."""

    source_position: int = Field(ge=1, le=100)
    note_id: str
    outcome: ExtensionSelectionOutcome
    reason: ExtensionSelectionReason | None = None
    publication_eligible: bool = False
    likes_eligible: bool = False
    exact_likes: int | None = Field(default=None, ge=0)
    selection_rank: int | None = Field(default=None, ge=1, le=20)
    selection_basis: ExtensionSelectionBasis = "exact_likes_desc"
    inclusion: ExtensionSelectionInclusion | None = None
    detail_outcome: ExtensionDetailOutcome | None = None

    @model_validator(mode="before")
    @classmethod
    def require_legacy_eligibility_facts(cls, value: object) -> object:
        if (
            isinstance(value, dict)
            and value.get("selection_basis", "exact_likes_desc") == "exact_likes_desc"
            and ("publication_eligible" not in value or "likes_eligible" not in value)
        ):
            raise ValueError("exact-likes entries require eligibility facts")
        return value

    @field_validator("source_position", "exact_likes", "selection_rank", mode="before")
    @classmethod
    def validate_exact_integers(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("extension selection integers must be exact integers")
        return value

    @field_validator("note_id")
    @classmethod
    def validate_note_id(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None or not is_safe_evidence_key(value):
            raise ValueError("note_id must be safe")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.selection_basis == "page_order":
            if self.inclusion is None:
                raise ValueError("page-order entries require an inclusion outcome")
            if self.outcome == "selected":
                if self.inclusion != "included" or self.reason is not None or self.selection_rank is None:
                    raise ValueError("page-order selected entry must be included and ranked")
            elif self.selection_rank is not None:
                raise ValueError("page-order non-selected entry cannot have a selection rank")
            if self.inclusion == "excluded" and self.reason not in {"sponsored", "invalid_card", "integrity_failure"}:
                raise ValueError("page-order excluded entry requires a finite exclusion reason")
            if self.detail_outcome is not None and self.outcome != "selected":
                raise ValueError("page-order detail outcome requires a selected entry")
            return self
        if self.outcome == "selected":
            if (
                self.reason is not None
                or not self.publication_eligible
                or not self.likes_eligible
                or self.exact_likes is None
                or self.selection_rank is None
            ):
                raise ValueError("selected entry requires exact eligible ranking facts")
            return self
        if self.reason == "selection_limit_reached" and (
            not self.publication_eligible
            or not self.likes_eligible
            or self.exact_likes is None
        ):
            raise ValueError("selection limit entry requires exact eligible ranking facts")
        required_reasons: dict[ExtensionSelectionOutcome, frozenset[ExtensionSelectionReason]] = {
            "excluded": frozenset(
                {
                    "publication_time_unavailable",
                    "likes_unavailable",
                    "likes_not_exact",
                    "selection_limit_reached",
                }
            ),
            "unavailable": frozenset(
                {"detail_unavailable", "login_required", "challenge_detected"}
            ),
            "rejected": frozenset(
                {"candidate_invalid", "route_mismatch", "identity_mismatch", "message_limit_exceeded"}
            ),
            "selected": frozenset(),
        }
        if self.reason not in required_reasons[self.outcome] or self.selection_rank is not None:
            raise ValueError("extension selection outcome requires its finite reason")
        return self


class ExtensionSelectionSummary(VisibleResultModel):
    """Strict batch-ranking ledger retained only for extension batch runs."""

    collection_surface: ExtensionCollectionSurface
    candidate_scan_limit: int = Field(ge=1, le=100)
    candidate_scanned_count: int = Field(ge=0, le=100)
    requested_count: int = Field(ge=1, le=20)
    selected_count: int | None = Field(default=None, ge=0, le=20)
    publication_cutoff: datetime | None = None
    selection_basis: ExtensionSelectionBasis = "exact_likes_desc"
    entries: list[ExtensionSelectionEntry]

    @field_validator(
        "candidate_scan_limit", "candidate_scanned_count", "requested_count", mode="before"
    )
    @classmethod
    def validate_exact_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("extension selection integers must be exact integers")
        return value

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.candidate_scanned_count > self.candidate_scan_limit:
            raise ValueError("candidate scanned count cannot exceed scan limit")
        if self.requested_count > self.candidate_scan_limit:
            raise ValueError("requested count cannot exceed scan limit")
        if self.candidate_scanned_count != len(self.entries):
            raise ValueError("candidate scanned count must equal selection entry count")
        if len({entry.note_id for entry in self.entries}) != len(self.entries):
            raise ValueError("selection entries must have unique note IDs")
        if len({entry.source_position for entry in self.entries}) != len(self.entries):
            raise ValueError("selection entries must have unique source positions")
        if any(entry.selection_basis != self.selection_basis for entry in self.entries):
            raise ValueError("selection entries must use the summary selection basis")
        if self.selection_basis == "page_order":
            if self.collection_surface != "extension_search":
                raise ValueError("page-order selection is search-only")
            if self.requested_count not in {5, 10}:
                raise ValueError("page-order selection must request five or ten notes")
            if self.publication_cutoff is not None:
                raise ValueError("page-order selection cannot retain a publication cutoff")
            if self.entries != sorted(self.entries, key=lambda entry: entry.source_position):
                raise ValueError("page-order entries must retain visible source order")
            selected = [entry for entry in self.entries if entry.selection_rank is not None]
            expected = [
                entry for entry in self.entries if entry.inclusion == "included"
            ][: self.requested_count]
            if [entry.note_id for entry in selected] != [entry.note_id for entry in expected]:
                raise ValueError("page-order selection must preserve the visible eligible prefix")
            if [entry.selection_rank for entry in selected] != list(range(1, len(selected) + 1)):
                raise ValueError("page-order selected ranks must be contiguous")
            if self.selected_count != len(selected):
                raise ValueError("page-order selected count must match selected entries")
            return self
        if self.selected_count is not None:
            selected = [entry for entry in self.entries if entry.outcome == "selected"]
            if self.selected_count != len(selected):
                raise ValueError("selected count must match selected entries")
        eligible_entries = [
            entry
            for entry in self.entries
            if entry.publication_eligible and entry.likes_eligible
        ]
        if any(entry.exact_likes is None for entry in eligible_entries):
            raise ValueError("eligible selection entry requires exact likes")
        ranked_entries = sorted(
            eligible_entries,
            key=lambda entry: (-_require_exact_likes(entry), entry.source_position, entry.note_id),
        )
        selected_prefix = ranked_entries[: self.requested_count]
        for expected_rank, entry in enumerate(selected_prefix, start=1):
            if entry.outcome != "selected" or entry.selection_rank != expected_rank:
                raise ValueError("selected entries must be the exact selection prefix")
        for entry in ranked_entries[self.requested_count :]:
            if entry.outcome != "excluded" or entry.reason != "selection_limit_reached":
                raise ValueError("selection limit entries must follow the selection prefix")
        return self


class ExtensionSearchRunSummary(VisibleResultModel):
    """Bounded page-order search facts retained alongside an extension run."""

    source_page_url: str
    sort_label: str | None = Field(default=None, max_length=100)
    requested_count: int = Field(ge=1, le=20)
    selected_count: int = Field(ge=0, le=20)
    enriched_count: int = Field(ge=0, le=20)
    scroll_rounds: int = Field(ge=0, le=2)
    status: ExtensionSearchRunStatus
    error_code: ExtensionSearchRunErrorCode | None = None

    @field_validator("requested_count", "selected_count", "enriched_count", "scroll_rounds", mode="before")
    @classmethod
    def validate_exact_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("search run counts must be exact integers")
        return value

    @field_validator("source_page_url")
    @classmethod
    def validate_source_page_url(cls, value: str) -> str:
        if not is_canonical_xhs_search_route(value):
            raise ValueError("search run source URL must be the canonical query-free search route")
        return value

    @field_validator("sort_label")
    @classmethod
    def validate_sort_label(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or not is_safe_retained_text(value) or _URL_LIKE.search(value)):
            raise ValueError("sort label must be safe retained public text")
        return value

    @model_validator(mode="after")
    def validate_counts_and_status(self) -> Self:
        if self.requested_count not in {5, 10}:
            raise ValueError("page-order search runs must request five or ten notes")
        if not self.enriched_count <= self.selected_count <= self.requested_count:
            raise ValueError("search run counts are inconsistent")
        if self.status == "complete" and (
            self.selected_count != self.requested_count
            or self.enriched_count != self.selected_count
            or self.error_code is not None
        ):
            raise ValueError("complete search runs require all selected details and no error")
        if self.status == "stopped" and self.error_code != "stopped":
            raise ValueError("stopped search runs require the stopped error code")
        if self.status != "stopped" and self.error_code == "stopped":
            raise ValueError("stopped error code requires stopped status")
        failed_errors = {"structural_error", "route_mismatch", "identity_mismatch"}
        if self.status == "failed" and self.error_code not in failed_errors:
            raise ValueError("failed search runs require a finite structural error code")
        if self.status != "failed" and self.error_code in failed_errors:
            raise ValueError("structural error codes require failed status")
        return self


def _require_exact_likes(entry: ExtensionSelectionEntry) -> int:
    """Narrow a validated eligible entry for its deterministic sort key."""
    assert entry.exact_likes is not None
    return entry.exact_likes


class CollectionRun(VisibleResultModel):
    """One explicitly-statused, visible-data collection attempt."""

    run_id: str
    mode: str
    input_summary: str
    requested_count: int
    actual_count: int
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    error_code: str | None = None
    account: AccountRecord | None = None
    notes: list[NoteRecord]
    candidate_attempts: list[CandidateAttempt] = Field(default_factory=list)
    pacing_summary: PacingSummary | None = None
    collection_surface: ExtensionCollectionSurface | None = None
    extension_selection: ExtensionSelectionSummary | None = None
    extension_search_run: ExtensionSearchRunSummary | None = None

    @model_validator(mode="after")
    def validate_extension_selection(self) -> Self:
        note_has_selection = any(
            note.selection_rank is not None or note.selection_basis is not None for note in self.notes
        )
        if self.collection_surface is None:
            if self.extension_selection is not None or self.extension_search_run is not None or note_has_selection:
                raise ValueError("extension selection metadata requires an extension collection surface")
            return self
        if self.collection_surface == "extension_current":
            if self.extension_selection is not None or self.extension_search_run is not None or note_has_selection:
                raise ValueError("extension current runs cannot retain batch selection metadata")
            return self
        summary = self.extension_selection
        if summary is None or summary.collection_surface != self.collection_surface:
            raise ValueError("extension batch runs require a matching selection summary")
        if summary.candidate_scanned_count != len(summary.entries):
            raise ValueError("candidate scanned count must equal selection entry count")
        if summary.requested_count != self.requested_count:
            raise ValueError("selection requested count must match run")
        if summary.selection_basis == "page_order":
            if self.extension_search_run is None:
                raise ValueError("page-order runs require a search run summary")
            if self.extension_search_run.requested_count != self.requested_count:
                raise ValueError("search run requested count must match run")
        elif self.extension_search_run is not None:
            raise ValueError("exact-likes runs cannot retain a page-order search run summary")
        if len(self.notes) != self.actual_count:
            raise ValueError("extension actual count must match selected notes")
        if len({note.note_id for note in self.notes}) != len(self.notes):
            raise ValueError("selected notes must have unique note IDs")
        if len({note.source_position for note in self.notes}) != len(self.notes):
            raise ValueError("selected notes must have unique source positions")
        selected_entries = [entry for entry in summary.entries if entry.outcome == "selected"]
        if len(selected_entries) != self.actual_count:
            raise ValueError("selected entries must match actual count")
        if len({entry.note_id for entry in summary.entries}) != len(summary.entries):
            raise ValueError("selection entries must have unique note IDs")
        if len({entry.source_position for entry in summary.entries}) != len(summary.entries):
            raise ValueError("selection entries must have unique source positions")
        if any(note.selection_rank is None for note in self.notes) or any(
            entry.selection_rank is None for entry in selected_entries
        ):
            raise ValueError("selected notes and entries require selection ranks")
        note_ranks = [note.selection_rank for note in self.notes if note.selection_rank is not None]
        entry_ranks = [
            entry.selection_rank for entry in selected_entries if entry.selection_rank is not None
        ]
        expected_ranks = list(range(1, self.actual_count + 1))
        if sorted(note_ranks) != expected_ranks or sorted(entry_ranks) != expected_ranks:
            raise ValueError("selected ranks must be contiguous")
        entries_by_note = {entry.note_id: entry for entry in selected_entries}
        if set(entries_by_note) != {note.note_id for note in self.notes}:
            raise ValueError("selected entries must correspond to persisted notes")
        if summary.selection_basis == "page_order":
            search_run = self.extension_search_run
            assert search_run is not None
            if summary.selected_count != len(selected_entries) or search_run.selected_count != len(selected_entries):
                raise ValueError("page-order selected counts must match selected entries")
            if any(entry.detail_outcome is None for entry in selected_entries):
                raise ValueError("page-order selected entries require detail outcomes")
            detail_outcomes = [entry.detail_outcome for entry in selected_entries]
            assert all(outcome is not None for outcome in detail_outcomes)
            terminal_outcomes = {"login_required", "challenge_detected", "stopped"}
            observed_terminals = [
                outcome for outcome in detail_outcomes if outcome in terminal_outcomes
            ]
            if observed_terminals:
                terminal = observed_terminals[0]
                if any(outcome != terminal for outcome in observed_terminals):
                    raise ValueError("page-order runs require exactly one terminal cause")
                terminal_index = detail_outcomes.index(terminal)
                if any(outcome != terminal for outcome in detail_outcomes[terminal_index:]):
                    raise ValueError("page-order terminal outcome must halt later selected ranks")
            enriched_count = detail_outcomes.count("enriched")
            if search_run.enriched_count != enriched_count:
                raise ValueError("page-order enriched count must match detail outcomes")
            if self.status.value != search_run.status or self.error_code != search_run.error_code:
                raise ValueError("page-order outer status and error must match search run")
            failed_errors = {"structural_error", "route_mismatch", "identity_mismatch"}
            if self.status is RunStatus.FAILED:
                if selected_entries or self.actual_count or enriched_count or self.error_code not in failed_errors:
                    raise ValueError("failed page-order runs cannot retain selected results")
            elif "stopped" in detail_outcomes:
                if self.status is not RunStatus.STOPPED or self.error_code != "stopped":
                    raise ValueError("stopped detail outcome requires stopped run")
            elif "login_required" in detail_outcomes:
                if self.status is not RunStatus.PARTIAL or self.error_code != "login_required":
                    raise ValueError("login detail outcome requires partial login-required run")
            elif "challenge_detected" in detail_outcomes:
                if self.status is not RunStatus.PARTIAL or self.error_code != "challenge_detected":
                    raise ValueError("challenge detail outcome requires partial challenge run")
            elif "detail_unavailable" in detail_outcomes:
                if self.status is not RunStatus.PARTIAL or self.error_code != "detail_unavailable":
                    raise ValueError("unavailable detail requires partial run")
            elif self.actual_count == self.requested_count:
                if self.status is not RunStatus.COMPLETE or self.error_code is not None:
                    raise ValueError("fully enriched page-order run must be complete")
            elif self.status is not RunStatus.PARTIAL or self.error_code is not None:
                raise ValueError("short fully enriched page-order run must be partial")
            for note in self.notes:
                entry = entries_by_note[note.note_id]
                if (
                    note.selection_rank is None
                    or note.selection_basis != "page_order"
                    or entry.source_position != note.source_position
                    or entry.selection_rank != note.selection_rank
                ):
                    raise ValueError("selected note must match its page-order selection entry")
            return self
        ranked_notes: list[tuple[int, int, str]] = []
        for note in self.notes:
            entry = entries_by_note[note.note_id]
            likes = note.metrics.likes
            if (
                note.selection_rank is None
                or note.selection_basis != "exact_likes_desc"
                or entry.source_position != note.source_position
                or entry.selection_rank != note.selection_rank
                or likes is None
                or likes.precision != "exact"
                or likes.normalized_value is None
                or entry.exact_likes != likes.normalized_value
            ):
                raise ValueError("selected note must match its exact selection entry")
            ranked_notes.append((-likes.normalized_value, note.source_position, note.note_id))
        rank_order = [note.note_id for note in sorted(self.notes, key=lambda note: note.selection_rank or 0)]
        expected_order = [note_id for _, _, note_id in sorted(ranked_notes)]
        if rank_order != expected_order:
            raise ValueError("selected ranks must follow exact likes sort order")
        return self


def _validate_local_relative_path(value: str | None) -> str | None:
    """Reject absolute, URI-like, empty, and traversing local artifact paths."""
    if value is None:
        return None
    if not value or value.isspace():
        raise ValueError("local asset path must not be empty")
    if urlsplit(value).scheme or value.startswith("//"):
        raise ValueError("local asset path must not be a URL or URI")
    windows_path = PureWindowsPath(value)
    if value.startswith("/") or windows_path.is_absolute() or windows_path.drive:
        raise ValueError("local asset path must be relative")
    if "\\" in value or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("local asset path must not traverse directories")
    return value


def _validate_platform_metric_label(value: str) -> None:
    """Reject empty, URI-like, and sensitive platform metric labels without rewriting them."""
    if not value or value.isspace():
        raise ValueError("platform metric label must not be empty")
    if urlsplit(value).scheme or value.startswith("//"):
        raise ValueError("platform metric label must not be URI-like")
    if not is_safe_evidence_key(value):
        raise ValueError("platform metric label is sensitive")
