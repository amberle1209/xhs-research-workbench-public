"""Write a self-contained, local HTML view of one visible collection run."""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from xhs_workbench.media import MediaMissingReason, sniff_media_mime, validate_media_container
from xhs_workbench.models import (
    CollectionRun,
    LocalAsset,
    MetricValue,
    NoteMediaSlot,
    NoteRecord,
    is_canonical_xhs_search_route,
    is_safe_retained_text,
)
from xhs_workbench.security import is_safe_evidence_key, sanitize_evidence


def _publication_date(value: datetime | None) -> str | None:
    if value is None:
        return None
    # Legacy date-only records have no timezone; preserve their calendar date.
    if value.tzinfo is not None:
        value = value.astimezone(timezone(timedelta(hours=8)))
    return value.date().isoformat()


class BundleWriteError(ValueError):
    """A finite, safe reason why a result bundle was not written."""


@dataclass(frozen=True)
class BundlePaths:
    """Absolute paths for the two user-facing result files."""

    json_path: Path
    html_path: Path


@dataclass(frozen=True)
class VideoReport:
    """Already-verified local transcription facts for an HTML revision."""

    note_id: str
    status: str
    reason: str | None = None
    duration_ms: int | None = None
    transcript: str | None = None
    subtitle: str | None = None
    subtitle_status: str = "not_exposed"
    report_update_failed: bool = False


_VIDEO_STATUS_LABELS = {
    "running": "音频转录中。可以关闭笔记页或弹窗，保持 Chrome 运行和电脑不休眠；稍后刷新或重新打开本报告。",
    "preparing_model": "准备转录模型：首次需要下载模型并占用本地空间。请保持 Chrome 运行和电脑不休眠，稍后刷新报告。",
    "complete": "音频转录已完成（自动识别，标点和分段经过基础整理）。",
    "skipped_too_long": "超过首期转录时长上限（15 分钟），未转录。",
    "skipped_no_audio": "视频无音轨，未转录。",
    "no_speech": "未识别到可转录人声。",
    "not_started": "未转录：视频未保存。",
    "failed": "音频转录失败。",
}
_VIDEO_REASON_LABELS = {
    "video_not_saved": "视频未保存", "duration_unknown": "无法确认时长",
    "audio_unreadable": "音频不可读", "dependencies_unavailable": "转录依赖不可用",
    "model_preparation_failed": "模型准备失败", "processing_timeout": "处理超时",
    "task_interrupted": "任务中断", "stopped": "用户已停止",
    "worker_start_failed": "转录任务启动失败", "transcript_too_large": "文字稿超过大小限制",
    "report_update_failed": "报告更新失败", "job_in_progress": "已有视频任务正在处理",
}


MEDIA_REASON_LABELS: dict[MediaMissingReason, str] = {
    "source_not_exposed": "平台未公开来源",
    "unsupported_source": "来源格式暂不支持",
    "unsafe_source": "来源未通过安全校验",
    "download_failed": "下载失败",
    "mime_mismatch": "文件类型校验失败",
    "size_limit": "文件超过大小限制",
    "note_budget": "超过单篇媒体限制",
    "run_budget": "超过本次任务媒体限制",
    "slot_limit": "超过单篇图片下载数量限制",
    "discovery_limit": "页面图片数量超过可表示上限",
}

MEDIA_ROLE_ORDER = {"image": 0, "video_cover": 1, "video": 2}

NOTE_TYPE_LABELS = {"normal": "图文笔记", "video": "视频笔记"}
TIME_KIND_LABELS = {
    "published": "发布时间",
    "edited": "编辑时间",
    "unknown": "页面时间原文",
}
MEDIA_ROLE_LABELS = {"image": "图片", "video_cover": "视频封面", "video": "视频文件"}
MEDIA_STATUS_LABELS = {"downloaded": "已下载", "missing": "未取得", "rejected": "已拒绝"}


