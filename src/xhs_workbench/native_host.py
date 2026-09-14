"""Chrome Native Messaging host for bounded extension collection imports.

The host intentionally has no browser, CLI, or network dependencies.  It owns
the short-lived on-disk run capability, validates every wire response against
the request that caused it, and exposes finished reports only through a
verified registry entry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Literal, cast

from xhs_workbench import video_processing
from xhs_workbench.extension_import import (
    ExtensionJob,
    ExtensionJobState,
    _open_existing_directory_no_symlinks,
    _rename_no_replace,
)
from xhs_workbench.extension_models import (
    CandidateResult,
    ErrorResponse,
    ExtensionBeginJob,
    ExtensionCandidateSnapshot,
    ExtensionCandidateUnavailable,
    ExtensionFinishJob,
    ExtensionFinishScan,
    ExtensionHealth,
    ExtensionMediaBegin,
    ExtensionMediaChunk,
    ExtensionMediaEnd,
    ExtensionMediaMissing,
    ExtensionOpenReport,
    ExtensionStopJob,
    ExtensionVideoStatus,
    ExtensionVideoStop,
    HealthResult,
    JobMessage,
    JobResult,
    JobStarted,
    MediaResult,
    NativeRequest,
    NativeResponse,
    Progress,
    ReportResult,
    SelectedNote,
    SelectionResult,
    VideoProcessingSummary,
    VideoResult,
)
from xhs_workbench.models import CollectionRun
from xhs_workbench.native_protocol import (
    NativeProtocolError,
    read_native_message,
    write_native_message,
)
from xhs_workbench.path_security import has_extended_acl as _has_extended_acl

_CONFIG_FILENAME = "extension.json"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_REGISTRY_BYTES = 16 * 1024
_MAX_RESULTS_BYTES = 8 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}/$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class InstallerConfig:
    """Validated local installer state; Chrome never supplies any of these paths."""

    allowed_origin: str
    output_root: Path
    output_root_identity: tuple[int, int]
    config_path: Path
    config_directory: Path
    config_directory_identity: tuple[int, int]
    config_file_identity: tuple[int, int]


@dataclass(frozen=True)
class _RegistryEntry:
    """The full immutable identity chain recorded after a finished bundle."""

    run_id: str
    run_identity: tuple[int, int]
    output_root_identity: tuple[int, int]
    results_sha256: str


@dataclass
class _VerifiedReport:
    """Held report identities around the required absolute-path macOS launch.

    LaunchServices does not reliably carry a ``/dev/fd`` capability through to
    the GUI application.  These descriptors therefore remain open only to
    detect observed path rebinding before and after the required ``open`` call;
    they do not defend a malicious same-UID replacement in the exact spawn
    window.
    """

    config: InstallerConfig
    run_id: str
    output_root_fd: int
    output_root_identity: tuple[int, int]
    run_fd: int
    run_identity: tuple[int, int]
    index_fd: int
    index_identity: tuple[int, int]
    terminal_status: Literal["complete", "partial", "stopped", "failed"] | None = None

    @property
    def verified_index_path(self) -> Path:
        """Return the fixed absolute path derived only from verified host state."""
        return self.config.output_root / self.run_id / "index.html"

    def close(self) -> None:
        descriptors = (self.index_fd, self.run_fd, self.output_root_fd)
        self.index_fd = -1
        self.run_fd = -1
        self.output_root_fd = -1
        close_error: OSError | None = None
        for descriptor in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError as error:
                if close_error is None:
                    close_error = error
        if close_error is not None:
            raise close_error


class _SessionPhase(str, Enum):
    """One native connection has one idle probe or one terminal operation."""

    IDLE = "idle"
    ACTIVE = "active"
    AWAITING_TERMINAL_CLOSE = "awaiting_terminal_close"
    TERMINAL = "terminal"
    FAILED = "failed"


class _HostError(ValueError):
    """A mapped public error whose text never crosses the native boundary."""

    def __init__(self, code: Literal["invalid_request", "invalid_state", "identity_mismatch", "internal_error", "job_in_progress"], fatal: bool) -> None:
        self.code = code
        self.fatal = fatal
        super().__init__(code)


def _default_config_path() -> Path:
    return Path.home() / ".config" / "xhs-workbench" / _CONFIG_FILENAME


def main() -> None:
    """Run Chrome's stdio host with exactly the caller origin argument."""
    arguments = sys.argv[1:]
    if len(arguments) != 1:
        raise SystemExit(1)
    exit_code = run_native_host(
        arguments[0],
        config_path=_default_config_path(),
        input_stream=sys.stdin.buffer,
        output_stream=sys.stdout.buffer,
    )
    if exit_code != 0:
        raise SystemExit(exit_code)


