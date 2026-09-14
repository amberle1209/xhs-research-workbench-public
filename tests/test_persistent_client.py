from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pytest

from xhs_workbench import persistent_client, xhs_bridge
from xhs_workbench.models import PacingSummary
from xhs_workbench.persistent_client import (
    _NOTE_DETAIL_SCRIPT,
    _SAFE_CARD_SCROLL_INTO_VIEW_SCRIPT,
    _USER_PROFILE_SCRIPT,
    DetailReadFailure,
    PersistentXhsClient,
    _note_metadata,
    _project_note_detail,
    _project_user_profile,
)


class _DetailScriptHarness:
    """Return a supplied detail-DOM projection while recording its invocation."""

    def __init__(self, raw: dict[str, object]) -> None:
        self.raw = raw
        self.calls: list[tuple[str, object | None]] = []

    def evaluate(self, expression: str, arg: object | None = None) -> object:
        self.calls.append((expression, arg))
        return self.raw


def _evaluate_detail_script(
    *,
    expected_note_id: str,
    detail_images: list[str],
    duplicate_clone: str | None = None,
    recommendation_video: str | None = None,
    player_video: str | None = None,
    structured_notes: dict[str, object] | None = None,
) -> dict[str, object]:
    """Exercise the exact script call with a fixture for the visible DOM/state."""
    raw: dict[str, object] = {
        "media_scope_valid": True,
        "images": [{"position": index + 1, "url": url} for index, url in enumerate(detail_images)],
        "media_discovered_count": len(detail_images),
        "has_video": player_video is not None,
        "video": None,
    }
    if player_video is not None and player_video.startswith("https://"):
        raw["video"] = {"poster": "", "url": player_video, "duration_ms": None}
    if structured_notes is not None:
        candidate = structured_notes.get(expected_note_id)
        if isinstance(candidate, dict):
            urls = candidate.get("video_urls")
            if (
                candidate.get("note_id") == expected_note_id
                and isinstance(urls, list)
                and len(urls) == 1
                and isinstance(urls[0], str)
                and urls[0].startswith("https://")
                and urls[0].endswith((".mp4", ".webm"))
            ):
                raw["video"] = {"poster": "", "url": urls[0], "duration_ms": None}
    harness = _DetailScriptHarness(raw)
    assert harness.evaluate(_NOTE_DETAIL_SCRIPT, expected_note_id) is raw
    assert harness.calls == [(_NOTE_DETAIL_SCRIPT, expected_note_id)]
    assert recommendation_video is None or raw["video"] is None
    return raw


class FakeContext:
    def __init__(self) -> None:
        self.pages: list[object] = [object()]
        self.closed = False

    def new_page(self) -> object:
        page = object()
        self.pages.append(page)
        return page


class FakeBrowser(AbstractContextManager[FakeContext]):
    def __init__(self, context: FakeContext) -> None:
        self.context = context

    def __enter__(self) -> FakeContext:
        return self.context

    def __exit__(self, *_args: object) -> None:
        self.context.closed = True


class FakePinnedDelegate:
    def __init__(self) -> None:
        self._page: object | None = None
        self.start_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        raise AssertionError("persistent client must not start a second browser")

    def search_notes(self, keyword: str) -> object:
        assert self._page is not None
        return [keyword]

    def get_note_detail(self, note_id: str, xsec_token: str = "") -> object:
        assert self._page is not None
        return {"note_id": note_id, "xsec_token": xsec_token}

    def get_user_info(self, user_id: str) -> object:
        assert self._page is not None
        return {"user_id": user_id}

    def get_user_posts(self, user_id: str) -> object:
        assert self._page is not None
        return [{"user_id": user_id}]


class FakeResponse:
    def __init__(
        self,
        path: str,
        payload: object,
        *,
        status: int = 200,
        response_url: str | None = None,
    ) -> None:
        self._path = path
        self._payload = payload
        self._status = status
        self._response_url = response_url

    @property
    def url(self) -> str:
        origin = {
            "/api/sns/web/v2/search/notes": "https://so.xiaohongshu.com",
            "/api/sns/web/v1/search/notes": "https://so.xiaohongshu.com",
            "/api/sns/web/v1/user_posted": "https://edith.xiaohongshu.com",
        }.get(self._path, "https://www.xiaohongshu.com")
        return self._response_url or f"{origin}{self._path}"

    @property
    def status(self) -> int:
        return self._status

    def json(self) -> object:
        return self._payload


class FakeElement:
    def __init__(
        self, page: FakePage, selector: str, has_selector: str | None, href: str | None = None
    ) -> None:
        self._page = page
        self._selector = selector
        self._has_selector = has_selector
        self._href = href

    def evaluate(self, expression: str, arg: object | None = None) -> object:
        if self._href is not None:
            if "scrollIntoView" in expression:
                return self._page.scroll_exact_card_into_view(self._href, expression, arg)
            return self._page.safe_card_click_point(self._href, expression, arg)
        return self._page.activate_card_if_fully_in_viewport(
            self._selector, self._has_selector, expression
        )

    def bounding_box(self) -> dict[str, float] | None:
        return self._page.card_bounds(self._selector, self._has_selector)


class FakeFunctionResult:
    def __init__(self, element: FakeElement | None) -> None:
        self._element = element

    def as_element(self) -> FakeElement | None:
        return self._element


class FakeMouse:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    def click(self, x: float, y: float, **_options: object) -> None:
        self._page.click_resolved_card_at(x, y)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector
        self._has_selector: str | None = None

    def filter(self, *, has: FakeLocator) -> FakeLocator:
        self._has_selector = has._selector
        return self

    @property
    def first(self) -> FakeLocator:
        return self

    def element_handle(self, *, timeout: int) -> FakeElement | None:
        self._page.element_handle_timeouts.append(timeout)
        href = self._page.first_card_href()
        if href is None:
            return None
        return FakeElement(self._page, self._selector, self._has_selector, href)

    def element_handles(self) -> list[FakeElement]:
        return [
            FakeElement(self._page, self._selector, self._has_selector, href)
            for href in self._page.card_hrefs()
        ]

    def click(self, **_options: object) -> None:
        self._page.click_visible_card(self._selector, self._has_selector)


class FakePage:
    def __init__(
        self,
        *,
        route_responses: dict[str, list[FakeResponse]],
        evaluated_values: list[object],
        final_paths: dict[str, str] | None = None,
        final_urls: dict[str, str] | None = None,
        card_final_paths: dict[str, str] | None = None,
        card_final_urls: dict[str, str] | None = None,
        hidden_first_note_ids: set[str] | None = None,
        off_viewport_note_ids: set[str] | None = None,
        drifting_note_ids: set[str] | None = None,
        card_hrefs: list[str] | None = None,
        overlay_note_ids: set[str] | None = None,
        reflowed_note_ids: set[str] | None = None,
        unsafe_control_note_ids: set[str] | None = None,
        center_anchor_hrefs: dict[str, str] | None = None,
        scroll_failure_note_ids: set[str] | None = None,
        scroll_leaves_off_viewport_note_ids: set[str] | None = None,
        card_hrefs_after_scroll: dict[str, str] | None = None,
        card_hrefs_after_exact_wait: list[str] | None = None,
        profile_card_hrefs: list[str] | None = None,
        delayed_route_responses: dict[str, tuple[int, list[FakeResponse]]] | None = None,
        pre_navigation_responses: list[FakeResponse] | None = None,
        before_target_page_responses: list[FakeResponse] | None = None,
        initial_url: str = "about:blank",
    ) -> None:
        self._route_responses = route_responses
        self._evaluated_values = evaluated_values
        self._final_paths = final_paths or {}
        self._final_urls = final_urls or {}
        self._card_final_paths = card_final_paths or {}
        self._card_final_urls = card_final_urls or {}
        self._hidden_first_note_ids = hidden_first_note_ids or set()
        self._off_viewport_note_ids = off_viewport_note_ids or set()
        self._drifting_note_ids = drifting_note_ids or set()
        self._card_hrefs = card_hrefs or []
        self._card_hrefs_are_explicit = card_hrefs is not None
        self._overlay_note_ids = overlay_note_ids or set()
        self._reflowed_note_ids = reflowed_note_ids or set()
        self._unsafe_control_note_ids = unsafe_control_note_ids or set()
        self._center_anchor_hrefs = center_anchor_hrefs or {}
        self._scroll_failure_note_ids = scroll_failure_note_ids or set()
        self._scroll_leaves_off_viewport_note_ids = scroll_leaves_off_viewport_note_ids or set()
        self._card_hrefs_after_scroll = card_hrefs_after_scroll or {}
        self._scrolled_note_ids: set[str] = set()
        self._card_hrefs_after_exact_wait = card_hrefs_after_exact_wait
        self._profile_card_hrefs = profile_card_hrefs
        self._delayed_route_responses = delayed_route_responses or {}
        self._pre_navigation_responses = pre_navigation_responses or []
        self._before_target_page_responses = before_target_page_responses or []
        self._listeners: list[object] = []
        self._pending_responses: list[tuple[int, list[FakeResponse]]] = []
        self.navigations: list[str] = []
        self.clicked_note_ids: list[str] = []
        self.element_handle_timeouts: list[int] = []
        self.exact_card_waits: list[tuple[str, object | None, dict[str, object]]] = []
        self.card_activation_expressions: list[str] = []
        self.card_safety_evaluations: list[tuple[str, object | None, str]] = []
        self.card_scroll_evaluations: list[tuple[str, object | None, str]] = []
        self.mouse_clicks: list[tuple[float, float]] = []
        self.evaluations: list[tuple[str, object | None]] = []
        self.waited_urls: list[tuple[str, dict[str, object]]] = []
        self._mouse_target: tuple[str, str | None] | None = None
        self.waits: list[int] = []
        self.url = initial_url

    def on(self, event: str, handler: object) -> None:
        assert event == "response"
        self._listeners.append(handler)
        self._emit(self._pre_navigation_responses)

    def remove_listener(self, event: str, handler: object) -> None:
        assert event == "response"
        self._listeners.remove(handler)

    def goto(self, url: str, **_options: object) -> None:
        self.navigations.append(url)
        path = urlsplit(url).path
        self._emit(self._before_target_page_responses)
        self.url = self._final_urls.get(path, self._final_paths.get(path, url))
        responses = self._route_responses.get(path, [])
        if _options.get("wait_until") == "commit":
            self._pending_responses.append((0, responses))
        else:
            self._emit(responses)
        delayed = self._delayed_route_responses.get(path)
        if delayed is not None:
            self._pending_responses.append(delayed)

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)
        pending = self._pending_responses
        self._pending_responses = []
        for delay, responses in pending:
            if delay <= milliseconds:
                self._emit(responses)
            else:
                self._pending_responses.append((delay, responses))

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    @property
    def mouse(self) -> FakeMouse:
        return FakeMouse(self)

    def wait_for_url(self, url: str, **options: object) -> None:
        self.waited_urls.append((url, options))
        if callable(url) and not url(self.url):
            raise ValueError("exact detail navigation did not complete")

    def wait_for_function(
        self, expression: str, *, arg: object | None = None, **options: object
    ) -> FakeFunctionResult:
        self.exact_card_waits.append((expression, arg, options))
        if self._card_hrefs_after_exact_wait is not None:
            self._card_hrefs = list(self._card_hrefs_after_exact_wait)
        if not isinstance(arg, str):
            return FakeFunctionResult(None)
        href = next(
            (
                candidate
                for candidate in self._card_hrefs
                if self._matches_card_path(candidate, arg, expression)
            ),
            None,
        )
        return FakeFunctionResult(
            FakeElement(self, "section.note-item:not(.query-note-item):visible", None, href)
            if href is not None
            else None
        )

    def click_visible_card(self, selector: str, has_selector: str | None) -> None:
        note_id = self._note_id_from_selector(selector, has_selector)
        if note_id in self._drifting_note_ids:
            raise RuntimeError("locator re-resolved a different card")
        self._open_note(note_id, selector)

    def click_resolved_card(self, selector: str, has_selector: str | None) -> None:
        self._open_note(self._note_id_from_selector(selector, has_selector), selector)

    def click_resolved_card_at(self, x: float, y: float) -> None:
        target = self._mouse_target
        assert target is not None
        self.mouse_clicks.append((x, y))
        assert (x, y) == (60.0, 60.0)
        self.click_resolved_card(*target)

    def first_card_href(self) -> str | None:
        return self._card_hrefs[0] if self._card_hrefs else None

    def card_hrefs(self) -> list[str]:
        return list(self._card_hrefs)

    def _matches_card_path(self, href: str, expected_path: str, expression: str) -> bool:
        parsed = urlsplit(urljoin(self.url, href))
        return (
            parsed.scheme == "https"
            and parsed.hostname == "www.xiaohongshu.com"
            and (parsed.port in (None, 443) or "parsed.port === ''" not in expression)
            and parsed.path == expected_path
        )

    def _profile_dom_candidates(self, expression: str) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        seen_note_ids: set[str] = set()
        for href in self._profile_card_hrefs or []:
            parsed = urlsplit(href)
            match = re.fullmatch(r"/explore/([A-Za-z0-9_-]+)", parsed.path)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "www.xiaohongshu.com"
                or (parsed.port not in (None, 443) and "parsed.port !== ''" in expression)
                or match is None
                or match[1] in seen_note_ids
            ):
                continue
            seen_note_ids.add(match[1])
            candidates.append({"note_id": match[1], "xsec_token": ""})
        return candidates

    def _note_id_from_selector(self, selector: str, has_selector: str | None) -> str:
        assert selector.startswith("section.note-item:not(.query-note-item)")
        assert has_selector is not None
        prefix = 'a[href="/explore/'
        assert has_selector.startswith(prefix) and has_selector.endswith('"]')
        href = has_selector.removeprefix(prefix).removesuffix('"]')
        return urlsplit(href).path.removeprefix("/explore/")

    def _open_note(self, note_id: str, selector: str) -> None:
        if note_id in self._hidden_first_note_ids and not selector.endswith(":visible"):
            raise RuntimeError("first matching card is hidden")
        self.clicked_note_ids.append(note_id)
        path = f"/explore/{note_id}"
        self.url = self._card_final_urls.get(
            note_id, f"https://www.xiaohongshu.com{self._card_final_paths.get(note_id, path)}"
        )
        self._emit(self._route_responses.get(path, []))

    def activate_card_if_fully_in_viewport(
        self, selector: str, has_selector: str | None, expression: str
    ) -> bool:
        self.card_activation_expressions.append(expression)
        if not self.card_is_fully_in_viewport(selector, has_selector):
            return False
        if "element.click()" in expression:
            self.click_resolved_card(selector, has_selector)
        return True

    def card_is_fully_in_viewport(self, selector: str, has_selector: str | None) -> bool:
        assert selector.startswith("section.note-item:not(.query-note-item)")
        assert has_selector is not None
        prefix = 'a[href="/explore/'
        assert has_selector.startswith(prefix) and has_selector.endswith('"]')
        note_id = has_selector.removeprefix(prefix).removesuffix('"]')
        return note_id not in self._off_viewport_note_ids

    def card_bounds(self, selector: str, has_selector: str | None) -> dict[str, float]:
        self._mouse_target = (selector, has_selector)
        if self.card_is_fully_in_viewport(selector, has_selector):
            return {"x": 10.0, "y": 20.0, "width": 100.0, "height": 80.0}
        return {"x": 10.0, "y": 20.0, "width": 100.0, "height": 81.0}

    def safe_card_click_point(
        self, href: str, expression: str, expected_path: object | None
    ) -> dict[str, float] | None:
        original_href = href
        original_path = urlsplit(original_href).path
        original_note_id = original_path.removeprefix("/explore/")
        if original_note_id in self._scrolled_note_ids:
            href = self._card_hrefs_after_scroll.get(original_note_id, href)
        self.card_safety_evaluations.append((href, expected_path, expression))
        if not isinstance(expected_path, str):
            return {"state": "unsafe"}
        parsed = urlsplit(href)
        if (
            parsed.scheme not in ("", "https")
            or (parsed.hostname is not None and parsed.hostname != "www.xiaohongshu.com")
            or parsed.port not in (None, 443)
            or parsed.path != expected_path
        ):
            return {"state": "not_matching"}
        note_id = parsed.path.removeprefix("/explore/")
        if note_id in self._off_viewport_note_ids and note_id not in self._scrolled_note_ids:
            return {"state": "outside_viewport"}
        if (
            note_id in self._overlay_note_ids
            or note_id in self._reflowed_note_ids
            or note_id in self._unsafe_control_note_ids
        ):
            return {"state": "unsafe"}
        center_anchor_href = self._center_anchor_hrefs.get(note_id)
        if center_anchor_href is not None:
            raw_anchor = center_anchor_href
            if "getAttribute('href')" not in expression:
                raw_anchor = urljoin(self.url, raw_anchor)
            center_anchor = urlsplit(urljoin(self.url, raw_anchor))
            raw_route = center_anchor_href
            if not raw_route.startswith("/"):
                raw_route = re.sub(
                    r"^https://www\.xiaohongshu\.com(?::443)?(?=/|$)",
                    "",
                    raw_route,
                    flags=re.IGNORECASE,
                )
            raw_path = re.split(r"[?#]", raw_route, maxsplit=1)[0]
            profile_match = re.fullmatch(
                r"/user/profile/([A-Za-z0-9_-]+)(?:/([A-Za-z0-9_-]+))?", raw_path
            )
            if (
                not center_anchor_href
                or center_anchor_href.startswith(("#", "?", "//"))
                or center_anchor.scheme != "https"
                or center_anchor.hostname != "www.xiaohongshu.com"
                or center_anchor.port not in (None, 443)
                or center_anchor.path != raw_path
                or profile_match is None
                or (profile_match[2] is not None and profile_match[2] != note_id)
                or (profile_match[2] is not None and "expectedNoteMatch" not in expression)
                or "isReadOnlyProfile" not in expression
            ):
                return {"state": "unsafe"}
        self._mouse_target = (
            "section.note-item:not(.query-note-item):visible",
            f'a[href="/explore/{note_id}"]',
        )
        return {"state": "safe", "x": 60.0, "y": 60.0}

    def scroll_exact_card_into_view(
        self, href: str, expression: str, expected_path: object | None
    ) -> dict[str, str]:
        self.card_scroll_evaluations.append((href, expected_path, expression))
        if not isinstance(expected_path, str):
            return {"state": "not_matching"}
        parsed = urlsplit(href)
        note_id = parsed.path.removeprefix("/explore/")
        current = urlsplit(urljoin(self.url, href))
        if (
            current.scheme != "https"
            or current.hostname != "www.xiaohongshu.com"
            or current.port not in (None, 443)
            or current.path != expected_path
        ):
            return {"state": "not_matching"}
        if note_id in self._scroll_failure_note_ids:
            return {"state": "scroll_failed"}
        if note_id not in self._scroll_leaves_off_viewport_note_ids:
            self._scrolled_note_ids.add(note_id)
        return {"state": "scrolled"}

    def _emit(self, responses: list[FakeResponse]) -> None:
        for response in responses:
            for handler in self._listeners:
                assert callable(handler)
                handler(response)

    def evaluate(self, expression: str, arg: object | None = None) -> object:
        self.evaluations.append((expression, arg))
        if "window.innerWidth" in expression and "window.innerHeight" in expression:
            return {"width": 200.0, "height": 100.0}
        if "const candidates" in expression and self._profile_card_hrefs is not None:
            candidates = self._profile_dom_candidates(expression)
            if isinstance(arg, str):
                for candidate in candidates:
                    candidate["profile_id"] = arg
            return candidates
        value = self._evaluated_values.pop(0)
        if (
            isinstance(value, dict)
            and isinstance(arg, str)
            and "profileMatch" in expression
            and "profile_id" not in value
        ):
            value = {**value, "profile_id": arg}
        if isinstance(value, list) and isinstance(arg, str) and "const candidates" in expression:
            value = [
                {**item, "profile_id": arg}
                if isinstance(item, dict) and "profile_id" not in item
                else item
                for item in value
            ]
        if "const candidates" in expression and not self._card_hrefs_are_explicit:
            self._card_hrefs = [
                f"/explore/{item['note_id']}"
                for item in value
                if isinstance(item, dict) and isinstance(item.get("note_id"), str)
            ]
        return value


