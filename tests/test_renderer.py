from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from xhs_workbench.models import (
    AccountRecord,
    CandidateAttempt,
    CollectionRun,
    LocalAsset,
    MediaMime,
    MetricValue,
    NoteMediaSlot,
    NoteMetrics,
    NoteRecord,
    PacingSummary,
    RunStatus,
    TimeEvidence,
)

JPEG = b"\xff\xd8\xffvisible-image"
WEBP = b"RIFF\x08\x00\x00\x00WEBPfixture"
PNG = b"\x89PNG\r\n\x1a\nfixture"


def _mp4_box(kind: bytes, payload: bytes = b"") -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


MP4 = _mp4_box(b"ftyp", b"isom\x00\x00\x00\x00") + _mp4_box(b"moov") + _mp4_box(
    b"mdat", b"frame"
)


def _metric(value: int, *, rounded: bool = False) -> MetricValue:
    return MetricValue(
        raw_value=("1.2k" if rounded else str(value)),
        normalized_value=value,
        precision="display_rounded" if rounded else "exact",
    )


def _run(*, body: str = "完整正文", status: RunStatus = RunStatus.COMPLETE) -> CollectionRun:
    image = LocalAsset(
        local_path="assets/note-cover.jpg",
        mime_type="image/jpeg",
        size_bytes=len(JPEG),
        sha256=hashlib.sha256(JPEG).hexdigest(),
    )
    avatar = LocalAsset(
        local_path="assets/account-avatar.jpg",
        mime_type="image/jpeg",
        size_bytes=len(JPEG),
        sha256=hashlib.sha256(JPEG).hexdigest(),
    )
    note = NoteRecord(
        note_id="note_a",
        canonical_url="https://www.xiaohongshu.com/explore/note_a",
        title="<标题 & 作者>",
        body=body,
        note_type="图文",
        published_at=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        metrics=NoteMetrics(
            likes=_metric(1200, rounded=True),
            collects=_metric(2),
            comments=_metric(3),
            shares=_metric(4),
        ),
        source_position=2,
        cover_local_path=image.local_path,
        cover_asset=image,
    )
    return CollectionRun(
        run_id="run_fixture",
        mode="account",
        input_summary="https://www.xiaohongshu.com/user/profile/account_a",
        requested_count=3,
        actual_count=1,
        started_at=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 29, 10, 1, tzinfo=UTC),
        status=status,
        error_code="profile_unavailable" if status == RunStatus.FAILED else None,
        account=AccountRecord(
            account_id="account_a",
            profile_url="https://www.xiaohongshu.com/user/profile/account_a",
            avatar_local_path=avatar.local_path,
            avatar_asset=avatar,
            name="<账号名>",
            bio="<简介 & 文本>",
            note_count=_metric(8),
            follower_count=_metric(99),
            platform_metrics={"获赞与收藏": _metric(42)},
        ),
        notes=[note],
    )


def _extension_batch_run() -> CollectionRun:
    base = _run()
    first = base.notes[0].model_copy(
        update={
            "note_id": "note_b",
            "canonical_url": "https://www.xiaohongshu.com/explore/note_b",
            "source_position": 4,
            "selection_rank": 1,
            "selection_basis": "exact_likes_desc",
            "metrics": NoteMetrics(likes=_metric(50)),
        }
    )
    second = base.notes[0].model_copy(
        update={
            "note_id": "note_a",
            "canonical_url": "https://www.xiaohongshu.com/explore/note_a",
            "source_position": 1,
            "selection_rank": 2,
            "selection_basis": "exact_likes_desc",
            "metrics": NoteMetrics(likes=_metric(20)),
        }
    )
    values = base.model_dump()
    values.update(
        {
            "mode": "extension_search",
            "requested_count": 2,
            "actual_count": 2,
            "notes": [second.model_dump(), first.model_dump()],
            "collection_surface": "extension_search",
            "extension_selection": {
                "collection_surface": "extension_search",
                "candidate_scan_limit": 10,
                "candidate_scanned_count": 2,
                "requested_count": 2,
                "publication_cutoff": "2026-03-01T00:00:00+08:00",
                "entries": [
                    {
                        "note_id": "note_a",
                        "source_position": 1,
                        "outcome": "selected",
                        "publication_eligible": True,
                        "likes_eligible": True,
                        "exact_likes": 20,
                        "selection_rank": 2,
                    },
                    {
                        "note_id": "note_b",
                        "source_position": 4,
                        "outcome": "selected",
                        "publication_eligible": True,
                        "likes_eligible": True,
                        "exact_likes": 50,
                        "selection_rank": 1,
                    },
                ],
            },
        }
    )
    return CollectionRun.model_validate(values)