def run_native_host(
    caller_origin: str,
    *,
    config_path: Path,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
) -> int:
    """Serve one Native Messaging connection without logging page-derived data."""
    try:
        config = _load_installer_config(config_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1
    if caller_origin != config.allowed_origin:
        return 1

    session = _NativeHostSession(config)
    active_request: NativeRequest | None = None
    response: NativeResponse
    response_started = False
    exit_code = 0
    try:
        while True:
            try:
                request = read_native_message(input_stream)
            except NativeProtocolError as error:
                # A malformed frame may have no parsed request to pair with.
                # If it does, cleanup failure supersedes the protocol error so
                # the caller receives a finite, paired fatal response.
                cleanup_succeeded = session.close_for_port_loss()
                if error.request is not None:
                    response = _error_for_request(
                        error.request,
                        "unsupported_protocol" if cleanup_succeeded else "internal_error",
                        fatal=True,
                    )
                    write_native_message(output_stream, response, request=error.request)
                exit_code = 0 if error.eof and cleanup_succeeded else 1
                break
            active_request = request
            response = session.dispatch(request)
            response_started = True
            write_native_message(output_stream, response, request=request)
            response_started = False
            active_request = None
            if session.awaiting_terminal_close:
                while input_stream.read(64 * 1024):
                    pass
                exit_code = 0 if session.close_for_port_loss() else 1
                break
            if session.is_terminal:
                exit_code = session.exit_code
                break
    except Exception:  # noqa: BLE001 - host diagnostics never reach stdio.
        session.close_for_port_loss()
        if active_request is not None and not response_started:
            try:
                write_native_message(
                    output_stream,
                    _error_for_request(active_request, "internal_error", fatal=True),
                    request=active_request,
                )
            except (NativeProtocolError, OSError):
                pass
        exit_code = 1
    finally:
        if not session.is_terminal and not session.close_for_port_loss():
            exit_code = 1
        if session.failed:
            exit_code = 1
    return exit_code


class _NativeHostSession:
    """The exact single-job ordering boundary for one connected extension port."""

    def __init__(self, config: InstallerConfig) -> None:
        self._config = config
        self._job: ExtensionJob | None = None
        self._phase = _SessionPhase.IDLE
        self._begun = False
        self._cleanup_failed = False
        self._video_lock_fd: int | None = None

    @property
    def is_terminal(self) -> bool:
        return self._phase in {_SessionPhase.TERMINAL, _SessionPhase.FAILED}

    @property
    def awaiting_terminal_close(self) -> bool:
        return self._phase is _SessionPhase.AWAITING_TERMINAL_CLOSE

    @property
    def failed(self) -> bool:
        return self._phase is _SessionPhase.FAILED

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def dispatch(self, request: NativeRequest) -> NativeResponse:
        try:
            if isinstance(request, ExtensionHealth):
                if self._phase is not _SessionPhase.IDLE:
                    raise _HostError("invalid_state", fatal=False)
                return HealthResult(
                    protocol_version="1.0", kind="health_result", status="ready", host_version="0.1.3"
                )
            if isinstance(request, ExtensionBeginJob):
                return self._begin_job(request)
            if isinstance(request, (ExtensionVideoStatus, ExtensionVideoStop)):
                if self._phase is not _SessionPhase.IDLE or self._begun:
                    raise _HostError("invalid_state", fatal=False)
                processing = video_processing.processing_for_job(
                    self._config, request.job_id, stop=isinstance(request, ExtensionVideoStop)
                )
                self._phase = _SessionPhase.TERMINAL
                return VideoResult(protocol_version="1.0", kind="video_result",
                                   job_id=request.job_id, processing=processing)
            if isinstance(request, ExtensionCandidateSnapshot):
                job = self._require_active_job(request)
                job.add_candidate(request)
                return CandidateResult(
                    protocol_version="1.0",
                    kind="candidate_result",
                    job_id=request.job_id,
                    note_id=request.note_id,
                    source_position=request.source_position,
                    outcome="recorded",
                )
            if isinstance(request, ExtensionCandidateUnavailable):
                job = self._require_active_job(request)
                job.add_candidate_unavailable(request)
                return CandidateResult(
                    protocol_version="1.0",
                    kind="candidate_result",
                    job_id=request.job_id,
                    note_id=request.note_id,
                    source_position=request.source_position,
                    outcome="unavailable",
                )
            if isinstance(request, ExtensionFinishScan):
                return self._finish_scan(request)
            if isinstance(request, ExtensionMediaBegin):
                job = self._require_active_job(request)
                if not self._allows_media_transfer(job):
                    return _error_for_request(request, "invalid_state", fatal=False)
                slot = job.begin_media(request)
                if slot is not None:
                    return MediaResult(
                        protocol_version="1.0",
                        kind="media_result",
                        job_id=request.job_id,
                        note_id=request.note_id,
                        role=request.role,
                        position=request.position,
                        outcome=slot.status,
                        reason=slot.missing_reason,
                    )
                return _progress_for_job(job, request.job_id, request.note_id)
            if isinstance(request, ExtensionMediaChunk):
                job = self._require_active_job(request)
                if not self._allows_media_transfer(job):
                    return _error_for_request(request, "invalid_state", fatal=False)
                job.append_media_chunk(request)
                return _progress_for_job(job, request.job_id, request.note_id)
            if isinstance(request, ExtensionMediaEnd):
                job = self._require_active_job(request)
                if not self._allows_media_transfer(job):
                    return _error_for_request(request, "invalid_state", fatal=False)
                slot = job.finish_media(request)
                return MediaResult(
                    protocol_version="1.0",
                    kind="media_result",
                    job_id=request.job_id,
                    note_id=request.note_id,
                    role=request.role,
                    position=request.position,
                    outcome=slot.status,
                    reason=slot.missing_reason,
                )
            if isinstance(request, ExtensionMediaMissing):
                job = self._require_active_job(request)
                if not self._allows_media_transfer(job):
                    return _error_for_request(request, "invalid_state", fatal=False)
                slot = job.mark_media_missing(request)
                return MediaResult(
                    protocol_version="1.0",
                    kind="media_result",
                    job_id=request.job_id,
                    note_id=request.note_id,
                    role=request.role,
                    position=request.position,
                    outcome=slot.status,
                    reason=slot.missing_reason,
                )
            if isinstance(request, ExtensionFinishJob):
                return self._finish_job(request)
            if isinstance(request, ExtensionStopJob):
                return self._stop_job(request)
            if isinstance(request, ExtensionOpenReport):
                if self._phase is not _SessionPhase.IDLE or self._begun:
                    raise _HostError("invalid_state", fatal=False)
                verified_report = _open_verified_report(self._config, request.job_id)
                try:
                    if video_processing.load_processing(verified_report.run_fd) is not None:
                        verified_report.close()
                        video_processing.processing_for_job(self._config, request.job_id)
                        verified_report = _open_verified_report(self._config, request.job_id)
                    _launch_verified_report(verified_report)
                finally:
                    verified_report.close()
                self._phase = _SessionPhase.TERMINAL
                return ReportResult(
                    protocol_version="1.0",
                    kind="report_result",
                    job_id=request.job_id,
                    opened=True,
                    terminal_status=verified_report.terminal_status,
                )
            raise _HostError("invalid_state", fatal=False)
        except _HostError as error:
            return self._terminal_error(request, error.code, fatal=error.fatal)
        except ValueError as error:
            message = str(error)
            code: Literal["invalid_state", "identity_mismatch", "internal_error"]
            fatal = False
            if "job ID" in message or "identity" in message:
                code = "identity_mismatch"
                fatal = True
            elif self._job is not None and self._job.state is ExtensionJobState.STOPPED:
                code = "internal_error"
                fatal = True
            else:
                code = "invalid_state"
            return self._terminal_error(request, code, fatal=fatal)
        except OSError:
            return self._terminal_error(request, "internal_error", fatal=True)

    def _terminal_error(
        self,
        request: NativeRequest,
        code: Literal["invalid_request", "invalid_state", "identity_mismatch", "internal_error", "job_in_progress"],
        *,
        fatal: bool,
    ) -> ErrorResponse:
        cleanup_succeeded = self.discard_unfinished()
        self._release_video_lock()
        self._phase = _SessionPhase.FAILED
        if not cleanup_succeeded:
            return _error_for_request(request, "internal_error", fatal=True)
        return _error_for_request(request, code, fatal=fatal)

    def discard_unfinished(self) -> bool:
        """Terminal cleanup that retains a job reference until success is known."""
        if self._cleanup_failed:
            return False
        job = self._job
        if job is None:
            return True
        if job.state is ExtensionJobState.FINISHED:
            self._job = None
            return True
        try:
            job.discard()
        except (OSError, ValueError):
            # The Task 4 boundary intentionally retains opaque recovery
            # residue if a binding could have drifted.  The host must stop;
            # clearing the job here would permit a false clean continuation.
            self._cleanup_failed = True
            return False
        if job.cleanup_requires_recovery:
            self._cleanup_failed = True
            return False
        self._job = None
        return True

    def close_for_port_loss(self) -> bool:
        """End the session on EOF, malformed input, or an outer interruption."""
        if self._phase is _SessionPhase.FAILED:
            return False
        if self._phase is _SessionPhase.TERMINAL:
            return True
        cleanup_succeeded = self.discard_unfinished()
        self._release_video_lock()
        self._phase = _SessionPhase.TERMINAL if cleanup_succeeded else _SessionPhase.FAILED
        return cleanup_succeeded

    def _begin_job(self, request: ExtensionBeginJob) -> NativeResponse:
        if self._phase is not _SessionPhase.IDLE or self._begun or self._job is not None:
            raise _HostError("invalid_state", fatal=False)
        if request.collection_surface == "extension_current":
            try:
                self._video_lock_fd = video_processing.reserve_video_lock(self._config, request.job_id)
            except ValueError as error:
                if "already in progress" in str(error):
                    raise _HostError("job_in_progress", fatal=False) from error
                raise
        for _ in range(16):
            run_id = secrets.token_hex(32)
            if not _SAFE_ID.fullmatch(run_id):
                raise _HostError("internal_error", fatal=True)
            output_dir = self._config.output_root / run_id
            if output_dir.exists():
                continue
            try:
                self._job = ExtensionJob(
                    run_id,
                    output_dir,
                    request,
                    output_root_identity=self._config.output_root_identity,
                )
            except ValueError as error:
                if output_dir.exists():
                    continue
                raise _HostError("internal_error", fatal=True) from error
            self._begun = True
            self._phase = _SessionPhase.ACTIVE
            return JobStarted(protocol_version="1.0", kind="job_started", job_id=request.job_id, status="started")
        raise _HostError("internal_error", fatal=True)

    def _release_video_lock(self) -> None:
        fd, self._video_lock_fd = self._video_lock_fd, None
        if fd is not None:
            os.close(fd)

    def _require_active_job(self, request: JobMessage) -> ExtensionJob:
        job = self._job
        if self._phase is not _SessionPhase.ACTIVE or job is None:
            raise _HostError("invalid_state", fatal=False)
        if job.request.job_id != request.job_id:
            raise _HostError("identity_mismatch", fatal=True)
        return job

    @staticmethod
    def _allows_media_transfer(job: ExtensionJob) -> bool:
        """Keep Simple page-order runs metadata-only at the host boundary."""
        return job.request.selection_order != "page_order"

    def _finish_scan(self, request: ExtensionFinishScan) -> NativeResponse:
        job = self._require_active_job(request)
        if job.request.selection_order == "page_order" and request.scroll_rounds is None:
            raise _HostError("invalid_request", fatal=False)
        summary = job.finish_scan(
            scroll_rounds=request.scroll_rounds,
            sort_label=request.sort_label,
        )
        if summary is not None and summary.selection_basis == "page_order":
            selected = [
                SelectedNote(note_id=entry.note_id, selection_rank=entry.selection_rank)
                for entry in summary.entries
                if entry.selection_rank is not None
            ]
            eligible_count = sum(entry.inclusion == "included" for entry in summary.entries)
        else:
            selected = [
                SelectedNote(note_id=candidate.note_id, selection_rank=index)
                for index, candidate in enumerate(job.selected_candidates, start=1)
            ]
            eligible_count = (
                len(job.selected_candidates)
                if summary is None
                else sum(
                    entry.publication_eligible and entry.likes_eligible for entry in summary.entries
                )
            )
        if summary is None:
            scanned_count = len(job.selected_candidates)
        else:
            scanned_count = summary.candidate_scanned_count
        return SelectionResult(
            protocol_version="1.0",
            kind="selection_result",
            job_id=request.job_id,
            scanned_count=scanned_count,
            eligible_count=eligible_count,
            selected_count=len(selected),
            status=job.selection_status or "partial",
            selected=selected,
        )

    def _finish_job(self, request: ExtensionFinishJob) -> NativeResponse:
        job = self._require_active_job(request)
        metadata = request.video_metadata
        video_candidate = None
        if job.request.collection_surface == "extension_current":
            selected = job.selected_candidates
            if len(selected) == 1:
                video_candidate = next(
                    (item for item in selected[0].media_slots if item.role == "video"), None
                )
        if metadata is not None and (
            video_candidate is None or len(job.selected_candidates) != 1
            or metadata.note_id != job.selected_candidates[0].note_id
        ):
            raise _HostError("invalid_request", fatal=False)
        try:
            run = job.finish_job()
            _write_verified_registry(self._config, request.job_id, job)
        except Exception as error:
            raise _HostError("internal_error", fatal=True) from error
        video_summary = None
        try:
            if video_candidate is not None and self._video_lock_fd is not None:
                try:
                    video_summary = video_processing.start_processing(
                        self._config, request.job_id, job.selected_candidates[0].note_id,
                        lock_fd=self._video_lock_fd,
                        duration_hint_ms=metadata.duration_ms if metadata is not None else None,
                        subtitle=metadata.subtitle_srt if metadata is not None else None,
                        subtitle_status=metadata.subtitle_status if metadata is not None else None,
                    )
                except (OSError, ValueError):
                    try:
                        video_summary = video_processing.start_failure(
                            self._config, request.job_id, job.selected_candidates[0].note_id,
                            duration_hint_ms=metadata.duration_ms if metadata is not None else None,
                        )
                    except (OSError, ValueError):
                        video_summary = VideoProcessingSummary(
                            status="failed", reason="worker_start_failed", report_update_failed=True
                        )
        finally:
            self._release_video_lock()
        self._job = None
        self._phase = _SessionPhase.AWAITING_TERMINAL_CLOSE
        status: Literal["complete", "partial"] = "complete" if run.status.value == "complete" else "partial"
        return JobResult(
            protocol_version="1.0",
            kind="job_result",
            job_id=request.job_id,
            status=status,
            retained_count=run.actual_count,
            report_available=True,
            report_file="index.html",
            video_processing=video_summary,
        )

    def _stop_job(self, request: ExtensionStopJob) -> NativeResponse:
        job = self._require_active_job(request)
        cause = request.terminal_cause or "stopped"
        if (
            job.request.selection_order == "page_order"
            and job.selection_summary is not None
        ):
            if cause in {"structural_error", "route_mismatch", "identity_mismatch"}:
                try:
                    run = job.fail_page_order_integrity(cast(Literal["structural_error", "route_mismatch", "identity_mismatch"], cause))
                    _write_verified_registry(self._config, request.job_id, job)
                except Exception as error:
                    raise _HostError("internal_error", fatal=True) from error
                self._job = None
                self._release_video_lock()
                self._phase = _SessionPhase.AWAITING_TERMINAL_CLOSE
                return JobResult(
                    protocol_version="1.0", kind="job_result", job_id=request.job_id,
                    status="failed", retained_count=run.actual_count,
                    report_available=True, report_file="index.html",
                )
            try:
                job.set_page_terminal_error(cast(Literal["login_required", "challenge_detected", "stopped"], cause))
                run = job.finish_job()
                _write_verified_registry(self._config, request.job_id, job)
            except Exception as error:
                raise _HostError("internal_error", fatal=True) from error
            self._job = None
            self._release_video_lock()
            self._phase = _SessionPhase.AWAITING_TERMINAL_CLOSE
            return JobResult(
                protocol_version="1.0",
                kind="job_result",
                job_id=request.job_id,
                status="stopped" if cause == "stopped" else "partial",
                retained_count=run.actual_count,
                report_available=True,
                report_file="index.html",
            )
        if not self.discard_unfinished():
            raise _HostError("internal_error", fatal=True)
        self._release_video_lock()
        self._phase = _SessionPhase.AWAITING_TERMINAL_CLOSE
        status: Literal["failed", "partial", "stopped"] = "failed" if cause in {"structural_error", "route_mismatch", "identity_mismatch"} else ("partial" if cause in {"login_required", "challenge_detected"} else "stopped")
        return JobResult(
            protocol_version="1.0",
            kind="job_result",
            job_id=request.job_id,
            status=status,
            retained_count=0,
            report_available=False,
        )

def _progress_for_job(job: ExtensionJob, job_id: str, note_id: str) -> Progress:
    """Return only finite counters; no page data or local path crosses this boundary."""
    discovered = job.candidate_scanned_count
    inspected = discovered
    if job.selection_summary is None:
        eligible = len(job.selected_candidates)
    elif job.selection_summary.selection_basis == "page_order":
        eligible = sum(entry.inclusion == "included" for entry in job.selection_summary.entries)
    else:
        eligible = sum(
            entry.publication_eligible and entry.likes_eligible for entry in job.selection_summary.entries
        )
    selected = len(job.selected_candidates)
    position = next(
        (candidate.source_position for candidate in job.selected_candidates if candidate.note_id == note_id),
        None,
    )
    return Progress(
        protocol_version="1.0",
        kind="progress",
        job_id=job_id,
        phase="downloading",
        discovered=discovered,
        inspected=inspected,
        eligible=eligible,
        selected=selected,
        saved=0,
        current_source_position=position,
    )


def _error_for_request(
    request: NativeRequest,
    code: Literal["invalid_request", "invalid_state", "identity_mismatch", "internal_error", "unsupported_protocol", "job_in_progress"],
    *,
    fatal: bool,
) -> ErrorResponse:
    job_id = request.job_id if isinstance(request, JobMessage) else None
    return ErrorResponse(protocol_version="1.0", kind="error", job_id=job_id, code=code, fatal=fatal)


def _load_installer_config(config_path: Path) -> InstallerConfig:
    """Read the installer-owned mode-0600 config through non-symlink descriptors."""
    path = Path(config_path)
    if (
        not path.is_absolute()
        or path.name != _CONFIG_FILENAME
        or ".." in path.parts
        or path.parent == path
    ):
        raise ValueError("unsafe installer config path")
    config_directory_fd = _open_existing_directory_no_symlinks(path.parent)
    config_fd = -1
    try:
        directory_info = os.fstat(config_directory_fd)
        _require_owned_directory(directory_info)
        if _has_extended_acl(path.parent):
            raise ValueError("unsafe config directory ACL")
        if stat.S_IMODE(directory_info.st_mode) & 0o022:
            raise ValueError("unsafe config directory permissions")
        config_directory_identity = directory_info.st_dev, directory_info.st_ino
        config_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=config_directory_fd)
        config_info = os.fstat(config_fd)
        _require_owned_regular(config_info, mode=0o600)
        if _has_extended_acl(path):
            raise ValueError("unsafe config file ACL")
        config_file_identity = config_info.st_dev, config_info.st_ino
        raw = _read_bounded(config_fd, _MAX_CONFIG_BYTES)
    finally:
        if config_fd >= 0:
            os.close(config_fd)
        os.close(config_directory_fd)
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid installer config") from error
    if not isinstance(decoded, dict):
        raise TypeError("invalid installer config")
    allowed_origin = decoded.get("allowed_origin")
    output_root_value = decoded.get("output_root")
    if (
        not isinstance(allowed_origin, str)
        or not _ORIGIN.fullmatch(allowed_origin)
        or not isinstance(output_root_value, str)
    ):
        raise ValueError("invalid installer config")
    output_root = Path(output_root_value)
    if not output_root.is_absolute() or ".." in output_root.parts:
        raise ValueError("invalid installer config")
    output_root_fd = _open_existing_directory_no_symlinks(output_root)
    try:
        output_root_info = os.fstat(output_root_fd)
        _require_owned_directory(output_root_info)
        if _has_extended_acl(output_root):
            raise ValueError("unsafe output root ACL")
        if stat.S_IMODE(output_root_info.st_mode) & 0o022:
            raise ValueError("unsafe output root permissions")
        output_root_identity = output_root_info.st_dev, output_root_info.st_ino
    finally:
        os.close(output_root_fd)
    return InstallerConfig(
        allowed_origin=allowed_origin,
        output_root=output_root,
        output_root_identity=output_root_identity,
        config_path=path,
        config_directory=path.parent,
        config_directory_identity=config_directory_identity,
        config_file_identity=config_file_identity,
    )


