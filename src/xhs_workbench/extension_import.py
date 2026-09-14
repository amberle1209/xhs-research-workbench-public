"""Fail-closed selection, local media staging, and bundle writing for the extension."""

from __future__ import annotations

import base64
import binascii
import ctypes
import errno
import hashlib
import os
import re
import secrets
import stat
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from xhs_workbench.extension_models import (
    ExtensionBeginJob,
    ExtensionCandidateSnapshot,
    ExtensionCandidateUnavailable,
    ExtensionMediaBegin,
    ExtensionMediaChunk,
    ExtensionMediaEnd,
    ExtensionMediaMissing,
    ExtensionTimeEvidence,
)
from xhs_workbench.media import (
    IMAGE_MIME_TYPES,
    MAX_IMAGE_BYTES,
    MAX_NOTE_MEDIA_BYTES,
    MAX_RUN_MEDIA_BYTES,
    MAX_VIDEO_BYTES,
    MIME_EXTENSIONS,
    VIDEO_MIME_TYPES,
    MediaMime,
    MediaMissingReason,
    MediaRole,
    sniff_media_mime,
    validate_media_container,
)
from xhs_workbench.models import (
    CollectionRun,
    ExtensionDetailOutcome,
    ExtensionSearchRunErrorCode,
    ExtensionSearchRunSummary,
    ExtensionSelectionEntry,
    ExtensionSelectionSummary,
    LocalAsset,
    NoteMediaSlot,
    NoteMetrics,
    NoteRecord,
    RunStatus,
    TimeEvidence,
)
from xhs_workbench.renderer import write_result_bundle_at

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ISO_VISIBLE_PUBLICATION = re.compile(r"发布于 ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})")
_CHINESE_VISIBLE_PUBLICATION = re.compile(r"([0-9]{4})年([0-9]{2})月([0-9]{2})日 ([0-9]{2}):([0-9]{2})")
_RENAME_EXCL = 0x00000004


class ExtensionJobState(str, Enum):
    """One-way lifecycle states shared with later media staging work."""

    OPEN = "open"
    SCAN_FINISHED = "scan_finished"
    MEDIA_RECEIVING = "media_receiving"
    FINISHED = "finished"
    STOPPED = "stopped"