def test_extension_batch_renderer_uses_selected_rank_without_leaking_ledger_details(
    tmp_path: Path,
) -> None:
    from xhs_workbench.renderer import write_result_bundle

    html = write_result_bundle(_extension_batch_run(), _output_dir(tmp_path)).html_path.read_text(
        encoding="utf-8"
    )

    assert html.index("笔记 ID：note_b") < html.index("笔记 ID：note_a")
    assert "按精确点赞排序，仅来自已检查候选" in html
    assert "当前候选列表第 4 条（非互动排名）" in html
    assert "当前候选列表第 1 条（非互动排名）" in html
    assert "已选排名</dt><dd>第 1 名" in html
    for hidden in (
        "precision",
        "provenance",
        "likes_not_exact",
        "selection_rank",
        "extension_selection",
        "https://sns",
    ):
        assert hidden not in html


def test_page_order_renderer_scope_does_not_claim_likes_sorting(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    values = _extension_batch_run().model_dump()
    template = values["notes"][0]
    for position in range(3, 6):
        note = dict(template)
        note.update(
            {
                "note_id": f"page_{position}",
                "canonical_url": f"https://www.xiaohongshu.com/explore/page_{position}",
                "source_position": position,
            }
        )
        values["notes"].append(note)
    values["requested_count"] = 5
    values["actual_count"] = 5
    for position, note in enumerate(values["notes"], start=1):
        note.update(
            {
                "note_id": f"page_{position}",
                "canonical_url": f"https://www.xiaohongshu.com/explore/page_{position}",
                "source_position": position,
            }
        )
    values["extension_selection"]["selection_basis"] = "page_order"
    values["extension_selection"]["publication_cutoff"] = None
    values["extension_selection"].update(
        {"candidate_scan_limit": 5, "candidate_scanned_count": 5, "requested_count": 5, "selected_count": 5}
    )
    values["extension_search_run"] = {
        "source_page_url": "https://www.xiaohongshu.com/search_result",
        "requested_count": 5,
        "selected_count": 5,
        "enriched_count": 5,
        "scroll_rounds": 0,
        "status": "complete",
    }
    values["extension_selection"]["entries"] = [
        {
            "note_id": f"page_{position}",
            "source_position": position,
            "outcome": "selected",
            "publication_eligible": False,
            "likes_eligible": False,
            "selection_rank": position,
            "selection_basis": "page_order",
            "inclusion": "included",
            "detail_outcome": "enriched",
        }
        for position in range(1, 6)
    ]
    for entry in values["extension_selection"]["entries"]:
        entry.update(
            {
                "selection_basis": "page_order",
                "inclusion": "included",
                "exact_likes": None,
                "likes_eligible": False,
                "selection_rank": entry["source_position"],
                "detail_outcome": "enriched",
            }
        )
    for note in values["notes"]:
        note.update(
            {
                "selection_basis": "page_order",
                "selection_rank": note["source_position"],
            }
        )
    run = CollectionRun.model_validate(values)

    html = write_result_bundle(run, _output_dir(tmp_path)).html_path.read_text(encoding="utf-8")

    assert "当前页面顺序" in html
    assert "按精确点赞排序，仅来自已检查候选" not in html


def test_renderer_keeps_historical_runs_in_source_position_order(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    base = _run()
    later = base.notes[0].model_copy(
        update={
            "note_id": "note_b",
            "canonical_url": "https://www.xiaohongshu.com/explore/note_b",
            "source_position": 4,
        }
    )
    earlier = base.notes[0].model_copy(update={"source_position": 1})
    html = write_result_bundle(
        base.model_copy(update={"actual_count": 2, "notes": [later, earlier]}), _output_dir(tmp_path)
    ).html_path.read_text(encoding="utf-8")

    assert html.index("笔记 ID：note_a") < html.index("笔记 ID：note_b")
    assert "按精确点赞排序，仅来自已检查候选" not in html


def _output_dir(tmp_path: Path) -> Path:
    output = tmp_path / "run_fixture"
    assets = output / "assets"
    assets.mkdir(parents=True)
    (assets / "note-cover.jpg").write_bytes(JPEG)
    (assets / "account-avatar.jpg").write_bytes(JPEG)
    return output


def _media_asset(path: str, body: bytes, mime_type: MediaMime) -> LocalAsset:
    return LocalAsset(
        local_path=path,
        mime_type=mime_type,
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )


def _run_with_media(
    slots: list[NoteMediaSlot], *, status: RunStatus = RunStatus.COMPLETE
) -> CollectionRun:
    run = _run(status=status)
    note = run.notes[0]
    first_cover = next(
        (
            slot.asset
            for slot in slots
            if slot.role in {"image", "video_cover"}
            and slot.position == 1
            and slot.asset is not None
        ),
        None,
    )
    updated = note.model_copy(
        update={
            "media_manifest_version": 2,
            "media_discovered_count": len([slot for slot in slots if slot.role == "image"]),
            "media_discovery_truncated": False,
            "media_slots": slots,
            "cover_local_path": first_cover.local_path if first_cover else None,
            "cover_asset": first_cover,
        }
    )
    return run.model_copy(update={"notes": [updated]})


def _output_with_media(tmp_path: Path, files: dict[str, bytes]) -> Path:
    output = _output_dir(tmp_path)
    for relative_path, body in files.items():
        destination = output / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
    return output


def test_write_result_bundle_escapes_visible_text_and_only_uses_verified_local_assets(
    tmp_path: Path,
) -> None:
    from xhs_workbench.renderer import write_result_bundle

    output = _output_dir(tmp_path)
    paths = write_result_bundle(_run(), output)
    html = paths.html_path.read_text(encoding="utf-8")
    saved = json.loads(paths.json_path.read_text(encoding="utf-8"))

    assert paths.json_path == output / "results.json"
    assert paths.html_path == output / "index.html"
    assert '<meta charset="utf-8">' in html
    assert "<script" not in html.lower()
    assert "http://" not in html
    assert "&lt;标题 &amp; 作者&gt;" in html
    assert "&lt;账号名&gt;" in html
    assert "&lt;简介 &amp; 文本&gt;" in html
    assert 'src="assets/note-cover.jpg"' in html
    assert 'src="assets/account-avatar.jpg"' in html
    assert "<dt>总互动</dt><dd>1209</dd>" in html
    assert "（约值）" not in html
    assert "结果仅引用已校验的本地媒体文件；不加载远程图片或视频。" in html
    assert saved["notes"][0]["cover_asset"]["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert saved["account"]["avatar_asset"]["mime_type"] == "image/jpeg"


def test_write_result_bundle_keeps_strict_account_diagnostics_out_of_html(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    output = _output_dir(tmp_path)
    base = _run()
    assert base.account is not None
    account = base.account.model_copy(
        update={
            "name": "Hana",
            "field_statuses": {
                "name": "exposed",
                "bio": "exposed_empty",
                "note_count": "not_exposed",
                "follower_count": "not_exposed",
                "avatar": "exposed",
                "platform_metrics": "not_exposed",
            },
        }
    )
    note = base.notes[0].model_copy(
        update={"metric_provenance": {"shares": "account_card_interface"}}
    )
    run = base.model_copy(
        update={
            "account": account,
            "notes": [note],
            "candidate_attempts": [
                CandidateAttempt(
                    position=1,
                    outcome="unavailable",
                    stage="candidate",
                    reason="candidate_shortfall",
                ),
                CandidateAttempt(
                    position=2,
                    note_id="note_b",
                    outcome="unavailable",
                    stage="detail_navigation",
                    reason="detail_timeout",
                ),
            ],
            "pacing_summary": PacingSummary(
                policy="conservative_jitter_v1",
                profile_open_delay_ms=2_000,
                detail_delay_ms=[3_000],
            ),
        }
    )

    paths = write_result_bundle(run, output)
    html = paths.html_path.read_text(encoding="utf-8")
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))

    for hidden in (
        "conservative_jitter_v1",
        "candidate_shortfall",
        "detail_timeout",
        "exposed_empty",
        "account_card_interface",
        "display_rounded",
    ):
        assert hidden not in html
    assert "Hana" in html
    assert "笔记 ID" in html
    assert payload["candidate_attempts"][0]["reason"] == "candidate_shortfall"
    assert payload["candidate_attempts"][1]["reason"] == "detail_timeout"
    assert payload["pacing_summary"]["policy"] == "conservative_jitter_v1"
    assert payload["account"]["field_statuses"]["bio"] == "exposed_empty"
    assert payload["notes"][0]["metric_provenance"]["shares"] == "account_card_interface"


def test_write_result_bundle_rejects_sensitive_text_before_creating_artifacts(
    tmp_path: Path,
) -> None:
    from xhs_workbench.renderer import BundleWriteError, write_result_bundle

    output = _output_dir(tmp_path)

    with pytest.raises(BundleWriteError, match="unsafe_bundle"):
        write_result_bundle(_run(body="web" + "_session=do-not-retain"), output)

    assert not (output / "results.json").exists()
    assert not (output / "index.html").exists()
    assert (output / "assets" / "note-cover.jpg").read_bytes() == JPEG


def test_write_result_bundle_rejects_bearer_style_sensitive_text(tmp_path: Path) -> None:
    from xhs_workbench.renderer import BundleWriteError, write_result_bundle

    output = _output_dir(tmp_path)

    with pytest.raises(BundleWriteError, match="unsafe_bundle"):
        write_result_bundle(_run(body="Authorization Bearer do-not-retain"), output)

    assert not (output / "results.json").exists()
    assert not (output / "index.html").exists()


def test_write_result_bundle_never_overwrites_existing_result_files(tmp_path: Path) -> None:
    from xhs_workbench.renderer import BundleWriteError, write_result_bundle

    output = _output_dir(tmp_path)
    existing = output / "results.json"
    existing.write_text("user-owned", encoding="utf-8")

    with pytest.raises(BundleWriteError, match="output_conflict"):
        write_result_bundle(_run(), output)

    assert existing.read_text(encoding="utf-8") == "user-owned"
    assert not (output / "index.html").exists()


def test_write_result_bundle_rolls_back_its_first_file_when_second_link_fails(
    monkeypatch, tmp_path: Path
) -> None:
    from xhs_workbench import renderer

    output = _output_dir(tmp_path)
    real_link = renderer.os.link
    calls = 0

    def fail_second_link(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        real_link(*args, **kwargs)

    monkeypatch.setattr(renderer.os, "link", fail_second_link)

    with pytest.raises(renderer.BundleWriteError, match="output_write_failed"):
        renderer.write_result_bundle(_run(), output)

    assert not (output / "results.json").exists()
    assert not (output / "index.html").exists()
    assert list(output.glob("*.tmp")) == []


@pytest.mark.parametrize("late_failure_call", [2, 4])
def test_write_result_bundle_rolls_back_files_after_post_link_fsync_failure(
    monkeypatch, tmp_path: Path, late_failure_call: int
) -> None:
    from xhs_workbench import renderer

    output = _output_dir(tmp_path)
    real_fsync = renderer.os.fsync
    calls = 0

    def fail_late_fsync(file_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == late_failure_call:
            raise OSError("simulated late durability failure")
        real_fsync(file_descriptor)

    monkeypatch.setattr(renderer.os, "fsync", fail_late_fsync)

    with pytest.raises(renderer.BundleWriteError, match="output_write_failed"):
        renderer.write_result_bundle(_run(), output)

    assert not (output / "results.json").exists()
    assert not (output / "index.html").exists()
    assert list(output.glob("*.tmp")) == []


def test_write_result_bundle_labels_missing_visible_fields_and_note_author(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    output = _output_dir(tmp_path)
    run = _run()
    note = run.notes[0].model_copy(
        update={
            "cover_local_path": None,
            "cover_asset": None,
            "author_name": "可见作者",
            "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_a",
            "missing_fields": ["cover", "body"],
        }
    )
    account = run.account.model_copy(
        update={
            "avatar_local_path": None,
            "avatar_asset": None,
            "platform_metrics": {},
            "missing_fields": ["avatar", "bio"],
        }
    )
    paths = write_result_bundle(
        run.model_copy(update={"account": account, "notes": [note]}), output
    )
    rendered = paths.html_path.read_text(encoding="utf-8")

    for expected in (
        "账号头像</dt><dd>未公开",
        "平台互动字段</dt><dd>未公开",
        "作品封面</dt><dd>未公开",
        "作者</dt><dd>可见作者",
        "https://www.xiaohongshu.com/user/profile/author_a",
        "缺失字段</dt><dd>avatar、bio",
        "缺失字段</dt><dd>cover、body",
    ):
        assert expected in rendered


def test_write_result_bundle_renders_an_empty_run(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    output = _output_dir(tmp_path)
    empty = _run().model_copy(update={"actual_count": 0, "notes": []})
    paths = write_result_bundle(empty, output)

    assert "本次没有可展示的作品" in paths.html_path.read_text(encoding="utf-8")


def test_fixed_research_card_renders_labeled_persisted_note_evidence(tmp_path: Path) -> None:
    """Keep the card concise while preserving hidden evidence in JSON."""
    from xhs_workbench.renderer import write_result_bundle

    image = _media_asset("assets/note_a-image-001.jpg", JPEG, "image/jpeg")
    slots = [
        NoteMediaSlot(note_id="note_a", role="image", position=1, status="downloaded", asset=image)
    ]
    note = _run_with_media(slots).notes[0].model_copy(
        update={
            "title": "Example title",
            "note_type": "normal",
            "time_evidence": TimeEvidence(kind="edited", raw_text="编辑于 08-12"),
            "metric_provenance": {
                "likes": "search_card_interface",
                "collects": "detail_visible_count",
                "comments": "detail_visible_count",
                "shares": "search_card_interface",
            },
        }
    )
    output = _output_with_media(tmp_path, {image.local_path: JPEG})

    paths = write_result_bundle(
        _run_with_media(slots).model_copy(update={"notes": [note]}), output
    )
    html = paths.html_path.read_text(encoding="utf-8")
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))

    assert "笔记 ID：note_a" in html
    assert "<dt>帖子标题</dt><dd>Example title</dd>" in html
    assert "<dt>类型</dt><dd>图文笔记</dd>" in html
    assert "<dt>原始类型</dt><dd>normal</dd>" in html
    assert "编辑时间" in html
    assert "编辑于 08-12" in html
    assert "当前候选列表第 2 条（非互动排名）" in html
    assert "<dt>本地路径</dt><dd>assets/note_a-image-001.jpg</dd>" in html
    for hidden_label in (
        "点赞精度",
        "点赞来源",
        "收藏精度",
        "收藏来源",
        "评论精度",
        "评论来源",
        "分享精度",
        "分享来源",
        "总互动精度",
    ):
        assert f"<dt>{hidden_label}</dt>" not in html
    assert {
        name: metric["precision"]
        for name, metric in payload["notes"][0]["metrics"].items()
    } == {
        "likes": "display_rounded",
        "collects": "exact",
        "comments": "exact",
        "shares": "exact",
    }
    assert payload["notes"][0]["metric_provenance"] == {
        "likes": "search_card_interface",
        "collects": "detail_visible_count",
        "comments": "detail_visible_count",
        "shares": "search_card_interface",
    }


def test_fixed_research_card_renders_mixed_unavailable_evidence_and_media_metadata(
    tmp_path: Path,
) -> None:
    from xhs_workbench.renderer import write_result_bundle

    image = _media_asset("assets/note_a-image-001.jpg", JPEG, "image/jpeg")
    slots = [
        NoteMediaSlot(note_id="note_a", role="image", position=1, status="downloaded", asset=image),
        NoteMediaSlot(
            note_id="note_a",
            role="image",
            position=2,
            status="missing",
            missing_reason="source_not_exposed",
        ),
    ]
    note = _run_with_media(slots).notes[0].model_copy(
        update={
            "published_at": None,
            "time_evidence": TimeEvidence(kind="edited", raw_text="编辑于 08-12"),
            "metric_provenance": {},
            "missing_fields": ["published_at"],
        }
    )
    output = _output_with_media(tmp_path, {image.local_path: JPEG})

    html = write_result_bundle(
        _run_with_media(slots).model_copy(update={"notes": [note]}), output
    ).html_path.read_text(encoding="utf-8")

    for expected in (
        "发布时间</dt><dd>未公开",
        "编辑时间",
        "编辑于 08-12",
        "<dt>本地路径</dt><dd>assets/note_a-image-001.jpg</dd>",
        "<dt>未取得原因</dt><dd>平台未公开来源</dd>",
        "缺失字段</dt><dd>published_at</dd>",
    ):
        assert expected in html


def test_fixed_research_card_labels_unknown_note_type_without_hiding_raw_value(
    tmp_path: Path,
) -> None:
    """Catch an unfamiliar upstream type being either misclassified or hidden."""
    from xhs_workbench.renderer import write_result_bundle

    output = _output_dir(tmp_path)
    note = _run().notes[0].model_copy(update={"note_type": "live_note"})

    html = write_result_bundle(
        _run().model_copy(update={"notes": [note]}), output
    ).html_path.read_text(encoding="utf-8")

    assert "<dt>类型</dt><dd>其他笔记类型</dd>" in html
    assert "<dt>原始类型</dt><dd>live_note</dd>" in html


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.PARTIAL])
def test_write_result_bundle_preserves_failed_and_partial_run_status(
    tmp_path: Path, status: RunStatus
) -> None:
    from xhs_workbench.renderer import write_result_bundle

    output = _output_dir(tmp_path)
    paths = write_result_bundle(_run(status=status), output)

    assert json.loads(paths.json_path.read_text(encoding="utf-8"))["status"] == status.value
    assert status.value in paths.html_path.read_text(encoding="utf-8")


def test_report_renders_ordered_local_gallery_and_manifest(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    first = _media_asset("assets/note_a-image-001.jpg", JPEG, "image/jpeg")
    second = _media_asset("assets/note_a-image-002.webp", WEBP, "image/webp")
    third = _media_asset("assets/note_a-image-003.png", PNG, "image/png")
    slots = [
        NoteMediaSlot(note_id="note_a", role="image", position=1, status="downloaded", asset=first),
        NoteMediaSlot(
            note_id="note_a", role="image", position=2, status="downloaded", asset=second
        ),
        NoteMediaSlot(note_id="note_a", role="image", position=3, status="downloaded", asset=third),
    ]
    output = _output_with_media(
        tmp_path,
        {first.local_path: JPEG, second.local_path: WEBP, third.local_path: PNG},
    )

    paths = write_result_bundle(_run_with_media(slots), output)
    html = paths.html_path.read_text(encoding="utf-8")
    saved = json.loads(paths.json_path.read_text(encoding="utf-8"))

    assert html.index('src="assets/note_a-image-001.jpg"') < html.index(
        'src="assets/note_a-image-002.webp"'
    ) < html.index('src="assets/note_a-image-003.png"')
    assert [slot["position"] for slot in saved["notes"][0]["media_slots"]] == [1, 2, 3]
    assert first.sha256 in html and second.sha256 in html and third.sha256 in html
    assert "image/jpeg" in html and "image/webp" in html and "image/png" in html
    assert "https://sns" not in html


def test_report_renders_local_video_without_autoplay_or_remote_source(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    poster = _media_asset("assets/note_a-video-cover.jpg", JPEG, "image/jpeg")
    video = _media_asset("assets/note_a-video.mp4", MP4, "video/mp4")
    slots = [
        NoteMediaSlot(
            note_id="note_a", role="video_cover", position=1, status="downloaded", asset=poster
        ),
        NoteMediaSlot(
            note_id="note_a",
            role="video",
            position=1,
            status="downloaded",
            asset=video,
            duration_ms=12345,
        ),
    ]
    output = _output_with_media(tmp_path, {poster.local_path: JPEG, video.local_path: MP4})

    html = write_result_bundle(_run_with_media(slots), output).html_path.read_text(encoding="utf-8")

    assert '<video controls preload="metadata"' in html
    assert 'poster="assets/note_a-video-cover.jpg"' in html
    assert '<source src="assets/note_a-video.mp4" type="video/mp4">' in html
    assert "autoplay" not in html
    assert "https://sns" not in html


@pytest.mark.parametrize("video_status", ["missing", "rejected"])
def test_report_preserves_a_local_video_poster_when_video_is_unavailable(
    tmp_path: Path, video_status: str
) -> None:
    from xhs_workbench.renderer import write_result_bundle

    poster = _media_asset("assets/note_a-video-cover.jpg", JPEG, "image/jpeg")
    slots = [
        NoteMediaSlot(
            note_id="note_a", role="video_cover", position=1, status="downloaded", asset=poster
        ),
        NoteMediaSlot(
            note_id="note_a",
            role="video",
            position=1,
            status=video_status,  # type: ignore[arg-type]
            missing_reason="source_not_exposed",
        ),
    ]
    output = _output_with_media(tmp_path, {poster.local_path: JPEG})
    run = _run_with_media(slots, status=RunStatus.PARTIAL)
    note = run.notes[0].model_copy(update={"cover_local_path": None, "cover_asset": None})

    html = write_result_bundle(run.model_copy(update={"notes": [note]}), output).html_path.read_text(
        encoding="utf-8"
    )

    assert '<img src="assets/note_a-video-cover.jpg" alt="视频封面">' in html
    assert "视频未下载，保留本地封面" in html
    assert "视频文件" in html and "平台未公开来源" in html
    assert "<video " not in html


def test_bundle_canonicalizes_v2_image_slots_for_json_html_and_validation(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    first = _media_asset("assets/note_a-image-001.jpg", JPEG, "image/jpeg")
    second = _media_asset("assets/note_a-image-002.webp", WEBP, "image/webp")
    slots = [
        NoteMediaSlot(
            note_id="note_a", role="image", position=2, status="downloaded", asset=second
        ),
        NoteMediaSlot(note_id="note_a", role="image", position=1, status="downloaded", asset=first),
    ]
    output = _output_with_media(tmp_path, {first.local_path: JPEG, second.local_path: WEBP})

    paths = write_result_bundle(_run_with_media(slots), output)
    saved = json.loads(paths.json_path.read_text(encoding="utf-8"))
    html = paths.html_path.read_text(encoding="utf-8")

    assert [slot["position"] for slot in saved["notes"][0]["media_slots"]] == [1, 2]
    assert html.index('src="assets/note_a-image-001.jpg"') < html.index(
        'src="assets/note_a-image-002.webp"'
    )
    assert html.index("第 1 张图片") < html.index("第 2 张图片")


def test_bundle_canonicalizes_video_cover_before_video_in_json_and_metadata(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    poster = _media_asset("assets/note_a-video-cover.jpg", JPEG, "image/jpeg")
    video = _media_asset("assets/note_a-video.mp4", MP4, "video/mp4")
    slots = [
        NoteMediaSlot(
            note_id="note_a",
            role="video",
            position=1,
            status="downloaded",
            asset=video,
            duration_ms=1000,
        ),
        NoteMediaSlot(
            note_id="note_a", role="video_cover", position=1, status="downloaded", asset=poster
        ),
    ]
    output = _output_with_media(tmp_path, {poster.local_path: JPEG, video.local_path: MP4})

    paths = write_result_bundle(_run_with_media(slots), output)
    saved = json.loads(paths.json_path.read_text(encoding="utf-8"))
    html = paths.html_path.read_text(encoding="utf-8")

    assert [slot["role"] for slot in saved["notes"][0]["media_slots"]] == ["video_cover", "video"]
    assert html.index("视频封面") < html.index("视频文件")


def test_report_displays_exact_missing_media_reason(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    first = _media_asset("assets/note_a-image-001.jpg", JPEG, "image/jpeg")
    slots = [
        NoteMediaSlot(note_id="note_a", role="image", position=1, status="downloaded", asset=first),
        NoteMediaSlot(
            note_id="note_a",
            role="image",
            position=2,
            status="missing",
            missing_reason="source_not_exposed",
        ),
    ]
    output = _output_with_media(tmp_path, {first.local_path: JPEG})

    html = write_result_bundle(_run_with_media(slots, status=RunStatus.PARTIAL), output).html_path.read_text(
        encoding="utf-8"
    )

    assert "第 2 张图片" in html
    assert "平台未公开来源" in html


def test_report_keeps_a_labeled_legacy_single_cover_readable(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    paths = write_result_bundle(_run(), _output_dir(tmp_path))
    html = paths.html_path.read_text(encoding="utf-8")

    assert 'src="assets/note-cover.jpg"' in html
    assert "历史单封面结果" in html


def test_report_keeps_an_empty_v2_manifest_readable(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    output = _output_dir(tmp_path)
    html = write_result_bundle(_run_with_media([]), output).html_path.read_text(encoding="utf-8")

    assert "媒体清单为空" in html
    assert "历史单封面结果" not in html


@pytest.mark.parametrize(
    ("asset_path", "replacement"),
    [
        ("assets/note_a-image-002.webp", b"RIFF\x08\x00\x00\x00WEBPreplaced"),
        ("assets/note_a-video.mp4", MP4 + b"tampered"),
    ],
)
def test_bundle_rejects_replaced_or_tampered_downloaded_manifest_assets(
    tmp_path: Path, asset_path: str, replacement: bytes
) -> None:
    from xhs_workbench.renderer import BundleWriteError, write_result_bundle

    first = _media_asset("assets/note_a-image-001.jpg", JPEG, "image/jpeg")
    second = _media_asset("assets/note_a-image-002.webp", WEBP, "image/webp")
    video = _media_asset("assets/note_a-video.mp4", MP4, "video/mp4")
    slots = [
        NoteMediaSlot(note_id="note_a", role="image", position=1, status="downloaded", asset=first),
        NoteMediaSlot(
            note_id="note_a", role="image", position=2, status="downloaded", asset=second
        ),
    ]
    run = _run_with_media(slots)
    if asset_path == video.local_path:
        video_slots = [
            NoteMediaSlot(
                note_id="note_a", role="video_cover", position=1, status="downloaded", asset=first
            ),
            NoteMediaSlot(
                note_id="note_a",
                role="video",
                position=1,
                status="downloaded",
                asset=video,
                duration_ms=1000,
            ),
        ]
        run = _run_with_media(video_slots)
    output = _output_with_media(
        tmp_path,
        {
            first.local_path: JPEG,
            second.local_path: WEBP,
            video.local_path: MP4,
            asset_path: replacement,
        },
    )

    with pytest.raises(BundleWriteError, match="invalid_local_asset"):
        write_result_bundle(run, output)

    assert not (output / "results.json").exists()
    assert not (output / "index.html").exists()


def test_bundle_rejects_mp4_prefix_without_valid_container(tmp_path: Path) -> None:
    from xhs_workbench.renderer import BundleWriteError, write_result_bundle

    poster = _media_asset("assets/note_a-video-cover.jpg", JPEG, "image/jpeg")
    fake_mp4 = b"\x00\x00\x00\x18ftypisomnot-a-container"
    video = _media_asset("assets/note_a-video.mp4", fake_mp4, "video/mp4")
    slots = [
        NoteMediaSlot(
            note_id="note_a", role="video_cover", position=1, status="downloaded", asset=poster
        ),
        NoteMediaSlot(
            note_id="note_a",
            role="video",
            position=1,
            status="downloaded",
            asset=video,
            duration_ms=1000,
        ),
    ]
    output = _output_with_media(tmp_path, {poster.local_path: JPEG, video.local_path: fake_mp4})

    with pytest.raises(BundleWriteError, match="invalid_local_asset"):
        write_result_bundle(_run_with_media(slots), output)


def test_bundle_rejects_a_missing_downloaded_manifest_video(tmp_path: Path) -> None:
    from xhs_workbench.renderer import BundleWriteError, write_result_bundle

    poster = _media_asset("assets/note_a-video-cover.jpg", JPEG, "image/jpeg")
    video = _media_asset("assets/note_a-video.mp4", MP4, "video/mp4")
    slots = [
        NoteMediaSlot(
            note_id="note_a", role="video_cover", position=1, status="downloaded", asset=poster
        ),
        NoteMediaSlot(
            note_id="note_a",
            role="video",
            position=1,
            status="downloaded",
            asset=video,
            duration_ms=1000,
        ),
    ]
    output = _output_with_media(tmp_path, {poster.local_path: JPEG})

    with pytest.raises(BundleWriteError, match="invalid_local_asset"):
        write_result_bundle(_run_with_media(slots), output)


def test_bundle_enumerates_every_downloaded_v2_manifest_asset(tmp_path: Path) -> None:
    from xhs_workbench.renderer import BundleWriteError, write_result_bundle

    first = _media_asset("assets/note_a-image-001.jpg", JPEG, "image/jpeg")
    second = _media_asset("assets/note_a-image-002.webp", WEBP, "image/webp")
    slots = [
        NoteMediaSlot(note_id="note_a", role="image", position=1, status="downloaded", asset=first),
        NoteMediaSlot(
            note_id="note_a", role="image", position=2, status="downloaded", asset=second
        ),
    ]
    output = _output_with_media(tmp_path, {first.local_path: JPEG})

    with pytest.raises(BundleWriteError, match="invalid_local_asset"):
        write_result_bundle(_run_with_media(slots), output)


def test_bundle_rejects_a_symlinked_downloaded_manifest_asset(tmp_path: Path) -> None:
    from xhs_workbench.renderer import BundleWriteError, write_result_bundle

    first = _media_asset("assets/note_a-image-001.jpg", JPEG, "image/jpeg")
    second = _media_asset("assets/note_a-image-002.webp", WEBP, "image/webp")
    slots = [
        NoteMediaSlot(note_id="note_a", role="image", position=1, status="downloaded", asset=first),
        NoteMediaSlot(
            note_id="note_a", role="image", position=2, status="downloaded", asset=second
        ),
    ]
    output = _output_with_media(tmp_path, {first.local_path: JPEG})
    (output / second.local_path).symlink_to(output / first.local_path)

    with pytest.raises(BundleWriteError, match="invalid_local_asset"):
        write_result_bundle(_run_with_media(slots), output)


def test_bundle_rejects_sensitive_query_text_in_a_v2_manifest_before_writing(
    tmp_path: Path,
) -> None:
    from xhs_workbench.renderer import BundleWriteError, write_result_bundle

    safe_asset = _media_asset("assets/note_a-image-001.jpg", JPEG, "image/jpeg")
    unsafe_asset = safe_asset.model_copy(
        update={"local_path": "assets/note_a-image-001.jpg?xsec_token=secret"}
    )
    safe_slot = NoteMediaSlot(
        note_id="note_a", role="image", position=1, status="downloaded", asset=safe_asset
    )
    slot = safe_slot.model_copy(
        update={"asset": unsafe_asset}
    )
    output = _output_dir(tmp_path)

    with pytest.raises(BundleWriteError, match="unsafe_bundle"):
        write_result_bundle(_run_with_media([slot]), output)

    assert not (output / "results.json").exists()
    assert not (output / "index.html").exists()


def test_publication_date_is_full_china_calendar_date(tmp_path: Path) -> None:
    from xhs_workbench.renderer import write_result_bundle

    run = _run()
    note = run.notes[0].model_copy(update={
        "published_at": datetime(2026, 8, 29, 17, 0, tzinfo=UTC),
    })
    paths = write_result_bundle(run.model_copy(update={"notes": [note]}), _output_dir(tmp_path))
    rendered = paths.html_path.read_text(encoding="utf-8")
    assert "<dt>发布时间</dt><dd>2026-08-30</dd>" in rendered
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert payload["notes"][0]["published_at"].startswith("2026-08-29T17:00:00")


def test_legacy_naive_publication_date_does_not_depend_on_machine_timezone() -> None:
    import os
    import time

    from xhs_workbench.renderer import _publication_date

    original = os.environ.get("TZ")
    try:
        for tz in ("Asia/Shanghai", "Asia/Tokyo", "America/Los_Angeles"):
            os.environ["TZ"] = tz
            time.tzset()
            assert _publication_date(datetime(2026, 8, 29)) == "2026-08-29"  # noqa: DTZ001 - legacy date-only input
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()