def _write_verified_registry(config: InstallerConfig, job_id: str, job: ExtensionJob) -> None:
    """Atomically register only a bundle still bound to its host-owned run inode."""
    if not _SAFE_ID.fullmatch(job_id) or job.request.job_id != job_id:
        raise ValueError("unsafe job identity")
    digest, _ = _verify_finished_bundle(config, job.run_id, job._run_identity)
    config_directory_fd = _open_verified_config_directory(config)
    temporary_name: str | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        final_name = _registry_name(job_id)
        payload = {
            "job_id": job_id,
            "run_id": job.run_id,
            "run_identity": {
                "device": job._run_identity[0],
                "inode": job._run_identity[1],
            },
            "output_root_identity": {
                "device": config.output_root_identity[0],
                "inode": config.output_root_identity[1],
            },
            "results_sha256": digest,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_REGISTRY_BYTES:
            raise ValueError("registry is too large")
        for _ in range(16):
            candidate = f".xhs-registry-{secrets.token_hex(24)}"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=config_directory_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            try:
                _write_all(descriptor, encoded)
                os.fsync(descriptor)
                info = os.fstat(descriptor)
                _require_owned_regular(info, mode=0o600)
                temporary_identity = info.st_dev, info.st_ino
            finally:
                os.close(descriptor)
            break
        if temporary_name is None or temporary_identity is None:
            raise OSError("could not reserve registry entry")
        _rename_no_replace(config_directory_fd, temporary_name, final_name)
        final_info = os.lstat(final_name, dir_fd=config_directory_fd)
        _require_owned_regular(final_info, mode=0o600)
        if (final_info.st_dev, final_info.st_ino) != temporary_identity:
            raise OSError("registry binding changed")
        # Atomic no-replace rename removes the temporary directory entry as a
        # single operation; no later pathname unlink is needed.
        temporary_name = None
        _require_owned_regular(os.lstat(final_name, dir_fd=config_directory_fd), mode=0o600)
        os.fsync(config_directory_fd)
    finally:
        # On errors, retain the opaque mode-0600 temporary file for recovery
        # rather than attempting a racy pathname unlink.
        os.close(config_directory_fd)


def open_report(job_id: str) -> bool:
    """Open the fixed report only after descriptor-level registry verification."""
    if not _SAFE_ID.fullmatch(job_id):
        raise ValueError("unsafe job ID")
    config = _load_installer_config(_default_config_path())
    verified_report = _open_verified_report(config, job_id)
    try:
        if video_processing.load_processing(verified_report.run_fd) is not None:
            verified_report.close()
            video_processing.processing_for_job(config, job_id)
            verified_report = _open_verified_report(config, job_id)
        _launch_verified_report(verified_report)
    finally:
        verified_report.close()
    return True


def _launch_verified_report(verified_report: _VerifiedReport) -> None:
    """Open the fixed verified path and fail closed on observed rebinding."""
    _assert_report_path_binding(verified_report)
    result = subprocess.run(
        ["open", str(verified_report.verified_index_path)],
        check=False,
        close_fds=True,
    )
    _assert_report_path_binding(verified_report)
    if result.returncode != 0:
        raise OSError("report opener failed")


def _assert_report_path_binding(verified_report: _VerifiedReport) -> None:
    """Check held and absolute root/run/index identities at one launch boundary."""
    path_root_fd = -1
    path_run_fd = -1
    path_index_fd = -1
    try:
        _assert_fd_identity(
            verified_report.output_root_fd, verified_report.output_root_identity
        )
        _assert_fd_identity(verified_report.run_fd, verified_report.run_identity)
        _assert_fd_identity(
            verified_report.index_fd, verified_report.index_identity, regular=True
        )
        path_root_fd = _open_verified_output_root(verified_report.config)
        _assert_fd_identity(path_root_fd, verified_report.output_root_identity)
        _assert_path_identity(
            path_root_fd,
            verified_report.run_id,
            verified_report.run_identity,
            directory=True,
        )
        path_run_fd = os.open(
            verified_report.run_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=path_root_fd,
        )
        _assert_fd_identity(path_run_fd, verified_report.run_identity)
        _assert_path_identity(
            path_root_fd,
            verified_report.run_id,
            verified_report.run_identity,
            directory=True,
        )
        path_index_fd = os.open(
            "index.html", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=path_run_fd
        )
        _assert_fd_identity(path_index_fd, verified_report.index_identity, regular=True)
        _assert_path_identity(
            path_run_fd, "index.html", verified_report.index_identity, regular=True
        )
    except (OSError, ValueError) as error:
        raise ValueError("report identity changed") from error
    finally:
        if path_index_fd >= 0:
            os.close(path_index_fd)
        if path_run_fd >= 0:
            os.close(path_run_fd)
        if path_root_fd >= 0:
            os.close(path_root_fd)


def _open_verified_report(config: InstallerConfig, job_id: str) -> _VerifiedReport:
    """Resolve a registry entry without ever treating its job ID as a path."""
    if not _SAFE_ID.fullmatch(job_id):
        raise ValueError("unsafe job ID")
    config_directory_fd = _open_verified_config_directory(config)
    try:
        registry_path = config.config_directory / _registry_name(job_id)
        try:
            registry_has_acl = _has_extended_acl(registry_path)
        except OSError as error:
            raise ValueError("registry is unavailable") from error
        if registry_has_acl:
            raise ValueError("unsafe registry ACL")
        registry_fd, registry_identity = _open_owned_regular_at(
            config_directory_fd, _registry_name(job_id), maximum_bytes=_MAX_REGISTRY_BYTES, mode=0o600
        )
        try:
            registry_bytes = _read_bounded(registry_fd, _MAX_REGISTRY_BYTES)
            _assert_fd_identity(registry_fd, registry_identity, regular=True)
            _assert_path_identity(config_directory_fd, _registry_name(job_id), registry_identity, regular=True)
        finally:
            os.close(registry_fd)
    finally:
        os.close(config_directory_fd)
    registry = _parse_registry(registry_bytes, job_id)
    if registry.output_root_identity != config.output_root_identity:
        raise ValueError("registry output root changed")
    digest, terminal_status, verified_report = _open_verified_bundle(
        config, registry.run_id, registry.run_identity
    )
    if digest != registry.results_sha256:
        verified_report.close()
        raise ValueError("results digest changed")
    verified_report.terminal_status = terminal_status
    return verified_report


def _verify_finished_bundle(
    config: InstallerConfig, run_id: str, expected_run_identity: tuple[int, int] | None
) -> tuple[str, Path]:
    """Verify a finished bundle and close its index descriptor after inspection."""
    digest, _, verified_report = _open_verified_bundle(config, run_id, expected_run_identity)
    try:
        return digest, verified_report.verified_index_path
    finally:
        verified_report.close()


def _open_verified_bundle(
    config: InstallerConfig, run_id: str, expected_run_identity: tuple[int, int] | None
) -> tuple[str, Literal["complete", "partial", "stopped", "failed"] | None, _VerifiedReport]:
    """Verify a bundle and retain root/run/index identities for report opening."""
    if not _SAFE_ID.fullmatch(run_id):
        raise ValueError("unsafe run ID")
    output_root_fd = _open_verified_output_root(config)
    run_fd = -1
    results_fd = -1
    index_fd = -1
    try:
        run_path = config.output_root / run_id
        if _has_extended_acl(run_path):
            raise ValueError("unsafe run directory ACL")
        run_metadata = os.lstat(run_id, dir_fd=output_root_fd)
        if not stat.S_ISDIR(run_metadata.st_mode) or stat.S_ISLNK(run_metadata.st_mode):
            raise ValueError("run is not a private directory")
        run_fd = os.open(
            run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=output_root_fd
        )
        run_info = os.fstat(run_fd)
        _require_owned_directory(run_info)
        run_identity = run_info.st_dev, run_info.st_ino
        if (run_metadata.st_dev, run_metadata.st_ino) != run_identity:
            raise ValueError("run binding changed")
        if expected_run_identity is not None and run_identity != expected_run_identity:
            raise ValueError("run identity changed")
        results_fd, results_identity = _open_owned_regular_at(
            run_fd, "results.json", maximum_bytes=_MAX_RESULTS_BYTES, mode=None
        )
        if _has_extended_acl(run_path / "results.json"):
            raise ValueError("unsafe result manifest ACL")
        results_bytes = _read_bounded(results_fd, _MAX_RESULTS_BYTES)
        _assert_fd_identity(results_fd, results_identity, regular=True)
        _assert_path_identity(run_fd, "results.json", results_identity, regular=True)
        try:
            manifest = json.loads(results_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid result manifest") from error
        if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
            raise ValueError("result manifest identity changed")
        terminal_status = _verified_terminal_status(manifest)
        index_fd, index_identity = _open_owned_regular_at(
            run_fd, "index.html", maximum_bytes=_MAX_RESULTS_BYTES, mode=None
        )
        if _has_extended_acl(run_path / "index.html"):
            raise ValueError("unsafe report index ACL")
        _assert_fd_identity(index_fd, index_identity, regular=True)
        _assert_path_identity(run_fd, "index.html", index_identity, regular=True)
        _assert_path_identity(output_root_fd, run_id, run_identity, directory=True)
        digest = hashlib.sha256(results_bytes).hexdigest()
        verified_report = _VerifiedReport(
            config=config,
            run_id=run_id,
            output_root_fd=output_root_fd,
            output_root_identity=config.output_root_identity,
            run_fd=run_fd,
            run_identity=run_identity,
            index_fd=index_fd,
            index_identity=index_identity,
        )
        output_root_fd = -1
        run_fd = -1
        index_fd = -1
        return digest, terminal_status, verified_report
    finally:
        if index_fd >= 0:
            os.close(index_fd)
        if results_fd >= 0:
            os.close(results_fd)
        if run_fd >= 0:
            os.close(run_fd)
        if output_root_fd >= 0:
            os.close(output_root_fd)


def _verified_terminal_status(
    manifest: object,
) -> Literal["complete", "partial", "stopped", "failed"] | None:
    """Expose only a validated finite outcome from an already-verified result manifest."""
    try:
        status = CollectionRun.model_validate(manifest).status.value
    except ValueError:
        return None
    if status == "complete":
        return "complete"
    if status == "partial":
        return "partial"
    if status == "stopped":
        return "stopped"
    if status == "failed":
        return "failed"
    return None


def _parse_registry(raw: bytes, job_id: str) -> _RegistryEntry:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid registry") from error
    if not isinstance(decoded, dict) or set(decoded) != {
        "job_id",
        "run_id",
        "run_identity",
        "output_root_identity",
        "results_sha256",
    }:
        raise ValueError("invalid registry")
    stored_job_id = decoded.get("job_id")
    run_id = decoded.get("run_id")
    run_identity = decoded.get("run_identity")
    identity = decoded.get("output_root_identity")
    digest = decoded.get("results_sha256")
    if (
        stored_job_id != job_id
        or not isinstance(run_id, str)
        or not _SAFE_ID.fullmatch(run_id)
        or not isinstance(run_identity, dict)
        or set(run_identity) != {"device", "inode"}
        or type(run_identity["device"]) is not int
        or type(run_identity["inode"]) is not int
        or run_identity["device"] < 0
        or run_identity["inode"] < 0
        or not isinstance(identity, dict)
        or set(identity) != {"device", "inode"}
        or type(identity["device"]) is not int
        or type(identity["inode"]) is not int
        or identity["device"] < 0
        or identity["inode"] < 0
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
    ):
        raise ValueError("invalid registry")
    return _RegistryEntry(
        run_id=run_id,
        run_identity=(run_identity["device"], run_identity["inode"]),
        output_root_identity=(identity["device"], identity["inode"]),
        results_sha256=digest,
    )


def _open_verified_config_directory(config: InstallerConfig) -> int:
    directory_fd = _open_existing_directory_no_symlinks(config.config_directory)
    try:
        directory_info = os.fstat(directory_fd)
        _require_owned_directory(directory_info)
        if _has_extended_acl(config.config_directory):
            raise ValueError("unsafe config directory ACL")
        if stat.S_IMODE(directory_info.st_mode) & 0o022:
            raise ValueError("unsafe config directory permissions")
        if (directory_info.st_dev, directory_info.st_ino) != config.config_directory_identity:
            raise ValueError("config directory changed")
        config_fd = os.open(
            config.config_path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        try:
            config_info = os.fstat(config_fd)
            _require_owned_regular(config_info, mode=0o600)
            if _has_extended_acl(config.config_path):
                raise ValueError("unsafe config file ACL")
            if (config_info.st_dev, config_info.st_ino) != config.config_file_identity:
                raise ValueError("config file changed")
        finally:
            os.close(config_fd)
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_verified_output_root(config: InstallerConfig) -> int:
    output_root_fd = _open_existing_directory_no_symlinks(config.output_root)
    try:
        output_root_info = os.fstat(output_root_fd)
        _require_owned_directory(output_root_info)
        if _has_extended_acl(config.output_root):
            raise ValueError("unsafe output root ACL")
        if stat.S_IMODE(output_root_info.st_mode) & 0o022:
            raise ValueError("unsafe output root permissions")
        if (output_root_info.st_dev, output_root_info.st_ino) != config.output_root_identity:
            raise ValueError("output root changed")
        return output_root_fd
    except Exception:
        os.close(output_root_fd)
        raise


def _registry_name(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()


def _open_owned_regular_at(
    directory_fd: int, name: str, *, maximum_bytes: int, mode: int | None
) -> tuple[int, tuple[int, int]]:
    if Path(name).name != name:
        raise ValueError("unsafe private filename")
    try:
        metadata = os.lstat(name, dir_fd=directory_fd)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("unsafe regular file")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            info = os.fstat(descriptor)
            _require_owned_regular(info, mode=mode)
            identity = info.st_dev, info.st_ino
            if (metadata.st_dev, metadata.st_ino) != identity or info.st_size > maximum_bytes:
                raise ValueError("regular file binding changed")
            return descriptor, identity
        except Exception:
            os.close(descriptor)
            raise
    except OSError as error:
        raise ValueError("regular file is unavailable") from error


def _assert_fd_identity(
    descriptor: int, expected_identity: tuple[int, int], *, regular: bool = False
) -> None:
    info = os.fstat(descriptor)
    if regular:
        _require_owned_regular(info, mode=None)
    else:
        _require_owned_directory(info)
    if (info.st_dev, info.st_ino) != expected_identity:
        raise ValueError("descriptor identity changed")


def _assert_path_identity(
    directory_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    *,
    regular: bool = False,
    directory: bool = False,
) -> None:
    metadata = os.lstat(name, dir_fd=directory_fd)
    if regular and (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        raise ValueError("regular file binding changed")
    if directory and (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        raise ValueError("directory binding changed")
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        raise ValueError("path binding changed")


def _require_owned_directory(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("unsafe directory owner")


def _require_owned_regular(
    info: os.stat_result, *, mode: int | None, links: int | None = 1
) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or (links is not None and info.st_nlink != links)
    ):
        raise ValueError("unsafe regular file owner")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise ValueError("unsafe regular file mode")


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ValueError("file exceeds bounded size")
        chunks.append(chunk)


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise OSError("short private write")
        offset += written


if __name__ == "__main__":
    main()