def write_result_bundle(run: CollectionRun, output_dir: Path) -> BundlePaths:
    """Path wrapper for callers that do not already hold directory capabilities."""
    output = _validated_output_dir(output_dir)
    output_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assets_fd = os.open(
            "assets", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=output_fd
        )
    except OSError as error:
        os.close(output_fd)
        raise BundleWriteError("invalid_output_dir") from error
    try:
        write_result_bundle_at(run, output_fd, assets_fd)
    finally:
        os.close(assets_fd)
        os.close(output_fd)
    return BundlePaths(output / "results.json", output / "index.html")


def write_result_bundle_at(run: CollectionRun, run_fd: int, assets_fd: int) -> None:
    """Atomically publish a bundle using only already-held directory capabilities.

    ``run_fd`` and ``assets_fd`` are borrowed descriptors for the exact run and
    its local ``assets`` directory.  This boundary intentionally accepts no
    paths: a caller that already verified those descriptors never re-resolves a
    mutable ancestor while validating media or publishing result files.
    """
    try:
        _require_directory_fd(run_fd)
        _require_directory_fd(assets_fd)
    except OSError as error:
        raise BundleWriteError("invalid_output_dir") from error

    canonical_run = _canonicalize_media_slots(run)
    payload = canonical_run.model_dump(mode="json")
    if not _safe_value(payload):
        raise BundleWriteError("unsafe_bundle")
    _validate_referenced_assets_at(canonical_run, run_fd, assets_fd)

    json_text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    html_text = _render_html(canonical_run)
    if not _safe_value(json_text) or not _safe_value(html_text):
        raise BundleWriteError("unsafe_bundle")

    _ensure_absent_at(run_fd, "results.json")
    _ensure_absent_at(run_fd, "index.html")
    json_identity = _atomic_create_at(run_fd, "results.json", json_text.encode("utf-8"))
    try:
        _atomic_create_at(run_fd, "index.html", html_text.encode("utf-8"))
    except BundleWriteError:
        _remove_if_same_file_at(run_fd, "results.json", json_identity)
        raise


def _validated_output_dir(value: Path) -> Path:
    path = Path(value)
    try:
        if not path.is_dir() or path.is_symlink() or (path / "assets").is_symlink():
            raise OSError
        resolved = path.resolve(strict=True)
        assets = (path / "assets").resolve(strict=True)
        if assets.parent != resolved or not assets.is_dir():
            raise OSError
    except OSError as error:
        raise BundleWriteError("invalid_output_dir") from error
    return resolved


def _require_directory_fd(directory_fd: int) -> None:
    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("bundle descriptor is not a directory")


