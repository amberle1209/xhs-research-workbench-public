"""Local ASR subprocess adapter. No collected media leaves this machine.

The parent owns timeout/cancellation and passes already-verified local paths.
Model preparation is a separate invocation so first download is not charged to
ordinary transcription. Optional heavy dependencies are never imported by the
legacy collection path.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import select
import sys
import threading
import time
from collections.abc import Iterable
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

MODEL_NAME = "small"
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
MODEL_HASHES = {
    "model.bin": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
    "config.json": "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
    "tokenizer.json": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
    "vocabulary.txt": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
}
MAX_TEXT_BYTES = 512 * 1024
REQUIRED_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")


def _start_owner_watchdog() -> None:
    """Keep the worker's flock until this ASR process exits, even if its guard dies."""
    names = ("XHS_ASR_OWNER_FD", "XHS_ASR_LOCK_FD", "XHS_ASR_DEADLINE_NS")
    raw = tuple(os.environ.pop(name, None) for name in names)
    if raw == (None, None, None):
        return  # Standalone adapter calls used by diagnostics and unit tests.
    try:
        if any(value is None for value in raw):
            raise ValueError("incomplete ASR ownership")
        owner_fd, lock_fd, deadline_ns = (int(value) for value in raw if value is not None)
        if owner_fd < 0 or lock_fd < 0 or deadline_ns <= time.monotonic_ns():
            raise ValueError("invalid ASR ownership")
        os.fstat(owner_fd)
        os.fstat(lock_fd)
        # Retain the descriptors in this process, but do not hand them to any
        # future executable launched by a dependency.
        os.set_inheritable(owner_fd, False)
        os.set_inheritable(lock_fd, False)

        def watch() -> None:
            try:
                while True:
                    remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
                    if remaining <= 0:
                        os._exit(1)
                    readable, _, _ = select.select([owner_fd], [], [], remaining)
                    if readable:
                        # Only the worker owns the write end; readiness means
                        # EOF or an invalid ownership channel. Fail closed.
                        os._exit(1)
            except BaseException:  # noqa: BLE001 - watchdog failure cannot leave ASR running.
                os._exit(1)

        threading.Thread(target=watch, name="asr-owner-watchdog", daemon=True).start()
    except (OSError, RuntimeError, ValueError):
        os._exit(1)


def duration_milliseconds(seconds: Decimal) -> int:
    if not seconds.is_finite() or seconds <= 0:
        raise ValueError("duration_unknown")
    return int((seconds * 1000).to_integral_value(rounding=ROUND_CEILING))


def transcript_text(segments: Iterable[Any]) -> str:
    terminal_marks = '。！？.!?；;：:“”"」』'

    def punctuate(value: str) -> str:
        return value if value[-1:] in terminal_marks else value.rstrip("，,") + "。"

    paragraphs: list[str] = []
    paragraph = ""
    previous_end = 0.0
    paragraph_start = 0.0
    for segment in segments:
        text = str(segment.text).strip()
        if not text:
            continue
        start, end = float(segment.start), float(segment.end)
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("audio_unreadable")
        if paragraph and (start - previous_end >= 2 or end - paragraph_start >= 30):
            paragraphs.append(punctuate(paragraph))
            paragraph = ""
        if not paragraph:
            paragraph_start = start
        # Small Whisper sometimes omits Chinese punctuation. Add only segment
        # separators and paragraph endings; never rewrite recognized words.
        separator = ""
        if (
            paragraph
            and paragraph[-1] not in terminal_marks + "，,"
            and text[0] not in terminal_marks + "，,"
        ):
            separator = "，"
        elif paragraph and paragraph[-1:].isascii() and text[:1].isascii():
            separator = " "
        paragraph += separator + text
        previous_end = end
        if sum(len(part) for part in paragraphs) + len(paragraph) > MAX_TEXT_BYTES:
            raise ValueError("transcript_too_large")
    if paragraph:
        paragraphs.append(punctuate(paragraph))
    result = "\n\n".join(paragraphs)
    if len(result.encode("utf-8")) > MAX_TEXT_BYTES or "\x00" in result:
        raise ValueError("transcript_too_large")
    return result


def model_ready(directory: Path) -> bool:
    return (
        directory.is_dir()
        and not directory.is_symlink()
        and all(
            (directory / name).is_file()
            and not (directory / name).is_symlink()
            and hashlib.sha256((directory / name).read_bytes()).hexdigest() == MODEL_HASHES[name]
            for name in REQUIRED_MODEL_FILES
        )
    )


def prepare_model(directory: Path) -> None:
    snapshot_download = importlib.import_module("huggingface_hub").snapshot_download

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("model_preparation_failed")
    snapshot_download(
        "Systran/faster-whisper-small",
        revision=MODEL_REVISION,
        local_dir=str(directory),
        allow_patterns=list(REQUIRED_MODEL_FILES),
        token=False,
        max_workers=2,
    )
    if not model_ready(directory):
        raise ValueError("model_preparation_failed")


def probe_video(path: Path) -> dict[str, Any]:
    av = importlib.import_module("av")

    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError("audio_unreadable")
        video = container.streams.video[0]
        # Container duration is in microseconds. Prefer complete container time
        # over rounded player labels. Unknown duration never implies <= 15 min.
        if container.duration is None:
            raise ValueError("duration_unknown")
        duration_ms = duration_milliseconds(Decimal(container.duration) / Decimal(1_000_000))
        return {
            "duration_ms": duration_ms,
            "audio_present": bool(container.streams.audio),
            "width": video.width,
            "height": video.height,
        }


def transcribe(path: Path, model_directory: Path) -> dict[str, Any]:

    probe = probe_video(path)
    if probe["duration_ms"] > 900000:
        return {**probe, "status": "skipped_too_long"}
    if not probe["audio_present"]:
        return {**probe, "status": "skipped_no_audio"}
    if not model_ready(model_directory):
        raise ValueError("model_preparation_failed")
    WhisperModel = importlib.import_module("faster_whisper").WhisperModel
    model = WhisperModel(
        str(model_directory),
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        num_workers=1,
        local_files_only=True,
    )
    segments, _ = model.transcribe(
        str(path), language="zh", beam_size=5, vad_filter=True, condition_on_previous_text=False
    )
    text = transcript_text(segments)
    return {
        **probe,
        "status": "complete" if text else "no_speech",
        "text": text,
        "model": "faster-whisper-small-cpu-int8",
        "language": "zh",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "probe", "transcribe"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--model", type=Path)
    args = parser.parse_args()
    _start_owner_watchdog()
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    try:
        if args.operation == "prepare":
            prepare_model(args.path)
            result: dict[str, Any] = {"status": "ready"}
        elif args.operation == "probe":
            result = probe_video(args.path)
        elif args.model is not None:
            result = transcribe(args.path, args.model)
        else:
            raise ValueError("model_preparation_failed")
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001 - subprocess boundary must emit finite errors
        # Dependency errors and decoder exceptions can contain local paths or
        # signed URLs. Emit finite reasons only; never log speech or raw errors.
        allowed = {
            "duration_unknown",
            "audio_unreadable",
            "transcript_too_large",
            "model_preparation_failed",
        }
        reason = (
            str(error)
            if isinstance(error, ValueError) and str(error) in allowed
            else (
                "dependencies_unavailable" if isinstance(error, ImportError) else "audio_unreadable"
            )
        )
        sys.stdout.write(json.dumps({"status": "failed", "reason": reason}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
