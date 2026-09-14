"""Orchestrate visible XHS snapshots into caller-owned local artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from xhs_workbench.media import (
    MAX_IMAGE_BYTES,
    MAX_RUN_MEDIA_BYTES,
    MAX_VIDEO_BYTES,
    MediaMime,
    mime_extension,
    sniff_media_mime,
    validate_media_container,
)
from xhs_workbench.models import (
    AccountFieldKey,
    AccountRecord,
    CandidateAttempt,
    CollectionRun,
    LocalAsset,
    NoteMediaSlot,
    NoteRecord,
    RunStatus,
)
from xhs_workbench.security import parse_xhs_url
from xhs_workbench.xhs_bridge import (
    MAX_MEDIA_BYTES,
    MIME_EXTENSIONS,
    AccountPayload,
    BridgeErrorCode,
    BridgeNote,
    BridgeResponse,
    CoverCandidate,
    SearchPayload,
    StagedMediaCandidate,
    is_safe_retained_text,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HTTP_FRAGMENT_URL = re.compile(r'(?i)https?://[^\s<>"\']*#')
_COLLECTOR_ERRORS = frozenset(
    {
        "adapter_failure",
        "invalid_bridge_payload",
        "invalid_output_dir",
        "output_conflict",
        "media_unavailable",
        "profile_unavailable",
    }
)
_OMITTABLE_NOTE_FIELDS = frozenset(
    {
        "title",
        "body",
        "note_type",
        "published_at",
        "author_id",
        "author_name",
        "author_profile_url",
    }
)
_MEDIA_ROLE_ORDER = {"image": 0, "video_cover": 1, "video": 2}


class _VisibleAdapter(Protocol):
    def collect_search(
        self, keyword: str, limit: int, asset_staging_dir: Path
    ) -> BridgeResponse: ...

    def collect_account(
        self, account_id: str, limit: int, asset_staging_dir: Path
    ) -> BridgeResponse: ...


class VisibleCollector:
    """Turn isolated bridge responses into validated, local-only visible evidence."""

    def __init__(
        self,
        adapter: _VisibleAdapter,
        *,
        clock: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex)
        self._issued_run_ids: set[str] = set()

    def collect_search(self, keyword: str, limit: int, output_dir: Path) -> CollectionRun:
        """Collect at most ``limit`` visible notes for an unchanged keyword."""
        if not isinstance(keyword, str) or not keyword:
            raise ValueError("keyword must be a non-empty string")
        if not _is_safe_text(keyword):
            raise ValueError("keyword contains unsafe retained text")
        self._validate_limit(limit)
        return self._collect(
            mode="search",
            input_summary=keyword,
            limit=limit,
            output_dir=output_dir,
            requested_account_id=None,
            invoke=lambda staging: self._adapter.collect_search(keyword, limit, staging),
        )

    def collect_account(self, profile_url: str, limit: int, output_dir: Path) -> CollectionRun:
        """Collect one visible account page after reducing its URL to its opaque ID."""
        self._validate_limit(limit)
        profile = parse_xhs_url(profile_url)
        if profile.object_type != "profile":
            raise ValueError("profile_url must identify a Xiaohongshu profile")
        return self._collect(
            mode="account",
            input_summary=profile.canonical_url,
            limit=limit,
            output_dir=output_dir,
            requested_account_id=profile.object_id,
            invoke=lambda staging: self._adapter.collect_account(profile.object_id, limit, staging),
        )

    def _collect(
        self,
        *,
        mode: Literal["search", "account"],
        input_summary: str,
        limit: int,
        output_dir: Path,
        requested_account_id: str | None,
        invoke: Callable[[Path], BridgeResponse],
    ) -> CollectionRun:
        run_id = self._new_run_id()
        started_at = self._now()
        output = Path(output_dir)
        assets: Path | None = None
        assets_identity: tuple[int, int] | None = None
        staging: Path | None = None
        staging_created = False
        retain_assets = False

        try:
            output = self._create_output_dir(output)
            assets = self._create_private_child(output, "assets")
            assets_identity = self._private_directory_identity(assets)
            staging = self._create_private_child(output, ".staging")
            staging_created = True
        except (OSError, ValueError):
            return self._failed_run(
                run_id, mode, input_summary, limit, started_at, "output_conflict"
            )

        try:
            try:
                response = invoke(staging)
            except Exception:  # noqa: BLE001 - adapter diagnostics must not cross the boundary.
                return self._failed_run(
                    run_id, mode, input_summary, limit, started_at, "adapter_failure"
                )
            if not isinstance(response, BridgeResponse):
                return self._failed_run(
                    run_id, mode, input_summary, limit, started_at, "invalid_bridge_payload"
                )
            if response.status == "failed":
                return self._failed_run(
                    run_id,
                    mode,
                    input_summary,
                    limit,
                    started_at,
                    self._bridge_error_code(response.error_code),
                )

            if mode == "search":
                search_payload = self._parse_search_payload(response.payload)
                if not all(_is_safe_note(note) for note in search_payload.notes):
                    return self._failed_run(
                        run_id, mode, input_summary, limit, started_at, "invalid_bridge_payload"
                    )
                account = None
                notes, media_failed, invalid_note = self._materialize_notes(
                    search_payload.notes, limit, staging, assets
                )
                profile_incomplete = False
                candidate_attempts = []
                pacing_summary = None
            else:
                account_payload = self._parse_account_payload(response.payload)
                assert requested_account_id is not None
                if (
                    account_payload.requested_count != limit
                    or not _is_safe_account(account_payload.account)
                    or not self._account_avatar_evidence_is_consistent(account_payload)
                    or not all(_is_safe_note(note) for note in account_payload.notes)
                    or not _account_payload_matches(account_payload, requested_account_id)
                ):
                    return self._failed_run(
                        run_id, mode, input_summary, limit, started_at, "invalid_bridge_payload"
                    )
                account, avatar_failed, profile_incomplete = self._materialize_account(
                    account_payload.account, account_payload.avatar, staging, assets
                )
                notes, cover_failed, invalid_note = self._materialize_notes(
                    account_payload.notes,
                    limit,
                    staging,
                    assets,
                    initial_run_media_bytes=(
                        account.avatar_asset.size_bytes
                        if account is not None and account.avatar_asset is not None
                        else 0
                    ),
                )
                media_failed = avatar_failed or cover_failed
                profile_incomplete = profile_incomplete or any(
                    note.author_id is None for note in account_payload.notes[:limit]
                )
                complete_attempt_count = sum(
                    attempt.outcome == "complete"
                    for attempt in account_payload.candidate_attempts
                )
                if len(notes) != complete_attempt_count:
                    return self._failed_run(
                        run_id, mode, input_summary, limit, started_at, "invalid_bridge_payload"
                    )
                candidate_attempts = [
                    attempt.model_copy(deep=True) for attempt in account_payload.candidate_attempts
                ]
                pacing_summary = account_payload.pacing_summary.model_copy(deep=True)

            if invalid_note:
                return self._failed_run(
                    run_id, mode, input_summary, limit, started_at, "invalid_bridge_payload"
                )

            bridge_partial = response.status == "partial"
            if mode == "account":
                attempts_incomplete = any(
                    attempt.outcome != "complete" for attempt in candidate_attempts
                )
                count_incomplete = len(notes) != limit
                status = (
                    RunStatus.PARTIAL
                    if bridge_partial
                    or attempts_incomplete
                    or count_incomplete
                    or media_failed
                    or profile_incomplete
                    else RunStatus.COMPLETE
                )
                error_code = self._account_error_code(
                    response.error_code,
                    candidate_attempts,
                    attempts_incomplete or count_incomplete,
                    media_failed,
                    profile_incomplete,
                )
            else:
                status = (
                    RunStatus.PARTIAL
                    if bridge_partial or media_failed or profile_incomplete
                    else RunStatus.COMPLETE
                )
                error_code = (
                    self._bridge_error_code(response.error_code)
                    if bridge_partial and response.error_code is not None
                    else "media_unavailable"
                    if media_failed
                    else "profile_unavailable"
                    if profile_incomplete
                    else None
                )
            run = CollectionRun(
                run_id=run_id,
                mode=mode,
                input_summary=input_summary,
                requested_count=limit,
                actual_count=len(notes),
                started_at=started_at,
                finished_at=self._now(),
                status=status,
                error_code=error_code,
                account=account,
                notes=notes,
                candidate_attempts=candidate_attempts,
                pacing_summary=pacing_summary,
            )
            retain_assets = True
            return run
        except Exception:  # noqa: BLE001 - bridge data is treated as untrusted at this boundary.
            return self._failed_run(
                run_id, mode, input_summary, limit, started_at, "invalid_bridge_payload"
            )
        finally:
            if not retain_assets and assets is not None and assets_identity is not None:
                self._cleanup_failed_assets(assets, assets_identity)
            if staging_created and staging is not None:
                self._cleanup_staging(staging)

    def _materialize_notes(
        self,
        candidates: list[BridgeNote],
        limit: int,
        staging: Path,
        assets: Path,
        *,
        initial_run_media_bytes: int = 0,
    ) -> tuple[list[NoteRecord], bool, bool]:
        notes: list[NoteRecord] = []
        media_failed = False
        run_media_bytes = initial_run_media_bytes
        for candidate in candidates[:limit]:
            if not self._safe_note_identity(candidate):
                return [], False, True
            try:
                slots: list[NoteMediaSlot] = []
                for media_candidate in _ordered_media_candidates(candidate.media_candidates):
                    if (
                        media_candidate.status == "downloaded"
                        and (media_candidate.size_bytes is None
                             or run_media_bytes + media_candidate.size_bytes > MAX_RUN_MEDIA_BYTES)
                    ):
                        raise ValueError("media exceeds the run byte budget")
                    slot = _persist_media_candidate(media_candidate, candidate.note_id, staging, assets)
                    slots.append(slot)
                    if slot.status == "downloaded":
                        assert slot.asset is not None
                        run_media_bytes += slot.asset.size_bytes
                cover_asset = _compatibility_cover(slots)
                note = NoteRecord(
                    note_id=candidate.note_id,
                    canonical_url=candidate.canonical_url,
                    title=candidate.title,
                    body=candidate.body,
                    tags=list(candidate.tags),
                    note_type=candidate.note_type,
                    published_at=candidate.published_at,
                    time_evidence=(
                        candidate.time_evidence.model_copy(deep=True)
                        if candidate.time_evidence is not None
                        else None
                    ),
                    author_id=candidate.author_id,
                    author_name=candidate.author_name,
                    author_profile_url=candidate.author_profile_url,
                    metrics=candidate.metrics.model_copy(deep=True),
                    metric_provenance=dict(candidate.metric_provenance),
                    source_position=candidate.source_position,
                    media_manifest_version=2,
                    media_slots=slots,
                    media_discovered_count=candidate.media_discovered_count,
                    media_discovery_truncated=candidate.media_discovery_truncated,
                    cover_local_path=cover_asset.local_path if cover_asset is not None else None,
                    cover_asset=cover_asset,
                    missing_fields=_merge_missing_fields(
                        self._note_missing_fields(candidate),
                        _media_missing_fields(
                            slots, discovery_truncated=candidate.media_discovery_truncated
                        ),
                    ),
                )
            except (OSError, ValueError, TypeError):
                return [], False, True
            media_failed = media_failed or candidate.media_discovery_truncated or any(
                slot.status != "downloaded" for slot in slots
            )
            notes.append(note)
        return notes, media_failed, False

    @staticmethod
    def _parse_search_payload(payload: dict[str, object]) -> SearchPayload:
        if type(payload) is not dict or set(payload) != {"notes"}:
            raise ValueError("invalid search payload")
        notes = _parse_bridge_notes(payload["notes"])
        return SearchPayload(notes=notes)

    @staticmethod
    def _parse_account_payload(payload: dict[str, object]) -> AccountPayload:
        if type(payload) is not dict:
            raise ValueError("invalid account payload")
        return AccountPayload.model_validate(payload)

    def _materialize_account(
        self,
        candidate: AccountRecord | None,
        avatar: CoverCandidate | None,
        staging: Path,
        assets: Path,
    ) -> tuple[AccountRecord | None, bool, bool]:
        if candidate is None:
            return None, avatar is not None, True
        avatar_asset: LocalAsset | None = None
        if avatar is not None:
            avatar_asset = self._persist_candidate(
                staging,
                assets,
                avatar,
                f"account-avatar{MIME_EXTENSIONS[avatar.mime_type]}",
            )
        local_avatar = avatar_asset.local_path if avatar_asset is not None else None
        avatar_failed = candidate.field_statuses.get("avatar") == "exposed" and local_avatar is None
        account = AccountRecord(
            account_id=candidate.account_id,
            profile_url=candidate.profile_url,
            avatar_local_path=local_avatar,
            avatar_asset=avatar_asset,
            name=candidate.name,
            bio=candidate.bio,
            note_count=candidate.note_count,
            follower_count=candidate.follower_count,
            platform_metrics=dict(candidate.platform_metrics),
            missing_fields=self._account_missing_fields(candidate, local_avatar),
            field_statuses=dict(candidate.field_statuses),
        )
        profile_incomplete = self._account_profile_incomplete(account)
        return account, avatar_failed, profile_incomplete

    @staticmethod
    def _account_avatar_evidence_is_consistent(payload: AccountPayload) -> bool:
        account = payload.account
        return account is not None and (
            (account.field_statuses.get("avatar") == "exposed") == (payload.avatar is not None)
        )

    @staticmethod
    def _safe_note_identity(note: BridgeNote) -> bool:
        if not _SAFE_ID.fullmatch(note.note_id):
            return False
        try:
            parsed = parse_xhs_url(note.canonical_url)
        except ValueError:
            return False
        return parsed.object_type == "note" and parsed.object_id == note.note_id

    @staticmethod
    def _note_missing_fields(note: BridgeNote) -> list[str]:
        fields = [
            name
            for name, value in (
                ("title", note.title),
                ("body", note.body),
                ("note_type", note.note_type),
                ("published_at", note.published_at),
                ("author_id", note.author_id),
                ("author_name", note.author_name),
                ("author_profile_url", note.author_profile_url),
                ("metrics.likes", note.metrics.likes),
                ("metrics.collects", note.metrics.collects),
                ("metrics.comments", note.metrics.comments),
                ("metrics.shares", note.metrics.shares),
            )
            if value is None
        ]
        return fields

    @staticmethod
    def _account_missing_fields(account: AccountRecord, avatar_local_path: str | None) -> list[str]:
        fields = [
            name
            for name, value in (("account_id", account.account_id), ("profile_url", account.profile_url))
            if value is None
        ]
        statuses = account.field_statuses
        field_values: tuple[tuple[AccountFieldKey, object], ...] = (
            ("name", account.name),
            ("bio", account.bio),
            ("note_count", account.note_count),
            ("follower_count", account.follower_count),
            ("platform_metrics", account.platform_metrics),
        )
        for name, value in field_values:
            status = statuses.get(name)
            if status in {"not_exposed", "exposed_empty"}:
                continue
            if status == "unavailable" or value is None:
                fields.append(name)
        avatar_status = statuses.get("avatar")
        if avatar_status == "unavailable" or (
            avatar_status == "exposed" and avatar_local_path is None
        ):
            fields.append("avatar")
        if avatar_status == "exposed" and avatar_local_path is None:
            fields.append("avatar_local_path")
        return fields

    @staticmethod
    def _account_profile_incomplete(account: AccountRecord) -> bool:
        statuses = account.field_statuses
        return (
            account.account_id is None
            or account.profile_url is None
            or statuses.get("name") != "exposed"
            or account.name is None
            or statuses.get("avatar") != "exposed"
            or any(status == "unavailable" for status in statuses.values())
        )

    @staticmethod
    def _persist_candidate(
        staging: Path, assets: Path, candidate: CoverCandidate, target_name: str
    ) -> LocalAsset | None:
        if (
            Path(candidate.staging_name).name != candidate.staging_name
            or Path(target_name).name != target_name
            or not _SHA256.fullmatch(candidate.sha256)
            or candidate.size_bytes <= 0
            or candidate.size_bytes > MAX_MEDIA_BYTES
            or not candidate.staging_name.endswith(MIME_EXTENSIONS[candidate.mime_type])
            or not target_name.endswith(MIME_EXTENSIONS[candidate.mime_type])
        ):
            return None
        try:
            staging_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            assets_fd = os.open(assets, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return None
        created_target = False
        persisted_target = False
        try:
            try:
                source_fd = os.open(
                    candidate.staging_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=staging_fd
                )
            except OSError:
                return None
            try:
                target_fd = os.open(
                    target_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=assets_fd,
                )
            except OSError:
                os.close(source_fd)
                return None
            created_target = True
            try:
                snapshot = _copy_and_verify_candidate(source_fd, target_fd, candidate)
                if snapshot is None:
                    return None
                os.fsync(target_fd)
                target_snapshot = _snapshot_file(target_fd)
                if target_snapshot != snapshot:
                    return None
            finally:
                os.close(target_fd)
                os.close(source_fd)
            try:
                os.unlink(candidate.staging_name, dir_fd=staging_fd)
            except OSError:
                # The destination is verified; run-owned staging cleanup handles a residual source.
                pass
            asset = LocalAsset(
                local_path=f"assets/{target_name}",
                mime_type=snapshot.mime_type,
                size_bytes=snapshot.size_bytes,
                sha256=snapshot.sha256,
            )
            persisted_target = True
            return asset
        finally:
            if created_target and not persisted_target:
                try:
                    os.unlink(target_name, dir_fd=assets_fd)
                except OSError:
                    pass
            os.close(assets_fd)
            os.close(staging_fd)

    @staticmethod
    def _create_output_dir(path: Path) -> Path:
        if ".." in path.parts or _has_symlink_component(path) or path.exists():
            raise ValueError("unsafe output directory")
        path.mkdir(parents=True, mode=0o700)
        if _has_symlink_component(path):
            raise ValueError("unsafe output directory")
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("output path is not a directory")
        resolved.chmod(0o700)
        return resolved

    @staticmethod
    def _create_private_child(parent: Path, name: str) -> Path:
        path = parent / name
        if path.exists() or path.is_symlink():
            raise ValueError("output child conflict")
        path.mkdir(mode=0o700)
        resolved = path.resolve(strict=True)
        if resolved.parent != parent or not resolved.is_dir() or path.is_symlink():
            raise ValueError("unsafe output child")
        resolved.chmod(0o700)
        return resolved

    @staticmethod
    def _cleanup_staging(staging: Path) -> None:
        try:
            if staging.exists() and staging.is_dir() and not staging.is_symlink():
                shutil.rmtree(staging)
        except OSError:
            pass

    @staticmethod
    def _private_directory_identity(directory: Path) -> tuple[int, int]:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            entry = os.fstat(descriptor)
            if not stat.S_ISDIR(entry.st_mode):
                raise ValueError("private child is not a directory")
            return entry.st_dev, entry.st_ino
        finally:
            os.close(descriptor)

    @staticmethod
    def _cleanup_failed_assets(assets: Path, expected_identity: tuple[int, int]) -> None:
        """Remove only collector-created direct asset children after a failed run."""
        try:
            assets_fd = os.open(assets, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return
        try:
            directory = os.fstat(assets_fd)
            if (
                not stat.S_ISDIR(directory.st_mode)
                or (directory.st_dev, directory.st_ino) != expected_identity
            ):
                return
            for name in os.listdir(assets_fd):
                try:
                    entry = os.stat(name, dir_fd=assets_fd, follow_symlinks=False)
                    if stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
                        os.unlink(name, dir_fd=assets_fd)
                except OSError:
                    continue
            os.fsync(assets_fd)
        except OSError:
            pass
        finally:
            os.close(assets_fd)

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer from 1 to 10")

    def _new_run_id(self) -> str:
        run_id = self._run_id_factory()
        if (
            not isinstance(run_id, str)
            or not _SAFE_ID.fullmatch(run_id)
            or run_id in self._issued_run_ids
        ):
            raise ValueError("run_id_factory must produce unique safe IDs")
        self._issued_run_ids.add(run_id)
        return run_id

    def _failed_run(
        self,
        run_id: str,
        mode: str,
        input_summary: str,
        limit: int,
        started_at: datetime,
        error_code: str,
    ) -> CollectionRun:
        code = (
            error_code if error_code in _COLLECTOR_ERRORS else self._bridge_error_code(error_code)
        )
        return CollectionRun(
            run_id=run_id,
            mode=mode,
            input_summary=input_summary,
            requested_count=limit,
            actual_count=0,
            started_at=started_at,
            finished_at=self._now(),
            status=RunStatus.FAILED,
            error_code=code,
            notes=[],
        )

    @staticmethod
    def _bridge_error_code(error: BridgeErrorCode | str | None) -> str:
        if isinstance(error, BridgeErrorCode):
            return error.value
        if isinstance(error, str):
            try:
                return BridgeErrorCode(error).value
            except ValueError:
                pass
        return BridgeErrorCode.UPSTREAM_ERROR.value

    def _account_error_code(
        self,
        bridge_error: BridgeErrorCode | str | None,
        candidate_attempts: Sequence[CandidateAttempt],
        attempts_or_count_incomplete: bool,
        media_failed: bool,
        profile_incomplete: bool,
    ) -> str | None:
        code = self._bridge_error_code(bridge_error) if bridge_error is not None else None
        if code == BridgeErrorCode.MEDIA_SCOPE_INVALID.value or any(
            getattr(attempt, "reason", None) == "media_scope_invalid"
            for attempt in candidate_attempts
        ):
            return BridgeErrorCode.MEDIA_SCOPE_INVALID.value
        if attempts_or_count_incomplete or code in {
            BridgeErrorCode.DETAIL_ID_MISMATCH.value,
            BridgeErrorCode.DETAIL_UNAVAILABLE.value,
        }:
            return BridgeErrorCode.DETAIL_UNAVAILABLE.value
        if media_failed or code == BridgeErrorCode.MEDIA_PARTIAL.value:
            return "media_unavailable"
        if profile_incomplete or code == BridgeErrorCode.PROFILE_UNAVAILABLE.value:
            return BridgeErrorCode.PROFILE_UNAVAILABLE.value
        return code

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        return value


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    for part in path.parts:
        if part in {path.anchor, "."}:
            continue
        current /= part
        if current.is_symlink():
            return True
    return False


@dataclass(frozen=True)
class _AssetSnapshot:
    mime_type: MediaMime
    size_bytes: int
    sha256: str


def _persist_media_candidate(
    candidate: StagedMediaCandidate, note_id: str, staging_dir: Path, assets_dir: Path
) -> NoteMediaSlot:
    """Persist exactly one V2 slot after an independent no-follow verification pass.

    Unavailable slots are evidence-only and intentionally never open either directory.
    A downloaded slot is copied to its already-bound public filename and then reopened
    from the asset directory before a ``LocalAsset`` is constructed.
    """
    if candidate.note_id != note_id:
        raise ValueError("media candidate note ID does not match note")
    if (
        candidate.role not in {"image", "video_cover", "video"}
        or type(candidate.position) is not int
        or candidate.position < 1
        or type(candidate.status) is not str
        or (candidate.duration_ms is not None and (
            type(candidate.duration_ms) is not int or candidate.duration_ms < 1
        ))
    ):
        raise ValueError("media candidate slot is malformed")
    if candidate.status != "downloaded":
        if (
            candidate.status not in {"missing", "rejected"}
            or candidate.staging_name is not None
            or candidate.mime_type is not None
            or candidate.size_bytes is not None
            or candidate.sha256 is not None
            or candidate.missing_reason is None
        ):
            raise ValueError("unavailable media slot is malformed")
        return NoteMediaSlot(
            note_id=note_id,
            role=candidate.role,
            position=candidate.position,
            status=candidate.status,
            duration_ms=candidate.duration_ms,
            missing_reason=candidate.missing_reason,
        )

    if (
        candidate.role not in {"image", "video_cover", "video"}
        or not isinstance(candidate.staging_name, str)
        or not isinstance(candidate.mime_type, str)
        or type(candidate.size_bytes) is not int
        or not isinstance(candidate.sha256, str)
        or candidate.missing_reason is not None
        or not _SHA256.fullmatch(candidate.sha256)
    ):
        raise ValueError("downloaded media slot is malformed")
    extension = mime_extension(candidate.mime_type)
    maximum = MAX_VIDEO_BYTES if candidate.role == "video" else MAX_IMAGE_BYTES
    expected_name = _media_asset_name(note_id, candidate.role, candidate.position, extension)
    if (
        extension is None
        or candidate.staging_name != expected_name
        or Path(candidate.staging_name).name != candidate.staging_name
        or candidate.size_bytes <= 0
        or candidate.size_bytes > maximum
        or (candidate.role != "video" and candidate.mime_type not in {
            "image/jpeg", "image/png", "image/webp"
        })
        or (candidate.role == "video" and candidate.mime_type not in {"video/mp4", "video/webm"})
        or (candidate.role != "video" and candidate.duration_ms is not None)
    ):
        raise ValueError("downloaded media slot does not bind its file")

    try:
        staging_fd = os.open(staging_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("unable to open media directories") from error
    try:
        assets_fd = os.open(assets_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        os.close(staging_fd)
        raise ValueError("unable to open media directories") from error
    created_target = False
    persisted_target = False
    try:
        try:
            source_fd = os.open(
                candidate.staging_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=staging_fd
            )
        except OSError as error:
            raise ValueError("unable to open media source") from error
        try:
            target_fd = os.open(
                expected_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=assets_fd,
            )
        except OSError as error:
            os.close(source_fd)
            raise ValueError("unable to open media destination") from error
        created_target = True
        try:
            snapshot = _copy_and_verify_media(source_fd, target_fd, candidate)
            if snapshot is None:
                raise ValueError("staged media revalidation failed")
            os.fsync(target_fd)
        finally:
            os.close(target_fd)
            os.close(source_fd)

        # Do not trust the descriptor used for the copy: reopen the published name
        # with O_NOFOLLOW and rehash/sniff/parse it at the persistence boundary.
        try:
            verified_fd = os.open(
                expected_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=assets_fd
            )
        except OSError as error:
            raise ValueError("unable to reopen persisted media") from error
        try:
            published = _snapshot_media_file(verified_fd, candidate.mime_type, maximum)
        finally:
            os.close(verified_fd)
        if published != snapshot:
            raise ValueError("persisted media revalidation failed")
        os.fsync(assets_fd)
        try:
            os.unlink(candidate.staging_name, dir_fd=staging_fd)
        except OSError:
            # The run-owned staging directory is removed at the outer boundary.
            pass
        asset = LocalAsset(
            local_path=f"assets/{expected_name}",
            mime_type=published.mime_type,
            size_bytes=published.size_bytes,
            sha256=published.sha256,
        )
        persisted_target = True
        return NoteMediaSlot(
            note_id=note_id,
            role=candidate.role,
            position=candidate.position,
            status="downloaded",
            asset=asset,
            duration_ms=candidate.duration_ms,
        )
    finally:
        if created_target and not persisted_target:
            try:
                os.unlink(expected_name, dir_fd=assets_fd)
            except OSError:
                pass
        os.close(assets_fd)
        os.close(staging_fd)


def _media_asset_name(note_id: str, role: str, position: int, extension: str | None) -> str:
    if extension is None:
        return ""
    if role == "image":
        return f"{note_id}-image-{position:03d}{extension}"
    if role in {"video_cover", "video"} and position == 1:
        return f"{note_id}-{role.replace('_', '-')}{extension}"
    return ""


def _ordered_media_candidates(
    candidates: list[StagedMediaCandidate],
) -> list[StagedMediaCandidate]:
    """Normalize the persisted V2 manifest to its stable role/position order."""
    return sorted(candidates, key=lambda item: (_MEDIA_ROLE_ORDER[item.role], item.position))


def _copy_and_verify_media(
    source_fd: int, target_fd: int, candidate: StagedMediaCandidate
) -> _AssetSnapshot | None:
    """Copy a regular staged file while binding its bytes to the child manifest."""
    assert candidate.mime_type is not None
    assert candidate.size_bytes is not None
    assert candidate.sha256 is not None
    maximum = MAX_VIDEO_BYTES if candidate.role == "video" else MAX_IMAGE_BYTES
    try:
        initial = os.fstat(source_fd)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_size != candidate.size_bytes
            or initial.st_size <= 0
            or initial.st_size > maximum
        ):
            return None
        digest = hashlib.sha256()
        head = bytearray()
        size_bytes = 0
        while chunk := os.read(source_fd, 64 * 1024):
            if len(head) < 64:
                head.extend(chunk[: 64 - len(head)])
            digest.update(chunk)
            size_bytes += len(chunk)
            _write_all(target_fd, chunk)
        final = os.fstat(source_fd)
        if (
            final.st_ino != initial.st_ino
            or final.st_size != candidate.size_bytes
            or size_bytes != candidate.size_bytes
            or digest.hexdigest() != candidate.sha256
            or sniff_media_mime(bytes(head)) != candidate.mime_type
        ):
            return None
        with os.fdopen(os.dup(source_fd), "rb") as source:
            if not validate_media_container(source, candidate.mime_type, size_bytes):
                return None
        return _AssetSnapshot(candidate.mime_type, size_bytes, digest.hexdigest())
    except OSError:
        return None


def _snapshot_media_file(file_fd: int, expected_mime: MediaMime, maximum: int) -> _AssetSnapshot | None:
    try:
        initial = os.fstat(file_fd)
        if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= maximum:
            return None
        os.lseek(file_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        head = bytearray()
        size_bytes = 0
        while chunk := os.read(file_fd, 64 * 1024):
            if len(head) < 64:
                head.extend(chunk[: 64 - len(head)])
            digest.update(chunk)
            size_bytes += len(chunk)
        final = os.fstat(file_fd)
        if (
            final.st_ino != initial.st_ino
            or final.st_size != initial.st_size
            or size_bytes != initial.st_size
            or sniff_media_mime(bytes(head)) != expected_mime
        ):
            return None
        with os.fdopen(os.dup(file_fd), "rb") as stream:
            if not validate_media_container(stream, expected_mime, size_bytes):
                return None
        return _AssetSnapshot(expected_mime, size_bytes, digest.hexdigest())
    except OSError:
        return None


def _compatibility_cover(slots: list[NoteMediaSlot]) -> LocalAsset | None:
    for role in ("image", "video_cover"):
        for slot in slots:
            if slot.role == role and slot.position == 1 and slot.status == "downloaded":
                return slot.asset
    return None


def _media_missing_fields(
    slots: list[NoteMediaSlot], *, discovery_truncated: bool
) -> list[str]:
    """Expose one stable, slot-specific missing field for every unavailable media item."""
    fields: list[str] = []
    for slot in slots:
        if slot.status == "downloaded":
            continue
        if slot.role == "image":
            fields.append(f"media.image.{slot.position:03d}")
        elif slot.role == "video_cover":
            fields.append("media.video_cover")
        else:
            fields.append("media.video")
    if discovery_truncated:
        fields.append("media.discovery_after.100")
    return _deduplicate_fields(fields)


def _merge_missing_fields(*field_lists: list[str]) -> list[str]:
    return _deduplicate_fields([field for fields in field_lists for field in fields])


def _deduplicate_fields(fields: list[str]) -> list[str]:
    return list(dict.fromkeys(fields))


def _copy_and_verify_candidate(
    source_fd: int, target_fd: int, candidate: CoverCandidate
) -> _AssetSnapshot | None:
    try:
        initial = os.fstat(source_fd)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_size != candidate.size_bytes
            or initial.st_size <= 0
            or initial.st_size > MAX_MEDIA_BYTES
        ):
            return None
        digest = hashlib.sha256()
        head = b""
        size_bytes = 0
        while chunk := os.read(source_fd, 64 * 1024):
            if len(head) < 12:
                head += chunk[: 12 - len(head)]
            digest.update(chunk)
            size_bytes += len(chunk)
            _write_all(target_fd, chunk)
        final = os.fstat(source_fd)
        mime_type = _magic_mime(head)
        if (
            final.st_ino != initial.st_ino
            or final.st_size != candidate.size_bytes
            or size_bytes != candidate.size_bytes
            or digest.hexdigest() != candidate.sha256
            or mime_type != candidate.mime_type
        ):
            return None
        return _AssetSnapshot(
            mime_type=candidate.mime_type,
            size_bytes=size_bytes,
            sha256=digest.hexdigest(),
        )
    except OSError:
        return None


def _snapshot_file(file_fd: int) -> _AssetSnapshot | None:
    try:
        initial = os.fstat(file_fd)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_size <= 0
            or initial.st_size > MAX_MEDIA_BYTES
        ):
            return None
        os.lseek(file_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        head = b""
        size_bytes = 0
        while chunk := os.read(file_fd, 64 * 1024):
            if len(head) < 12:
                head += chunk[: 12 - len(head)]
            digest.update(chunk)
            size_bytes += len(chunk)
        final = os.fstat(file_fd)
        mime_type = _magic_mime(head)
        if (
            final.st_ino != initial.st_ino
            or final.st_size != initial.st_size
            or size_bytes != initial.st_size
            or mime_type is None
        ):
            return None
        return _AssetSnapshot(mime_type=mime_type, size_bytes=size_bytes, sha256=digest.hexdigest())
    except OSError:
        return None


def _write_all(file_fd: int, value: bytes) -> None:
    written = 0
    while written < len(value):
        result = os.write(file_fd, value[written:])
        if result <= 0:
            raise OSError("unable to write local asset")
        written += result


def _magic_mime(head: bytes) -> Literal["image/jpeg", "image/png", "image/webp"] | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _parse_bridge_notes(value: object) -> list[BridgeNote]:
    if type(value) is not list:
        raise ValueError("invalid note list")
    notes: list[BridgeNote] = []
    for raw_note in value:
        if type(raw_note) is not dict:
            raise ValueError("invalid note")
        restored = dict(raw_note)
        for field in _OMITTABLE_NOTE_FIELDS:
            restored.setdefault(field, None)
        notes.append(BridgeNote.model_validate(restored))
    return notes


def _is_safe_text(value: str) -> bool:
    return is_safe_retained_text(value) and _HTTP_FRAGMENT_URL.search(value) is None


def _is_safe_note(note: BridgeNote) -> bool:
    text_fields = (note.title, note.body, note.note_type, note.author_name)
    if not all(value is None or _is_safe_text(value) for value in text_fields):
        return False
    if not all(_is_safe_text(tag) for tag in note.tags):
        return False
    if note.author_id is None:
        if note.author_profile_url is not None:
            return False
    else:
        if not _SAFE_ID.fullmatch(note.author_id):
            return False
        if note.author_profile_url is not None:
            try:
                profile = parse_xhs_url(note.author_profile_url)
            except ValueError:
                return False
            if profile.object_type != "profile" or profile.object_id != note.author_id:
                return False
    return _metrics_are_safe(
        note.metrics.likes, note.metrics.collects, note.metrics.comments, note.metrics.shares
    )


def _is_safe_account(account: AccountRecord | None) -> bool:
    if account is None:
        return True
    if not all(value is None or _is_safe_text(value) for value in (account.name, account.bio)):
        return False
    if not _metrics_are_safe(account.note_count, account.follower_count):
        return False
    return all(
        _is_safe_text(label) and _metrics_are_safe(metric)
        for label, metric in account.platform_metrics.items()
    )


def _metrics_are_safe(*metrics: object) -> bool:
    for metric in metrics:
        raw_value = getattr(metric, "raw_value", None)
        if raw_value is not None and (
            not isinstance(raw_value, str) or not _is_safe_text(raw_value)
        ):
            return False
    return True


def _account_payload_matches(payload: AccountPayload, requested_account_id: str) -> bool:
    account = payload.account
    if account is not None:
        if account.account_id is not None and account.account_id != requested_account_id:
            return False
        if account.profile_url is not None:
            try:
                profile = parse_xhs_url(account.profile_url)
            except ValueError:
                return False
            if profile.object_type != "profile" or profile.object_id != requested_account_id:
                return False
    return all(
        note.author_id is None or note.author_id == requested_account_id for note in payload.notes
    )