class ExtensionJob:
    """One host-owned extension run, from candidate selection to local bundle."""

    def __init__(
        self,
        run_id: str,
        output_dir: Path,
        request: ExtensionBeginJob,
        *,
        output_root_identity: tuple[int, int] | None = None,
    ) -> None:
        if not _safe_host_run_id(run_id):
            raise ValueError("host run ID is unsafe")
        self.run_id = run_id
        (
            self.output_dir,
            self._output_parent_fd,
            self._run_fd,
        ) = _create_run_directory(output_dir, expected_parent_identity=output_root_identity)
        self._run_name = self.output_dir.name
        self._run_identity = _directory_identity(self._run_fd)
        self._assets_dir = self.output_dir / "assets"
        self._staging_dir = self.output_dir / ".staging"
        self._assets_fd = -1
        self._assets_identity: tuple[int, int] | None = None
        self._staging_fd = -1
        self._staging_identity: tuple[int, int] | None = None
        try:
            self._assets_fd = _create_private_directory(self._run_fd, "assets")
            self._assets_identity = _directory_identity(self._assets_fd)
            self._staging_fd = _create_private_directory(self._run_fd, ".staging")
            self._staging_identity = _directory_identity(self._staging_fd)
        except OSError as error:
            try:
                _remove_owned_run(
                    self._output_parent_fd,
                    self._run_name,
                    self._run_identity,
                    self._run_fd,
                    _owned_child_identities(self),
                )
            except OSError:
                pass
            _close_directory_fds(
                getattr(self, "_staging_fd", -1),
                getattr(self, "_assets_fd", -1),
                self._run_fd,
                self._output_parent_fd,
            )
            raise ValueError("output directory must be a fresh private run directory") from error
        self._owns_output_dir = True
        self.request = request
        self.state = ExtensionJobState.OPEN
        self._candidates: list[ExtensionCandidateSnapshot] = []
        self._unavailable_candidates: list[ExtensionCandidateUnavailable] = []
        self._source_positions: set[int] = set()
        self._note_ids: set[str] = set()
        self.selected_note_ids: tuple[str, ...] = ()
        self.selected_candidates: tuple[ExtensionCandidateSnapshot, ...] = ()
        self.selection_status: Literal["complete", "partial"] | None = None
        self.selection_summary: ExtensionSelectionSummary | None = None
        self._page_terminal_error: ExtensionSearchRunErrorCode | None = None
        self._page_scroll_rounds: int | None = None
        self._page_sort_label: str | None = None
        self._slot_results: dict[tuple[str, MediaRole, int], NoteMediaSlot] = {}
        self._active_media: _MediaTransfer | None = None
        self._staging_files: dict[str, tuple[int, int]] = {}
        self._used_sequences: set[int] = set()
        self._next_media_sequence = 1
        self._downloaded_note_bytes: dict[str, int] = {}
        self._downloaded_run_bytes = 0
        self._cleanup_residue = False

    @classmethod
    def begin(cls, request: ExtensionBeginJob) -> ExtensionJob:
        """Compatibility helper for selection-only callers.

        The production host uses the explicit constructor with a host-owned
        run directory. Selection tests use a private ephemeral directory that
        is removed once their process exits.
        """
        import tempfile

        parent = Path(tempfile.mkdtemp(prefix="xhs-selection-")).resolve(strict=True)
        return cls("selection_only", parent / "run", request)

    @property
    def candidate_scanned_count(self) -> int:
        """The finite ledger count, including detail facts without snapshots."""
        return len(self._candidates) + len(self._unavailable_candidates)

    def add_candidate(self, candidate: ExtensionCandidateSnapshot) -> None:
        """Record a scan candidate or enrich the next already-frozen page-order rank."""
        if (
            self.state in {ExtensionJobState.SCAN_FINISHED, ExtensionJobState.MEDIA_RECEIVING}
            and self.request.selection_order == "page_order"
        ):
            self._replace_frozen_page_detail(candidate)
            return
        if self.state is not ExtensionJobState.OPEN:
            raise ValueError("candidate scan is not open")
        if candidate.job_id != self.request.job_id:
            raise ValueError("candidate job ID does not match")
        if candidate.source_position in self._source_positions:
            raise ValueError("duplicate candidate source position")
        if candidate.note_id in self._note_ids:
            raise ValueError("duplicate candidate note ID")
        if self.candidate_scanned_count >= self.request.candidate_scan_limit:
            raise ValueError("candidate scan limit exceeded")
        if (
            self.request.collection_surface == "extension_current"
            and self._candidates
        ):
            raise ValueError("current-note job accepts only one candidate")
        self._candidates.append(candidate)
        self._source_positions.add(candidate.source_position)
        self._note_ids.add(candidate.note_id)

    def add_candidate_unavailable(self, candidate: ExtensionCandidateUnavailable) -> None:
        """Retain one safe batch detail failure without retaining page diagnostics."""
        if (
            self.state in {ExtensionJobState.SCAN_FINISHED, ExtensionJobState.MEDIA_RECEIVING}
            and self.request.selection_order == "page_order"
        ):
            if candidate.job_id != self.request.job_id:
                raise ValueError("candidate job ID does not match")
            entry = self._next_page_detail_entry()
            if (
                candidate.reason != "detail_unavailable"
                or entry.note_id != candidate.note_id
                or entry.source_position != candidate.source_position
            ):
                raise ValueError("page-order detail failure must match the next frozen rank")
            self.record_page_detail_outcome(candidate.note_id, "detail_unavailable")
            return
        if self.state is not ExtensionJobState.OPEN:
            raise ValueError("candidate scan is not open")
        if self.request.collection_surface == "extension_current":
            raise ValueError("current-note job cannot retain a batch candidate failure")
        if self.request.selection_order == "page_order" and candidate.reason not in {
            "sponsored",
            "invalid_card",
        }:
            raise ValueError("page-order scan exclusions must be sponsored or invalid cards")
        if candidate.job_id != self.request.job_id:
            raise ValueError("candidate job ID does not match")
        if candidate.source_position in self._source_positions:
            raise ValueError("duplicate candidate source position")
        if candidate.note_id in self._note_ids:
            raise ValueError("duplicate candidate note ID")
        if self.candidate_scanned_count >= self.request.candidate_scan_limit:
            raise ValueError("candidate scan limit exceeded")
        self._unavailable_candidates.append(candidate)
        self._source_positions.add(candidate.source_position)
        self._note_ids.add(candidate.note_id)

    def _next_page_detail_entry(self) -> ExtensionSelectionEntry:
        if self.request.selection_order != "page_order" or self.selection_summary is None:
            raise ValueError("page-order detail outcome requires a finished page-order scan")
        if self._page_terminal_error is not None:
            raise ValueError("page-order terminal condition prevents later detail outcomes")
        entry = next(
            (
                item
                for item in sorted(
                    (item for item in self.selection_summary.entries if item.selection_rank is not None),
                    key=lambda item: item.selection_rank or 0,
                )
                if item.detail_outcome is None
            ),
            None,
        )
        if entry is None:
            raise ValueError("page-order detail outcome has no unfinished frozen rank")
        return entry

    def _replace_frozen_page_detail(self, candidate: ExtensionCandidateSnapshot) -> None:
        """Accept one detail projection only for the next immutable selected rank."""
        if candidate.job_id != self.request.job_id:
            raise ValueError("candidate job ID does not match")
        entry = self._next_page_detail_entry()
        if entry.note_id != candidate.note_id or entry.source_position != candidate.source_position:
            raise ValueError("page-order detail snapshot must match the next frozen rank")
        candidate_index = next(
            (index for index, item in enumerate(self._candidates) if item.note_id == candidate.note_id),
            None,
        )
        if candidate_index is None:
            raise ValueError("page-order detail snapshot must match a frozen candidate")
        self._candidates[candidate_index] = candidate
        self.selected_candidates = tuple(
            candidate if item.note_id == candidate.note_id else item
            for item in self.selected_candidates
        )
        for slot in candidate.media_slots:
            key = _slot_key(candidate.note_id, slot.role, slot.position)
            if key in self._slot_results:
                raise ValueError("page-order detail slot is already closed")
            self._slot_results[key] = _unavailable_slot(
                candidate.note_id, slot.role, slot.position, "missing", "source_not_exposed"
            )
        self.record_page_detail_outcome(candidate.note_id, "enriched")

    def record_page_detail_outcome(self, note_id: str, outcome: ExtensionDetailOutcome) -> None:
        """Record one finite detail result without changing the frozen selection."""
        if self.request.selection_order != "page_order" or self.selection_summary is None:
            raise ValueError("page-order detail outcome requires a finished page-order scan")
        if self.state not in {ExtensionJobState.SCAN_FINISHED, ExtensionJobState.MEDIA_RECEIVING}:
            raise ValueError("page-order detail outcome requires an open finished scan")
        if self._page_terminal_error is not None:
            raise ValueError("page-order terminal condition prevents later detail outcomes")
        if outcome not in {"enriched", "detail_unavailable"}:
            raise ValueError("terminal page-order outcomes must use the terminal transition")
        if note_id not in self.selected_note_ids:
            raise ValueError("page-order detail outcome requires a selected note")
        next_entry = self._next_page_detail_entry()
        if next_entry.note_id != note_id:
            raise ValueError("page-order detail outcomes must follow the next frozen rank")
        entries: list[ExtensionSelectionEntry] = []
        for entry in self.selection_summary.entries:
            if entry.note_id == note_id:
                if entry.detail_outcome is not None:
                    raise ValueError("page-order detail outcome is already recorded")
                entries.append(entry.model_copy(update={"detail_outcome": outcome}))
            else:
                entries.append(entry)
        self.selection_summary = self.selection_summary.model_copy(update={"entries": entries})

    def set_page_terminal_error(
        self, error_code: Literal["login_required", "challenge_detected", "stopped"]
    ) -> None:
        """Latch one terminal page-order condition without discarding retained results."""
        if self.request.selection_order != "page_order" or self.selection_summary is None:
            raise ValueError("page-order terminal error requires a finished page-order scan")
        if self.state not in {ExtensionJobState.SCAN_FINISHED, ExtensionJobState.MEDIA_RECEIVING}:
            raise ValueError("page-order terminal error requires an open finished scan")
        if self._page_terminal_error is not None:
            raise ValueError("page-order terminal error is already recorded")
        entries = [
            entry.model_copy(update={"detail_outcome": error_code})
            if entry.selection_rank is not None and entry.detail_outcome is None
            else entry
            for entry in self.selection_summary.entries
        ]
        if entries == self.selection_summary.entries:
            raise ValueError("page-order terminal error requires an unfinished selected rank")
        self.selection_summary = self.selection_summary.model_copy(update={"entries": entries})
        self._page_terminal_error = error_code

    def fail_page_order_integrity(
        self, error_code: Literal["structural_error", "route_mismatch", "identity_mismatch"]
    ) -> CollectionRun:
        """Persist a zero-attribution failed bundle when frozen identity is no longer trustworthy."""
        if self.request.selection_order != "page_order" or self.selection_summary is None:
            raise ValueError("page-order integrity failure requires a finished scan")
        if self.state not in {ExtensionJobState.SCAN_FINISHED, ExtensionJobState.MEDIA_RECEIVING}:
            raise ValueError("page-order integrity failure requires an open finished scan")
        entries = [
            entry.model_copy(
                update={
                    "outcome": "excluded",
                    "reason": entry.reason if entry.inclusion == "excluded" else "integrity_failure",
                    "selection_rank": None,
                    "selection_basis": "page_order",
                    "inclusion": "excluded",
                    "detail_outcome": None,
                }
            )
            for entry in self.selection_summary.entries
        ]
        self.selection_summary = self.selection_summary.model_copy(
            update={"selected_count": 0, "entries": entries}
        )
        self.selected_note_ids = ()
        self.selected_candidates = ()
        self.selection_status = "partial"
        self._page_terminal_error = error_code
        return self.finish_job()

    def finish_scan(
        self, *, scroll_rounds: int | None = None, sort_label: str | None = None
    ) -> ExtensionSelectionSummary | None:
        """Close a scan once and deterministically produce its batch selection."""
        if self.state is not ExtensionJobState.OPEN:
            raise ValueError("candidate scan can only finish once")
        if self.request.selection_order == "page_order":
            # The host enforces this wire requirement.  Keep the direct
            # selection helper compatible for model-level callers.
            if scroll_rounds is None:
                scroll_rounds = 0
            if type(scroll_rounds) is not int or scroll_rounds < 0 or scroll_rounds > 2:
                raise ValueError("page-order scan requires observed bounded scroll rounds")
            self._page_scroll_rounds = scroll_rounds
            self._page_sort_label = sort_label
        if self.request.collection_surface == "extension_current":
            if len(self._candidates) != 1:
                raise ValueError("current-note job requires exactly one candidate")
            self.state = ExtensionJobState.SCAN_FINISHED
            self._finish_current_note_scan()
            return None
        self.state = ExtensionJobState.SCAN_FINISHED
        summary = self._finish_batch_scan()
        self.selection_summary = summary
        return summary

    def begin_media(self, message: ExtensionMediaBegin) -> NoteMediaSlot | None:
        """Reserve one declared selected slot and create an opaque private file."""
        self._require_media_open()
        self._require_job_id(message.job_id)
        key = _slot_key(message.note_id, message.role, message.position)
        if key not in self._declared_selected_slots():
            raise ValueError("media slot is not declared by a selected note")
        if key in self._slot_results or self._active_media is not None:
            raise ValueError("media slot is already closed or receiving")
        if message.sequence != self._next_media_sequence or message.sequence in self._used_sequences:
            raise ValueError("media sequence is not monotonic")
        role_limit = _slot_maximum(message.role)
        note_remaining = MAX_NOTE_MEDIA_BYTES - self._downloaded_note_bytes.get(message.note_id, 0)
        run_remaining = MAX_RUN_MEDIA_BYTES - self._downloaded_run_bytes
        maximum = min(message.size_limit_bytes, role_limit, note_remaining, run_remaining)
        overflow_reason = _transfer_overflow_reason(
            message.size_limit_bytes, role_limit, note_remaining, run_remaining
        )
        if maximum <= 0:
            self._used_sequences.add(message.sequence)
            self._next_media_sequence += 1
            rejected = _unavailable_slot(
                message.note_id, message.role, message.position, "rejected", overflow_reason
            )
            self._slot_results[key] = rejected
            self.state = ExtensionJobState.MEDIA_RECEIVING
            return rejected
        staging_name = f".{secrets.token_hex(24)}.staging"
        staging_fd, staging_identity = _create_private_file(self._staging_fd, staging_name)
        self._used_sequences.add(message.sequence)
        self._next_media_sequence += 1
        self._active_media = _MediaTransfer(
            key=key,
            sequence=message.sequence,
            maximum_bytes=maximum,
            overflow_reason=overflow_reason,
            staging_name=staging_name,
            staging_identity=staging_identity,
            file_fd=staging_fd,
        )
        self._staging_files[staging_name] = staging_identity
        self.state = ExtensionJobState.MEDIA_RECEIVING
        return None

    def append_media_chunk(self, message: ExtensionMediaChunk) -> None:
        """Append exactly one ordered, bounded base64 chunk to the active slot."""
        active = self._require_active_media(message.job_id, message.note_id, message.role, message.position, message.sequence)
        if message.chunk_index != active.next_chunk_index:
            raise ValueError("media chunk index is out of order")
        try:
            chunk = base64.b64decode(message.data_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            self._reject_active("mime_mismatch")
            raise ValueError("media chunk is not valid base64") from error
        if not chunk or len(chunk) > 256 * 1024:
            self._reject_active("size_limit")
            raise ValueError("media chunk exceeds byte limit")
        if active.size_bytes + len(chunk) > active.maximum_bytes:
            self._reject_active(active.overflow_reason)
            raise ValueError("media size limit exceeded")
        try:
            _write_all(active.file_fd, chunk)
        except OSError as error:
            self.discard()
            raise ValueError("media staging write failed") from error
        active.size_bytes += len(chunk)
        active.next_chunk_index += 1

    def finish_media(self, message: ExtensionMediaEnd) -> NoteMediaSlot:
        """Verify an active slot and atomically publish its fixed local asset name."""
        active = self._require_active_media(message.job_id, message.note_id, message.role, message.position, message.sequence)
        try:
            if not _mime_matches_role(message.role, message.mime_type):
                return self._reject_active("mime_mismatch")
            os.fsync(active.file_fd)
            os.close(active.file_fd)
            active.file_fd = -1
            snapshot = _snapshot_staged_file(
                self._staging_fd,
                active.staging_name,
                active.staging_identity,
                message.mime_type,
                active.maximum_bytes,
            )
            if snapshot is None or snapshot.sha256 != message.sha256:
                return self._reject_active("mime_mismatch")
            if snapshot.size_bytes > active.maximum_bytes:
                return self._reject_active(active.overflow_reason)
            note_bytes = self._downloaded_note_bytes.get(message.note_id, 0) + snapshot.size_bytes
            if note_bytes > MAX_NOTE_MEDIA_BYTES:
                return self._reject_active("note_budget")
            if self._downloaded_run_bytes + snapshot.size_bytes > MAX_RUN_MEDIA_BYTES:
                return self._reject_active("run_budget")
            asset_name = _asset_name(message.note_id, message.role, message.position, message.mime_type)
            if not asset_name:
                return self._reject_active("mime_mismatch")
            active.published_identity = _publish_private_file(
                self._staging_fd,
                self._assets_fd,
                active.staging_name,
                asset_name,
                active.staging_identity,
            )
            active.published_name = asset_name
            try:
                asset = LocalAsset(
                    local_path=f"assets/{asset_name}",
                    mime_type=message.mime_type,
                    size_bytes=snapshot.size_bytes,
                    sha256=snapshot.sha256,
                )
                slot = NoteMediaSlot(
                    note_id=message.note_id,
                    role=message.role,
                    position=message.position,
                    status="downloaded",
                    asset=asset,
                )
            except ValueError:
                return self._reject_active("mime_mismatch")
            self._slot_results[active.key] = slot
            self._downloaded_note_bytes[message.note_id] = note_bytes
            self._downloaded_run_bytes += snapshot.size_bytes
            self._active_media = None
            return slot
        except OSError as error:
            self.discard()
            raise ValueError("media filesystem failure") from error
        except ValueError as error:
            # Any unexpected helper/model failure is terminal once a transfer exists.
            self.discard()
            raise ValueError("media finalization failure") from error

    def mark_media_missing(self, message: ExtensionMediaMissing) -> NoteMediaSlot:
        """Close one declared selected slot without accepting bytes."""
        self._require_media_open()
        self._require_job_id(message.job_id)
        key = _slot_key(message.note_id, message.role, message.position)
        if key not in self._declared_selected_slots() or key in self._slot_results:
            raise ValueError("media slot is not open and declared")
        if self._active_media is not None:
            raise ValueError("active media slot must be closed before marking missing")
        slot = _unavailable_slot(message.note_id, message.role, message.position, "missing", message.reason)
        self._slot_results[key] = slot
        self.state = ExtensionJobState.MEDIA_RECEIVING
        return slot

    def finish_job(self) -> CollectionRun:
        """Render a bundle only when each selected declared slot is finite."""
        self._require_media_open()
        if self._active_media is not None:
            self.discard()
            raise ValueError("cannot finish an open media slot")
        expected = self._declared_selected_slots()
        if set(self._slot_results) != expected:
            self.discard()
            raise ValueError("every selected declared media slot must be closed")
        if self.request.selection_order == "page_order":
            assert self.selection_summary is not None
            if any(
                entry.selection_rank is not None and entry.detail_outcome is None
                for entry in self.selection_summary.entries
            ):
                raise ValueError("page-order selected entries require an explicit detail outcome")
        try:
            # Darwin exposes no deletion operation bound to this held directory
            # inode.  Retain opaque staging residue rather than turn a final
            # lstat-to-rmdir gap into a replacement-data deletion primitive.
            if (
                self._staging_identity is None
                or _directory_identity(self._staging_fd) != self._staging_identity
            ):
                raise ValueError("staging directory changed")
            staging_metadata = os.lstat(".staging", dir_fd=self._run_fd)
            if not _matches_owned_entry(staging_metadata, self._staging_identity, "directory"):
                raise ValueError("staging directory changed")
            _verify_opaque_staging_files(self._staging_fd, self._staging_files)
            os.fsync(self._staging_fd)
            os.close(self._staging_fd)
            self._staging_fd = -1
            page_status: RunStatus | None = None
            page_error: ExtensionSearchRunErrorCode | None = None
            if self.request.selection_order == "page_order":
                assert self.selection_summary is not None
                outcomes = [
                    entry.detail_outcome
                    for entry in self.selection_summary.entries
                    if entry.selection_rank is not None
                ]
                if self._page_terminal_error is not None:
                    page_error = self._page_terminal_error
                elif "detail_unavailable" in outcomes:
                    page_error = "detail_unavailable"
                if page_error == "stopped":
                    page_status = RunStatus.STOPPED
                elif page_error in {"structural_error", "route_mismatch", "identity_mismatch"}:
                    page_status = RunStatus.FAILED
                elif page_error in {"detail_unavailable", "login_required", "challenge_detected"}:
                    page_status = RunStatus.PARTIAL
            notes = [self._record_for_candidate(candidate) for candidate in self.selected_candidates]
            all_downloaded = all(slot.status == "downloaded" for slot in self._slot_results.values())
            status = page_status or (
                RunStatus.COMPLETE
                if self.selection_status == "complete" and all_downloaded
                else RunStatus.PARTIAL
            )
            now = datetime.now(_SHANGHAI)
            search_run = None
            if self.request.selection_order == "page_order":
                summary = self.selection_summary
                assert summary is not None
                assert self._page_scroll_rounds is not None
                search_run = ExtensionSearchRunSummary(
                    source_page_url=self.request.source_page_url,
                    requested_count=self.request.requested_count,
                    selected_count=sum(
                        entry.selection_rank is not None for entry in summary.entries
                    ),
                    enriched_count=sum(
                        entry.detail_outcome == "enriched"
                        for entry in summary.entries
                        if entry.selection_rank is not None
                    ),
                    scroll_rounds=self._page_scroll_rounds,
                    sort_label=self._page_sort_label,
                    status=status.value,
                    error_code=page_error,
                )
            run = CollectionRun(
                run_id=self.run_id,
                mode="extension",
                input_summary=self.request.collection_surface,
                requested_count=self.request.requested_count,
                actual_count=len(notes),
                started_at=now,
                finished_at=now,
                status=status,
                error_code=page_error,
                notes=notes,
                collection_surface=self.request.collection_surface,
                extension_selection=self.selection_summary,
                extension_search_run=search_run,
            )
            write_result_bundle_at(run, self._run_fd, self._assets_fd)
            self._assert_output_path_binding()
        except Exception:
            self.discard()
            raise
        self._owns_output_dir = False
        _close_directory_fds(
            self._assets_fd, self._run_fd, self._output_parent_fd
        )
        self._assets_fd = -1
        self._run_fd = -1
        self._output_parent_fd = -1
        self.state = ExtensionJobState.FINISHED
        return run

    def discard(self) -> None:
        """Quarantine an unfinished run without deleting through a mutable name."""
        if self.state is ExtensionJobState.FINISHED:
            return
        if self.state is ExtensionJobState.STOPPED:
            return
        active = self._active_media
        self._active_media = None
        cleanup_error: OSError | None = None
        if active is not None and active.file_fd >= 0:
            try:
                os.close(active.file_fd)
            except OSError as error:
                cleanup_error = error
        if self._owns_output_dir:
            try:
                _remove_owned_run(
                    self._output_parent_fd,
                    self._run_name,
                    self._run_identity,
                    self._run_fd,
                    _owned_child_identities(self),
                )
                self._owns_output_dir = False
            except _CleanupResidueError:
                # This is the deliberate, verified no-delete fallback.  The
                # host reads cleanup_requires_recovery and latches its session
                # terminal; direct callers retain a stopped object instead of
                # being invited to retry path cleanup.
                self._cleanup_residue = True
            except OSError as error:
                cleanup_error = error
            finally:
                # Once a run is either fully removed or quarantined as opaque
                # recovery residue, do not retain capabilities for a second
                # path-based cleanup attempt.
                self._owns_output_dir = False
                _close_directory_fds(
                    self._staging_fd,
                    self._assets_fd,
                    self._run_fd,
                    self._output_parent_fd,
                )
                self._staging_fd = -1
                self._assets_fd = -1
                self._run_fd = -1
                self._output_parent_fd = -1
        self.state = ExtensionJobState.STOPPED
        if cleanup_error is not None:
            raise ValueError("owned run cleanup failed") from cleanup_error

    @property
    def cleanup_requires_recovery(self) -> bool:
        """Whether cleanup retained opaque residue instead of deleting by name."""
        return self._cleanup_residue

    def _require_media_open(self) -> None:
        if self.state not in {ExtensionJobState.SCAN_FINISHED, ExtensionJobState.MEDIA_RECEIVING}:
            raise ValueError("media job is not open")
        if self.selection_status is None:
            raise ValueError("candidate selection is not finished")

    def _require_job_id(self, job_id: str) -> None:
        if job_id != self.request.job_id:
            raise ValueError("media job ID does not match")

    def _require_active_media(
        self, job_id: str, note_id: str, role: MediaRole, position: int, sequence: int
    ) -> _MediaTransfer:
        self._require_media_open()
        self._require_job_id(job_id)
        active = self._active_media
        if active is None or active.key != _slot_key(note_id, role, position) or active.sequence != sequence:
            raise ValueError("media message does not match the active slot")
        return active

    def _reject_active(self, reason: MediaMissingReason) -> NoteMediaSlot:
        active = self._active_media
        if active is None:
            raise ValueError("no active media slot")
        cleanup_error: OSError | None = None
        try:
            if active.file_fd >= 0:
                os.close(active.file_fd)
                active.file_fd = -1
            quarantined_staging_name = _unlink_private_file(
                self._staging_fd, active.staging_name, active.staging_identity
            )
            if quarantined_staging_name is not None:
                self._staging_files.pop(active.staging_name, None)
                assert active.staging_identity is not None
                self._staging_files[quarantined_staging_name] = active.staging_identity
            if active.published_name is not None:
                _unlink_private_file(
                    self._assets_fd,
                    active.published_name,
                    active.published_identity,
                )
        except OSError as error:
            cleanup_error = error
        finally:
            # No cleanup failure may leave a slot accepting later messages.
            self._active_media = None
        if cleanup_error is not None:
            self.discard()
            raise ValueError("media cleanup failure") from cleanup_error
        note_id, role, position = active.key
        slot = _unavailable_slot(note_id, role, position, "rejected", reason)
        self._slot_results[active.key] = slot
        self.state = ExtensionJobState.MEDIA_RECEIVING
        return slot

    def _assert_output_path_binding(self) -> None:
        """Reject bundle output if the visible path no longer names this run inode."""
        try:
            output_fd = _open_existing_directory_no_symlinks(self.output_dir)
        except OSError as error:
            raise ValueError("output directory changed after job creation") from error
        try:
            if _directory_identity(output_fd) != self._run_identity:
                raise ValueError("output directory changed after job creation")
        finally:
            os.close(output_fd)

    def _declared_selected_slots(self) -> set[tuple[str, MediaRole, int]]:
        return {
            _slot_key(candidate.note_id, slot.role, slot.position)
            for candidate in self.selected_candidates
            for slot in candidate.media_slots
        }

    def _record_for_candidate(self, candidate: ExtensionCandidateSnapshot) -> NoteRecord:
        slots = [
            self._slot_results[_slot_key(candidate.note_id, slot.role, slot.position)]
            for slot in candidate.media_slots
        ]
        slots.sort(key=lambda slot: (_role_order(slot.role), slot.position))
        cover = next(
            (slot.asset for slot in slots if slot.role == "image" and slot.position == 1 and slot.asset),
            None,
        )
        rank = None
        if self.selection_summary is not None:
            rank = next(
                entry.selection_rank
                for entry in self.selection_summary.entries
                if entry.note_id == candidate.note_id
            )
        return NoteRecord(
            note_id=candidate.note_id,
            canonical_url=candidate.canonical_url,
            title=candidate.title,
            body=candidate.body,
            tags=candidate.tags,
            note_type=candidate.note_type,
            published_at=_parse_published_at(candidate.published_at),
            time_evidence=(
                None
                if candidate.time_evidence is None
                else TimeEvidence.model_validate(candidate.time_evidence.model_dump())
            ),
            author_id=candidate.author_id,
            author_name=candidate.author_name,
            author_profile_url=candidate.author_profile_url,
            metrics=NoteMetrics.model_validate(candidate.metrics.model_dump()),
            metric_provenance=candidate.metric_provenance,
            source_position=candidate.source_position,
            selection_rank=rank,
            selection_basis=(self.request.selection_order if rank is not None else None),
            cover_local_path=None if cover is None else cover.local_path,
            cover_asset=cover,
            media_manifest_version=2,
            media_slots=slots,
            media_discovered_count=sum(1 for slot in candidate.media_slots if slot.role == "image"),
            media_discovery_truncated=False,
        )

    def _finish_current_note_scan(self) -> None:
        if len(self._candidates) != 1:
            raise ValueError("current-note job requires exactly one candidate")
        self.selected_note_ids = (self._candidates[0].note_id,)
        self.selected_candidates = (self._candidates[0],)
        self.selection_status = "complete"

    def _finish_batch_scan(self) -> ExtensionSelectionSummary:
        if self.request.selection_order == "page_order":
            return self._finish_page_order_scan()
        cutoff = _parse_publication_cutoff(self.request.publication_cutoff)
        finite_window = self.request.publication_cutoff is not None
        provisional: list[_CandidateSelection] = [
            _evaluate_candidate(candidate, cutoff, finite_window) for candidate in self._candidates
        ]
        eligible = [item for item in provisional if item.publication_eligible and item.likes_eligible]
        eligible.sort(key=_selection_sort_key)
        selected = eligible[: self.request.requested_count]
        selected_ranks = {item.note_id: index for index, item in enumerate(selected, start=1)}
        self.selected_note_ids = tuple(item.note_id for item in selected)
        self.selected_candidates = tuple(item.candidate for item in selected)
        self.selection_status = (
            "complete"
            if len(selected) == self.request.requested_count and not self._unavailable_candidates
            else "partial"
        )
        entries = [_selection_entry(item, selected_ranks.get(item.note_id)) for item in provisional]
        entries.extend(
            ExtensionSelectionEntry(
                source_position=candidate.source_position,
                note_id=candidate.note_id,
                outcome="unavailable",
                reason=candidate.reason,
                publication_eligible=False,
                likes_eligible=False,
            )
            for candidate in self._unavailable_candidates
        )
        return ExtensionSelectionSummary(
            collection_surface=self.request.collection_surface,
            candidate_scan_limit=self.request.candidate_scan_limit,
            candidate_scanned_count=len(entries),
            requested_count=self.request.requested_count,
            publication_cutoff=cutoff,
            entries=entries,
        )

    def _finish_page_order_scan(self) -> ExtensionSelectionSummary:
        ordered = sorted(self._candidates, key=lambda candidate: candidate.source_position)
        selected = ordered[: self.request.requested_count]
        selected_ranks = {candidate.note_id: rank for rank, candidate in enumerate(selected, start=1)}
        self.selected_note_ids = tuple(candidate.note_id for candidate in selected)
        self.selected_candidates = tuple(selected)
        self.selection_status = "complete" if len(selected) == self.request.requested_count else "partial"
        entries = [
            ExtensionSelectionEntry(
                source_position=candidate.source_position,
                note_id=candidate.note_id,
                outcome="selected" if candidate.note_id in selected_ranks else "excluded",
                reason=None if candidate.note_id in selected_ranks else "selection_limit_reached",
                publication_eligible=False,
                likes_eligible=False,
                selection_rank=selected_ranks.get(candidate.note_id),
                selection_basis="page_order",
                inclusion="included",
            )
            for candidate in ordered
        ]
        entries.extend(
            ExtensionSelectionEntry(
                source_position=candidate.source_position,
                note_id=candidate.note_id,
                outcome="excluded",
                reason=candidate.reason,
                publication_eligible=False,
                likes_eligible=False,
                selection_basis="page_order",
                inclusion="excluded",
            )
            for candidate in self._unavailable_candidates
        )
        entries.sort(key=lambda entry: entry.source_position)
        return ExtensionSelectionSummary(
            collection_surface=self.request.collection_surface,
            candidate_scan_limit=self.request.candidate_scan_limit,
            candidate_scanned_count=len(entries),
            requested_count=self.request.requested_count,
            selected_count=len(selected),
            selection_basis="page_order",
            entries=entries,
        )


class _MediaTransfer:
    """Private mutable state for the one slot currently receiving bytes."""

    def __init__(
        self,
        *,
        key: tuple[str, MediaRole, int],
        sequence: int,
        maximum_bytes: int,
        overflow_reason: MediaMissingReason,
        staging_name: str,
        staging_identity: tuple[int, int],
        file_fd: int,
    ) -> None:
        self.key = key
        self.sequence = sequence
        self.maximum_bytes = maximum_bytes
        self.overflow_reason = overflow_reason
        self.staging_name = staging_name
        self.staging_identity = staging_identity
        self.published_name: str | None = None
        self.published_identity: tuple[int, int] | None = None
        self.file_fd = file_fd
        self.next_chunk_index = 0
        self.size_bytes = 0


class _MediaSnapshot:
    """Verified immutable facts read back from a private staging inode."""

    def __init__(self, size_bytes: int, sha256: str) -> None:
        self.size_bytes = size_bytes
        self.sha256 = sha256


def _safe_host_run_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value))