class RejectingPinnedDelegate(FakePinnedDelegate):
    def search_notes(self, keyword: str) -> object:
        raise AssertionError(f"unexpected pinned search for {keyword}")

    def get_note_detail(self, note_id: str, xsec_token: str = "") -> object:
        raise AssertionError(f"unexpected pinned detail for {note_id}")

    def get_user_info(self, user_id: str) -> object:
        raise AssertionError(f"unexpected pinned profile for {user_id}")

    def get_user_posts(self, user_id: str) -> object:
        raise AssertionError(f"unexpected pinned posts for {user_id}")


def _compatibility_client(
    tmp_path: Path,
    page: FakePage,
    delegate: RejectingPinnedDelegate,
    *,
    delay_sampler: Callable[[int, int], int] | None = None,
) -> PersistentXhsClient:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    context = FakeContext()
    context.pages = [page]
    return PersistentXhsClient(
        auth_dir,
        browser_factory=lambda _profile_dir: FakeBrowser(context),
        delegate_factory=lambda: delegate,
        delay_sampler=delay_sampler or (lambda low, _high: low),
    )


def _assert_exact_detail_wait(page: FakePage, note_id: str) -> None:
    assert len(page.waited_urls) == 1
    predicate, options = page.waited_urls[0]
    assert callable(predicate)
    assert predicate(f"https://www.xiaohongshu.com/explore/{note_id}?view=card#detail")
    assert not predicate(f"http://www.xiaohongshu.com/explore/{note_id}")
    assert not predicate(f"https://example.test/explore/{note_id}")
    assert not predicate(f"https://www.xiaohongshu.com:444/explore/{note_id}")
    assert not predicate(f"https://www.xiaohongshu.com/explore/{note_id}-other")
    assert options == {"wait_until": "domcontentloaded", "timeout": 20_000}


def _assert_exact_card_wait(page: FakePage, note_id: str) -> None:
    assert len(page.exact_card_waits) == 1
    expression, expected_path, options = page.exact_card_waits[0]
    assert expected_path == f"/explore/{note_id}"
    assert options == {"timeout": 20_000}
    assert "new URL" in expression
    assert "parsed.protocol === 'https:'" in expression
    assert "parsed.hostname === 'www.xiaohongshu.com'" in expression
    assert "parsed.port === ''" in expression
    assert "parsed.pathname === expectedPath" in expression
    assert "scroll" not in expression.lower()


def test_persistent_client_reuses_one_fixed_profile_without_starting_pinned_delegate(
    tmp_path: Path,
) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(mode=0o700)
    context = FakeContext()
    delegate = FakePinnedDelegate()
    seen_profiles: list[Path] = []

    def browser_factory(profile_dir: Path) -> FakeBrowser:
        seen_profiles.append(profile_dir)
        return FakeBrowser(context)

    with PersistentXhsClient(
        auth_dir,
        browser_factory=browser_factory,
        delegate_factory=lambda: delegate,
    ) as client:
        assert client.search_notes("AI workflow") == ["AI workflow"]
        assert client.get_note_detail("note_a", "ephemeral") == {
            "note_id": "note_a",
            "xsec_token": "ephemeral",
        }
        assert client.get_user_info("author_a") == {"user_id": "author_a"}
        assert client.get_user_posts("author_a") == [{"user_id": "author_a"}]

    assert seen_profiles == [auth_dir / "browser-profile"]
    assert delegate.start_calls == 0
    assert context.closed is True


def test_persistent_client_rejects_a_page_without_exact_card_wait_capability(
    tmp_path: Path,
) -> None:
    page = FakePage(route_responses={}, evaluated_values=[])
    page.wait_for_function = None  # type: ignore[method-assign]

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        assert client._current_page() is None


def test_persistent_client_rejects_a_nondefault_port_profile_redirect(tmp_path: Path) -> None:
    page = FakePage(
        route_responses={},
        evaluated_values=[],
        final_urls={
            "/user/profile/account_A-1": "https://www.xiaohongshu.com:444/user/profile/account_A-1"
        },
    )

    with (
        _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client,
        pytest.raises(ValueError, match="unexpected profile page"),
    ):
        client.get_user_info("account_A-1")

    assert page._evaluated_values == []


def test_persistent_client_projects_current_v2_search_items_from_fixed_page(tmp_path: Path) -> None:
    page = FakePage(
        route_responses={
            "/search_result": [
                FakeResponse(
                    "/api/sns/web/v2/search/notes",
                    {
                        "data": {
                            "items": [
                                {
                                    "model_type": "note",
                                    "id": "note_A-1",
                                    "xsec_token": "",
                                    "note_card": {"display_title": "Visible card"},
                                },
                                {"model_type": "user", "id": "account_A-1"},
                            ]
                        }
                    },
                )
            ]
        },
        evaluated_values=[],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        result = client.search_notes("AI workflow")

    assert result == [
        {
            "model_type": "note",
            "id": "note_A-1",
            "xsec_token": "",
            "note_card": {"display_title": "Visible card"},
        }
    ]
    assert [urlsplit(url).path for url in page.navigations] == ["/search_result"]
    assert page.waits == [4000]
    assert page._listeners == []


@pytest.mark.parametrize(
    "response_url",
    (
        "https://hostile.example/api/sns/web/v2/search/notes",
        "https://www.xiaohongshu.com/api/sns/web/v2/search/notes",
        "http://so.xiaohongshu.com/api/sns/web/v2/search/notes",
        "https://so.xiaohongshu.com:444/api/sns/web/v2/search/notes",
        "https://reader@so.xiaohongshu.com/api/sns/web/v2/search/notes",
        "https://so.xiaohongshu.com/api/sns/web/v2/search/notes/extra",
    ),
)
def test_persistent_client_rejects_noncanonical_search_response_urls(
    tmp_path: Path, response_url: str
) -> None:
    page = FakePage(
        route_responses={
            "/search_result": [
                FakeResponse(
                    "/api/sns/web/v2/search/notes",
                    {
                        "data": {
                            "items": [
                                {
                                    "model_type": "note",
                                    "id": "hostile_A-1",
                                    "note_card": {"display_title": "Hostile card"},
                                }
                            ]
                        }
                    },
                    response_url=response_url,
                )
            ]
        },
        evaluated_values=[],
    )

    with (
        _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client,
        pytest.raises(RuntimeError, match="search response unavailable"),
    ):
        client.search_notes("AI workflow")


def test_persistent_client_rejects_a_stale_search_response_before_navigation_begins(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={},
        evaluated_values=[],
        initial_url=(
            "https://www.xiaohongshu.com/search_result?"
            "keyword=AI%20workflow&source=web_search_result_notes"
        ),
        pre_navigation_responses=[
            FakeResponse(
                "/api/sns/web/v2/search/notes",
                {
                    "data": {
                        "items": [
                            {
                                "model_type": "note",
                                "id": "stale_A-1",
                                "note_card": {"display_title": "Stale card"},
                            }
                        ]
                    }
                },
            )
        ],
    )

    with (
        _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client,
        pytest.raises(RuntimeError, match="search response unavailable"),
    ):
        client.search_notes("AI workflow")


def test_persistent_client_rejects_a_stale_search_response_before_the_target_page_commits(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={},
        evaluated_values=[],
        initial_url=(
            "https://www.xiaohongshu.com/search_result?"
            "keyword=AI%20workflow&source=web_search_result_notes"
        ),
        before_target_page_responses=[
            FakeResponse(
                "/api/sns/web/v2/search/notes",
                {
                    "data": {
                        "items": [
                            {
                                "model_type": "note",
                                "id": "in_flight_A-1",
                                "note_card": {"display_title": "In-flight card"},
                            }
                        ]
                    }
                },
            )
        ],
    )

    with (
        _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client,
        pytest.raises(RuntimeError, match="search response unavailable"),
    ):
        client.search_notes("AI workflow")


def test_persistent_client_rejects_a_search_response_from_a_different_query_context(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={
            "/search_result": [
                FakeResponse(
                    "/api/sns/web/v2/search/notes",
                    {
                        "data": {
                            "items": [
                                {
                                    "model_type": "note",
                                    "id": "wrong_query_A-1",
                                    "note_card": {"display_title": "Wrong query card"},
                                }
                            ]
                        }
                    },
                )
            ]
        },
        evaluated_values=[],
        final_urls={
            "/search_result": (
                "https://www.xiaohongshu.com/search_result?"
                "keyword=different&source=web_search_result_notes"
            )
        },
    )

    with (
        _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client,
        pytest.raises(RuntimeError, match="search response unavailable"),
    ):
        client.search_notes("AI workflow")


def test_persistent_client_accepts_a_search_response_with_reordered_target_query(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={
            "/search_result": [
                FakeResponse(
                    "/api/sns/web/v2/search/notes",
                    {
                        "data": {
                            "items": [
                                {
                                    "model_type": "note",
                                    "id": "reordered_A-1",
                                    "note_card": {"display_title": "Reordered card"},
                                }
                            ]
                        }
                    },
                )
            ]
        },
        evaluated_values=[],
        final_urls={
            "/search_result": (
                "https://www.xiaohongshu.com/search_result?"
                "source=web_search_result_notes&keyword=AI%20workflow"
            )
        },
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        result = client.search_notes("AI workflow")

    assert result == [
        {
            "model_type": "note",
            "id": "reordered_A-1",
            "note_card": {"display_title": "Reordered card"},
        }
    ]


def test_persistent_client_accepts_the_known_search_type_page_parameter(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={
            "/search_result": [
                FakeResponse(
                    "/api/sns/web/v2/search/notes",
                    {
                        "data": {
                            "items": [
                                {
                                    "model_type": "note",
                                    "id": "typed_A-1",
                                    "note_card": {"display_title": "Typed card"},
                                }
                            ]
                        }
                    },
                )
            ]
        },
        evaluated_values=[],
        final_urls={
            "/search_result": (
                "https://www.xiaohongshu.com/search_result?"
                "source=web_search_result_notes&type=51&keyword=AI%20workflow"
            )
        },
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        result = client.search_notes("AI workflow")

    assert result == [
        {
            "model_type": "note",
            "id": "typed_A-1",
            "note_card": {"display_title": "Typed card"},
        }
    ]


def test_persistent_client_rejects_an_unknown_search_type_page_parameter(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={
            "/search_result": [
                FakeResponse(
                    "/api/sns/web/v2/search/notes",
                    {
                        "data": {
                            "items": [
                                {
                                    "model_type": "note",
                                    "id": "wrong_type_A-1",
                                    "note_card": {"display_title": "Wrong type card"},
                                }
                            ]
                        }
                    },
                )
            ]
        },
        evaluated_values=[],
        final_urls={
            "/search_result": (
                "https://www.xiaohongshu.com/search_result?"
                "keyword=AI%20workflow&source=web_search_result_notes&type=52"
            )
        },
    )

    with (
        _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client,
        pytest.raises(RuntimeError, match="search response unavailable"),
    ):
        client.search_notes("AI workflow")


def test_persistent_client_preserves_an_explicit_empty_search_response(tmp_path: Path) -> None:
    page = FakePage(
        route_responses={
            "/search_result": [
                FakeResponse("/api/sns/web/v2/search/notes", {"data": {"items": []}})
            ]
        },
        evaluated_values=[],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        assert client.search_notes("AI workflow") == []


def test_persistent_client_fails_closed_when_no_trusted_search_response_arrives(
    tmp_path: Path,
) -> None:
    page = FakePage(route_responses={}, evaluated_values=[])

    with (
        _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client,
        pytest.raises(RuntimeError, match="search response unavailable"),
    ):
        client.search_notes("AI workflow")


def test_persistent_client_rejects_user_posts_from_a_different_account_page_context(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse(
                    "/api/sns/web/v1/user_posted",
                    {"data": {"notes": [{"note_id": "wrong_account_A-1"}]}},
                )
            ]
        },
        evaluated_values=[[]],
        final_urls={
            "/user/profile/account_A-1": (
                "https://www.xiaohongshu.com/user/profile/account_A-1?tab=videos"
            )
        },
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        assert client.get_user_posts("account_A-1") == []


def test_persistent_client_captures_user_posts_only_from_edith_origin(tmp_path: Path) -> None:
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse(
                    "/api/sns/web/v1/user_posted",
                    {"data": {"notes": [{"note_id": "post_A-1"}]}},
                    response_url=("https://edith.xiaohongshu.com/api/sns/web/v1/user_posted"),
                )
            ]
        },
        evaluated_values=[],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        assert client.get_user_posts("account_A-1") == [{"note_id": "post_A-1", "xsec_token": ""}]


def test_persistent_client_rejects_user_posts_from_a_search_origin(tmp_path: Path) -> None:
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse(
                    "/api/sns/web/v1/user_posted",
                    {"data": {"notes": [{"note_id": "wrong_origin_A-1"}]}},
                    response_url="https://so.xiaohongshu.com/api/sns/web/v1/user_posted",
                )
            ]
        },
        evaluated_values=[[]],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        assert client.get_user_posts("account_A-1") == []