def _ensure_absent_at(directory_fd: int, name: str) -> None:
    try:
        os.lstat(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        raise BundleWriteError("output_write_failed") from error
    raise BundleWriteError("output_conflict")


def _atomic_create_at(directory_fd: int, name: str, content: bytes) -> tuple[int, int]:
    """Make one new file durable without a replace operation."""
    temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
    temporary_created = False
    linked_identity: tuple[int, int] | None = None
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        temporary_created = True
        try:
            _write_all(temporary_fd, content)
            os.fsync(temporary_fd)
            temporary_info = os.fstat(temporary_fd)
            if not stat.S_ISREG(temporary_info.st_mode):
                raise OSError("temporary result is not a regular file")
            linked_identity = temporary_info.st_dev, temporary_info.st_ino
        finally:
            os.close(temporary_fd)
        os.link(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
        created = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(created.st_mode) or (created.st_dev, created.st_ino) != linked_identity:
            raise OSError("new result is not a regular file")
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_created = False
        return created.st_dev, created.st_ino
    except FileExistsError as error:
        _remove_if_same_file_at(directory_fd, name, linked_identity)
        raise BundleWriteError("output_conflict") from error
    except OSError as error:
        _remove_if_same_file_at(directory_fd, name, linked_identity)
        raise BundleWriteError("output_write_failed") from error
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass


def _remove_if_same_file_at(directory_fd: int, name: str, identity: tuple[int, int] | None) -> None:
    """Remove a final name only when it is the link made by this write attempt."""
    if identity is None:
        return
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
    except OSError:
        pass


def _write_all(file_descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        count = os.write(file_descriptor, view)
        if count <= 0:
            raise OSError("short file write")
        view = view[count:]


def _safe_value(value: object) -> bool:
    if isinstance(value, str):
        return is_safe_retained_text(value)
    if isinstance(value, dict):
        if not all(
            isinstance(key, str) and is_safe_evidence_key(key) and _safe_value(item)
            for key, item in value.items()
        ):
            return False
        sanitized = sanitize_evidence(value)
        if sanitized == value:
            return True
        # Search routes are retained only by the dedicated strict run-summary
        # field.  They are not note/profile identities understood by the
        # generic evidence sanitizer, but are still query-free XHS HTTPS.
        return sanitized == _sanitizer_expected_value(value)
    if isinstance(value, list):
        return all(_safe_value(item) for item in value)
    return value is None or type(value) in {bool, int, float}


def _is_safe_search_source_url(value: str) -> bool:
    return is_canonical_xhs_search_route(value)


def _sanitizer_expected_value(value: object) -> object:
    """Mirror the one approved search-route redaction used by the sanitizer."""
    if isinstance(value, dict):
        search_summary_keys = {
            "source_page_url",
            "sort_label",
            "requested_count",
            "selected_count",
            "enriched_count",
            "scroll_rounds",
            "status",
            "error_code",
        }
        is_search_summary = set(value) == search_summary_keys
        expected: dict[str, object] = {}
        for key, item in value.items():
            if key == "source_page_url":
                if (
                    not is_search_summary
                    or not isinstance(item, str)
                    or not _is_safe_search_source_url(item)
                ):
                    return object()
                expected[key] = "[REDACTED_URL]"
            else:
                expected[key] = _sanitizer_expected_value(item)
        return expected
    if isinstance(value, list):
        return [_sanitizer_expected_value(item) for item in value]
    return value


def _canonicalize_media_slots(run: CollectionRun) -> CollectionRun:
    """Make V2 manifest order deterministic before every bundle boundary."""
    notes: list[NoteRecord] = []
    for note in run.notes:
        if note.media_manifest_version is None:
            notes.append(note)
        else:
            notes.append(note.model_copy(update={"media_slots": _ordered_media_slots(note)}))
    return run.model_copy(update={"notes": notes})


def _ordered_media_slots(note: NoteRecord) -> list[NoteMediaSlot]:
    """Return the approved role/position ordering for one V2 manifest."""
    return sorted(
        note.media_slots,
        key=lambda slot: (MEDIA_ROLE_ORDER[slot.role], slot.position),
    )


def _validate_referenced_assets_at(run: CollectionRun, run_fd: int, assets_fd: int) -> None:
    assets: list[LocalAsset] = []
    if run.account is not None and run.account.avatar_asset is not None:
        assets.append(run.account.avatar_asset)
    for note in run.notes:
        if note.media_manifest_version is None:
            if note.cover_asset is not None:
                assets.append(note.cover_asset)
            continue
        for slot in _ordered_media_slots(note):
            if slot.status == "downloaded":
                assert slot.asset is not None
                assets.append(slot.asset)
    for asset in assets:
        _validate_asset_at(asset, run_fd, assets_fd)


def _validate_asset_at(asset: LocalAsset, run_fd: int, assets_fd: int) -> None:
    try:
        descriptor = _open_asset_no_follow_at(run_fd, assets_fd, asset.local_path)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != asset.size_bytes:
                raise OSError("local asset is not the declared regular file")
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if digest != asset.sha256:
                raise OSError("local asset digest changed")
            handle.seek(0)
            if sniff_media_mime(handle.read(12)) != asset.mime_type:
                raise OSError("local asset MIME changed")
            if not validate_media_container(handle, asset.mime_type, asset.size_bytes):
                raise OSError("local asset container is invalid")
    except OSError as error:
        raise BundleWriteError("invalid_local_asset") from error


def _open_asset_no_follow_at(run_fd: int, assets_fd: int, local_path: str) -> int:
    """Open a relative asset beneath held run/assets directory capabilities."""
    parts = local_path.split("/")
    if not parts or any(not part for part in parts):
        raise OSError("local asset path is empty")
    if parts[0] == "assets":
        if len(parts) == 1:
            raise OSError("local asset path names the assets directory")
        directory_fd = os.dup(assets_fd)
        parts = parts[1:]
    else:
        directory_fd = os.dup(run_fd)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except OSError:
        os.close(directory_fd)
        raise


def render_video_report(run: CollectionRun, video: VideoReport) -> bytes:
    """Render a complete same-index revision without changing the base manifest."""
    return _render_html(run, video=video).encode("utf-8")


def _render_html(run: CollectionRun, video: VideoReport | None = None) -> str:
    account = _render_account(run)
    notes = "".join(_render_note(run, note, video) for note in sorted(run.notes, key=lambda item: _note_order(run, item)))
    if not notes:
        notes = "<p>本次没有可展示的作品。</p>"
    selection_scope = _selection_scope(run)
    scope_html = "" if not selection_scope else f"<p>{_text(selection_scope)}</p>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小红书可见研究结果</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;line-height:1.65;margin:0;background:#f7f7f5;color:#1f2328}} main{{max-width:980px;margin:auto;padding:28px 18px}} main>section,.note-card{{background:#fff;border:1px solid #d8dce0;border-radius:6px;padding:20px;margin:16px 0}} h1,h2,h3,h4{{margin-top:0;line-height:1.35}} h1{{font-size:1.55rem}} h2{{font-size:1.12rem}} h3{{font-size:1.1rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:650}} h4{{font-size:.95rem;margin-bottom:10px}} dl{{display:grid;grid-template-columns:minmax(9rem,max-content) minmax(0,1fr);gap:6px 18px;margin:0}} dt{{color:#57606a}} dd{{margin:0;overflow-wrap:anywhere;word-break:break-word}} img{{max-width:180px;max-height:180px;object-fit:cover;border-radius:4px}} figure{{margin:0 0 12px}} figcaption{{color:#57606a;font-size:.875rem;overflow-wrap:anywhere;word-break:break-word}} .record-section{{border-top:1px solid #e7e9eb;margin-top:18px;padding-top:16px}} .record-section:first-of-type{{border-top:0;margin-top:0;padding-top:0}} .muted,.legacy-media{{color:#57606a;font-size:.9rem}} .body{{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}} .media-gallery{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}} .media-gallery img,.local-video{{display:block;width:100%;max-width:100%;max-height:none;aspect-ratio:1;object-fit:cover;border-radius:4px}} .local-video{{aspect-ratio:16/9;background:#111}} .media-metadata{{margin-top:12px}} a{{color:#8b2635;text-underline-offset:2px}} a:focus-visible{{outline:3px solid #2f81f7;outline-offset:2px}} @media (max-width:600px){{main{{padding:16px 12px}}main>section,.note-card{{padding:14px}}dl{{grid-template-columns:1fr;gap:4px}}dt{{margin-top:8px}}.media-gallery{{grid-template-columns:1fr}}}}
</style>
</head>
<body><main>
<h1>小红书可见研究结果</h1>
<section><h2>运行信息</h2><dl>
<dt>运行 ID</dt><dd>{_text(run.run_id)}</dd><dt>模式</dt><dd>{_text(run.mode)}</dd><dt>输入摘要</dt><dd>{_text(run.input_summary)}</dd><dt>请求作品数</dt><dd>{run.requested_count}</dd><dt>实际作品数</dt><dd>{run.actual_count}</dd><dt>状态</dt><dd>{_text(run.status.value)}</dd><dt>错误码</dt><dd>{_text(run.error_code)}</dd>
</dl></section>
{account}
<section><h2>作品</h2>{scope_html}{notes}</section>
<section><strong>结果仅引用已校验的本地媒体文件；不加载远程图片或视频。</strong></section>
</main></body></html>\n"""


def _render_account(run: CollectionRun) -> str:
    account = run.account
    if account is None:
        return "<section><h2>账号</h2><p>本次未取得账号信息。</p></section>"
    avatar = _asset_image(account.avatar_asset, "账号头像")
    avatar_field = "" if account.avatar_asset is not None else "<dt>账号头像</dt><dd>未公开</dd>"
    platform_metrics = "".join(
        f"<dt>{_text(label)}</dt><dd>{_metric(value)}</dd>"
        for label, value in sorted(account.platform_metrics.items())
    )
    if not platform_metrics:
        platform_metrics = "<dt>平台互动字段</dt><dd>未公开</dd>"
    missing = _missing_fields(account.missing_fields)
    return f"""<section><h2>账号</h2>{avatar}<dl>
<dt>主页</dt><dd>{_link(account.profile_url)}</dd>{avatar_field}<dt>名称</dt><dd>{_text(account.name)}</dd><dt>简介</dt><dd>{_text(account.bio)}</dd><dt>作品数</dt><dd>{_metric(account.note_count)}</dd><dt>粉丝</dt><dd>{_metric(account.follower_count)}</dd>{platform_metrics}{missing}
</dl></section>"""


def _note_order(run: CollectionRun, note: NoteRecord) -> tuple[int, int, str]:
    """Use persisted selection rank only for extension batch report cards."""
    if run.collection_surface in {"extension_search", "extension_account"}:
        assert note.selection_rank is not None
        return note.selection_rank, note.source_position, note.note_id
    return note.source_position, note.source_position, note.note_id


def _selection_scope(run: CollectionRun) -> str:
    """Return the sole user-facing explanation for a strict extension batch ranking."""
    if run.collection_surface in {"extension_search", "extension_account"}:
        if run.extension_selection is not None and run.extension_selection.selection_basis == "page_order":
            return "当前页面顺序"
        return "按精确点赞排序，仅来自已检查候选"
    return ""


def _render_note(run: CollectionRun, note: NoteRecord, video: VideoReport | None = None) -> str:
    cover_field = (
        ""
        if note.media_manifest_version == 2 or note.cover_asset is not None
        else "<dt>作品封面</dt><dd>未公开</dd>"
    )
    time_evidence = note.time_evidence
    time_kind = None if time_evidence is None else TIME_KIND_LABELS[time_evidence.kind]
    time_text = None if time_evidence is None else time_evidence.raw_text
    tags = "、".join(note.tags) if note.tags else None
    return f"""<article class="note-card"><h3>笔记 ID：{_text(note.note_id)}</h3>
<section class="record-section"><h4>基本信息</h4><dl>
<dt>帖子标题</dt><dd>{_text(note.title)}</dd><dt>作品链接</dt><dd>{_link(note.canonical_url)}</dd>{cover_field}<dt>作者</dt><dd>{_text(note.author_name)}</dd><dt>作者 ID</dt><dd>{_text(note.author_id)}</dd><dt>作者主页</dt><dd>{_link(note.author_profile_url)}</dd><dt>类型</dt><dd>{_text(_note_type_label(note.note_type))}</dd><dt>原始类型</dt><dd>{_text(note.note_type)}</dd>
</dl></section>
<section class="record-section"><h4>时间</h4><dl>
<dt>发布时间</dt><dd>{_text(_publication_date(note.published_at))}</dd><dt>页面时间原文</dt><dd>{_text(time_text)}</dd><dt>时间含义</dt><dd>{_text(time_kind)}</dd>
</dl></section>
<section class="record-section"><h4>互动</h4><dl>{_render_interactions(note)}</dl></section>
<section class="record-section"><h4>内容</h4><dl><dt>标签</dt><dd>{_text(tags)}</dd></dl><h4>完整正文</h4><div class="body">{_text(note.body)}</div></section>
<section class="record-section"><h4>媒体</h4>{_render_media(note)}{_render_media_metadata(note)}{_render_video_processing(video) if video is not None and video.note_id == note.note_id else ""}</section>
<section class="record-section"><h4>采集上下文</h4><dl>
<dt>候选位置</dt><dd>{note.source_position}</dd><dt>位置说明</dt><dd>当前候选列表第 {note.source_position} 条（非互动排名）</dd>{_selection_rank_field(run, note)}{_missing_fields(note.missing_fields)}
</dl></section></article>"""


def _render_video_processing(video: VideoReport) -> str:
    label = _VIDEO_STATUS_LABELS.get(video.status, "转录状态未知。")
    if video.report_update_failed:
        label = "文字稿已生成，报告更新失败。" if video.transcript else "报告更新失败，原报告仍可打开。"
    reason = "" if video.reason is None else f"<p>{_text(_VIDEO_REASON_LABELS.get(video.reason, '转录失败'))}</p>"
    duration = "" if video.duration_ms is None else f"<p>已校验视频时长：{video.duration_ms / 1000:g} 秒</p>"
    transcript = ""
    if video.transcript is not None:
        transcript = (
            '<h4>音频转录（自动识别）</h4><p><a href="transcript.txt" download>另存文字稿</a></p>'
            f'<div class="body">{_text(video.transcript)}</div>'
        )
    subtitle = ""
    if video.subtitle is not None:
        cue_text = "\n\n".join(
            "\n".join(block.splitlines()[2:])
            for block in video.subtitle.strip().replace("\r\n", "\n").split("\n\n")
        )
        subtitle = (
            '<h4>独立字幕（页面提供）</h4><p><a href="independent-subtitles.srt" download>另存独立字幕</a></p>'
            f'<div class="body">{_text(cue_text)}</div>'
        )
    subtitle_label = {
        "available": "已保存页面提供的独立字幕。",
        "failed": "独立字幕获取失败；不影响音频转录。",
        "not_exposed": "页面未提供可直接获取的独立字幕。",
    }.get(video.subtitle_status, "独立字幕状态未知。")
    return f'<section class="record-section"><h4>视频处理</h4><p>{_text(label)}</p>{duration}{reason}<p>{_text(subtitle_label)}</p>{transcript}{subtitle}</section>'


def _selection_rank_field(run: CollectionRun, note: NoteRecord) -> str:
    if run.collection_surface in {"extension_search", "extension_account"}:
        assert note.selection_rank is not None
        return f"<dt>已选排名</dt><dd>第 {note.selection_rank} 名</dd>"
    return ""


def _note_type_label(raw_type: str | None) -> str:
    if raw_type is None:
        return "未公开"
    return NOTE_TYPE_LABELS.get(raw_type, "其他笔记类型")


def _render_interactions(note: NoteRecord) -> str:
    rows: list[str] = []
    metric_rows = (
        ("likes", "点赞"),
        ("collects", "收藏"),
        ("comments", "评论"),
        ("shares", "分享"),
    )
    for name, label in metric_rows:
        metric = getattr(note.metrics, name)
        rows.append(f"<dt>{label}</dt><dd>{_metric(metric)}</dd>")
    total = _total_interactions(note)
    total_text = "未公开" if total is None else str(total)
    total_basis = "无法按完整字段合计" if total is None else "按上列已公开互动字段合计"
    rows.append(
        f"<dt>总互动</dt><dd>{total_text}</dd><dt>总互动说明</dt><dd>{total_basis}</dd>"
    )
    return "".join(rows)


def _render_media(note: NoteRecord) -> str:
    """Render V2 media strictly from its manifest, with a legacy cover fallback."""
    if note.media_manifest_version is None:
        return (
            '<p class="legacy-media">历史单封面结果</p>'
            + _asset_image(note.cover_asset, "作品封面")
        )
    slots = _ordered_media_slots(note)
    images = [
        slot
        for slot in slots
        if slot.role == "image" and slot.status == "downloaded" and slot.asset is not None
    ]
    video = next(
        (
            slot
            for slot in slots
            if slot.role == "video" and slot.status == "downloaded" and slot.asset is not None
        ),
        None,
    )
    poster = next(
        (
            slot
            for slot in slots
            if slot.role == "video_cover" and slot.status == "downloaded" and slot.asset is not None
        ),
        None,
    )
    if video is not None:
        assert video.asset is not None
        poster_attr = (
            f' poster="{_asset_source(poster.asset)}"' if poster is not None and poster.asset else ""
        )
        return (
            f'<video controls preload="metadata" class="local-video"{poster_attr}>'
            f'<source src="{_asset_source(video.asset)}" type="{_text(video.asset.mime_type)}">'
            "</video>"
        )
    if images:
        return '<div class="media-gallery">' + "".join(
            _asset_image(slot.asset, f"作品图片 {slot.position}") for slot in images
        ) + "</div>"
    if poster is not None:
        assert poster.asset is not None
        return (
            '<p class="muted">视频未下载，保留本地封面。</p>'
            + _asset_image(poster.asset, "视频封面")
        )
    return '<p class="muted">媒体清单为空或未下载可展示媒体。</p>'


def _render_media_metadata(note: NoteRecord) -> str:
    """Render finite slot-level integrity details for a V2 manifest."""
    if note.media_manifest_version is None or not note.media_slots:
        return ""
    rows: list[str] = []
    for slot in _ordered_media_slots(note):
        label = f"第 {slot.position} 张图片" if slot.role == "image" else MEDIA_ROLE_LABELS[slot.role]
        rows.extend(
            [
                f"<dt>媒体槽位</dt><dd>{_text(label)}</dd>",
                f"<dt>媒体角色</dt><dd>{_text(MEDIA_ROLE_LABELS[slot.role])}</dd>",
                f"<dt>媒体状态</dt><dd>{_text(MEDIA_STATUS_LABELS[slot.status])}</dd>",
            ]
        )
        if slot.status == "downloaded":
            assert slot.asset is not None
            rows.extend(
                [
                    f"<dt>本地路径</dt><dd>{_text(slot.asset.local_path)}</dd>",
                    f"<dt>MIME 类型</dt><dd>{_text(slot.asset.mime_type)}</dd>",
                    f"<dt>文件大小</dt><dd>{slot.asset.size_bytes} B</dd>",
                    f"<dt>SHA-256</dt><dd>{_text(slot.asset.sha256)}</dd>",
                ]
            )
            if slot.duration_ms is not None:
                rows.append(f"<dt>时长</dt><dd>{slot.duration_ms} ms</dd>")
        else:
            assert slot.missing_reason is not None
            rows.append(
                f"<dt>未取得原因</dt><dd>{_text(MEDIA_REASON_LABELS[slot.missing_reason])}</dd>"
            )
    return '<dl class="media-metadata">' + "".join(rows) + "</dl>"


def _asset_image(asset: LocalAsset | None, alt: str) -> str:
    if asset is None:
        return ""
    metadata = (
        f"{asset.local_path} · {asset.mime_type} · {asset.size_bytes} B · SHA-256 {asset.sha256}"
    )
    return f'<figure><img src="{_asset_source(asset)}" alt="{_text(alt)}"><figcaption>{_text(metadata)}</figcaption></figure>'


def _asset_source(asset: LocalAsset) -> str:
    """Encode a model-validated relative local path for an HTML attribute."""
    return _text(quote(asset.local_path, safe="/-._~"))


def _metric(metric: MetricValue | None) -> str:
    if metric is None or metric.precision == "not_exposed":
        return "未公开"
    return _text(metric.raw_value)


def _total_interactions(note: NoteRecord) -> int | None:
    metrics = (
        note.metrics.likes,
        note.metrics.collects,
        note.metrics.comments,
        note.metrics.shares,
    )
    if any(metric is None or metric.normalized_value is None for metric in metrics):
        return None
    values = [metric for metric in metrics if metric is not None]
    return sum(metric.normalized_value for metric in values if metric.normalized_value is not None)


def _text(value: object | None) -> str:
    return "未公开" if value is None else html.escape(str(value), quote=True)


def _link(value: str | None) -> str:
    if value is None:
        return "未公开"
    escaped = _text(value)
    return f'<a href="{escaped}">{escaped}</a>'


def _missing_fields(fields: list[str]) -> str:
    if not fields:
        return ""
    return f"<dt>缺失字段</dt><dd>{_text('、'.join(fields))}</dd>"