def _create_run_directory(
    value: Path, *, expected_parent_identity: tuple[int, int] | None = None
) -> tuple[Path, int, int]:
    path = Path(value)
    if not path.is_absolute() or path.parent == path or ".." in path.parts:
        raise ValueError("unsafe output directory")
    parent_fd: int | None = None
    run_fd: int | None = None
    try:
        parent_fd = _open_existing_directory_no_symlinks(path.parent)
        if (
            expected_parent_identity is not None
            and _directory_identity(parent_fd) != expected_parent_identity
        ):
            raise OSError("output root changed before run creation")
        os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        run_fd = os.open(
            path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        created_identity = _directory_identity(run_fd)
        created_metadata = os.lstat(path.name, dir_fd=parent_fd)
        if (
            stat.S_ISLNK(created_metadata.st_mode)
            or not stat.S_ISDIR(created_metadata.st_mode)
            or (created_metadata.st_dev, created_metadata.st_ino) != created_identity
        ):
            raise OSError("run directory changed during creation")
        return path, parent_fd, run_fd
    except OSError as error:
        if run_fd is not None:
            os.close(run_fd)
            run_fd = None
        # Do not rmdir a name after an error: a local race may have rebound
        # it to another inode.  The generated run name is opaque, so retaining
        # this initialization residue is safer than deleting a replacement.
        raise ValueError("output directory must be a fresh private run directory") from error
    finally:
        if parent_fd is not None and run_fd is None:
            os.close(parent_fd)


def _open_existing_directory_no_symlinks(path: Path) -> int:
    """Open an absolute existing directory only through non-symlink components."""
    value = Path(path)
    if not value.is_absolute() or ".." in value.parts:
        raise OSError("unsafe directory path")
    directory_fd = os.open(value.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in value.parts[1:]:
            metadata = os.lstat(component, dir_fd=directory_fd)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise OSError("directory component is not a private directory")
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _directory_identity(directory_fd: int) -> tuple[int, int]:
    info = os.fstat(directory_fd)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("expected directory")
    return info.st_dev, info.st_ino


def _create_private_directory(parent_fd: int, name: str) -> int:
    """Create one trusted child directory through a confined parent descriptor."""
    if Path(name).name != name:
        raise ValueError("unsafe private directory name")
    child_fd: int | None = None
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        identity = _directory_identity(child_fd)
        metadata = os.lstat(name, dir_fd=parent_fd)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            raise OSError("private directory changed during creation")
        return child_fd
    except OSError:
        if child_fd is not None:
            os.close(child_fd)
        raise


def _slot_key(note_id: str, role: MediaRole, position: int) -> tuple[str, MediaRole, int]:
    return note_id, role, position


def _slot_maximum(role: MediaRole) -> int:
    return MAX_VIDEO_BYTES if role == "video" else MAX_IMAGE_BYTES


def _transfer_overflow_reason(
    declared: int, role_limit: int, note_remaining: int, run_remaining: int
) -> MediaMissingReason:
    if run_remaining <= 0 or run_remaining < min(declared, role_limit, note_remaining):
        return "run_budget"
    if note_remaining <= 0 or note_remaining < min(declared, role_limit):
        return "note_budget"
    return "size_limit"


def _mime_matches_role(role: MediaRole, mime_type: MediaMime) -> bool:
    if role == "video":
        return mime_type in VIDEO_MIME_TYPES
    return mime_type in IMAGE_MIME_TYPES


def _role_order(role: MediaRole) -> int:
    return {"image": 0, "video_cover": 1, "video": 2}[role]


def _asset_name(note_id: str, role: MediaRole, position: int, mime_type: MediaMime) -> str | None:
    extension = MIME_EXTENSIONS.get(mime_type)
    if extension is None:
        return None
    if role == "image":
        return f"{note_id}-image-{position:03d}{extension}"
    if role in {"video_cover", "video"} and position == 1:
        return f"{note_id}-{role.replace('_', '-')}{extension}"
    return None


def _create_private_file(directory_fd: int, name: str) -> tuple[int, tuple[int, int]]:
    """Create one opaque regular file under a directory capability."""
    if Path(name).name != name:
        raise ValueError("unsafe media filename")
    file_fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("private media file is not regular")
        return file_fd, (info.st_dev, info.st_ino)
    except OSError:
        os.close(file_fd)
        raise


def _write_all(file_fd: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(file_fd, value[offset:])
        if written <= 0:
            raise OSError("unable to write media chunk")
        offset += written


def _snapshot_staged_file(
    directory_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    expected_mime: MediaMime,
    maximum: int,
) -> _MediaSnapshot | None:
    if Path(name).name != name:
        raise ValueError("unsafe media filename")
    file_fd = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
    )
    try:
        initial = os.fstat(file_fd)
        if (
            not stat.S_ISREG(initial.st_mode)
            or (initial.st_dev, initial.st_ino) != expected_identity
            or initial.st_size <= 0
            or initial.st_size > maximum
        ):
            return None
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
            initial.st_ino != final.st_ino
            or initial.st_size != final.st_size
            or size_bytes != initial.st_size
            or sniff_media_mime(bytes(head)) != expected_mime
        ):
            return None
        with os.fdopen(os.dup(file_fd), "rb") as stream:
            if not validate_media_container(stream, expected_mime, size_bytes):
                return None
        return _MediaSnapshot(size_bytes, digest.hexdigest())
    finally:
        os.close(file_fd)


def _verify_opaque_staging_files(
    directory_fd: int, expected_files: dict[str, tuple[int, int]]
) -> None:
    """Allow only known opaque staging entries to remain in a finished bundle."""
    observed_names = set(os.listdir(directory_fd))
    if observed_names != set(expected_files):
        raise ValueError("staging directory changed")
    for name, expected_identity in expected_files.items():
        metadata = os.lstat(name, dir_fd=directory_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != expected_identity
        ):
            raise ValueError("staging directory changed")


def _publish_private_file(
    staging_fd: int,
    assets_fd: int,
    source: str,
    destination: str,
    expected_identity: tuple[int, int],
) -> tuple[int, int]:
    if Path(source).name != source or Path(destination).name != destination:
        raise ValueError("unsafe media filename")
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=staging_fd)
    try:
        source_info = os.fstat(source_fd)
        if (
            not stat.S_ISREG(source_info.st_mode)
            or (source_info.st_dev, source_info.st_ino) != expected_identity
        ):
            raise OSError("staged media identity changed before publication")
    finally:
        os.close(source_fd)
    os.link(source, destination, src_dir_fd=staging_fd, dst_dir_fd=assets_fd, follow_symlinks=False)
    destination_info = os.lstat(destination, dir_fd=assets_fd)
    if (
        not stat.S_ISREG(destination_info.st_mode)
        or (destination_info.st_dev, destination_info.st_ino) != expected_identity
    ):
        raise OSError("published media identity changed")
    source_info = os.lstat(source, dir_fd=staging_fd)
    if (
        not stat.S_ISREG(source_info.st_mode)
        or (source_info.st_dev, source_info.st_ino) != expected_identity
    ):
        raise OSError("staged media identity changed during publication")
    # The source remains under its opaque staging name.  A filename deletion
    # after verification would be vulnerable to a name rebound; a harmless
    # private hard link is preferable to deleting a possible replacement.
    os.fsync(staging_fd)
    os.fsync(assets_fd)
    return expected_identity


