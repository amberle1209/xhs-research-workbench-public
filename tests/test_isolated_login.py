import stat
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from xhs_workbench import isolated_login
from xhs_workbench.isolated_login import acquire_project_login


class FakePage:
    def __init__(self, authenticated_states: list[bool] | None = None) -> None:
        self.visits: list[tuple[str, str, int]] = []
        self.authenticated_states = authenticated_states or []
        self.expressions: list[str] = []

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.visits.append((url, wait_until, timeout))

    def evaluate(self, _expression: str) -> bool:
        self.expressions.append(_expression)
        return self.authenticated_states.pop(0) if self.authenticated_states else False


class FakeContext:
    def __init__(
        self,
        cookie_snapshots: list[list[dict[str, str]]],
        authenticated_states: list[bool] | None = None,
    ) -> None:
        self.pages = [FakePage(authenticated_states)]
        self.cookie_snapshots = cookie_snapshots
        self.closed = False

    def cookies(self, _urls: list[str]) -> list[dict[str, str]]:
        return self.cookie_snapshots.pop(0)


class FakeBrowser(AbstractContextManager[FakeContext]):
    def __init__(self, context: FakeContext) -> None:
        self.context = context

    def __enter__(self) -> FakeContext:
        return self.context

    def __exit__(self, *_args: object) -> None:
        self.context.closed = True


def test_default_browser_uses_macos_chinese_locale_and_fonts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    browser = FakeBrowser(FakeContext([]))

    class Module:
        @staticmethod
        def Camoufox(**kwargs: object) -> FakeBrowser:
            captured.update(kwargs)
            return browser

    monkeypatch.setattr(isolated_login, "import_module", lambda _name: Module)

    result = isolated_login._default_browser_factory(tmp_path)

    assert result is browser
    assert captured["os"] == "macos"
    assert captured["locale"] == ["zh-CN", "zh", "en-US"]
    fonts = captured["fonts"]
    assert isinstance(fonts, list)
    assert {"PingFang SC", "Hiragino Sans GB", "Songti SC"}.issubset(fonts)


def test_authenticated_page_check_includes_visible_profile_navigation_fallback() -> None:
    page = FakePage([True])

    assert isolated_login._page_has_authenticated_user(page) is True
    assert "/user/profile/" in page.expressions[0]
    assert "我" in page.expressions[0]


def test_project_login_uses_fixed_private_profile_and_returns_only_required_cookies(
    tmp_path: Path,
) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    context = FakeContext(
        [
            [{"name": "a1", "value": "public-a1"}],
            [
                {"name": "a1", "value": "private-a1"},
                {"name": "web_session", "value": "private-session"},
                {"name": "unrelated", "value": "discard-me"},
            ],
        ],
        authenticated_states=[True],
    )
    seen_profiles: list[Path] = []

    def browser_factory(profile: Path) -> FakeBrowser:
        seen_profiles.append(profile)
        return FakeBrowser(context)

    now = iter([0.0, 0.1, 0.2])
    result = acquire_project_login(
        auth_dir,
        browser_factory=browser_factory,
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
        monotonic=lambda: next(now),
        sleeper=lambda _seconds: None,
    )

    assert result == {"a1": "private-a1", "web_session": "private-session"}
    assert seen_profiles == [auth_dir / "browser-profile"]
    assert stat.S_IMODE((auth_dir / "browser-profile").stat().st_mode) == 0o700
    assert context.pages[0].visits == [
        ("https://www.xiaohongshu.com/explore", "domcontentloaded", 30_000)
    ]
    assert context.closed is True


def test_project_login_rejects_symlinked_profile(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (auth_dir / "browser-profile").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        acquire_project_login(
            auth_dir,
            browser_factory=lambda _profile: pytest.fail("browser must not launch"),
        )


def test_project_login_closes_browser_when_required_cookie_never_arrives(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    context = FakeContext([[{"name": "a1", "value": "public-a1"}]])

    class Times:
        values = iter([0.0, 2.0])

        @classmethod
        def next(cls) -> float:
            return next(cls.values)

    result = acquire_project_login(
        auth_dir,
        browser_factory=lambda _profile: FakeBrowser(context),
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
        monotonic=Times.next,
        sleeper=lambda _seconds: None,
    )

    assert result is None
    assert context.closed is True


def test_project_login_rejects_complete_cookies_without_authenticated_page(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    context = FakeContext(
        [
            [
                {"name": "a1", "value": "public-a1"},
                {"name": "web_session", "value": "anonymous-session"},
            ]
        ],
        authenticated_states=[False],
    )
    now = iter([0.0, 0.1, 2.0])

    result = acquire_project_login(
        auth_dir,
        browser_factory=lambda _profile: FakeBrowser(context),
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
        monotonic=lambda: next(now),
        sleeper=lambda _seconds: None,
    )

    assert result is None
    assert context.closed is True