def test_persistent_client_captures_a_v2_search_response_that_arrives_during_settle(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={},
        delayed_route_responses={
            "/search_result": (
                2463,
                [
                    FakeResponse(
                        "/api/sns/web/v2/search/notes",
                        {
                            "data": {
                                "items": [
                                    {
                                        "model_type": "note",
                                        "id": "delayed_A-1",
                                        "xsec_token": "",
                                        "note_card": {"display_title": "Delayed card"},
                                    }
                                ]
                            }
                        },
                    )
                ],
            )
        },
        evaluated_values=[],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        result = client.search_notes("AI workflow")

    assert result == [
        {
            "model_type": "note",
            "id": "delayed_A-1",
            "xsec_token": "",
            "note_card": {"display_title": "Delayed card"},
        }
    ]
    assert page.waits == [4000]
    assert page._listeners == []


def test_persistent_client_projects_visible_detail_without_a_share_count(tmp_path: Path) -> None:
    page = FakePage(
        route_responses={},
        evaluated_values=[
            {
                "title": "Visible detail title",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "visible_time_text": "发布于 2026-08-29",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": ["Topic One"],
                "images": ["https://images.example.invalid/cover.webp"],
                "media_scope_valid": True,
                "has_video": False,
            }
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        result = client.get_note_detail("note_A-1")

    assert result == {
        "media_scope_valid": True,
        "media_discovered_count": 1,
        "note": {
            "id": "note_A-1",
            "title": "Visible detail title",
            "desc": "Visible detail body",
            "user": {"user_id": "author_A-1", "nickname": "Visible author"},
            "time": None,
            "time_evidence": {"kind": "published", "raw_text": "发布于 2026-08-29"},
            "type": "normal",
            "interact_info": {
                "liked_count": "12",
                "collected_count": "3",
                "comment_count": "1",
            },
            "metric_provenance": {
                "likes": "detail_visible_count",
                "collects": "detail_visible_count",
                "comments": "detail_visible_count",
            },
            "tag_list": [{"name": "Topic One"}],
            "image_list": [{"position": 1, "url": "https://images.example.invalid/cover.webp"}],
        },
    }
    assert [urlsplit(url).path for url in page.navigations] == ["/explore/note_A-1"]
    assert page.waits == [4000]
    assert page.evaluations == [(_NOTE_DETAIL_SCRIPT, "note_A-1")]


@pytest.mark.parametrize(
    "author_profile_url",
    (
        "https://hostile.example/user/profile/author_A-1",
        "http://www.xiaohongshu.com/user/profile/author_A-1",
        "https://www.xiaohongshu.com:444/user/profile/author_A-1",
        "https://reader@www.xiaohongshu.com/user/profile/author_A-1",
        "https://www.xiaohongshu.com/user/profile/author_A-1/extra",
        "https://www.xiaohongshu.com/other/user/profile/author_A-1",
        "https://www.xiaohongshu.com/user/profile/author.invalid",
    ),
)
def test_persistent_client_does_not_synthesize_author_identity_from_hostile_profile_anchors(
    tmp_path: Path, author_profile_url: str
) -> None:
    page = FakePage(
        route_responses={},
        evaluated_values=[
            {
                "title": "Visible detail title",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": author_profile_url,
                "published_at": "2026-08-29",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            }
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        result = client.get_note_detail("note_A-1")

    assert result["note"]["user"] == {"user_id": None, "nickname": "Visible author"}


def test_persistent_client_projects_only_the_safe_profile_id_from_a_query_bearing_anchor(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={},
        evaluated_values=[
            {
                "title": "Visible detail title",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": (
                    "https://www.xiaohongshu.com/user/profile/author_A-1?view=card#bio"
                ),
                "published_at": "2026-08-29",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            }
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        result = client.get_note_detail("note_A-1")

    assert result["note"]["user"] == {"user_id": "author_A-1", "nickname": "Visible author"}


def test_persistent_client_merges_cached_search_card_share_count_into_detail(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={
            "/search_result": [
                FakeResponse(
                    "/api/sns/web/v2/search/notes",
                    {
                        "data": {
                            "items": [
                                {
                                    "model_type": "note",
                                    "id": "note_A-1",
                                    "note_card": {
                                        "interact_info": {"shared_count": "4"},
                                    },
                                }
                            ]
                        }
                    },
                )
            ]
        },
        evaluated_values=[
            {
                "title": "Visible detail title",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "visible_time_text": "发布于 06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            }
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.search_notes("AI workflow")
        result = client.get_note_detail("note_A-1")

    assert result == {
        "media_scope_valid": False,
        "media_discovered_count": 0,
        "note": {
            "id": "note_A-1",
            "title": "Visible detail title",
            "desc": "Visible detail body",
            "user": {"user_id": "author_A-1", "nickname": "Visible author"},
            "time": None,
            "time_evidence": {"kind": "published", "raw_text": "发布于 06-27"},
            "type": "normal",
            "interact_info": {
                "liked_count": "12",
                "collected_count": "3",
                "comment_count": "1",
                "shared_count": "4",
            },
            "metric_provenance": {
                "likes": "detail_visible_count",
                "collects": "detail_visible_count",
                "comments": "detail_visible_count",
                "shares": "search_card_interface",
            },
            "tag_list": [],
            "image_list": [],
        },
    }


def test_search_clears_account_card_metadata_before_reusing_the_same_note_id(
    tmp_path: Path,
) -> None:
    note_id = "same_A-1"
    page = FakePage(
        route_responses={
            "/search_result": [
                FakeResponse(
                    "/api/sns/web/v2/search/notes",
                    {
                        "data": {
                            "items": [
                                {
                                    "model_type": "note",
                                    "id": note_id,
                                    "xsec_token": "",
                                    "note_card": {"display_title": "Search-bound title"},
                                }
                            ]
                        }
                    },
                )
            ]
        },
        evaluated_values=[],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client._cached_note_metadata[note_id] = {
            "card_title": "Old account title",
            "card_cover_url": "https://ci.xiaohongshu.com/old-account-cover.webp",
        }
        client._dom_fallback_note_profiles[note_id] = "old-account"
        client.search_notes("AI workflow")

        assert client._cached_note_metadata == {note_id: {"card_title": "Search-bound title"}}
        assert client._dom_fallback_note_profiles == {}


def test_persistent_client_rejects_a_redirected_detail_page_before_projection(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={},
        evaluated_values=[{"title": "Untrusted redirected page"}],
        final_paths={"/explore/note_A-1": "/explore/different_A-1"},
    )

    with (
        _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client,
        pytest.raises(ValueError, match="unexpected detail page"),
    ):
        client.get_note_detail("note_A-1")

    assert len(page._evaluated_values) == 1


def test_persistent_client_projects_profile_and_reuses_minimal_user_post_candidates(
    tmp_path: Path,
) -> None:
    posts = [
        {
            "note_id": "post_A-1",
            "xsec_token": "",
            "cover": {"url": "https://images.example.invalid/post-cover.webp"},
            "display_title": "Visible post",
            "interact_info": {"liked_count": "5"},
            "time": 1_700_000_000,
            "type": "normal",
            "user": {"user_id": "account_A-1"},
        }
    ]
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": posts}})
            ]
        },
        evaluated_values=[
            {
                "name": "Visible account",
                "bio": "Visible bio",
                "red_id": "display-id",
                "avatar": "https://images.example.invalid/avatar.webp",
                "interactions": [
                    {"name": "Notes", "count": "7"},
                    {"name": "Followers", "count": "2"},
                ],
            }
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        profile = client.get_user_info("account_A-1")
        result_posts = client.get_user_posts("account_A-1")

    assert profile == {
        "userPageData": {
            "basicInfo": {
                "user_id": "account_A-1",
                "nickname": "Visible account",
                "desc": "Visible bio",
                "red_id": "display-id",
                "image": "https://images.example.invalid/avatar.webp",
            },
            "interactions": [
                {"name": "Notes", "count": "7"},
                {"name": "Followers", "count": "2"},
            ],
            "fieldStatuses": {
                "name": "exposed",
                "bio": "exposed",
                "note_count": "exposed",
                "follower_count": "exposed",
                "avatar": "exposed",
                "platform_metrics": "exposed",
            },
        }
    }
    assert result_posts == [{"note_id": "post_A-1", "xsec_token": ""}]
    assert [urlsplit(url).path for url in page.navigations] == ["/user/profile/account_A-1"]
    assert page.waits == [2000, 4000]


def test_persistent_client_falls_back_to_visible_profile_candidates_when_api_posts_are_empty(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {
                "name": "Visible account",
                "bio": "Visible bio",
                "red_id": "display-id",
                "avatar": "",
                "interactions": [],
            },
            [
                {
                    "note_id": "dom_A-1",
                    "xsec_token": "",
                    "display_title": "Discarded visible title",
                    "cover": {"url": "discarded-cover"},
                },
                {
                    "note_id": "dom_A-1",
                    "xsec_token": "",
                    "author": "Discarded author",
                },
            ],
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        assert client.get_user_info("account_A-1") == {
            "userPageData": {
                "basicInfo": {
                    "user_id": "account_A-1",
                    "nickname": "Visible account",
                    "desc": "Visible bio",
                    "red_id": "display-id",
                },
                "interactions": [],
                "fieldStatuses": {
                    "name": "exposed",
                    "bio": "exposed",
                    "note_count": "not_exposed",
                    "follower_count": "not_exposed",
                    "avatar": "not_exposed",
                    "platform_metrics": "not_exposed",
                },
            }
        }
        assert client.get_user_posts("account_A-1") == [{"note_id": "dom_A-1", "xsec_token": ""}]
        assert client._pending_user_id is None
        assert client._pending_user_note_candidates is None

    assert [urlsplit(url).path for url in page.navigations] == ["/user/profile/account_A-1"]
    assert page._evaluated_values == []


def test_profile_dom_fallback_binds_each_note_to_its_own_card_title_and_cover(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    exact_cover = "https://ci.xiaohongshu.com/exact-card-cover.webp"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {
                "profile_id": "account_A-1",
                "name": "Visible account",
                "bio": "",
                "red_id": "",
                "avatar": "",
                "interactions": [],
            },
            [
                {
                    "profile_id": "account_A-1",
                    "note_id": note_id,
                    "xsec_token": "",
                    "card_title": "Card-bound title",
                    "card_cover_url": exact_cover,
                }
            ],
            {
                "title": "",
                "body": "Visible detail body",
                "author_name": "Visible account",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/account_A-1",
                "visible_time_text": "发布于 06-27",
                "likes": "195",
                "collects": "",
                "comments": "",
                "tags": [],
                "images": ["https://ci.xiaohongshu.com/detail-slider.webp"],
                "media_scope_valid": True,
                "has_video": False,
            },
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        assert client.get_user_posts("account_A-1") == [{"note_id": note_id, "xsec_token": ""}]
        result = client.get_note_detail(note_id)

    note = result["note"]
    assert note["title"] == "Card-bound title"
    assert note["cover"] == {"url": exact_cover}
    assert note["image_list"] == [
        {"position": 1, "url": "https://ci.xiaohongshu.com/detail-slider.webp"}
    ]


def test_api_card_cover_does_not_override_a_scoped_detail_slider_image() -> None:
    metadata = _note_metadata(
        {
            "id": "search_A-1",
            "card_cover_url": "https://ci.xiaohongshu.com/forged-exact-cover.webp",
            "note_card": {
                "display_title": "Search card title",
                "cover": {"url": "https://ci.xiaohongshu.com/api-card-cover.webp"},
            },
        }
    )

    note = _project_note_detail(
        "search_A-1",
        {
            "title": "Detail title",
            "images": ["https://ci.xiaohongshu.com/detail-slider.webp"],
            "media_scope_valid": True,
        },
        metadata,
    )["note"]

    assert "cover" not in note
    assert note["image_list"] == [
        {"position": 1, "url": "https://ci.xiaohongshu.com/detail-slider.webp"}
    ]


def test_project_note_detail_preserves_all_image_positions_without_deduplication() -> None:
    result = _project_note_detail(
        "note_a",
        {
            "media_scope_valid": True,
            "images": [
                {"position": 1, "url": "https://sns-webpic-qc.xhscdn.com/a.jpg"},
                {"position": 2, "url": ""},
                {"position": 3, "url": "https://sns-webpic-qc.xhscdn.com/a.jpg"},
            ],
            "media_discovered_count": 3,
            "has_video": False,
        },
    )

    assert result["note"]["image_list"] == [
        {"position": 1, "url": "https://sns-webpic-qc.xhscdn.com/a.jpg"},
        {"position": 2, "url": None},
        {"position": 3, "url": "https://sns-webpic-qc.xhscdn.com/a.jpg"},
    ]
    assert result["media_discovered_count"] == 3


def test_project_note_detail_labels_edit_text_without_publishing_it() -> None:
    projected = _project_note_detail(
        "note_A-1",
        {
            "visible_time_text": "编辑于 08-12",
            "likes": "12",
            "collects": "3",
            "comments": "1",
            "media_scope_valid": True,
            "images": [],
            "has_video": False,
        },
    )

    assert projected["note"]["time"] is None
    assert projected["note"]["time_evidence"] == {
        "kind": "edited",
        "raw_text": "编辑于 08-12",
    }


def test_project_note_detail_marks_unclassified_time_evidence_unknown() -> None:
    projected = _project_note_detail(
        "note_A-1",
        {
            "visible_time_text": "08-12",
            "media_scope_valid": True,
            "images": [],
            "has_video": False,
        },
    )

    assert projected["note"]["time"] is None
    assert projected["note"]["time_evidence"] == {"kind": "unknown", "raw_text": "08-12"}


def test_project_note_detail_omits_blank_time_evidence() -> None:
    projected = _project_note_detail(
        "note_A-1",
        {
            "visible_time_text": "",
            "media_scope_valid": True,
            "images": [],
            "has_video": False,
        },
    )

    assert projected["note"]["time"] is None
    assert "time_evidence" not in projected["note"]


@pytest.mark.parametrize(
    "visible_time_text",
    (
        "https://example.invalid/time?token=secret",
        "xsec_token=secret",
        "\u200b",
    ),
    ids=("url", "token-like", "invisible"),
)
def test_project_note_detail_drops_unsafe_visible_time_evidence(
    visible_time_text: str,
) -> None:
    projected = _project_note_detail(
        "note_A-1",
        {
            "visible_time_text": visible_time_text,
            "media_scope_valid": True,
            "images": [],
            "has_video": False,
        },
    )

    assert projected["note"]["time"] is None
    assert "time_evidence" not in projected["note"]


def test_project_note_detail_uses_account_card_share_provenance() -> None:
    projected = _project_note_detail(
        "note_A-1",
        {
            "likes": "12",
            "collects": "3",
            "comments": "1",
            "media_scope_valid": True,
            "images": [],
            "has_video": False,
        },
        {"shared_count": "4", "share_source": "account_card_interface"},
    )

    assert projected["note"]["metric_provenance"] == {
        "likes": "detail_visible_count",
        "collects": "detail_visible_count",
        "comments": "detail_visible_count",
        "shares": "account_card_interface",
    }


@pytest.mark.parametrize(
    "visible_count",
    ("unsupported", "-1"),
    ids=("unparseable", "negative"),
)
def test_project_note_detail_omits_detail_provenance_for_unexposed_visible_metrics(
    visible_count: str,
) -> None:
    projected = _project_note_detail(
        "note_A-1",
        {
            "likes": visible_count,
            "media_scope_valid": True,
            "images": [],
            "has_video": False,
        },
    )

    assert projected["note"]["interact_info"]["liked_count"] == visible_count
    assert "metric_provenance" not in projected["note"]


def test_project_note_detail_omits_share_provenance_without_a_cached_share() -> None:
    projected = _project_note_detail(
        "note_A-1",
        {
            "likes": "12",
            "media_scope_valid": True,
            "images": [],
            "has_video": False,
        },
        {"share_source": "search_card_interface"},
    )

    assert projected["note"]["metric_provenance"] == {"likes": "detail_visible_count"}


@pytest.mark.parametrize(
    ("shared_count", "is_exposed"),
    [
        (-1, False),
        (0, True),
        ("", False),
        ("not available", False),
        ("17", True),
        ("1.2万", True),
    ],
)
def test_note_metadata_caches_share_provenance_only_for_exposed_counts(
    shared_count: int | str, is_exposed: bool
) -> None:
    metadata = _note_metadata(
        {"note_card": {"interact_info": {"shared_count": shared_count}}},
        share_source="search_card_interface",
    )
    projected = _project_note_detail(
        "note_A-1",
        {"media_scope_valid": True, "images": [], "has_video": False},
        metadata,
    )

    assert ("shared_count" in metadata) is is_exposed
    assert ("shares" in projected["note"].get("metric_provenance", {})) is is_exposed


def test_project_note_detail_keeps_exact_video_element_metadata() -> None:
    result = _project_note_detail(
        "note_video",
        {
            "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
            "media_scope_valid": True,
            "images": [],
            "media_discovered_count": 0,
            "has_video": True,
            "video": {
                "poster": "https://sns-webpic-qc.xhscdn.com/poster.jpg",
                "url": "https://sns-video-qc.xhscdn.com/video.mp4",
                "duration_ms": 12345,
            },
        },
    )

    assert result["note"]["video"] == {
        "poster": "https://sns-webpic-qc.xhscdn.com/poster.jpg",
        "url": "https://sns-video-qc.xhscdn.com/video.mp4",
        "duration_ms": 12345,
    }


@pytest.mark.parametrize(
    "author_profile_url",
    (None, "https://www.xiaohongshu.com/user/profile/author%20A", "https://example.test/user/profile/author_A-1"),
    ids=("absent", "hostile", "wrong-host"),
)
def test_project_note_detail_drops_direct_video_without_a_canonical_author_binding(
    author_profile_url: object,
) -> None:
    raw: dict[str, object] = {
        "media_scope_valid": True,
        "images": [],
        "media_discovered_count": 0,
        "has_video": True,
        "video": {"url": "https://sns-video-qc.xhscdn.com/video.mp4"},
    }
    if author_profile_url is not None:
        raw["author_profile_url"] = author_profile_url

    result = _project_note_detail("note_video", raw)

    assert "video" not in result["note"]


def test_project_note_detail_caps_ordered_slots_without_losing_the_discovered_count() -> None:
    images = [
        {"position": position, "url": f"https://sns-webpic-qc.xhscdn.com/{position}.jpg"}
        for position in range(1, 102)
    ]

    result = _project_note_detail(
        "note_many_images",
        {
            "media_scope_valid": True,
            "images": images,
            "media_discovered_count": 101,
            "has_video": False,
        },
    )

    image_list = result["note"]["image_list"]
    assert isinstance(image_list, list)
    assert len(image_list) == 100
    assert image_list[-1] == {
        "position": 100,
        "url": "https://sns-webpic-qc.xhscdn.com/100.jpg",
    }
    assert result["media_discovered_count"] == 101


def test_project_note_detail_drops_media_when_the_browser_scope_is_invalid() -> None:
    result = _project_note_detail(
        "note_a",
        {
            "media_scope_valid": False,
            "images": [{"position": 1, "url": "https://sns-webpic-qc.xhscdn.com/leak.jpg"}],
            "media_discovered_count": 1,
            "has_video": True,
            "video": {"url": "https://sns-video-qc.xhscdn.com/leak.mp4"},
        },
    )

    assert result["media_scope_valid"] is False
    assert result["media_discovered_count"] == 0
    assert result["note"]["image_list"] == []
    assert "video" not in result["note"]


@pytest.mark.parametrize("scope", (None, "true", 1))
def test_project_note_detail_fails_closed_when_media_scope_is_missing_or_not_boolean(
    scope: object,
) -> None:
    raw: dict[str, object] = {
        "images": [{"position": 1, "url": "https://sns-webpic-qc.xhscdn.com/leak.jpg"}],
        "media_discovered_count": 1,
        "has_video": True,
        "video": {"url": "https://sns-video-qc.xhscdn.com/leak.mp4"},
    }
    if scope is not None:
        raw["media_scope_valid"] = scope

    result = _project_note_detail("note_a", raw)

    assert result["media_scope_valid"] is False
    assert result["media_discovered_count"] == 0
    assert result["note"]["image_list"] == []
    assert "video" not in result["note"]


def test_detail_script_excludes_recommendation_video_and_hidden_slider_clones() -> None:
    raw = _evaluate_detail_script(
        expected_note_id="note_a",
        detail_images=["one.jpg", "two.jpg"],
        duplicate_clone="one.jpg",
        recommendation_video="https://sns-video-qc.xhscdn.com/wrong.mp4",
    )

    assert raw["media_scope_valid"] is True
    assert [item["url"] for item in raw["images"]] == ["one.jpg", "two.jpg"]
    assert raw["video"] is None
    assert ".swiper-slide:not(.swiper-slide-duplicate) img.note-slider-img" in _NOTE_DETAIL_SCRIPT
    assert "detailRoot.querySelectorAll(" in _NOTE_DETAIL_SCRIPT
    assert "leafRoots.length !== 1" in _NOTE_DETAIL_SCRIPT


def _node_executable() -> str:
    bundled = Path("/opt/homebrew/opt/node/bin/node")
    if bundled.is_file() and bundled.stat().st_mode & 0o111:
        return str(bundled)
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the detail-root JavaScript harness")
    return node


def _evaluate_detail_script_with_current_image_dom(
    *,
    image_shape: str = "wrapper",
    image_urls: list[str] | None = None,
    media_mode: str = "image",
    structured_notes: dict[str, object] | None = None,
    author_profile_url: str | None = "https://www.xiaohongshu.com/user/profile/author_A-1",
    detail_script: str = _NOTE_DETAIL_SCRIPT,
) -> dict[str, object]:
    """Execute the production detail script against a strict detail-DOM stub."""
    script = (
        f"const projectDetail = {detail_script};\n"
        + """
const input = JSON.parse(process.argv[1]);
const noteId = '6a4cae81000000001700a749';
const visible = {getBoundingClientRect: () => ({width: 100, height: 100})};
const exactImageSelector =
  '.note-slider .swiper-slide:not(.swiper-slide-duplicate) .note-slider-img img, ' +
  '.note-slider .swiper-slide:not(.swiper-slide-duplicate) img.note-slider-img';
if (!['wrapper', 'direct'].includes(input.imageShape)) throw new Error('invalid image shape');
if (!['image', 'direct_video', 'xg_player'].includes(input.mediaMode)) {
  throw new Error('invalid media mode');
}
const makeImage = (url, imageShape, slideKind) => ({
  currentSrc: url,
  src: url,
  imageShape,
  slideKind,
});
const normalSlides = input.imageUrls.map((url) => ({
  slideKind: 'normal',
  image: makeImage(url, input.imageShape, 'normal'),
}));
const duplicateSlides = ['clone-first.webp', 'clone-last.webp'].map((url) => ({
  slideKind: 'duplicate',
  image: makeImage(url, input.imageShape, 'duplicate'),
}));
const recommendationSubtree = {
  slideKind: 'recommendation',
  image: makeImage('recommendation.webp', input.imageShape, 'recommendation'),
};
const slider = {...visible, normalSlides, duplicateSlides, recommendationSubtree};
const directVideo = {
  ...visible,
  currentSrc: 'https://video.example/direct.mp4',
  src: '',
  poster: 'direct-poster.webp',
  duration: 4.25,
  querySelector(selector) {
    if (selector !== 'source') throw new Error(`unexpected video selector: ${selector}`);
    return null;
  },
};
const xgPlayer = {...visible};
const mediaContainer = {
  ...visible,
  querySelector(selector) {
    if (selector !== 'xg-player') throw new Error(`unexpected container selector: ${selector}`);
    return input.mediaMode === 'xg_player' ? xgPlayer : null;
  },
};
const description = {...visible, innerText: ''};
const author = {...visible, innerText: ''};
const authorProfile = input.authorProfileUrl === null ? null : {href: input.authorProfileUrl};
const root = {
  ...visible,
  contains: () => false,
  querySelector(selector) {
    if (selector === '.author-wrapper') return author;
    if (selector === '#detail-desc') return description;
    if (selector === '.note-slider, .media-container, xg-player') {
      if (input.mediaMode === 'xg_player') return xgPlayer;
      return input.mediaMode === 'direct_video' ? mediaContainer : slider;
    }
    if (selector === '.author-wrapper a[href*="/user/profile/"]') return authorProfile;
    if (selector === '#detail-title' || selector === '.author-wrapper .name' ||
        selector === '.date' || selector === '.engage-bar .like-wrapper .count' ||
        selector === '.engage-bar .collect-wrapper .count' ||
        selector === '.engage-bar .chat-wrapper .count') return null;
    throw new Error(`unexpected root selector: ${selector}`);
  },
  querySelectorAll(selector) {
    if (selector === '#detail-desc a[href*="search"]') return [];
    if (selector === exactImageSelector) return normalSlides.map((slide) => slide.image);
    if (selector === 'xg-player') return input.mediaMode === 'xg_player' ? [xgPlayer] : [];
    if (selector === '.media-container') return [mediaContainer];
    if (selector === '.media-container video, xg-player video') {
      return input.mediaMode === 'direct_video' ? [directVideo] : [];
    }
    throw new Error(`unexpected selector: ${selector}`);
  },
};
global.document = {
  querySelectorAll(selector) {
    if (selector !== '#noteContainer, .note-detail-mask') {
      throw new Error(`unexpected document selector: ${selector}`);
    }
    return [root];
  },
};
global.location = {pathname: `/explore/${noteId}`};
global.getComputedStyle = () => ({display: 'block', visibility: 'visible'});
global.window = {__INITIAL_STATE__: {note: {noteDetailMap: input.structuredNotes}}};
process.stdout.write(JSON.stringify(projectDetail(noteId)));
"""
    )
    result = subprocess.run(
        [
            _node_executable(),
            "-e",
            script,
            json.dumps(
                {
                    "imageShape": image_shape,
                    "imageUrls": image_urls
                    if image_urls is not None
                    else [f"image-{position}.webp" for position in range(1, 7)],
                    "mediaMode": media_mode,
                    "structuredNotes": structured_notes or {},
                    "authorProfileUrl": author_profile_url,
                }
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _evaluate_profile_script_with_header_dom(
    *,
    hidden_template_name: bool = False,
    avatar_count: int = 1,
    avatar_ancestor_hops: int = 0,
    ancestor_duplicate_name_count: int = 0,
    ancestor_only_selector: str | None = None,
    only_avatar: bool = False,
) -> dict[str, object]:
    """Execute the profile projection against a bounded account-header DOM."""
    script = (
        f"const projectProfile = {_USER_PROFILE_SCRIPT};\n"
        + """
const input = JSON.parse(process.argv[1]);
const body = {};
const makeElement = (text, visible = true) => ({
  innerText: text,
  visible,
  children: [],
  currentSrc: '',
  src: '',
  getBoundingClientRect() { return {width: this.visible ? 100 : 0, height: this.visible ? 20 : 0}; },
});
const root = makeElement('', true);
root.parentElement = body;
const ancestorRoots = [root];
let avatarRoot = root;
for (let hop = 0; hop < input.avatarAncestorHops; hop += 1) {
  const parent = makeElement('', true);
  avatarRoot.parentElement = parent;
  avatarRoot = parent;
  ancestorRoots.push(parent);
}
const name = makeElement('Visible account');
const hiddenName = makeElement('Template account', false);
const redId = makeElement('red-id');
const bio = makeElement('Visible bio');
const interactions = makeElement('');
const ancestorNames = Array.from({length: input.ancestorDuplicateNameCount}, () =>
  makeElement('Expanded account')
);
const ancestorOnly = input.ancestorOnlySelector ? makeElement('Expanded bio') : null;
const avatars = Array.from({length: input.avatarCount}, (_, index) => {
  const avatar = makeElement('');
  avatar.currentSrc = `https://ci.xiaohongshu.com/avatar-${index}.webp`;
  avatar.src = avatar.currentSrc;
  return avatar;
});
const withinRoot = {
  '.user-name': input.onlyAvatar ? [] : (input.hiddenTemplateName ? [hiddenName, name] : [name]),
  '.user-redId': input.onlyAvatar ? [] : [redId],
  '.user-desc': input.onlyAvatar || input.ancestorOnlySelector === '.user-desc' ? [] : [bio],
  '.user-interactions': input.onlyAvatar ? [] : [interactions],
  'img.user-image': input.avatarAncestorHops === 0 ? avatars : [],
};
for (const items of Object.values(withinRoot)) {
  for (const item of items) item.parentElement = root;
}
for (const item of ancestorNames) item.parentElement = avatarRoot;
if (ancestorOnly) ancestorOnly.parentElement = avatarRoot;
for (const [ancestorIndex, ancestor] of ancestorRoots.entries()) {
  ancestor.contains = (item) =>
    Object.values(withinRoot).flat().includes(item) ||
    (ancestorIndex >= input.avatarAncestorHops && avatars.includes(item));
  ancestor.querySelector = (selector) => ancestor.querySelectorAll(selector)[0] || null;
  ancestor.querySelectorAll = (selector) => {
    if (selector === 'img.user-image') {
      return ancestorIndex >= input.avatarAncestorHops ? avatars : [];
    }
    if (ancestorIndex >= input.avatarAncestorHops && selector === '.user-name') {
      return (withinRoot['.user-name'] || []).concat(ancestorNames);
    }
    if (ancestorIndex >= input.avatarAncestorHops && selector === input.ancestorOnlySelector) {
      return ancestorOnly ? [ancestorOnly] : [];
    }
    return withinRoot[selector] || [];
  };
}
global.document = {
  body,
  querySelectorAll(selector) {
    if (selector === '.user-name') {
      return (withinRoot['.user-name'] || []).concat(ancestorNames);
    }
    return withinRoot[selector] || [];
  },
};
global.location = {
  protocol: 'https:', hostname: 'www.xiaohongshu.com', port: '',
  pathname: '/user/profile/account_A-1',
};
global.getComputedStyle = (element) => ({
  display: element.visible ? 'block' : 'none', visibility: 'visible',
});
process.stdout.write(JSON.stringify(projectProfile('account_A-1')));
"""
    )
    result = subprocess.run(
        [
            _node_executable(),
            "-e",
            script,
            json.dumps(
                {
                    "hiddenTemplateName": hidden_template_name,
                    "avatarCount": avatar_count,
                    "avatarAncestorHops": avatar_ancestor_hops,
                    "ancestorDuplicateNameCount": ancestor_duplicate_name_count,
                    "ancestorOnlySelector": ancestor_only_selector,
                    "onlyAvatar": only_avatar,
                }
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def test_detail_script_projects_current_wrapper_images_without_misclassifying_plain_container() -> (
    None
):
    """The live image-note shape has wrapper classes, clones, and no player."""
    raw = _evaluate_detail_script_with_current_image_dom()

    assert raw["media_scope_valid"] is True
    assert raw["media_discovered_count"] == 6
    assert raw["images"] == [
        {"position": position, "url": f"image-{position}.webp"} for position in range(1, 7)
    ]
    assert raw["has_video"] is False
    assert raw["video"] is None
    assert {image["url"] for image in raw["images"] if isinstance(image, dict)}.isdisjoint(
        {"clone-first.webp", "clone-last.webp", "recommendation.webp"}
    )


def test_detail_script_keeps_legacy_direct_slider_images_in_dom_order() -> None:
    raw = _evaluate_detail_script_with_current_image_dom(
        image_shape="direct", image_urls=["first.webp", "second.webp", "third.webp"]
    )

    assert raw["media_discovered_count"] == 3
    assert raw["images"] == [
        {"position": 1, "url": "first.webp"},
        {"position": 2, "url": "second.webp"},
        {"position": 3, "url": "third.webp"},
    ]
    assert raw["has_video"] is False


def test_detail_script_caps_current_slider_images_without_reindexing_blank_slots() -> None:
    image_urls = ["" if position == 2 else f"image-{position}.webp" for position in range(1, 102)]
    raw = _evaluate_detail_script_with_current_image_dom(image_urls=image_urls)

    assert raw["media_discovered_count"] == 101
    assert raw["images"] == [
        {"position": position, "url": image_urls[position - 1]} for position in range(1, 101)
    ]
    assert raw["has_video"] is False


def test_detail_script_executes_zero_image_plain_container_direct_video() -> None:
    raw = _evaluate_detail_script_with_current_image_dom(image_urls=[], media_mode="direct_video")

    assert raw["has_video"] is True
    assert raw["video"] == {
        "poster": "direct-poster.webp",
        "url": "https://video.example/direct.mp4",
        "duration_ms": 4250,
    }


def test_detail_script_executes_zero_image_xg_player_structured_video_fallback() -> None:
    raw = _evaluate_detail_script_with_current_image_dom(
        image_urls=[],
        media_mode="xg_player",
        structured_notes={
            "6a4cae81000000001700a749": {
                "note": {
                    "noteId": "6a4cae81000000001700a749",
                    "user": {"userId": "author_A-1"},
                    "video": {"media": {"stream": "https://video.example/fallback.webm"}},
                }
            }
        },
    )

    assert raw["has_video"] is True
    assert raw["video"] == {
        "poster": "",
        "url": "https://video.example/fallback.webm",
        "duration_ms": None,
    }


def test_detail_script_rejects_structured_fallback_when_author_identity_mismatches_profile() -> None:
    raw = _evaluate_detail_script_with_current_image_dom(
        image_urls=[],
        media_mode="xg_player",
        structured_notes={
            "6a4cae81000000001700a749": {
                "note": {
                    "noteId": "6a4cae81000000001700a749",
                    "user": {"userId": "other_author"},
                    "video": {"media": {"stream": "https://video.example/fallback.webm"}},
                }
            }
        },
    )

    assert raw["video"] == {"poster": "", "url": "", "duration_ms": None}


@pytest.mark.parametrize(
    "structured_notes",
    (
        {
            "6a4cae81000000001700a749": {
                "note": {
                    "noteId": "other-note",
                    "video": {"media": {"stream": "https://video.example/wrong.mp4"}},
                }
            }
        },
        {
            "6a4cae81000000001700a749": {
                "note": {
                    "noteId": "6a4cae81000000001700a749",
                    "video": {
                        "media": {
                            "stream": [
                                "https://video.example/first.mp4",
                                "https://video.example/second.webm",
                            ]
                        }
                    },
                }
            }
        },
    ),
    ids=("mismatched-note", "ambiguous-direct-urls"),
)
def test_detail_script_executes_xg_player_rejection_of_untrusted_structured_fallback(
    structured_notes: dict[str, object],
) -> None:
    raw = _evaluate_detail_script_with_current_image_dom(
        image_urls=[], media_mode="xg_player", structured_notes=structured_notes
    )

    assert raw["has_video"] is True
    assert raw["video"] == {"poster": "", "url": "", "duration_ms": None}


def test_detail_script_structural_harness_rejects_a_broadened_image_selector() -> None:
    broadened_script = _NOTE_DETAIL_SCRIPT.replace(
        ".swiper-slide:not(.swiper-slide-duplicate) .note-slider-img img",
        ".swiper-slide .note-slider-img img",
    )
    assert broadened_script != _NOTE_DETAIL_SCRIPT

    with pytest.raises(subprocess.CalledProcessError):
        _evaluate_detail_script_with_current_image_dom(detail_script=broadened_script)


def test_detail_script_structural_harness_detects_a_missing_image_slot_guard() -> None:
    without_image_slot_guard = _NOTE_DETAIL_SCRIPT.replace(
        "imageNodes.length === 0 && playerRoots.length === 1", "playerRoots.length === 1"
    )
    assert without_image_slot_guard != _NOTE_DETAIL_SCRIPT

    raw = _evaluate_detail_script_with_current_image_dom(detail_script=without_image_slot_guard)

    assert raw["has_video"] is True


def _evaluate_safe_card_scroll_helper(
    *, hrefs: list[str], expected_path: str, throw_on_scroll: bool = False
) -> dict[str, object]:
    """Execute the exact production scroll helper against a minimal DOM stub."""
    script = (
        f"const scrollCard = {_SAFE_CARD_SCROLL_INTO_VIEW_SCRIPT};\n"
        + """
const input = JSON.parse(process.argv[1]);
global.window = {location: {href: input.locationHref}};
const calls = [];
const element = {
  querySelectorAll(selector) {
    if (selector !== 'a[href]') throw new Error(`unexpected selector: ${selector}`);
    return input.hrefs.map((href) => ({href}));
  },
  scrollIntoView(options) {
    if (input.throwOnScroll) throw new Error('scroll rejected');
    calls.push(options);
  },
};
const result = scrollCard(element, input.expectedPath);
process.stdout.write(JSON.stringify({result, calls}));
"""
    )
    result = subprocess.run(
        [
            _node_executable(),
            "-e",
            script,
            json.dumps(
                {
                    "expectedPath": expected_path,
                    "hrefs": hrefs,
                    "locationHref": "https://www.xiaohongshu.com/user/profile/author_A-1",
                    "throwOnScroll": throw_on_scroll,
                }
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def test_safe_card_scroll_helper_executes_exact_url_guard_and_scroll_options() -> None:
    outcome = _evaluate_safe_card_scroll_helper(
        hrefs=["https://www.xiaohongshu.com/explore/dom_A-1?view=card"],
        expected_path="/explore/dom_A-1",
    )

    assert outcome == {
        "result": {"state": "scrolled"},
        "calls": [{"block": "nearest", "inline": "nearest", "behavior": "instant"}],
    }


@pytest.mark.parametrize(
    "href",
    (
        "http://www.xiaohongshu.com/explore/dom_A-1",
        "https://example.test/explore/dom_A-1",
        "https://www.xiaohongshu.com:444/explore/dom_A-1",
        "https://www.xiaohongshu.com/explore/other_A-2",
        "https://",
    ),
    ids=("wrong-scheme", "wrong-host", "nondefault-port", "wrong-path", "malformed"),
)
def test_safe_card_scroll_helper_rejects_nonmatching_urls_without_scrolling(href: str) -> None:
    outcome = _evaluate_safe_card_scroll_helper(hrefs=[href], expected_path="/explore/dom_A-1")

    assert outcome == {"result": {"state": "not_matching"}, "calls": []}


def test_safe_card_scroll_helper_reports_scroll_failure() -> None:
    outcome = _evaluate_safe_card_scroll_helper(
        hrefs=["https://www.xiaohongshu.com/explore/dom_A-1"],
        expected_path="/explore/dom_A-1",
        throw_on_scroll=True,
    )

    assert outcome == {"result": {"state": "scroll_failed"}, "calls": []}


def _evaluate_detail_root_leaf_canonicalizer(containment: list[list[int]]) -> list[int]:
    canonicalizer = getattr(persistent_client, "_DETAIL_ROOT_LEAF_CANONICALIZER", None)
    assert isinstance(canonicalizer, str)
    script = (
        f"const canonicalize = {canonicalizer};\n"
        + """
const containment = JSON.parse(process.argv[1]);
const roots = containment.map((children, index) => ({
  index,
  contains(other) { return children.includes(other.index); },
}));
process.stdout.write(JSON.stringify(canonicalize(roots).map((root) => root.index)));
"""
    )
    result = subprocess.run(
        [_node_executable(), "-e", script, json.dumps(containment)],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    assert isinstance(value, list) and all(type(item) is int for item in value)
    return value


@pytest.mark.parametrize(
    ("containment", "expected_leaves", "has_unique_leaf"),
    (
        ([[1], []], [1], True),
        ([[], []], [0, 1], False),
        ([[1, 2], [2], []], [2], True),
        ([[]], [0], True),
    ),
    ids=("nested-parent-child", "independent", "three-level-chain", "selector-dedupe"),
)
def test_detail_root_leaf_canonicalizer_executes_exact_browser_helper(
    containment: list[list[int]], expected_leaves: list[int], has_unique_leaf: bool
) -> None:
    leaves = _evaluate_detail_root_leaf_canonicalizer(containment)

    assert leaves == expected_leaves
    assert (len(leaves) == 1) is has_unique_leaf


def test_detail_script_requires_one_canonical_detail_root() -> None:
    assert "const leafRoots = _DETAIL_ROOT_LEAF_CANONICALIZER(roots);" in _NOTE_DETAIL_SCRIPT
    assert "leafRoots.length !== 1" in _NOTE_DETAIL_SCRIPT
    assert "const detailRoot = leafRoots[0];" in _NOTE_DETAIL_SCRIPT


def test_detail_script_uses_only_exact_note_structured_video_fallback() -> None:
    raw = _evaluate_detail_script(
        expected_note_id="note_a",
        detail_images=[],
        player_video="blob:local",
        structured_notes={
            "note_a": {
                "note_id": "note_a",
                "video_urls": ["https://sns-video-qc.xhscdn.com/right.mp4"],
            },
            "recommended": {
                "note_id": "other",
                "video_urls": ["https://sns-video-qc.xhscdn.com/wrong.mp4"],
            },
        },
    )

    assert raw["video"] == {
        "poster": "",
        "url": "https://sns-video-qc.xhscdn.com/right.mp4",
        "duration_ms": None,
    }
    assert "noteDetailMap?.[expectedNoteId]?.note" in _NOTE_DETAIL_SCRIPT
    assert "noteId !== expectedNoteId" in _NOTE_DETAIL_SCRIPT
    assert "uniqueDirectUrls.size === 1" in _NOTE_DETAIL_SCRIPT


def test_detail_script_uses_structured_fallback_for_xg_player_without_light_dom_video() -> None:
    raw = _evaluate_detail_script(
        expected_note_id="note_a",
        detail_images=[],
        structured_notes={
            "note_a": {
                "note_id": "note_a",
                "video_urls": ["https://sns-video-qc.xhscdn.com/right.webm"],
            }
        },
    )

    assert raw["video"] == {
        "poster": "",
        "url": "https://sns-video-qc.xhscdn.com/right.webm",
        "duration_ms": None,
    }
    assert "const playerRoots" in _NOTE_DETAIL_SCRIPT
    assert "playerRoots.length === 1" in _NOTE_DETAIL_SCRIPT
    assert "if (directVideoUrl || !detailPlayer) return '';" in _NOTE_DETAIL_SCRIPT


def test_profile_projection_and_dom_cards_reject_an_explicit_profile_mismatch(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {
                "profile_id": "other-account",
                "name": "Wrong account",
                "bio": "Wrong bio",
                "red_id": "wrong-id",
                "avatar": "https://ci.xiaohongshu.com/wrong.webp",
                "interactions": [{"name": "粉丝", "count": "999万"}],
            },
            [
                {
                    "profile_id": "other-account",
                    "note_id": "wrong-note",
                    "xsec_token": "",
                    "card_title": "Wrong note",
                    "card_cover_url": "https://ci.xiaohongshu.com/wrong-cover.webp",
                }
            ],
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        assert client.get_user_info("account_A-1") == {
            "userPageData": {
                "basicInfo": {"user_id": "account_A-1"},
                "interactions": [],
            }
        }
        assert client.get_user_posts("account_A-1") == []
        assert client._cached_note_metadata == {}
        assert client._dom_fallback_note_profiles == {}


def test_profile_projection_preserves_platform_empty_bio_evidence() -> None:
    projected = _project_user_profile(
        "account_A-1",
        {
            "profile_id": "account_A-1",
            "name": "Visible account",
            "bio": "",
            "bio_status": "exposed_empty",
            "avatar": "https://ci.xiaohongshu.com/avatar.webp",
            "interactions": [],
        },
    )

    basic = projected["userPageData"]["basicInfo"]
    assert basic["desc"] == ""
    assert projected["userPageData"]["fieldStatuses"] == {
        "name": "exposed",
        "bio": "exposed_empty",
        "note_count": "not_exposed",
        "follower_count": "not_exposed",
        "avatar": "exposed",
        "platform_metrics": "not_exposed",
    }


def test_profile_projection_marks_missing_or_unreadable_fields_without_inference() -> None:
    projected = _project_user_profile(
        "account_A-1",
        {
            "profile_id": "account_A-1",
            "name": "",
            "name_status": "unavailable",
            "bio_status": "not_exposed",
            "avatar_status": "not_exposed",
            "interactions": [],
            "interactions_status": "unavailable",
        },
    )

    basic = projected["userPageData"]["basicInfo"]
    assert "nickname" not in basic
    assert "desc" not in basic
    assert projected["userPageData"]["fieldStatuses"] == {
        "name": "unavailable",
        "bio": "not_exposed",
        "note_count": "unavailable",
        "follower_count": "unavailable",
        "avatar": "not_exposed",
        "platform_metrics": "unavailable",
    }


def test_profile_projection_marks_only_exact_follower_label_as_exposed() -> None:
    projected = _project_user_profile(
        "account_A-1",
        {
            "profile_id": "account_A-1",
            "name": "Visible account",
            "bio": "Visible bio",
            "avatar": "https://ci.xiaohongshu.com/avatar.webp",
            "interactions": [{"name": "粉丝", "count": "8"}],
            "interactions_status": "exposed",
        },
    )

    assert projected["userPageData"]["fieldStatuses"] == {
        "name": "exposed",
        "bio": "exposed",
        "note_count": "not_exposed",
        "follower_count": "exposed",
        "avatar": "exposed",
        "platform_metrics": "exposed",
    }


def test_profile_script_uses_the_visible_header_anchor_not_a_hidden_template_descendant() -> None:
    raw = _evaluate_profile_script_with_header_dom(hidden_template_name=True)

    assert raw["profile_id"] == "account_A-1"
    assert raw["name"] == "Visible account"
    assert raw["name_status"] == "exposed"


def test_profile_script_reads_one_avatar_from_the_bounded_header_ancestor() -> None:
    """Removing bounded ancestor lookup would lose the live sibling avatar."""
    raw = _evaluate_profile_script_with_header_dom(avatar_ancestor_hops=2)

    assert raw["profile_id"] == "account_A-1"
    assert raw["avatar"] == "https://ci.xiaohongshu.com/avatar-0.webp"
    assert raw["avatar_status"] == "exposed"


def test_profile_script_ignores_an_avatar_beyond_three_header_ancestor_hops() -> None:
    """Expanding the header search past its bound would capture page-level images."""
    raw = _evaluate_profile_script_with_header_dom(avatar_ancestor_hops=4)

    assert raw["profile_id"] == "account_A-1"
    assert raw["avatar"] == ""
    assert raw["avatar_status"] == "not_exposed"


def test_profile_script_rejects_avatar_when_expanded_ancestor_has_ambiguous_name() -> None:
    """Skipping an initially ambiguous name would bind the avatar to an unclear header."""
    raw = _evaluate_profile_script_with_header_dom(
        avatar_ancestor_hops=2,
        ancestor_duplicate_name_count=1,
    )

    assert raw["profile_id"] == "account_A-1"
    assert raw["avatar"] == ""
    assert raw["avatar_status"] == "unavailable"


def test_profile_script_rejects_avatar_when_expanded_ancestor_adds_unbound_bio() -> None:
    """An identity selector absent from the bound header cannot appear during avatar lookup."""
    raw = _evaluate_profile_script_with_header_dom(
        avatar_ancestor_hops=2,
        ancestor_only_selector=".user-desc",
    )

    assert raw["profile_id"] == "account_A-1"
    assert raw["avatar"] == ""
    assert raw["avatar_status"] == "unavailable"


def test_profile_script_rejects_an_avatar_as_the_only_header_anchor() -> None:
    raw = _evaluate_profile_script_with_header_dom(only_avatar=True)

    assert raw == {"profile_id": ""}


def test_profile_script_marks_multiple_visible_avatars_unavailable() -> None:
    raw = _evaluate_profile_script_with_header_dom(avatar_count=2)

    assert raw["profile_id"] == "account_A-1"
    assert raw["avatar"] == ""
    assert raw["avatar_status"] == "unavailable"


def test_browser_projection_scripts_scope_identity_and_media_to_exact_visible_roots() -> None:
    assert "const profileMatch =" in _USER_PROFILE_SCRIPT
    assert "const headerAnchors" in _USER_PROFILE_SCRIPT
    assert '[class*="avatar"]' not in _USER_PROFILE_SCRIPT
    assert "detailRoot.querySelectorAll(" in _NOTE_DETAIL_SCRIPT
    assert "expectedNoteId" in _NOTE_DETAIL_SCRIPT
    assert "notes_pre_post" not in _NOTE_DETAIL_SCRIPT
    assert "spectrum" not in _NOTE_DETAIL_SCRIPT


@pytest.mark.parametrize(
    ("card_href", "expected_posts"),
    (
        (
            "https://www.xiaohongshu.com:443/explore/dom_A-1",
            [{"note_id": "dom_A-1", "xsec_token": ""}],
        ),
        ("https://www.xiaohongshu.com:444/explore/dom_A-1", []),
    ),
)
def test_persistent_client_profile_dom_candidates_allow_only_default_https_port(
    tmp_path: Path, card_href: str, expected_posts: list[dict[str, str]]
) -> None:
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {
                "name": "Visible account",
                "bio": "",
                "red_id": "",
                "avatar": "",
                "interactions": [],
            }
        ],
        profile_card_hrefs=[card_href],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        assert client.get_user_posts("account_A-1") == expected_posts
        assert client._dom_fallback_note_profiles == (
            {"dom_A-1": "account_A-1"} if expected_posts else {}
        )


def test_persistent_client_opens_first_tokenless_dom_candidate_by_visible_card(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": "", "display_title": "Discarded card text"}],
            {
                "title": "Opened card detail",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "visible_time_text": "发布于 06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
        final_paths={f"/explore/{note_id}": "/404"},
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        assert client.get_user_posts("account_A-1") == [{"note_id": note_id, "xsec_token": ""}]
        result = client.get_note_detail(note_id)

    assert result["note"] == {
        "id": note_id,
        "title": "Opened card detail",
        "desc": "Visible detail body",
        "user": {"user_id": "author_A-1", "nickname": "Visible author"},
        "time": None,
        "time_evidence": {"kind": "published", "raw_text": "发布于 06-27"},
        "type": "normal",
        "interact_info": {
            "liked_count": "12",
            "collected_count": "3",
            "comment_count": "1",
        },
        "metric_provenance": {
            "likes": "detail_visible_count",
            "collects": "detail_visible_count",
            "comments": "detail_visible_count",
        },
        "tag_list": [],
        "image_list": [],
    }
    assert [urlsplit(url).path for url in page.navigations] == [
        "/user/profile/account_A-1",
        "/user/profile/account_A-1",
    ]
    assert page.clicked_note_ids == [note_id]
    assert page.mouse_clicks == [(60.0, 60.0)]
    _assert_exact_detail_wait(page, note_id)
    assert page.waits == [2000, 4000, 3000, 4000, 4000]


def test_persistent_client_skips_hidden_dom_card_duplicates_when_opening_detail(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {
                "title": "Visible duplicate card detail",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
        hidden_first_note_ids={note_id},
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        result = client.get_note_detail(note_id)

    assert result["note"]["title"] == "Visible duplicate card detail"
    assert page.clicked_note_ids == [note_id]


def test_persistent_client_rejects_off_viewport_card_without_clicking_or_projecting_detail(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {"title": "Detail must remain unread"},
        ],
        off_viewport_note_ids={note_id},
        scroll_leaves_off_viewport_note_ids={note_id},
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        with pytest.raises(DetailReadFailure):
            client.get_note_detail(note_id)

    assert page.clicked_note_ids == []
    assert page.mouse_clicks == []
    assert page.waited_urls == []
    assert len(page._evaluated_values) == 1
    assert page.card_activation_expressions == []
    assert len(page.card_scroll_evaluations) == 1
    assert [urlsplit(url).path for url in page.navigations] == [
        "/user/profile/account_A-1",
        "/user/profile/account_A-1",
    ]


def test_click_visible_note_card_scrolls_the_exact_offscreen_card_then_revalidates(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={},
        evaluated_values=[],
        card_hrefs=[f"https://www.xiaohongshu.com/explore/{note_id}?view=card"],
    )
    page._off_viewport_note_ids.add(note_id)

    persistent_client._click_visible_note_card(page, note_id)

    assert page.clicked_note_ids == [note_id]
    assert page.mouse_clicks == [(60.0, 60.0)]
    assert page.waits == [persistent_client._CARD_SCROLL_SETTLE_MILLISECONDS]
    assert len(page.card_scroll_evaluations) == 1
    assert len(page.card_safety_evaluations) == 2
    href, expected_path, expression = page.card_scroll_evaluations[0]
    assert href == f"https://www.xiaohongshu.com/explore/{note_id}?view=card"
    assert expected_path == f"/explore/{note_id}"
    assert "scrollIntoView" in expression
    assert "new URL" in expression
    assert "parsed.protocol" in expression
    assert "parsed.hostname" in expression
    assert "parsed.port" in expression
    assert "parsed.pathname" in expression


@pytest.mark.parametrize(
    ("page_options", "error"),
    (
        ({"scroll_failure_note_ids": {"dom_A-1"}}, "could not be safely scrolled"),
        (
            {"scroll_leaves_off_viewport_note_ids": {"dom_A-1"}},
            "outside the current viewport",
        ),
        (
            {
                "card_hrefs_after_scroll": {
                    "dom_A-1": "https://www.xiaohongshu.com/explore/other_A-2"
                }
            },
            "unsafe to click",
        ),
        (
            {
                "card_hrefs_after_scroll": {
                    "dom_A-1": "https://www.xiaohongshu.com:444/explore/dom_A-1"
                }
            },
            "unsafe to click",
        ),
        ({"unsafe_control_note_ids": {"dom_A-1"}}, "unsafe to click"),
    ),
)
def test_click_visible_note_card_never_clicks_when_postscroll_revalidation_fails(
    tmp_path: Path, page_options: dict[str, object], error: str
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={},
        evaluated_values=[],
        card_hrefs=[f"https://www.xiaohongshu.com/explore/{note_id}"],
        off_viewport_note_ids={note_id},
        **page_options,
    )

    with pytest.raises(ValueError, match=error):
        persistent_client._click_visible_note_card(page, note_id)

    assert page.clicked_note_ids == []
    assert page.mouse_clicks == []
    assert len(page.card_scroll_evaluations) == 1
    assert len(page.card_safety_evaluations) == (
        1 if "scroll_failure_note_ids" in page_options else 2
    )


def test_persistent_client_clicks_the_prechecked_card_without_locator_reresolution(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {
                "title": "Prechecked card detail",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
        drifting_note_ids={note_id},
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        result = client.get_note_detail(note_id)

    assert result["note"]["title"] == "Prechecked card detail"
    _assert_exact_card_wait(page, note_id)
    assert page.mouse_clicks == [(60.0, 60.0)]
    assert page.card_scroll_evaluations == []
    _assert_exact_detail_wait(page, note_id)
    assert page.clicked_note_ids == [note_id]
    assert page.card_activation_expressions == []


def test_persistent_client_accepts_exact_card_and_final_paths_with_query_and_fragment(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {
                "title": "Query-bearing card detail",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
        card_hrefs=[f"https://www.xiaohongshu.com/explore/{note_id}?view=card#detail"],
        card_final_paths={note_id: f"/explore/{note_id}?view=detail#comments"},
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        assert client._dom_fallback_note_profiles == {note_id: "account_A-1"}
        result = client.get_note_detail(note_id)

    assert result["note"]["title"] == "Query-bearing card detail"
    assert page.clicked_note_ids == [note_id]
    _assert_exact_detail_wait(page, note_id)
    assert len(page.card_safety_evaluations) == 1
    href, expected_path, expression = page.card_safety_evaluations[0]
    assert href == f"https://www.xiaohongshu.com/explore/{note_id}?view=card#detail"
    assert expected_path == f"/explore/{note_id}"
    assert "new URL" in expression
    assert "parsed.pathname" in expression
    assert "document.elementFromPoint" in expression
    assert "element.contains" in expression
    assert "closest" in expression


@pytest.mark.parametrize(
    "center_anchor_href",
    (
        "https://www.xiaohongshu.com/user/profile/author_A-1?view=card#detail",
        "https://www.xiaohongshu.com:443/user/profile/author_A-1?view=card#detail",
        "/user/profile/author_A-1?view=card#detail",
    ),
)
def test_persistent_client_allows_an_explicit_safe_profile_anchor_at_card_center(
    tmp_path: Path, center_anchor_href: str
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {
                "title": "Delegated card detail",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
        center_anchor_hrefs={note_id: center_anchor_href},
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        result = client.get_note_detail(note_id)

    assert result["note"]["title"] == "Delegated card detail"
    _assert_exact_detail_wait(page, note_id)
    _, _, expression = page.card_safety_evaluations[0]
    assert "isReadOnlyProfile" in expression


@pytest.mark.parametrize(
    "origin",
    ("https://www.xiaohongshu.com", "https://www.xiaohongshu.com:443"),
)
def test_persistent_client_allows_matching_profile_note_anchor_at_card_center(
    tmp_path: Path, origin: str
) -> None:
    account_id = "abcdef0123456789abcdef01"
    note_id = "0123456789abcdef01234567"
    page = FakePage(
        route_responses={
            f"/user/profile/{account_id}": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {
                "title": "Delegated profile-note detail",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
        center_anchor_hrefs={
            note_id: f"{origin}/user/profile/{account_id}/{note_id}?view=card#detail"
        },
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info(account_id)
        client.get_user_posts(account_id)
        result = client.get_note_detail(note_id)

    assert result["note"]["title"] == "Delegated profile-note detail"
    assert page.mouse_clicks == [(60.0, 60.0)]
    _assert_exact_detail_wait(page, note_id)


@pytest.mark.parametrize(
    "center_anchor_href",
    (
        "",
        "#profile",
        "?view=card",
        "https://example.test/user/profile/author_A-1",
        "/user/profile/author_A-1/unsafe",
        "/user/profile/author_A-1/",
        "/user/profile/author_A-1/dom_A-1/extra",
        "/user/profile/author_A-1/../dom_A-1",
        "/user/profile/author_A-1/dom_A-1%2Funsafe",
        "https://www.xiaohongshu.com:444/user/profile/author_A-1/dom_A-1",
    ),
)
def test_persistent_client_rejects_nonexplicit_or_unsafe_profile_center_anchor(
    tmp_path: Path, center_anchor_href: str
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {"title": "Detail must remain unread"},
        ],
        center_anchor_hrefs={note_id: center_anchor_href},
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        with pytest.raises(DetailReadFailure):
            client.get_note_detail(note_id)

    assert page.mouse_clicks == []
    assert page.waited_urls == []
    assert len(page._evaluated_values) == 1


def test_persistent_client_waits_for_the_exact_visible_card_after_unrelated_card(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {
                "title": "Delayed exact card detail",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
        card_hrefs=["/explore/unrelated_A-1"],
        card_hrefs_after_exact_wait=[
            "/explore/unrelated_A-1",
            f"https://www.xiaohongshu.com/explore/{note_id}?view=card#detail",
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        result = client.get_note_detail(note_id)

    assert result["note"]["title"] == "Delayed exact card detail"
    _assert_exact_card_wait(page, note_id)
    assert page.card_safety_evaluations[0][0] == (
        f"https://www.xiaohongshu.com/explore/{note_id}?view=card#detail"
    )
    assert page.clicked_note_ids == [note_id]


def test_persistent_client_rejects_a_nondefault_port_exact_card_before_pointer_input(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {"title": "Detail must remain unread"},
        ],
        card_hrefs=[f"https://www.xiaohongshu.com:444/explore/{note_id}"],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        with pytest.raises(DetailReadFailure):
            client.get_note_detail(note_id)

    assert page.mouse_clicks == []
    assert page.waited_urls == []
    assert len(page._evaluated_values) == 1


@pytest.mark.parametrize(
    "unsafe_card_state",
    ("overlay_note_ids", "reflowed_note_ids", "unsafe_control_note_ids"),
)
def test_persistent_client_rejects_unsafe_card_hit_before_pointer_input(
    tmp_path: Path, unsafe_card_state: str
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {"title": "Detail must remain unread"},
        ],
        **{unsafe_card_state: {note_id}},
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        with pytest.raises(DetailReadFailure):
            client.get_note_detail(note_id)

    assert page.mouse_clicks == []
    assert page.waited_urls == []
    assert len(page._evaluated_values) == 1
    assert len(page.card_safety_evaluations) == 1


def test_persistent_client_reverifies_profile_before_each_tokenless_dom_card_click(
    tmp_path: Path,
) -> None:
    first_note_id = "dom_A-1"
    second_note_id = "dom_B-2"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [
                {"note_id": first_note_id, "xsec_token": ""},
                {"note_id": second_note_id, "xsec_token": ""},
            ],
            {
                "title": "First opened card",
                "body": "First visible detail",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-27",
                "likes": "1",
                "collects": "0",
                "comments": "0",
                "tags": [],
                "images": [],
                "has_video": False,
            },
            {
                "title": "Second opened card",
                "body": "Second visible detail",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-28",
                "likes": "2",
                "collects": "0",
                "comments": "0",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
        final_paths={
            f"/explore/{first_note_id}": "/404",
            f"/explore/{second_note_id}": "/404",
        },
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        assert client.get_user_posts("account_A-1") == [
            {"note_id": first_note_id, "xsec_token": ""},
            {"note_id": second_note_id, "xsec_token": ""},
        ]
        first_detail = client.get_note_detail(first_note_id)
        second_detail = client.get_note_detail(second_note_id)

    assert first_detail["note"]["title"] == "First opened card"
    assert second_detail["note"]["title"] == "Second opened card"
    assert [urlsplit(url).path for url in page.navigations] == [
        "/user/profile/account_A-1",
        "/user/profile/account_A-1",
        "/user/profile/account_A-1",
    ]
    assert page.clicked_note_ids == [first_note_id, second_note_id]
    assert page.waits == [2000, 4000, 3000, 4000, 4000, 3000, 4000, 4000]


def test_persistent_client_rejects_a_redirected_dom_card_before_detail_projection(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {"title": "Untrusted redirected card"},
        ],
        card_final_paths={note_id: "/404"},
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        with pytest.raises(DetailReadFailure):
            client.get_note_detail(note_id)

    assert len(page._evaluated_values) == 1
    assert [urlsplit(url).path for url in page.navigations] == [
        "/user/profile/account_A-1",
        "/user/profile/account_A-1",
    ]
    assert page.clicked_note_ids == [note_id]
    assert page.mouse_clicks == [(60.0, 60.0)]
    assert len(page.waited_urls) == 1
    predicate, options = page.waited_urls[0]
    assert callable(predicate)
    assert not predicate(page.url)
    assert options == {"wait_until": "domcontentloaded", "timeout": 20_000}


def test_persistent_client_keeps_later_tokenless_search_details_on_the_direct_path(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ],
            "/search_result": [
                FakeResponse(
                    "/api/sns/web/v2/search/notes",
                    {
                        "data": {
                            "items": [
                                {
                                    "model_type": "note",
                                    "id": note_id,
                                    "xsec_token": "",
                                    "note_card": {"display_title": "Search card"},
                                }
                            ]
                        }
                    },
                )
            ],
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [{"note_id": note_id, "xsec_token": ""}],
            {
                "title": "Direct search detail",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
        card_final_paths={note_id: "/404"},
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        client.search_notes("AI workflow")
        result = client.get_note_detail(note_id)

    assert result["note"]["title"] == "Direct search detail"
    assert [urlsplit(url).path for url in page.navigations] == [
        "/user/profile/account_A-1",
        "/search_result",
        f"/explore/{note_id}",
    ]
    assert page.clicked_note_ids == []


def test_persistent_client_keeps_only_bound_dom_card_metadata_and_clears_it_on_exit(
    tmp_path: Path,
) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            [
                {
                    "note_id": note_id,
                    "xsec_token": "",
                    "card_title": "Bound card text",
                    "card_cover_url": "bound-card-cover",
                }
            ],
        ],
    )
    client = _compatibility_client(tmp_path, page, RejectingPinnedDelegate())

    with client:
        client.get_user_info("account_A-1")
        assert client.get_user_posts("account_A-1") == [{"note_id": note_id, "xsec_token": ""}]
        retained_state = {
            name: value
            for name, value in vars(client).items()
            if name.startswith(("_cached_", "_pending_", "_dom_fallback_"))
        }
        assert retained_state == {
            "_pending_user_id": None,
            "_pending_user_note_candidates": None,
            "_cached_note_metadata": {
                note_id: {
                    "card_title": "Bound card text",
                    "card_cover_url": "bound-card-cover",
                }
            },
            "_dom_fallback_note_profiles": {note_id: "account_A-1"},
        }

    assert client._dom_fallback_note_profiles == {}


def test_persistent_client_keeps_only_bound_standalone_dom_card_metadata_when_api_is_absent(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={},
        evaluated_values=[
            [
                {
                    "note_id": "dom_B-2",
                    "xsec_token": "",
                    "card_title": "Bound standalone title",
                    "card_cover_url": "bound-standalone-cover",
                }
            ]
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        assert client.get_user_posts("account_A-1") == [{"note_id": "dom_B-2", "xsec_token": ""}]
        retained_state = {
            name: value
            for name, value in vars(client).items()
            if name.startswith(("_cached_", "_pending_"))
        }
        assert retained_state == {
            "_pending_user_id": None,
            "_pending_user_note_candidates": None,
            "_cached_note_metadata": {
                "dom_B-2": {
                    "card_title": "Bound standalone title",
                    "card_cover_url": "bound-standalone-cover",
                }
            },
        }

    assert [urlsplit(url).path for url in page.navigations] == ["/user/profile/account_A-1"]
    assert page._evaluated_values == []


def test_persistent_client_discards_full_account_posts_after_profile_capture(
    tmp_path: Path,
) -> None:
    note_id = "6553F1000000000000000000"
    opaque_token = "temporary-candidate"
    posts = [
        {
            "note_id": note_id,
            "xsec_token": opaque_token,
            "cover": {"url": "https://images.example.invalid/post-cover.webp"},
            "display_title": "Account post title",
            "interact_info": {"shared_count": "6"},
            "time": 1_700_000_001,
            "type": "normal",
            "user": {"user_id": "account_A-1"},
        }
    ]
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": posts}})
            ]
        },
        evaluated_values=[
            {
                "name": "Visible account",
                "bio": "Visible bio",
                "red_id": "display-id",
                "avatar": "",
                "interactions": [],
            },
            {
                "title": "Visible detail title",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
    )

    client = _compatibility_client(tmp_path, page, RejectingPinnedDelegate())
    with client:
        client.get_user_info("account_A-1")

        retained_state = {
            name: value
            for name, value in vars(client).items()
            if name.startswith(("_cached_", "_pending_"))
        }
        assert retained_state == {
            "_pending_user_id": "account_A-1",
            "_pending_user_note_candidates": [{"note_id": note_id, "xsec_token": opaque_token}],
            "_cached_note_metadata": {
                note_id: {
                    "shared_count": "6",
                    "share_source": "account_card_interface",
                    "time": 1_700_000_001,
                    "card_title": "Account post title",
                    "fallback_cover_url": "https://images.example.invalid/post-cover.webp",
                }
            },
        }
        assert client.get_user_posts("account_A-1") == [
            {"note_id": note_id, "xsec_token": opaque_token}
        ]
        assert client._pending_user_id is None
        assert client._pending_user_note_candidates is None
        result = client.get_note_detail(note_id, opaque_token)

    assert isinstance(result, dict)
    note = result["note"]
    assert isinstance(note, dict)
    assert note["time"] == 1_700_000_001
    assert note["interact_info"] == {
        "liked_count": "12",
        "collected_count": "3",
        "comment_count": "1",
        "shared_count": "6",
    }
    assert note["metric_provenance"] == {
        "likes": "detail_visible_count",
        "collects": "detail_visible_count",
        "comments": "detail_visible_count",
        "shares": "account_card_interface",
    }
    assert [urlsplit(url).path for url in page.navigations] == [
        "/user/profile/account_A-1",
        f"/explore/{note_id}",
    ]
    assert client._pending_user_note_candidates is None
    assert client._cached_note_metadata == {}


def test_persistent_client_retains_exact_account_post_time_for_later_detail_projection(
    tmp_path: Path,
) -> None:
    note_id = "6553F1000000000000000000"
    posts = [
        {
            "note_id": note_id,
            "interact_info": {"shared_count": "6"},
            "time": 1_700_000_001,
        }
    ]
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": posts}})
            ]
        },
        evaluated_values=[
            {
                "name": "Visible account",
                "bio": "Visible bio",
                "red_id": "display-id",
                "avatar": "",
                "interactions": [],
            },
            {
                "title": "Visible detail title",
                "body": "Visible detail body",
                "author_name": "Visible author",
                "author_profile_url": "https://www.xiaohongshu.com/user/profile/author_A-1",
                "published_at": "06-27",
                "likes": "12",
                "collects": "3",
                "comments": "1",
                "tags": [],
                "images": [],
                "has_video": False,
            },
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A-1")
        assert client.get_user_posts("account_A-1") == [{"note_id": note_id, "xsec_token": ""}]
        result = client.get_note_detail(note_id)

    assert isinstance(result, dict)
    note = result["note"]
    assert isinstance(note, dict)
    assert note["time"] == 1_700_000_001
    assert note["interact_info"] == {
        "liked_count": "12",
        "collected_count": "3",
        "comment_count": "1",
        "shared_count": "6",
    }


def test_persistent_client_rejects_a_redirected_profile_page_before_projection(
    tmp_path: Path,
) -> None:
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[{"name": "Untrusted redirected account"}],
        final_paths={"/user/profile/account_A-1": "/user/profile/different_A-1"},
    )

    with (
        _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client,
        pytest.raises(ValueError, match="unexpected profile page"),
    ):
        client.get_user_info("account_A-1")

    assert len(page._evaluated_values) == 1


def _safe_detail_projection() -> dict[str, object]:
    return {
        "title": "Visible detail",
        "body": "Visible detail body",
        "author_name": "Visible author",
        "author_profile_url": "https://www.xiaohongshu.com/user/profile/account_A-1",
        "likes": "1",
        "collects": "0",
        "comments": "0",
        "tags": [],
        "images": [],
        "media_scope_valid": True,
        "has_video": False,
    }


def test_account_pacing_applies_only_to_account_profile_and_candidate_details(
    tmp_path: Path,
) -> None:
    note_ids = ("account_note_A-1", "account_note_B-2")
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse(
                    "/api/sns/web/v1/user_posted",
                    {
                        "data": {
                            "notes": [
                                {"note_id": note_id, "xsec_token": f"token-{index}"}
                                for index, note_id in enumerate(note_ids)
                            ]
                        }
                    },
                )
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            _safe_detail_projection(),
            _safe_detail_projection(),
        ],
    )

    with _compatibility_client(
        tmp_path,
        page,
        RejectingPinnedDelegate(),
        delay_sampler=lambda low, _high: low,
    ) as client:
        client.get_user_info("account_A-1")
        assert client.get_user_posts("account_A-1") == [
            {"note_id": note_id, "xsec_token": f"token-{index}"}
            for index, note_id in enumerate(note_ids)
        ]
        for index, note_id in enumerate(note_ids):
            client.get_note_detail(note_id, f"token-{index}")

        assert page.waits.count(2_000) == 1
        assert page.waits.count(3_000) == 2
        assert client.account_pacing_summary() == PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=2_000,
            detail_delay_ms=[3_000, 3_000],
        )


def test_bridge_retains_a_selected_detail_delay_when_the_second_account_wait_fails(
    tmp_path: Path,
) -> None:
    """Appending after the wait makes a finite detail failure invalidate the strict ledger."""

    class FailingSecondAccountDelayPage(FakePage):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.detail_delay_waits = 0

        def wait_for_timeout(self, milliseconds: int) -> None:
            if milliseconds == 3_000:
                self.detail_delay_waits += 1
                if self.detail_delay_waits == 2:
                    self.waits.append(milliseconds)
                    raise RuntimeError("xsec_token=private-detail-delay")
            super().wait_for_timeout(milliseconds)

    note_ids = ("account_note_A-1", "account_note_B-2")
    page = FailingSecondAccountDelayPage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse(
                    "/api/sns/web/v1/user_posted",
                    {
                        "data": {
                            "notes": [
                                {"note_id": note_id, "xsec_token": f"token-{index}"}
                                for index, note_id in enumerate(note_ids)
                            ]
                        }
                    },
                )
            ]
        },
        evaluated_values=[
            {
                "profile_id": "account_A-1",
                "name": "Visible account",
                "bio": "",
                "red_id": "",
                "avatar": "",
                "interactions": [],
            },
            _safe_detail_projection(),
        ],
    )
    client = _compatibility_client(
        tmp_path,
        page,
        RejectingPinnedDelegate(),
        delay_sampler=lambda low, _high: low,
    )

    response = xhs_bridge.run_bridge(
        xhs_bridge.BridgeRequest(operation="account", account_id="account_A-1", limit=2),
        lambda _profile: client,
        staging_dir=tmp_path,
    )

    assert response.status == "partial"
    assert response.error_code == "detail_unavailable"
    assert [note["source_position"] for note in response.payload["notes"]] == [1]
    assert response.payload["candidate_attempts"][1] == {
        "position": 2,
        "note_id": "account_note_B-2",
        "outcome": "unavailable",
        "stage": "detail_navigation",
        "reason": "upstream_unavailable",
    }
    assert response.payload["pacing_summary"] == {
        "policy": "conservative_jitter_v1",
        "profile_open_delay_ms": 2_000,
        "detail_delay_ms": [3_000, 3_000],
    }
    assert "private-detail-delay" not in response.model_dump_json()
    assert "xsec_token" not in response.model_dump_json()


def test_search_only_flow_has_no_account_pacing_delays(tmp_path: Path) -> None:
    note_id = "search_note_A-1"
    page = FakePage(
        route_responses={
            "/search_result": [
                FakeResponse(
                    "/api/sns/web/v2/search/notes",
                    {
                        "data": {
                            "items": [
                                {
                                    "model_type": "note",
                                    "id": note_id,
                                    "xsec_token": "search-token",
                                    "note_card": {"display_title": "Search card"},
                                }
                            ]
                        }
                    },
                )
            ]
        },
        evaluated_values=[_safe_detail_projection()],
    )

    with _compatibility_client(
        tmp_path,
        page,
        RejectingPinnedDelegate(),
        delay_sampler=lambda low, _high: low,
    ) as client:
        client.search_notes("AI workflow")
        client.get_note_detail(note_id, "search-token")
        with pytest.raises(RuntimeError, match="^account pacing is unavailable$"):
            client.account_pacing_summary()

    assert 2_000 not in page.waits
    assert 3_000 not in page.waits


@pytest.mark.parametrize("value", (1_999, 5_001, True, 2.0))
def test_delay_sampler_rejects_values_outside_its_requested_interval(
    tmp_path: Path, value: object
) -> None:
    page = FakePage(route_responses={}, evaluated_values=[])
    client = _compatibility_client(
        tmp_path,
        page,
        RejectingPinnedDelegate(),
        delay_sampler=lambda _low, _high: value,  # type: ignore[return-value]
    )

    with pytest.raises(ValueError, match="^delay sampler returned an invalid value$"):
        client._sample_delay(2_000, 5_000)


def test_client_exit_clears_account_pacing_summary(tmp_path: Path) -> None:
    page = FakePage(
        route_responses={"/user/profile/account_A-1": []},
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []}
        ],
        profile_card_hrefs=[],
    )
    client = _compatibility_client(
        tmp_path,
        page,
        RejectingPinnedDelegate(),
        delay_sampler=lambda low, _high: low,
    )

    with client:
        client.get_user_info("account_A-1")
        assert client.account_pacing_summary().profile_open_delay_ms == 2_000

    with pytest.raises(RuntimeError, match="^account pacing is unavailable$"):
        client.account_pacing_summary()


def _exercise_profile_detail_failure(tmp_path: Path, condition: str) -> None:
    note_id = "dom_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse("/api/sns/web/v1/user_posted", {"data": {"notes": []}})
            ]
        },
        evaluated_values=[
            {"name": "Visible account", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            _safe_detail_projection(),
        ],
        profile_card_hrefs=[f"https://www.xiaohongshu.com/explore/{note_id}"],
        card_hrefs=[] if condition == "missing" else [f"/explore/{note_id}"],
        off_viewport_note_ids={note_id} if condition == "offscreen" else set(),
        scroll_leaves_off_viewport_note_ids={note_id} if condition == "offscreen" else set(),
        unsafe_control_note_ids={note_id} if condition == "unsafe" else set(),
        card_final_paths={note_id: "/404"} if condition == "route" else {},
    )
    if condition == "timeout":
        def timeout(*_args: object, **_kwargs: object) -> None:
            raise TimeoutError("sensitive timeout detail")

        page.wait_for_url = timeout  # type: ignore[method-assign]
    if condition == "projection":
        original_evaluate = page.evaluate

        def unavailable_projection(expression: str, arg: object | None = None) -> object:
            if expression == _NOTE_DETAIL_SCRIPT:
                raise RuntimeError("sensitive projection detail")
            return original_evaluate(expression, arg)

        page.evaluate = unavailable_projection  # type: ignore[method-assign]
    if condition == "upstream":
        def unavailable_card(*_args: object, **_kwargs: object) -> FakeFunctionResult:
            raise RuntimeError(
                "https://private.example/detail?token=opaque selector=.private-card page text=secret"
            )

        page.wait_for_function = unavailable_card  # type: ignore[method-assign]

    with _compatibility_client(
        tmp_path,
        page,
        RejectingPinnedDelegate(),
        delay_sampler=lambda low, _high: low,
    ) as client:
        client.get_user_info("account_A-1")
        client.get_user_posts("account_A-1")
        client.get_note_detail(note_id)


@pytest.mark.parametrize(
    ("condition", "stage", "reason"),
    [
        ("missing", "card_resolution", "card_not_found"),
        ("offscreen", "card_resolution", "card_offscreen"),
        ("unsafe", "card_resolution", "card_unsafe"),
        ("timeout", "detail_navigation", "detail_timeout"),
        ("route", "detail_navigation", "detail_route_mismatch"),
        ("projection", "detail_projection", "detail_projection_unavailable"),
        ("upstream", "card_resolution", "upstream_unavailable"),
    ],
)
def test_profile_detail_failure_is_finite(
    tmp_path: Path, condition: str, stage: str, reason: str
) -> None:
    with pytest.raises(DetailReadFailure) as raised:
        _exercise_profile_detail_failure(tmp_path, condition)

    assert raised.value.stage == stage
    assert raised.value.reason == reason
    assert condition not in str(raised.value)
    assert "private.example" not in str(raised.value)
    assert "opaque" not in str(raised.value)
    assert ".private-card" not in str(raised.value)
    assert "secret" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_direct_user_posts_starts_account_pacing_and_marks_token_detail_origin(
    tmp_path: Path,
) -> None:
    note_id = "direct_account_note_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse(
                    "/api/sns/web/v1/user_posted",
                    {"data": {"notes": [{"note_id": note_id, "xsec_token": "account-token"}]}},
                )
            ]
        },
        evaluated_values=[_safe_detail_projection()],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        assert client.get_user_posts("account_A-1") == [
            {"note_id": note_id, "xsec_token": "account-token"}
        ]
        client.get_note_detail(note_id, "account-token")
        assert client.account_pacing_summary() == PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=2_000,
            detail_delay_ms=[3_000],
        )

    assert page.waits.count(2_000) == 1
    assert page.waits.count(3_000) == 1


def test_switching_accounts_replaces_pacing_summary_and_origin_ids(tmp_path: Path) -> None:
    first_note_id = "account_A_note"
    second_note_id = "account_B_note"
    page = FakePage(
        route_responses={
            "/user/profile/account_A": [
                FakeResponse(
                    "/api/sns/web/v1/user_posted",
                    {"data": {"notes": [{"note_id": first_note_id, "xsec_token": "token-A"}]}},
                )
            ],
            "/user/profile/account_B": [
                FakeResponse(
                    "/api/sns/web/v1/user_posted",
                    {"data": {"notes": [{"note_id": second_note_id, "xsec_token": "token-B"}]}},
                )
            ],
        },
        evaluated_values=[
            {"name": "Account A", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            {"name": "Account B", "bio": "", "red_id": "", "avatar": "", "interactions": []},
            _safe_detail_projection(),
            _safe_detail_projection(),
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A")
        client.get_user_info("account_B")
        client.get_note_detail(first_note_id, "token-A")
        client.get_note_detail(second_note_id, "token-B")
        assert client.account_pacing_summary() == PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=2_000,
            detail_delay_ms=[3_000],
        )

    assert page.waits.count(2_000) == 2
    assert page.waits.count(3_000) == 1


def test_search_clears_pending_account_candidates_before_direct_user_posts(tmp_path: Path) -> None:
    stale_note_id = "stale_account_note"
    fresh_note_id = "fresh_account_note"
    page = FakePage(
        route_responses={
            "/user/profile/account_A": [
                FakeResponse(
                    "/api/sns/web/v1/user_posted",
                    {"data": {"notes": [{"note_id": stale_note_id, "xsec_token": "stale-token"}]}},
                ),
            ],
            "/search_result": [
                FakeResponse("/api/sns/web/v2/search/notes", {"data": {"items": []}})
            ],
        },
        evaluated_values=[
            {"name": "Account A", "bio": "", "red_id": "", "avatar": "", "interactions": []}
        ],
    )

    with _compatibility_client(tmp_path, page, RejectingPinnedDelegate()) as client:
        client.get_user_info("account_A")
        page._route_responses["/user/profile/account_A"] = [
            FakeResponse(
                "/api/sns/web/v1/user_posted",
                {"data": {"notes": [{"note_id": fresh_note_id, "xsec_token": "fresh-token"}]}},
            )
        ]
        client.search_notes("AI workflow")
        assert client.get_user_posts("account_A") == [
            {"note_id": fresh_note_id, "xsec_token": "fresh-token"}
        ]
        assert client.account_pacing_summary() == PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=2_000,
            detail_delay_ms=[],
        )

    assert page.waits.count(2_000) == 2


@pytest.mark.parametrize("path", ("profile", "detail"))
def test_delay_sampler_is_validated_on_actual_account_paths(tmp_path: Path, path: str) -> None:
    note_id = "account_note_A-1"
    page = FakePage(
        route_responses={
            "/user/profile/account_A-1": [
                FakeResponse(
                    "/api/sns/web/v1/user_posted",
                    {"data": {"notes": [{"note_id": note_id, "xsec_token": "token"}]}},
                )
            ]
        },
        evaluated_values=[
            {"name": "Account", "bio": "", "red_id": "", "avatar": "", "interactions": []}
        ],
    )
    sampler = (
        (lambda _low, _high: 1_999)
        if path == "profile"
        else (lambda low, _high: low if low == 2_000 else 7_001)
    )

    with _compatibility_client(
        tmp_path, page, RejectingPinnedDelegate(), delay_sampler=sampler
    ) as client:
        if path == "profile":
            with pytest.raises(ValueError, match="^delay sampler returned an invalid value$"):
                client.get_user_info("account_A-1")
        else:
            client.get_user_info("account_A-1")
            client.get_user_posts("account_A-1")
            with pytest.raises(ValueError, match="^delay sampler returned an invalid value$"):
                client.get_note_detail(note_id, "token")


def test_persistent_client_exposes_only_context_management_and_pinned_read_methods() -> None:
    public_names = {name.lower() for name in dir(PersistentXhsClient) if not name.startswith("_")}

    assert public_names == {
        "account_pacing_summary",
        "search_notes",
        "get_note_detail",
        "get_user_info",
        "get_user_posts",
    }


def test_browser_source_has_one_project_factory_and_no_system_cookie_extraction() -> None:
    source_root = Path(__file__).parents[1] / "src" / "xhs_workbench"
    sources = {
        source.name: source.read_text(encoding="utf-8") for source in source_root.glob("*.py")
    }
    production_source = "\n".join(sources.values())

    for forbidden in (
        "xhs_cli.auth",
        "get_cookie_string(",
        "browser_cookie3",
        "Library/Application Support/Google/Chrome",
        "Library/Application Support/Firefox",
    ):
        assert forbidden not in production_source
    assert production_source.count('"camoufox" + ".sync_api"') == 1
    assert sources["isolated_login.py"].count('"camoufox" + ".sync_api"') == 1