def _unlink_private_file(
    directory_fd: int, name: str, expected_identity: tuple[int, int] | None
) -> str | None:
    if Path(name).name != name:
        raise ValueError("unsafe media filename")
    try:
        metadata = os.lstat(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    if (
        expected_identity is None
        or not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != expected_identity
    ):
        raise OSError("private media identity changed before cleanup")
    return _quarantine_owned_entry(directory_fd, name, expected_identity, "regular")


class _OwnedPathChangedError(OSError):
    """An owned directory entry no longer names the inode this job created."""


class _CleanupResidueError(_OwnedPathChangedError):
    """Cleanup isolated an entry but cannot delete it without a pathname race."""


def _owned_child_identities(job: ExtensionJob) -> dict[str, tuple[int, int]]:
    """Return direct run children whose original directory inodes remain known."""
    identities: dict[str, tuple[int, int]] = {}
    if job._assets_identity is not None:
        identities["assets"] = job._assets_identity
    if job._staging_identity is not None:
        identities[".staging"] = job._staging_identity
    return identities


def _remove_owned_run(
    parent_fd: int,
    run_name: str,
    expected_identity: tuple[int, int],
    run_fd: int,
    expected_children: dict[str, tuple[int, int]],
) -> None:
    """Atomically isolate an unfinished run, then retain it as opaque recovery residue."""
    if _directory_identity(run_fd) != expected_identity:
        raise _OwnedPathChangedError("owned run descriptor changed before cleanup")
    _quarantine_owned_entry(parent_fd, run_name, expected_identity, "directory")
    # macOS has atomic RENAME_EXCL but no descriptor-bound unlink/rmdir.  Once
    # the run is under an opaque, no-replace name, leaving it is the only
    # conservative answer: a later pathname deletion could remove a new inode.
    raise _CleanupResidueError("unfinished run retained as opaque recovery residue")


def _remove_empty_private_directory(
    run_fd: int,
    staging_fd: int,
    name: str,
    expected_identity: tuple[int, int] | None,
) -> None:
    """Isolate an empty private directory, never delete it by a mutable name."""
    if expected_identity is None or _directory_identity(staging_fd) != expected_identity:
        raise ValueError("staging directory changed")
    try:
        if os.listdir(staging_fd):
            raise OSError("staging directory is not empty")
        quarantine_name = _quarantine_owned_entry(
            run_fd, name, expected_identity, "directory"
        )
        _remove_quarantined_directory(run_fd, quarantine_name, expected_identity, staging_fd)
    except _OwnedPathChangedError as error:
        raise ValueError("staging directory changed") from error
    except OSError as error:
        raise ValueError("staging directory is not empty") from error


def _remove_directory_contents(
    directory_fd: int,
    expected_identity: tuple[int, int],
    expected_children: dict[str, tuple[int, int]] | None = None,
) -> None:
    """Recursively remove entries through an already-held, quarantined directory FD."""
    if _directory_identity(directory_fd) != expected_identity:
        raise _OwnedPathChangedError("owned directory descriptor changed during cleanup")
    for name in os.listdir(directory_fd):
        metadata = os.lstat(name, dir_fd=directory_fd)
        if stat.S_ISDIR(metadata.st_mode):
            identity = metadata.st_dev, metadata.st_ino
            if expected_children is not None:
                expected_child = expected_children.get(name)
                if expected_child is None or identity != expected_child:
                    raise _OwnedPathChangedError("owned child directory changed before cleanup")
            child_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            try:
                if _directory_identity(child_fd) != identity:
                    raise _OwnedPathChangedError("owned child changed before cleanup")
                quarantine_name = _quarantine_owned_entry(
                    directory_fd, name, identity, "directory"
                )
                _remove_directory_contents(child_fd, identity)
                _remove_quarantined_directory(
                    directory_fd, quarantine_name, identity, child_fd
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            identity = metadata.st_dev, metadata.st_ino
            quarantine_name = _quarantine_owned_entry(
                directory_fd, name, identity, "regular"
            )
            _remove_quarantined_regular_file(directory_fd, quarantine_name, identity)
        elif stat.S_ISLNK(metadata.st_mode):
            # A link is never followed.  Moving its exact inode to the random
            # quarantine name lets us remove only the link itself, while a
            # replacement race remains a terminal mismatch.
            identity = metadata.st_dev, metadata.st_ino
            quarantine_name = _quarantine_owned_entry(
                directory_fd, name, identity, "symlink"
            )
            _remove_quarantined_symlink(directory_fd, quarantine_name, identity)
        else:
            raise _OwnedPathChangedError("owned directory contains an unsafe replacement")
    os.fsync(directory_fd)


def _quarantine_owned_entry(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    kind: Literal["directory", "regular", "symlink"],
) -> str:
    """Move an expected entry to an opaque name with an atomic no-replace operation."""
    if Path(name).name != name:
        raise ValueError("unsafe private directory entry")
    metadata = os.lstat(name, dir_fd=parent_fd)
    if not _matches_owned_entry(metadata, expected_identity, kind):
        raise _OwnedPathChangedError("owned entry changed before quarantine")
    for _ in range(16):
        quarantine_name = _reserve_quarantine_name(parent_fd)
        try:
            _rename_no_replace(parent_fd, name, quarantine_name)
        except FileExistsError:
            continue
        moved = os.lstat(quarantine_name, dir_fd=parent_fd)
        if not _matches_owned_entry(moved, expected_identity, kind):
            # The replacement remains isolated under an opaque name.  Never
            # recurse into it, never move it again, and never delete it.
            raise _OwnedPathChangedError("owned entry changed during quarantine")
        os.fsync(parent_fd)
        return quarantine_name
    raise OSError("could not reserve an atomic cleanup capability")


def _reserve_quarantine_name(parent_fd: int) -> str:
    """Return an opaque candidate; RENAME_EXCL performs the actual reservation."""
    del parent_fd
    return f".xhs-cleanup-{secrets.token_hex(24)}"


def _rename_no_replace(parent_fd: int, source: str, destination: str) -> None:
    """Atomically rename a direct child only if the destination does not exist."""
    if Path(source).name != source or Path(destination).name != destination:
        raise ValueError("unsafe private directory entry")
    if sys.platform != "darwin":
        raise _CleanupResidueError("atomic no-replace rename is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        raise _CleanupResidueError("atomic no-replace rename is unavailable")
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        _RENAME_EXCL,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, os.strerror(error_number), destination)
        raise OSError(error_number, os.strerror(error_number), destination)


def _remove_quarantined_directory(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    held_fd: int,
) -> None:
    """Verify an isolated directory, then retain it rather than pathname-delete it."""
    if _directory_identity(held_fd) != expected_identity:
        raise _OwnedPathChangedError("quarantined directory descriptor changed")
    metadata = os.lstat(name, dir_fd=parent_fd)
    if not _matches_owned_entry(metadata, expected_identity, "directory"):
        raise _OwnedPathChangedError("quarantined directory changed before removal")
    if os.listdir(held_fd):
        raise OSError("quarantined directory is not empty")
    # The name was checked above, but it remains mutable until the deletion
    # syscall.  Re-open it through the parent capability at the final boundary
    # and compare the held inode again.  A drift is a terminal recovery
    # residue, never a reason to delete the replacement now named here.
    _reopen_and_verify_quarantined_directory(parent_fd, name, expected_identity)
    raise _CleanupResidueError("quarantined directory retained as recovery residue")


def _reopen_and_verify_quarantined_directory(
    parent_fd: int, name: str, expected_identity: tuple[int, int]
) -> None:
    """Confirm the final deletion name still binds the expected directory inode."""
    reopened_fd = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
    )
    try:
        if _directory_identity(reopened_fd) != expected_identity:
            raise _OwnedPathChangedError("quarantined directory changed before final deletion")
        metadata = os.lstat(name, dir_fd=parent_fd)
        if not _matches_owned_entry(metadata, expected_identity, "directory"):
            raise _OwnedPathChangedError("quarantined directory binding changed before final deletion")
    finally:
        os.close(reopened_fd)


def _remove_quarantined_regular_file(
    parent_fd: int, name: str, expected_identity: tuple[int, int]
) -> None:
    """Verify an isolated regular file, then retain it rather than unlink by name."""
    metadata = os.lstat(name, dir_fd=parent_fd)
    if not _matches_owned_entry(metadata, expected_identity, "regular"):
        raise _OwnedPathChangedError("quarantined file changed before removal")
    raise _CleanupResidueError("quarantined file retained as recovery residue")


def _remove_quarantined_symlink(
    parent_fd: int, name: str, expected_identity: tuple[int, int]
) -> None:
    """Verify an isolated link, then retain it rather than unlink by name."""
    metadata = os.lstat(name, dir_fd=parent_fd)
    if not _matches_owned_entry(metadata, expected_identity, "symlink"):
        raise _OwnedPathChangedError("quarantined symlink changed before removal")
    raise _CleanupResidueError("quarantined symlink retained as recovery residue")


def _matches_owned_entry(
    metadata: os.stat_result,
    expected_identity: tuple[int, int],
    kind: Literal["directory", "regular", "symlink"],
) -> bool:
    expected_mode = {
        "directory": stat.S_ISDIR,
        "regular": stat.S_ISREG,
        "symlink": stat.S_ISLNK,
    }[kind]
    return expected_mode(metadata.st_mode) and (
        metadata.st_dev,
        metadata.st_ino,
    ) == expected_identity


def _close_directory_fds(*descriptors: int) -> None:
    for descriptor in descriptors:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _unavailable_slot(
    note_id: str,
    role: MediaRole,
    position: int,
    status: Literal["missing", "rejected"],
    reason: MediaMissingReason,
) -> NoteMediaSlot:
    return NoteMediaSlot(
        note_id=note_id,
        role=role,
        position=position,
        status=status,
        missing_reason=reason,
    )


class _CandidateSelection:
    """Private, evaluated facts used before public rank assignment."""

    def __init__(
        self,
        candidate: ExtensionCandidateSnapshot,
        *,
        publication_eligible: bool,
        likes_eligible: bool,
        exact_likes: int | None,
        reason: Literal[
            "publication_time_unavailable", "likes_unavailable", "likes_not_exact"
        ]
        | None,
    ) -> None:
        self.candidate = candidate
        self.publication_eligible = publication_eligible
        self.likes_eligible = likes_eligible
        self.exact_likes = exact_likes
        self.reason = reason

    @property
    def source_position(self) -> int:
        return self.candidate.source_position

    @property
    def note_id(self) -> str:
        return self.candidate.note_id


def _evaluate_candidate(
    candidate: ExtensionCandidateSnapshot, cutoff: datetime | None, finite_window: bool
) -> _CandidateSelection:
    bound_time = _bound_publication_time(
        _parse_published_at(candidate.published_at), candidate.time_evidence
    )
    publication_eligible = not finite_window or (
        cutoff is not None and bound_time is not None and bound_time >= cutoff
    )
    likes = candidate.metrics.likes
    if likes is None:
        likes_eligible = False
        exact_likes = None
        likes_reason: Literal["likes_unavailable", "likes_not_exact"] = "likes_unavailable"
    elif likes.precision != "exact" or likes.normalized_value is None:
        likes_eligible = False
        exact_likes = None
        likes_reason = "likes_not_exact"
    else:
        likes_eligible = True
        exact_likes = likes.normalized_value
        likes_reason = "likes_not_exact"
    reason: Literal[
        "publication_time_unavailable", "likes_unavailable", "likes_not_exact"
    ] | None
    if not publication_eligible:
        reason = "publication_time_unavailable"
    elif not likes_eligible:
        reason = likes_reason
    else:
        reason = None
    return _CandidateSelection(
        candidate,
        publication_eligible=publication_eligible,
        likes_eligible=likes_eligible,
        exact_likes=exact_likes,
        reason=reason,
    )


def _selection_entry(item: _CandidateSelection, rank: int | None) -> ExtensionSelectionEntry:
    if rank is not None:
        return ExtensionSelectionEntry(
            source_position=item.source_position,
            note_id=item.note_id,
            outcome="selected",
            publication_eligible=True,
            likes_eligible=True,
            exact_likes=item.exact_likes,
            selection_rank=rank,
        )
    if item.reason is None:
        reason: Literal[
            "publication_time_unavailable",
            "likes_unavailable",
            "likes_not_exact",
            "selection_limit_reached",
        ] = "selection_limit_reached"
    else:
        reason = item.reason
    return ExtensionSelectionEntry(
        source_position=item.source_position,
        note_id=item.note_id,
        outcome="excluded",
        reason=reason,
        publication_eligible=item.publication_eligible,
        likes_eligible=item.likes_eligible,
        exact_likes=item.exact_likes,
    )


def _selection_sort_key(item: _CandidateSelection) -> tuple[int, int, str]:
    """Return the declared sort key after eligibility made exact likes mandatory."""
    assert item.exact_likes is not None
    return (-item.exact_likes, item.source_position, item.note_id)


def _parse_published_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_publication_cutoff(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_SHANGHAI)


def _bound_publication_time(
    published_at: datetime | None, evidence: ExtensionTimeEvidence | None
) -> datetime | None:
    """Return the Shanghai instant only when visible and transmitted times exactly agree."""
    if (
        published_at is None
        or evidence is None
        or evidence.kind != "published"
        or published_at.tzinfo is None
    ):
        return None
    visible_time = _parse_visible_publication_time(evidence.raw_text)
    if visible_time is None:
        return None
    normalized_published_at = published_at.astimezone(_SHANGHAI)
    if visible_time != normalized_published_at:
        return None
    return visible_time


def _parse_visible_publication_time(raw_text: str) -> datetime | None:
    """Parse exactly the two supported, complete visible publication labels."""
    iso_match = _ISO_VISIBLE_PUBLICATION.fullmatch(raw_text)
    chinese_match = _CHINESE_VISIBLE_PUBLICATION.fullmatch(raw_text)
    try:
        if iso_match is not None:
            local_time = datetime.strptime(iso_match.group(1), "%Y-%m-%d %H:%M").replace(
                tzinfo=_SHANGHAI
            )
        elif chinese_match is not None:
            local_time = datetime(
                int(chinese_match.group(1)),
                int(chinese_match.group(2)),
                int(chinese_match.group(3)),
                int(chinese_match.group(4)),
                int(chinese_match.group(5)),
                tzinfo=_SHANGHAI,
            )
        else:
            return None
    except ValueError:
        return None
    if _is_ambiguous_or_nonexistent(local_time):
        return None
    return local_time


def _is_ambiguous_or_nonexistent(local_time: datetime) -> bool:
    """Reject local values whose zone mapping is not a unique instant."""
    first = local_time.replace(fold=0)
    second = local_time.replace(fold=1)
    return first.utcoffset() != second.utcoffset()
