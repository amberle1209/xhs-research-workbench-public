"""Human-assisted login in a project-owned Camoufox profile.

This module deliberately has no system-browser cookie extraction path.  The
only browser state it can open lives below the caller-provided private auth
directory.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

_LOGIN_URL = "https://www.xiaohongshu.com/explore"
_COOKIE_URLS = ["https://www.xiaohongshu.com"]
_REQUIRED_COOKIES = frozenset({"a1", "web_session"})
_DEFAULT_TIMEOUT_SECONDS = 240.0
_DEFAULT_POLL_INTERVAL_SECONDS = 1.0


class _Page(Protocol):
    def goto(self, url: str, *, wait_until: str, timeout: int) -> object: ...

    def evaluate(self, expression: str) -> object: ...


class _BrowserContext(Protocol):
    pages: list[_Page]

    def new_page(self) -> _Page: ...

    def cookies(self, urls: list[str]) -> list[dict[str, object]]: ...


_BrowserFactory = Callable[[Path], AbstractContextManager[_BrowserContext]]


def acquire_project_login(
    auth_dir: Path,
    *,
    browser_factory: _BrowserFactory | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, str] | None:
    """Open a visible project browser and return only the required cookies."""
    if timeout_seconds <= 0 or poll_interval_seconds < 0:
        raise ValueError("invalid login timing")
    profile_dir = _prepare_profile_directory(auth_dir)
    factory = browser_factory or _default_browser_factory
    deadline = monotonic() + timeout_seconds
    with factory(profile_dir) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        while monotonic() < deadline:
            allowed = _required_cookie_values(context.cookies(_COOKIE_URLS))
            if allowed is not None and _page_has_authenticated_user(page):
                return allowed
            sleeper(poll_interval_seconds)
    return None


def _default_browser_factory(profile_dir: Path) -> AbstractContextManager[_BrowserContext]:
    module = import_module("camoufox" + ".sync_api")
    camoufox = cast(Callable[..., object], module.Camoufox)
    context_manager = camoufox(
        headless=False,
        persistent_context=True,
        user_data_dir=str(profile_dir),
        os="macos",
        locale=["zh-CN", "zh", "en-US"],
        fonts=[
            "PingFang SC",
            "Hiragino Sans GB",
            "Songti SC",
            "Heiti SC",
            "Arial Unicode MS",
        ],
        firefox_user_prefs={
            "signon.rememberSignons": False,
            "signon.autofillForms": False,
            "signon.formlessCapture.enabled": False,
            "browser.shell.checkDefaultBrowser": False,
        },
    )
    return cast(AbstractContextManager[_BrowserContext], context_manager)


def _required_cookie_values(raw: list[dict[str, object]]) -> dict[str, str] | None:
    allowed: dict[str, str] = {}
    for item in raw:
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and name in _REQUIRED_COOKIES and isinstance(value, str) and value:
            allowed[name] = value
    return allowed if _REQUIRED_COOKIES.issubset(allowed) else None


def _page_has_authenticated_user(page: _Page) -> bool:
    expression = r"""() => {
        const unwrap = (value) => {
            if (!value || typeof value !== "object") return value;
            if (Object.prototype.hasOwnProperty.call(value, "value")) return value.value;
            return value;
        };
        const state = window.__INITIAL_STATE__;
        const visible = (element) => {
            if (!element || !element.getBoundingClientRect) return false;
            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 &&
                style.display !== "none" && style.visibility !== "hidden";
        };
        const profileLink = Array.from(
            document.querySelectorAll('a[href*="/user/profile/"]')
        ).some((element) => element.textContent.trim() === "我" && visible(element));
        const navigationMe = Array.from(
            document.querySelectorAll('nav *, aside *, [class*="side"] *, [class*="Side"] *')
        ).some((element) =>
            element.children.length === 0 && element.textContent.trim() === "我" && visible(element)
        );
        if (profileLink || navigationMe) return true;
        if (!state || typeof state !== "object") return false;
        const candidates = [
            state.user && state.user.currentUser,
            state.user && state.user.userInfo,
            state.user && state.user.loginUser,
            state.user && state.user.userPageData,
            state.sidebar && state.sidebar.user,
            state.app && state.app.user,
        ];
        return candidates.some((raw) => {
            const value = unwrap(raw);
            if (!value || typeof value !== "object") return false;
            const nested = unwrap(value.userInfo) || unwrap(value.basicInfo) || value;
            const identifiers = [
                value.userId, value.user_id, value.id,
                nested && nested.userId, nested && nested.user_id, nested && nested.id,
            ];
            return identifiers.some(
                (item) => typeof item === "string" && item.length >= 6
            );
        });
    }"""
    try:
        return page.evaluate(expression) is True
    except Exception:  # noqa: BLE001 - a changing login page stays unauthenticated.
        return False


def _prepare_profile_directory(auth_dir: Path) -> Path:
    if not _is_owned_private_directory(auth_dir):
        raise ValueError("unsafe auth directory")
    profile = auth_dir / "browser-profile"
    if profile.is_symlink() or _has_existing_symlink(profile):
        raise ValueError("unsafe browser profile")
    profile.mkdir(mode=0o700, exist_ok=True)
    profile.chmod(0o700)
    if not _is_owned_private_directory(profile):
        raise ValueError("unsafe browser profile")
    return profile.resolve(strict=True)


def _is_owned_private_directory(path: Path) -> bool:
    if _has_existing_symlink(path):
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o700
        and info.st_uid == os.geteuid()
    )


def _has_existing_symlink(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    for part in path.parts:
        if part in {path.anchor, "."}:
            continue
        current /= part
        if current.exists() and current.is_symlink():
            return True
    return False
