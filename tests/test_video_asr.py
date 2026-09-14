"""Boundaries of the local-only ASR adapter, without downloading a model in unit tests."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from xhs_workbench.video_asr import duration_milliseconds, transcript_text


@pytest.mark.parametrize(
    ("seconds", "expected"), [("300", 300000), ("300.0001", 300001), ("0.001", 1)]
)
def test_duration_rounds_up_without_shortening(seconds, expected):
    assert duration_milliseconds(Decimal(seconds)) == expected


@pytest.mark.parametrize("seconds", ["0", "-1", "NaN", "Infinity"])
def test_duration_unknown_is_not_short(seconds):
    with pytest.raises(ValueError):
        duration_milliseconds(Decimal(seconds))


def test_transcript_preserves_speech_and_separates_paragraphs():
    segments = [
        SimpleNamespace(text=" 第一段。 ", start=0, end=2),
        SimpleNamespace(text="不要改写。", start=2, end=4),
        SimpleNamespace(text="第二段。", start=7, end=8),
    ]
    assert transcript_text(segments) == "第一段。不要改写。\n\n第二段。"


def test_empty_transcript_is_not_fabricated():
    assert transcript_text([SimpleNamespace(text=" ", start=0, end=1)]) == ""


def test_probe_requires_reliable_positive_duration(monkeypatch, tmp_path):
    from xhs_workbench import video_asr

    class Container:
        streams = SimpleNamespace(video=[SimpleNamespace(width=160, height=120)], audio=[])
        duration = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(
        video_asr.importlib, "import_module", lambda _: SimpleNamespace(open=lambda _: Container())
    )
    with pytest.raises(ValueError, match="duration_unknown"):
        video_asr.probe_video(tmp_path / "video.mp4")


@pytest.mark.parametrize(
    ("duration", "audio", "status"),
    [
        (900000, False, "skipped_no_audio"),
        (900001, False, "skipped_too_long"),
        (900001, True, "skipped_too_long"),
    ],
)
def test_skip_precedes_model_load(monkeypatch, tmp_path, duration, audio, status):
    from xhs_workbench import video_asr

    def reject_model(*args, **kwargs):
        raise AssertionError("model must not load")

    monkeypatch.setattr(video_asr.importlib, "import_module", lambda _: reject_model())
    monkeypatch.setattr(
        video_asr, "probe_video", lambda _: {"duration_ms": duration, "audio_present": audio}
    )
    assert video_asr.transcribe(tmp_path / "video", tmp_path / "model")["status"] == status


def test_exactly_fifteen_minutes_enters_transcription(monkeypatch, tmp_path):
    from xhs_workbench import video_asr

    class Model:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, *_args, **_kwargs):
            return [SimpleNamespace(text="十五分钟", start=0, end=1)], None

    monkeypatch.setattr(video_asr, "probe_video", lambda _: {
        "duration_ms": 900000, "audio_present": True
    })
    monkeypatch.setattr(video_asr, "model_ready", lambda _: True)
    monkeypatch.setattr(video_asr.importlib, "import_module", lambda _: SimpleNamespace(WhisperModel=Model))
    assert video_asr.transcribe(tmp_path / "video", tmp_path / "model")["status"] == "complete"


def test_unpunctuated_chinese_is_readable_without_rewriting_words():
    segments = [
        SimpleNamespace(text="这是第一句", start=0, end=2),
        SimpleNamespace(text="不要改写观点", start=2, end=4),
    ]
    assert transcript_text(segments) == "这是第一句，不要改写观点。"
