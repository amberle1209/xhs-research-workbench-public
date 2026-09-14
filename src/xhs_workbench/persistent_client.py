"""Read-only pinned-client adapter over the project-owned persistent browser context."""

from __future__ import annotations

import math
import re
import secrets
import sys
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, cast
from urllib.parse import SplitResult, parse_qsl, quote, urlsplit

from xhs_workbench.isolated_login import (
    _BrowserContext,
    _BrowserFactory,
    _default_browser_factory,
    _prepare_profile_directory,
)
from xhs_workbench.media import MAX_DISCOVERED_IMAGE_SLOTS
from xhs_workbench.models import (
    CandidateAttemptReason,
    CandidateAttemptStage,
    PacingSummary,
    TimeEvidence,
    TimeEvidenceKind,
)


class _PinnedReadOnlyDelegate(Protocol):
    _page: object

    def search_notes(self, keyword: str) -> object: ...

    def get_note_detail(self, note_id: str, xsec_token: str = "") -> object: ...

    def get_user_info(self, user_id: str) -> object: ...

    def get_user_posts(self, user_id: str) -> object: ...


_DelegateFactory = Callable[[], _PinnedReadOnlyDelegate]
_BASE_URL = "https://www.xiaohongshu.com"
_SEARCH_ENDPOINTS = (
    "/api/sns/web/v2/search/notes",
    "/api/sns/web/v1/search/notes",
)
_USER_POSTS_ENDPOINT = "/api/sns/web/v1/user_posted"
_API_RESPONSE_HOSTS = {
    "/api/sns/web/v2/search/notes": "so.xiaohongshu.com",
    "/api/sns/web/v1/search/notes": "so.xiaohongshu.com",
    _USER_POSTS_ENDPOINT: "edith.xiaohongshu.com",
}
_SEARCH_PAGE_QUERY_ADDITIONS = (("type", "51"),)
_SETTLE_MILLISECONDS = 4000
_SAFE_NOTE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_EXACT_METRIC_VALUE = re.compile(r"^(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)$")
_ROUNDED_METRIC_VALUE = re.compile(r"^\d+(?:\.\d+)?(?:万|w|W|k|K)$")
_CARD_TARGET_TIMEOUT_MILLISECONDS = 20_000
_CARD_SCROLL_SETTLE_MILLISECONDS = 250
_NoteMetadata = dict[str, int | str]
_UserPostCandidate = dict[str, str]


class DetailReadFailure(RuntimeError):
    """A finite account-detail failure that does not retain upstream data."""

    stage: CandidateAttemptStage
    reason: CandidateAttemptReason

    def __init__(self, stage: CandidateAttemptStage, reason: CandidateAttemptReason) -> None:
        self.stage = stage
        self.reason = reason
        super().__init__("account detail unavailable")


class _CardResolutionFailure(ValueError):
    """Internal safe-card outcome, converted at the account-detail boundary."""

    reason: CandidateAttemptReason

    def __init__(self, reason: CandidateAttemptReason, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class _CurrentResponse(Protocol):
    url: str

    status: int

    def json(self) -> object: ...


class _CurrentElementHandle(Protocol):
    def evaluate(self, expression: str, arg: object | None = None) -> object: ...


class _CurrentJSHandle(Protocol):
    def as_element(self) -> _CurrentElementHandle | None: ...


class _CurrentMouse(Protocol):
    def click(self, x: float, y: float, **options: object) -> object: ...


class _CurrentLocator(Protocol):
    def filter(self, *, has: _CurrentLocator) -> Self: ...

    @property
    def first(self) -> Self: ...

    def element_handle(self, *, timeout: int) -> _CurrentElementHandle | None: ...

    def element_handles(self) -> list[_CurrentElementHandle]: ...


class _CurrentPage(Protocol):
    url: str
    mouse: _CurrentMouse

    def on(self, event: str, handler: Callable[[_CurrentResponse], None]) -> None: ...

    def remove_listener(self, event: str, handler: Callable[[_CurrentResponse], None]) -> None: ...

    def goto(self, url: str, **options: object) -> object: ...

    def wait_for_timeout(self, milliseconds: int) -> object: ...

    def wait_for_url(self, url: Callable[[str], bool], **options: object) -> object: ...

    def wait_for_function(
        self, expression: str, *, arg: object | None = None, **options: object
    ) -> _CurrentJSHandle: ...

    def evaluate(self, expression: str, arg: object | None = None) -> object: ...

    def locator(self, selector: str) -> _CurrentLocator: ...


class PersistentXhsClient:
    """Reuse one project-owned browser context for pinned upstream read methods."""

    def __init__(
        self,
        auth_dir: Path,
        *,
        browser_factory: _BrowserFactory = _default_browser_factory,
        delegate_factory: _DelegateFactory | None = None,
        delay_sampler: Callable[[int, int], int] | None = None,
    ) -> None:
        self._profile_dir = _prepare_profile_directory(auth_dir)
        self._browser_factory = browser_factory
        self._delegate_factory = delegate_factory or _default_delegate_factory
        self._browser_manager: AbstractContextManager[_BrowserContext] | None = None
        self._delegate: _PinnedReadOnlyDelegate | None = None
        self._pending_user_id: str | None = None
        self._pending_user_note_candidates: list[_UserPostCandidate] | None = None
        self._cached_note_metadata: dict[str, _NoteMetadata] = {}
        self._dom_fallback_note_profiles: dict[str, str] = {}
        self._delay_sampler = delay_sampler or secrets.SystemRandom().randint
        self._account_profile_open_delay_ms: int | None = None
        self._account_detail_delay_ms: list[int] = []
        self._account_origin_note_ids: set[str] = set()
        self._active_account_id: str | None = None

    def __enter__(self) -> Self:
        manager = self._browser_factory(self._profile_dir)
        try:
            context = manager.__enter__()
            page = context.pages[0] if context.pages else context.new_page()
            delegate = self._delegate_factory()
            delegate._page = page
        except Exception:
            manager.__exit__(*sys.exc_info())
            raise
        self._browser_manager = manager
        self._delegate = delegate
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        manager = self._browser_manager
        self._browser_manager = None
        self._delegate = None
        self._clear_account_collection_state()
        self._cached_note_metadata.clear()
        self._dom_fallback_note_profiles.clear()
        return (
            bool(manager.__exit__(exc_type, exc_value, traceback)) if manager is not None else False
        )

    def search_notes(self, keyword: str) -> object:
        page = self._current_page()
        if page is not None:
            self._clear_account_collection_state()
            self._cached_note_metadata.clear()
            self._dom_fallback_note_profiles.clear()
            notes = _capture_search_notes(page, keyword)
            self._forget_dom_fallback_note_profiles(notes)
            self._remember_note_metadata(notes, share_source="search_card_interface")
            return notes
        return self._require_delegate().search_notes(keyword)

    def get_note_detail(self, note_id: str, xsec_token: str = "") -> object:
        page = self._current_page()
        if page is not None:
            profile_id = self._dom_fallback_note_profiles.get(note_id)
            if note_id in self._account_origin_note_ids:
                pacing_stage: CandidateAttemptStage = (
                    "card_resolution" if not xsec_token and profile_id is not None else "detail_navigation"
                )
                delay = self._sample_delay(3_000, 7_000)
                self._account_detail_delay_ms.append(delay)
                pacing_failure: DetailReadFailure | None = None
                try:
                    page.wait_for_timeout(delay)
                except Exception:  # noqa: BLE001 - browser pacing must fail at the finite boundary.
                    pacing_failure = DetailReadFailure(pacing_stage, "upstream_unavailable")
                if pacing_failure is not None:
                    raise pacing_failure
                return self._read_account_note_detail(
                    page,
                    note_id,
                    xsec_token,
                    profile_id,
                    self._cached_note_metadata.get(note_id),
                )
            return _read_note_detail(
                page, note_id, xsec_token, self._cached_note_metadata.get(note_id)
            )
        return self._require_delegate().get_note_detail(note_id, xsec_token)

    def get_user_info(self, user_id: str) -> object:
        page = self._current_page()
        if page is not None:
            self._start_account_collection(page, user_id)
            self._cached_note_metadata.clear()
            self._dom_fallback_note_profiles.clear()
            profile, posts, used_dom_fallback = _read_user_profile(page, user_id)
            candidates = _user_post_candidates(posts)
            self._account_origin_note_ids.update(candidate["note_id"] for candidate in candidates)
            self._pending_user_id = user_id
            self._pending_user_note_candidates = candidates
            self._remember_note_metadata(
                posts,
                exact_dom_card_cover=used_dom_fallback,
                share_source="account_card_interface",
            )
            if used_dom_fallback:
                self._remember_dom_fallback_note_profiles(user_id, candidates)
            else:
                self._forget_dom_fallback_note_profiles(posts)
            return profile
        return self._require_delegate().get_user_info(user_id)

    def get_user_posts(self, user_id: str) -> object:
        page = self._current_page()
        if page is not None:
            if (
                self._active_account_id == user_id
                and self._pending_user_id == user_id
                and self._pending_user_note_candidates is not None
            ):
                candidates = self._pending_user_note_candidates
                self._pending_user_id = None
                self._pending_user_note_candidates = None
                return candidates
            self._start_account_collection(page, user_id)
            self._cached_note_metadata.clear()
            self._dom_fallback_note_profiles.clear()
            posts, used_dom_fallback = _capture_user_posts(page, user_id)
            self._remember_note_metadata(
                posts,
                exact_dom_card_cover=used_dom_fallback,
                share_source="account_card_interface",
            )
            candidates = _user_post_candidates(posts)
            self._account_origin_note_ids.update(candidate["note_id"] for candidate in candidates)
            if used_dom_fallback:
                self._remember_dom_fallback_note_profiles(user_id, candidates)
            else:
                self._forget_dom_fallback_note_profiles(posts)
            return candidates
        return self._require_delegate().get_user_posts(user_id)

    def _clear_account_collection_state(self) -> None:
        self._pending_user_id = None
        self._pending_user_note_candidates = None
        self._account_profile_open_delay_ms = None
        self._account_detail_delay_ms.clear()
        self._account_origin_note_ids.clear()
        self._active_account_id = None

    def _start_account_collection(self, page: _CurrentPage, user_id: str) -> None:
        if self._active_account_id == user_id:
            return
        self._clear_account_collection_state()
        self._active_account_id = user_id
        self._account_profile_open_delay_ms = self._wait_account_delay(page, 2_000, 5_000)

    def account_pacing_summary(self) -> PacingSummary:
        if self._browser_manager is None or self._account_profile_open_delay_ms is None:
            raise RuntimeError("account pacing is unavailable")
        return PacingSummary(
            policy="conservative_jitter_v1",
            profile_open_delay_ms=self._account_profile_open_delay_ms,
            detail_delay_ms=self._account_detail_delay_ms,
        )

    def _sample_delay(self, low: int, high: int) -> int:
        value = self._delay_sampler(low, high)
        if type(value) is not int or not low <= value <= high:
            raise ValueError("delay sampler returned an invalid value")
        return value

    def _wait_account_delay(self, page: _CurrentPage, low: int, high: int) -> int:
        delay = self._sample_delay(low, high)
        page.wait_for_timeout(delay)
        return delay

    def _read_account_note_detail(
        self,
        page: _CurrentPage,
        note_id: str,
        xsec_token: str,
        profile_id: str | None,
        metadata: _NoteMetadata | None,
    ) -> dict[str, object]:
        stage: CandidateAttemptStage = "detail_navigation"
        encoded_note_id = quote(note_id, safe="")
        if not xsec_token and profile_id is not None:
            encoded_profile_id = quote(profile_id, safe="")
            navigation_failure = self._account_navigate(
                page, f"{_BASE_URL}/user/profile/{encoded_profile_id}"
            )
            if navigation_failure is not None:
                raise navigation_failure
            route_failure = self._account_require_page_path(
                page, f"/user/profile/{encoded_profile_id}"
            )
            if route_failure is not None:
                raise route_failure
            stage = "card_resolution"
            detail_navigation_entered = False
            card_failure: DetailReadFailure | None = None

            def entered_detail_navigation() -> None:
                nonlocal detail_navigation_entered
                detail_navigation_entered = True

            try:
                _click_visible_note_card(page, encoded_note_id, entered_detail_navigation)
            except _CardResolutionFailure as error:
                card_failure = DetailReadFailure("card_resolution", error.reason)
            except Exception as error:  # noqa: BLE001 - browser card resolution is untrusted.
                card_failure = DetailReadFailure(
                    "detail_navigation" if detail_navigation_entered else stage,
                    _navigation_failure_reason(error)
                    if detail_navigation_entered
                    else "upstream_unavailable",
                )
            if card_failure is not None:
                raise card_failure
            stage = "detail_navigation"
        else:
            query = "?xsec_source=pc_search"
            if xsec_token:
                query += f"&xsec_token={quote(xsec_token, safe='')}"
            navigation_failure = self._account_navigate(
                page, f"{_BASE_URL}/explore/{encoded_note_id}{query}"
            )
            if navigation_failure is not None:
                raise navigation_failure
            route_failure = self._account_require_page_path(page, f"/explore/{encoded_note_id}")
            if route_failure is not None:
                raise route_failure
        if profile_id is not None and not xsec_token:
            settle_failure: DetailReadFailure | None = None
            try:
                page.wait_for_timeout(_SETTLE_MILLISECONDS)
            except Exception:  # noqa: BLE001 - browser detail navigation is untrusted.
                settle_failure = DetailReadFailure(stage, "upstream_unavailable")
            if settle_failure is not None:
                raise settle_failure
            route_failure = self._account_require_page_path(page, f"/explore/{encoded_note_id}")
            if route_failure is not None:
                raise route_failure
        stage = "detail_projection"
        projection_failure: DetailReadFailure | None = None
        result: dict[str, object] | None = None
        try:
            raw = page.evaluate(_NOTE_DETAIL_SCRIPT, note_id)
            result = _project_note_detail(note_id, raw, metadata)
        except Exception:  # noqa: BLE001 - browser detail projection is untrusted.
            projection_failure = DetailReadFailure(
                "detail_projection", "detail_projection_unavailable"
            )
        if projection_failure is not None:
            raise projection_failure
        if result is None:
            raise DetailReadFailure(stage, "upstream_unavailable")
        return result

    @staticmethod
    def _account_navigate(page: _CurrentPage, url: str) -> DetailReadFailure | None:
        failure: DetailReadFailure | None = None
        try:
            _navigate(page, url)
        except Exception as error:  # noqa: BLE001 - browser navigation is untrusted.
            failure = DetailReadFailure("detail_navigation", _navigation_failure_reason(error))
        return failure

    @staticmethod
    def _account_require_page_path(
        page: _CurrentPage, expected_path: str
    ) -> DetailReadFailure | None:
        failure: DetailReadFailure | None = None
        try:
            _require_page_path(page, expected_path, "unexpected detail page")
        except Exception:  # noqa: BLE001 - browser route data is untrusted.
            failure = DetailReadFailure("detail_navigation", "detail_route_mismatch")
        return failure

    def _remember_note_metadata(
        self,
        items: object,
        *,
        exact_dom_card_cover: bool = False,
        share_source: str | None = None,
    ) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            note_id = _note_id(item)
            metadata = _note_metadata(
                item,
                exact_dom_card_cover=exact_dom_card_cover,
                share_source=share_source,
            )
            if note_id is None or not metadata:
                continue
            self._cached_note_metadata.setdefault(note_id, {}).update(metadata)

    def _remember_dom_fallback_note_profiles(
        self, user_id: str, candidates: list[_UserPostCandidate]
    ) -> None:
        for candidate in candidates:
            if candidate["xsec_token"] == "":
                self._dom_fallback_note_profiles[candidate["note_id"]] = user_id

    def _forget_dom_fallback_note_profiles(self, items: object) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                note_id = _note_id(item)
                if note_id is not None:
                    self._dom_fallback_note_profiles.pop(note_id, None)

    def _require_delegate(self) -> _PinnedReadOnlyDelegate:
        if self._delegate is None:
            raise RuntimeError("persistent client is not open")
        return self._delegate

    def _current_page(self) -> _CurrentPage | None:
        page = self._require_delegate()._page
        required_methods = (
            "on",
            "remove_listener",
            "goto",
            "wait_for_timeout",
            "wait_for_url",
            "wait_for_function",
            "evaluate",
            "locator",
        )
        if (
            not hasattr(page, "url")
            or not all(callable(getattr(page, name, None)) for name in required_methods)
            or not callable(getattr(getattr(page, "mouse", None), "click", None))
        ):
            return None
        return cast(_CurrentPage, page)


def _capture_search_notes(page: _CurrentPage, keyword: str) -> list[object]:
    captured = _capture_response(
        page,
        _SEARCH_ENDPOINTS,
        _search_items,
        f"{_BASE_URL}/search_result?keyword={quote(keyword, safe='')}&source=web_search_result_notes",
        allowed_page_query_additions=_SEARCH_PAGE_QUERY_ADDITIONS,
    )
    if captured is None:
        raise RuntimeError("search response unavailable")
    return captured


def _read_note_detail(
    page: _CurrentPage,
    note_id: str,
    xsec_token: str,
    metadata: _NoteMetadata | None = None,
) -> dict[str, object]:
    encoded_note_id = quote(note_id, safe="")
    query = "?xsec_source=pc_search"
    if xsec_token:
        query += f"&xsec_token={quote(xsec_token, safe='')}"
    _navigate(page, f"{_BASE_URL}/explore/{encoded_note_id}{query}")
    _require_page_path(page, f"/explore/{encoded_note_id}", "unexpected detail page")
    return _project_note_detail(note_id, page.evaluate(_NOTE_DETAIL_SCRIPT, note_id), metadata)


def _read_note_detail_from_profile_card(
    page: _CurrentPage,
    note_id: str,
    profile_id: str,
    metadata: _NoteMetadata | None = None,
) -> dict[str, object]:
    encoded_note_id = quote(note_id, safe="")
    encoded_profile_id = quote(profile_id, safe="")
    _navigate(page, f"{_BASE_URL}/user/profile/{encoded_profile_id}")
    _require_page_path(page, f"/user/profile/{encoded_profile_id}", "unexpected profile page")
    _click_visible_note_card(page, encoded_note_id)
    page.wait_for_timeout(_SETTLE_MILLISECONDS)
    _require_page_path(page, f"/explore/{encoded_note_id}", "unexpected detail page")
    return _project_note_detail(note_id, page.evaluate(_NOTE_DETAIL_SCRIPT, note_id), metadata)


def _read_user_profile(
    page: _CurrentPage, user_id: str
) -> tuple[dict[str, object], list[object], bool]:
    captured = _capture_response(
        page,
        (_USER_POSTS_ENDPOINT,),
        _user_posts,
        f"{_BASE_URL}/user/profile/{quote(user_id, safe='')}",
    )
    _require_page_path(page, f"/user/profile/{quote(user_id, safe='')}", "unexpected profile page")
    profile = _project_user_profile(user_id, page.evaluate(_USER_PROFILE_SCRIPT, user_id))
    if captured:
        return profile, captured, False
    return profile, _profile_note_candidates(page, user_id), True


def _capture_user_posts(page: _CurrentPage, user_id: str) -> tuple[list[object], bool]:
    captured = _capture_response(
        page,
        (_USER_POSTS_ENDPOINT,),
        _user_posts,
        f"{_BASE_URL}/user/profile/{quote(user_id, safe='')}",
    )
    _require_page_path(page, f"/user/profile/{quote(user_id, safe='')}", "unexpected profile page")
    if captured:
        return captured, False
    return _profile_note_candidates(page, user_id), True


def _capture_response(
    page: _CurrentPage,
    endpoints: tuple[str, ...],
    project: Callable[[object], list[object] | None],
    url: str,
    *,
    allowed_page_query_additions: tuple[tuple[str, str], ...] = (),
) -> list[object] | None:
    captured: list[object] | None = None
    navigation_started = False

    def receive(response: _CurrentResponse) -> None:
        nonlocal captured
        try:
            if (
                not navigation_started
                or not _matches_exact_page_target(page.url, url, allowed_page_query_additions)
                or response.status != 200
                or not _matches_exact_api_response(response.url, endpoints)
            ):
                return
            projected = project(response.json())
        except Exception:  # noqa: BLE001 - browser responses are untrusted.
            return
        if projected is not None:
            captured = projected

    page.on("response", receive)
    try:
        page.goto(url, wait_until="commit", timeout=20_000)
        navigation_started = True
        page.wait_for_timeout(_SETTLE_MILLISECONDS)
    finally:
        page.remove_listener("response", receive)
    return captured


def _navigate(page: _CurrentPage, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    page.wait_for_timeout(_SETTLE_MILLISECONDS)


def _click_visible_note_card(
    page: _CurrentPage,
    encoded_note_id: str,
    on_detail_navigation: Callable[[], None] | None = None,
) -> None:
    expected_path = f"/explore/{encoded_note_id}"
    target = page.wait_for_function(
        _EXACT_VISIBLE_NOTE_CARD_HANDLE_SCRIPT,
        arg=expected_path,
        timeout=_CARD_TARGET_TIMEOUT_MILLISECONDS,
    ).as_element()
    if target is None:
        raise _CardResolutionFailure("card_not_found", "note card is unavailable")
    outcome = target.evaluate(_SAFE_CARD_CLICK_POINT_SCRIPT, expected_path)
    state, click_point = _card_click_outcome(outcome)
    if state == "outside_viewport":
        scroll_state = _card_scroll_outcome(
            target.evaluate(_SAFE_CARD_SCROLL_INTO_VIEW_SCRIPT, expected_path)
        )
        if scroll_state != "scrolled":
            raise _CardResolutionFailure(
                "card_offscreen", "note card could not be safely scrolled into view"
            )
        page.wait_for_timeout(_CARD_SCROLL_SETTLE_MILLISECONDS)
        outcome = target.evaluate(_SAFE_CARD_CLICK_POINT_SCRIPT, expected_path)
        state, click_point = _card_click_outcome(outcome)
    if state == "outside_viewport":
        raise _CardResolutionFailure("card_offscreen", "note card is outside the current viewport")
    if state != "safe" or click_point is None:
        raise _CardResolutionFailure("card_unsafe", "note card is unsafe to click")
    page.mouse.click(*click_point)
    if on_detail_navigation is not None:
        on_detail_navigation()
    page.wait_for_url(
        lambda url: _matches_exact_detail_url(url, expected_path),
        wait_until="domcontentloaded",
        timeout=_CARD_TARGET_TIMEOUT_MILLISECONDS,
    )


def _card_click_outcome(raw: object) -> tuple[str, tuple[float, float] | None]:
    if not isinstance(raw, dict):
        return "unsafe", None
    state = raw.get("state")
    if state != "safe":
        return state if isinstance(state, str) else "unsafe", None
    x = _finite_coordinate(raw.get("x"))
    y = _finite_coordinate(raw.get("y"))
    return ("safe", (x, y)) if x is not None and y is not None else ("unsafe", None)


def _card_scroll_outcome(raw: object) -> str:
    if not isinstance(raw, dict):
        return "scroll_failed"
    state = raw.get("state")
    return state if isinstance(state, str) else "scroll_failed"


def _navigation_failure_reason(error: Exception) -> CandidateAttemptReason:
    if isinstance(error, TimeoutError) or type(error).__name__ == "TimeoutError":
        return "detail_timeout"
    return "detail_route_mismatch"


def _finite_coordinate(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _matches_exact_detail_url(url: str, expected_path: str) -> bool:
    parsed = urlsplit(url)
    return _is_exact_xhs_page(parsed) and parsed.path == expected_path


def _matches_exact_api_response(url: str, endpoints: tuple[str, ...]) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    expected_host = _API_RESPONSE_HOSTS.get(parsed.path)
    return (
        parsed.path in endpoints
        and expected_host is not None
        and _is_exact_https_host(parsed, expected_host)
    )


def _matches_exact_page_target(
    current_url: str,
    requested_url: str,
    allowed_query_additions: tuple[tuple[str, str], ...] = (),
) -> bool:
    try:
        current = urlsplit(current_url)
        requested = urlsplit(requested_url)
    except ValueError:
        return False
    current_query = Counter(parse_qsl(current.query, keep_blank_values=True))
    requested_query = Counter(parse_qsl(requested.query, keep_blank_values=True))
    return (
        _is_exact_xhs_page(current)
        and _is_exact_xhs_page(requested)
        and current.path == requested.path
        and current.fragment == requested.fragment
        and (
            current_query == requested_query
            or current_query == requested_query + Counter(allowed_query_additions)
        )
    )


def _require_page_path(page: _CurrentPage, expected_path: str, message: str) -> None:
    parsed = urlsplit(page.url)
    if not _is_exact_xhs_page(parsed) or parsed.path != expected_path:
        raise ValueError(message)


def _is_exact_xhs_page(parsed: SplitResult) -> bool:
    return _is_exact_https_host(parsed, "www.xiaohongshu.com")


def _is_exact_https_host(parsed: SplitResult, expected_host: str) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == expected_host
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
    )


def _search_items(payload: object) -> list[object] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("model_type", item.get("modelType")) == "note"
        and isinstance(item.get("note_card", item.get("noteCard")), dict)
    ]


def _user_posts(payload: object) -> list[object] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    notes = data.get("notes") if isinstance(data, dict) else None
    return list(notes) if isinstance(notes, list) else None


def _profile_note_candidates(page: _CurrentPage, user_id: str) -> list[object]:
    raw = page.evaluate(_PROFILE_NOTE_CANDIDATES_SCRIPT, user_id)
    bound: list[object] = (
        [item for item in raw if isinstance(item, dict) and item.get("profile_id") == user_id]
        if isinstance(raw, list)
        else []
    )
    safe_ids = {
        candidate["note_id"] for candidate in _user_post_candidates(bound, require_safe_ids=True)
    }
    return [item for item in bound if isinstance(item, dict) and _note_id(item) in safe_ids]


def _user_post_candidates(
    items: object, *, require_safe_ids: bool = False
) -> list[_UserPostCandidate]:
    if not isinstance(items, list):
        return []
    candidates: list[_UserPostCandidate] = []
    seen_note_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        note_id = _note_id(item)
        if note_id is None or (require_safe_ids and _SAFE_NOTE_ID.fullmatch(note_id) is None):
            continue
        if note_id in seen_note_ids:
            continue
        seen_note_ids.add(note_id)
        candidates.append(
            {
                "note_id": note_id,
                "xsec_token": _string(item.get("xsec_token", item.get("xsecToken"))) or "",
            }
        )
    return candidates


def _project_note_detail(
    note_id: str, raw: object, metadata: _NoteMetadata | None = None
) -> dict[str, object]:
    values = raw if isinstance(raw, dict) else {}
    author_id = _profile_id(values.get("author_profile_url"))
    cached_time = metadata.get("time") if metadata is not None else None
    shared_count = metadata.get("shared_count") if metadata is not None else None
    share_source = metadata.get("share_source") if metadata is not None else None
    cached_title = metadata.get("card_title") if metadata is not None else None
    exact_card_cover = metadata.get("card_cover_url") if metadata is not None else None
    fallback_cover = metadata.get("fallback_cover_url") if metadata is not None else None
    media_scope_valid = values.get("media_scope_valid") is True
    media_allowed = media_scope_valid
    detail_images = _detail_images(values.get("images")) if media_allowed else []
    media_discovered_count = (
        _media_discovered_count(values.get("media_discovered_count"), values.get("images"))
        if media_allowed
        else 0
    )
    note: dict[str, object] = {
        "id": note_id,
        "title": _string(values.get("title")) or _string(cached_title),
        "desc": _string(values.get("body")),
        "user": {
            "user_id": author_id,
            "nickname": _string(values.get("author_name")),
        },
        "time": cached_time,
        "type": "video" if values.get("has_video") is True else "normal",
        "interact_info": _interaction_values(values, shared_count),
        "tag_list": [{"name": tag} for tag in _string_list(values.get("tags"))],
        "image_list": detail_images,
    }
    if (time_evidence := _time_evidence(values.get("visible_time_text"))) is not None:
        note["time_evidence"] = time_evidence
    if metric_provenance := _metric_provenance(values, shared_count, share_source):
        note["metric_provenance"] = metric_provenance
    if values.get("has_video") is True and media_allowed and author_id is not None:
        video = _detail_video(values.get("video"))
        if video is not None:
            note["video"] = video
    cover_url = _string(exact_card_cover)
    if cover_url is None and not detail_images:
        cover_url = _string(fallback_cover)
    if cover_url is not None:
        note["cover"] = {"url": cover_url}
    return {
        "note": note,
        "media_scope_valid": media_scope_valid,
        "media_discovered_count": media_discovered_count,
    }


def _time_evidence(value: object) -> dict[str, str] | None:
    text = _string(value)
    if text is None:
        return None
    kind = (
        "edited"
        if re.match(r"^(编辑|更新)于", text)
        else "published"
        if text.startswith("发布于")
        else "unknown"
    )
    try:
        return TimeEvidence(kind=cast(TimeEvidenceKind, kind), raw_text=text).model_dump()
    except ValueError:
        return None


def _metric_provenance(
    values: dict[str, object], shared_count: int | str | None, share_source: int | str | None
) -> dict[str, str]:
    result = {
        destination: "detail_visible_count"
        for source, destination in (
            ("likes", "likes"),
            ("collects", "collects"),
            ("comments", "comments"),
        )
        if _exposed_metric_count(values.get(source)) is not None
    }
    if _exposed_metric_count(shared_count) is not None and isinstance(share_source, str):
        result["shares"] = share_source
    return result


def _project_user_profile(user_id: str, raw: object) -> dict[str, object]:
    values = raw if isinstance(raw, dict) else {}
    basic_info: dict[str, object] = {"user_id": user_id}
    if values.get("profile_id") != user_id:
        return {"userPageData": {"basicInfo": basic_info, "interactions": []}}
    name = _string(values.get("name"))
    name_status = _profile_read_status(values, "name", name)
    bio_value = values.get("bio")
    bio = bio_value if isinstance(bio_value, str) else None
    bio_status = _profile_read_status(values, "bio", bio)
    if bio == "还没有简介":
        bio = ""
        bio_status = "exposed_empty"
    avatar = _string(values.get("avatar"))
    avatar_status = _profile_read_status(values, "avatar", avatar)
    red_id = _string(values.get("red_id"))
    if name_status == "exposed" and name is not None:
        basic_info["nickname"] = name
    if bio_status in {"exposed", "exposed_empty"} and bio is not None:
        basic_info["desc"] = bio
    if red_id is not None:
        basic_info["red_id"] = red_id
    if avatar_status == "exposed" and avatar is not None:
        basic_info["image"] = avatar
    interactions = []
    interactions_status = _profile_read_status(
        values, "interactions", values.get("interactions")
    )
    if interactions_status == "exposed":
        raw_interactions = values.get("interactions")
        if isinstance(raw_interactions, list):
            for item in raw_interactions:
                if not isinstance(item, dict):
                    continue
                label = _string(item.get("name"))
                count = _string(item.get("count"))
                if label is not None and count is not None:
                    interactions.append({"name": label, "count": count})
    if interactions_status == "unavailable":
        note_count_status = "unavailable"
        follower_count_status = "unavailable"
        platform_metrics_status = "unavailable"
    else:
        labels = {
            re.sub(r"[\s_-]+", "_", item["name"].casefold())
            for item in interactions
            if isinstance(item.get("name"), str)
        }
        note_count_status = "exposed" if labels & {"note", "notes", "笔记"} else "not_exposed"
        follower_count_status = (
            "exposed" if labels & {"fan", "fans", "follower", "followers", "粉丝"} else "not_exposed"
        )
        platform_metrics_status = "exposed" if interactions else "not_exposed"
    return {
        "userPageData": {
            "basicInfo": basic_info,
            "interactions": interactions,
            "fieldStatuses": {
                "name": name_status,
                "bio": bio_status,
                "note_count": note_count_status,
                "follower_count": follower_count_status,
                "avatar": avatar_status,
                "platform_metrics": platform_metrics_status,
            },
        }
    }


def _profile_read_status(values: dict[str, object], field: str, value: object) -> str:
    status_key = "interactions_status" if field == "interactions" else f"{field}_status"
    status = values.get(status_key)
    if status in {"exposed", "exposed_empty", "not_exposed", "unavailable"}:
        return status
    if status is not None:
        return "unavailable"
    return "exposed" if value else "not_exposed"


def _interaction_values(
    values: dict[str, object], shared_count: int | str | None
) -> dict[str, int | str]:
    result: dict[str, int | str] = {}
    for source, destination in (
        ("likes", "liked_count"),
        ("collects", "collected_count"),
        ("comments", "comment_count"),
    ):
        value = _string(values.get(source))
        if value is not None:
            result[destination] = value
    if shared_count is not None:
        result["shared_count"] = shared_count
    return result


def _exposed_metric_count(value: object) -> int | str | None:
    if type(value) is int:
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    return (
        value
        if _EXACT_METRIC_VALUE.fullmatch(raw) is not None
        or _ROUNDED_METRIC_VALUE.fullmatch(raw) is not None
        else None
    )


def _note_id(item: dict[str, object]) -> str | None:
    for key in ("id", "note_id", "noteId"):
        value = _string(item.get(key))
        if value is not None:
            return value
    return None


def _note_metadata(
    item: dict[str, object], *, exact_dom_card_cover: bool = False, share_source: str | None = None
) -> _NoteMetadata:
    card = item.get("note_card", item.get("noteCard"))
    values = card if isinstance(card, dict) else item
    interactions = values.get("interact_info", values.get("interactInfo"))
    result: _NoteMetadata = {}
    if isinstance(interactions, dict):
        shared_count = interactions.get("shared_count", interactions.get("sharedCount"))
        if (exposed_shared_count := _exposed_metric_count(shared_count)) is not None:
            result["shared_count"] = exposed_shared_count
            if share_source is not None:
                result["share_source"] = share_source
    time = item.get("time")
    if type(time) is int or isinstance(time, str):
        result["time"] = time
    card_title = _string(
        item.get("card_title", values.get("display_title", values.get("displayTitle")))
    )
    if card_title is not None:
        result["card_title"] = card_title
    exact_card_cover = item.get("card_cover_url") if exact_dom_card_cover else None
    if (exact_card_cover_url := _string(exact_card_cover)) is not None:
        result["card_cover_url"] = exact_card_cover_url
    else:
        fallback_cover = values.get("cover")
        if fallback_cover is None and not exact_dom_card_cover:
            fallback_cover = item.get("card_cover_url", values.get("card_cover_url"))
        if isinstance(fallback_cover, dict):
            fallback_cover = fallback_cover.get(
                "url", fallback_cover.get("urlDefault", fallback_cover.get("url_default"))
            )
        if (fallback_cover_url := _string(fallback_cover)) is not None:
            result["fallback_cover_url"] = fallback_cover_url
    return result


def _profile_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    parts = parsed.path.split("/")
    if (
        not _is_exact_xhs_page(parsed)
        or len(parts) != 4
        or parts[:3] != ["", "user", "profile"]
        or _SAFE_NOTE_ID.fullmatch(parts[3]) is None
    ):
        return None
    return parts[3]


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_list(value: object) -> list[str]:
    return (
        [item for item in value if isinstance(item, str) and item]
        if isinstance(value, list)
        else []
    )


def _detail_images(value: object) -> list[dict[str, int | str | None]]:
    if not isinstance(value, list):
        return []
    images: list[dict[str, int | str | None]] = []
    for index, item in enumerate(value[:MAX_DISCOVERED_IMAGE_SLOTS], start=1):
        if isinstance(item, dict):
            raw_position = item.get("position")
            position = raw_position if type(raw_position) is int and raw_position > 0 else index
            url = _string(item.get("url"))
        else:
            position = index
            url = _string(item)
        images.append({"position": position, "url": url})
    return images


def _media_discovered_count(value: object, images: object) -> int:
    if type(value) is int and value >= 0:
        return value
    return len(images) if isinstance(images, list) else 0


def _detail_video(value: object) -> dict[str, int | str | None] | None:
    if not isinstance(value, dict):
        return None
    duration = value.get("duration_ms")
    return {
        "poster": _string(value.get("poster")),
        "url": _string(value.get("url")),
        "duration_ms": duration if type(duration) is int and duration >= 0 else None,
    }


_DETAIL_ROOT_LEAF_CANONICALIZER = """
(roots) => roots.filter((candidate) =>
  !roots.some((other) => candidate !== other && candidate.contains(other)))
""".strip()


_NOTE_DETAIL_SCRIPT = """
(expectedNoteId) => {
  const visible = (element) => {
    const bounds = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return bounds.width > 0 && bounds.height > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden';
  };
  const route = /^\\/explore\\/([A-Za-z0-9_-]+)$/.exec(location.pathname);
  const roots = Array.from(document.querySelectorAll('#noteContainer, .note-detail-mask'))
    .filter(visible).filter((root) => root.querySelector('.author-wrapper') &&
      root.querySelector('#detail-desc') &&
      root.querySelector('.note-slider, .media-container, xg-player'));
  const _DETAIL_ROOT_LEAF_CANONICALIZER = __DETAIL_ROOT_LEAF_CANONICALIZER__;
  const leafRoots = _DETAIL_ROOT_LEAF_CANONICALIZER(roots);
  if (!route || route[1] !== expectedNoteId || leafRoots.length !== 1) {
    return {media_scope_valid: false, images: [], media_discovered_count: 0, video: null,
      has_video: false};
  }
  const detailRoot = leafRoots[0];
  const text = (selector) => detailRoot.querySelector(selector)?.innerText?.trim() || '';
  const profile = detailRoot.querySelector('.author-wrapper a[href*="/user/profile/"]')?.href || '';
  const profileId = (value) => {
    if (typeof value !== 'string') return '';
    try {
      const parsed = new URL(value);
      const match = /^\\/user\\/profile\\/([A-Za-z0-9_-]+)$/.exec(parsed.pathname);
      return parsed.protocol === 'https:' && parsed.hostname === 'www.xiaohongshu.com' &&
        !parsed.username && !parsed.password && (!parsed.port || parsed.port === '443') && match
        ? match[1] : '';
    } catch (_) {
      return '';
    }
  };
  const authorId = profileId(profile);
  const tags = Array.from(detailRoot.querySelectorAll('#detail-desc a[href*="search"]'))
    .map((element) => element.innerText.trim()).filter(Boolean);
  const imageNodes = Array.from(detailRoot.querySelectorAll(
    '.note-slider .swiper-slide:not(.swiper-slide-duplicate) .note-slider-img img, ' +
    '.note-slider .swiper-slide:not(.swiper-slide-duplicate) img.note-slider-img'));
  const mediaDiscoveredCount = imageNodes.length;
  const images = imageNodes.slice(0, 100)
    .map((image, index) => ({position: index + 1, url: image.currentSrc || image.src || ''}));
  const visibleXgPlayers = Array.from(detailRoot.querySelectorAll('xg-player')).filter(visible);
  const visibleMediaContainers = Array.from(detailRoot.querySelectorAll('.media-container'))
    .filter(visible).filter((container) => !container.querySelector('xg-player'));
  const playerRoots = [...visibleXgPlayers, ...visibleMediaContainers];
  const detailPlayer = imageNodes.length === 0 && playerRoots.length === 1 ? playerRoots[0] : null;
  const playerVideos = Array.from(detailRoot.querySelectorAll(
    '.media-container video, xg-player video')).filter(visible);
  const videoElement = playerVideos.length === 1 ? playerVideos[0] : null;
  const directHttpsVideoUrl = (value) => {
    if (typeof value !== 'string') return '';
    try {
      const parsed = new URL(value);
      return parsed.protocol === 'https:' && /\\.(mp4|webm)$/i.test(parsed.pathname) ? parsed.href : '';
    } catch (_) {
      return '';
    }
  };
  const directVideoUrl = authorId && directHttpsVideoUrl(videoElement?.currentSrc || videoElement?.src ||
    videoElement?.querySelector('source')?.src || '');
  const structuredFallbackUrl = () => {
    if (directVideoUrl || !detailPlayer) return '';
    const noteDetailMap = window.__INITIAL_STATE__?.note?.noteDetailMap;
    const structuredNote = noteDetailMap?.[expectedNoteId]?.note;
    const noteId = structuredNote?.noteId ?? structuredNote?.note_id;
    const structuredUser = structuredNote?.user ?? structuredNote?.userInfo;
    const structuredAuthorId = structuredUser?.userId ?? structuredUser?.user_id ??
      structuredNote?.userId ?? structuredNote?.user_id;
    if (!authorId || !structuredNote || noteId !== expectedNoteId || structuredAuthorId !== authorId) {
      return '';
    }
    const directUrls = [];
    const collectUrls = (value) => {
      if (typeof value === 'string') {
        const direct = directHttpsVideoUrl(value);
        if (direct) directUrls.push(direct);
      } else if (Array.isArray(value)) {
        value.forEach(collectUrls);
      } else if (value && typeof value === 'object') {
        Object.values(value).forEach(collectUrls);
      }
    };
    collectUrls(structuredNote.video?.media?.stream);
    collectUrls(structuredNote.video?.stream);
    collectUrls(structuredNote.video?.streamVariants);
    const uniqueDirectUrls = new Set(directUrls);
    return uniqueDirectUrls.size === 1 ? Array.from(uniqueDirectUrls)[0] : '';
  };
  const duration = Number(videoElement?.duration);
  const video = detailPlayer ? {
    poster: videoElement?.poster || '', url: directVideoUrl || structuredFallbackUrl(),
    duration_ms: Number.isFinite(duration) && duration > 0 ? Math.round(duration * 1000) : null,
  } : null;
  return {
    media_scope_valid: true, media_discovered_count: mediaDiscoveredCount, images,
    title: text('#detail-title'), body: text('#detail-desc'), author_name: text('.author-wrapper .name'),
    author_profile_url: profile, visible_time_text: text('.date'), likes: text('.engage-bar .like-wrapper .count'),
    collects: text('.engage-bar .collect-wrapper .count'), comments: text('.engage-bar .chat-wrapper .count'),
    tags, has_video: detailPlayer !== null, video,
  };
}
""".replace("__DETAIL_ROOT_LEAF_CANONICALIZER__", _DETAIL_ROOT_LEAF_CANONICALIZER)


_USER_PROFILE_SCRIPT = """
(expectedProfileId) => {
  const text = (element) => element?.innerText?.trim() || '';
  const isVisible = (element) => {
    const bounds = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return bounds.width > 0 && bounds.height > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden';
  };
  const profileMatch = /^\\/user\\/profile\\/([A-Za-z0-9_-]+)$/.exec(location.pathname);
  if (location.protocol !== 'https:' || location.hostname !== 'www.xiaohongshu.com' ||
      location.port !== '' || profileMatch === null || profileMatch[1] !== expectedProfileId) {
    return {profile_id: ''};
  }
  const uniqueVisible = (selector) => {
    const matches = Array.from(document.querySelectorAll(selector)).filter(isVisible);
    return matches.length === 1 ? matches[0] : null;
  };
  const nameAnchor = uniqueVisible('.user-name');
  const redIdAnchor = uniqueVisible('.user-redId');
  const bioAnchor = uniqueVisible('.user-desc');
  const interactionAnchor = uniqueVisible('.user-interactions');
  const headerAnchors = [nameAnchor, redIdAnchor, bioAnchor, interactionAnchor].filter(Boolean);
  if (headerAnchors.length === 0) return {profile_id: ''};
  let root = headerAnchors[0].parentElement;
  while (root && root !== document.body && !headerAnchors.every((item) => root.contains(item))) {
    root = root.parentElement;
  }
  if (!root || root === document.body) {
    return {profile_id: ''};
  }
  const uniqueWithinRoot = (selector, anchor) => {
    const matches = Array.from(root.querySelectorAll(selector)).filter(isVisible);
    if (matches.length === 0) return {state: 'not_exposed', element: null};
    if (matches.length !== 1 || (anchor && matches[0] !== anchor)) {
      return {state: 'unavailable', element: null};
    }
    return {state: 'exposed', element: matches[0]};
  };
  const readVisibleText = (selector, anchor) => {
    const found = uniqueWithinRoot(selector, anchor);
    if (!found.element) return {state: found.state, value: ''};
    try {
      const value = found.element.innerText?.trim() || '';
      return {state: value ? 'exposed' : 'unavailable', value};
    } catch (_) {
      return {state: 'unavailable', value: ''};
    }
  };
  const nameRead = readVisibleText('.user-name', nameAnchor);
  const bioRead = readVisibleText('.user-desc', bioAnchor);
  const redIdRead = readVisibleText('.user-redId', redIdAnchor);
  const bioStatus = bioRead.value === '还没有简介' ? 'exposed_empty' : bioRead.state;
  const interactionBound = uniqueWithinRoot(
    '.user-interactions', interactionAnchor
  );
  const interactionRoot = interactionBound.element;
  let interactions = [];
  let interactionsStatus = interactionBound.state === 'exposed' ? 'unavailable' : interactionBound.state;
  if (interactionRoot && isVisible(interactionRoot)) {
    try {
      interactions = Array.from(interactionRoot.children).map((entry) => ({
        name: text(entry.querySelector('.shows')), count: text(entry.querySelector('.count')),
      })).filter((entry) => entry.name && entry.count);
      interactionsStatus = interactions.length ? 'exposed' : 'not_exposed';
    } catch (_) {
      interactions = [];
      interactionsStatus = 'unavailable';
    }
  }
  let avatarSource = '';
  let avatarStatus = 'not_exposed';
  let avatarRoot = root;
  const headerAnchorsRemainUnique = (candidateRoot) => [
    ['.user-name', nameAnchor],
    ['.user-redId', redIdAnchor],
    ['.user-desc', bioAnchor],
    ['.user-interactions', interactionAnchor],
  ].every(([selector, anchor]) => {
    const matches = Array.from(candidateRoot.querySelectorAll(selector)).filter(isVisible);
    return anchor ? matches.length === 1 && matches[0] === anchor : matches.length === 0;
  });
  for (let ancestorHops = 0;
       avatarRoot && avatarRoot !== document.body && ancestorHops <= 3;
       ancestorHops += 1, avatarRoot = avatarRoot.parentElement) {
    const avatarCandidates = Array.from(
      avatarRoot.querySelectorAll('img.user-image')
    ).filter(isVisible);
    if (avatarCandidates.length === 0) continue;
    if (!headerAnchorsRemainUnique(avatarRoot) || avatarCandidates.length !== 1) {
      avatarStatus = 'unavailable';
      break;
    }
    try {
      avatarSource = avatarCandidates[0].currentSrc || avatarCandidates[0].src || '';
      avatarStatus = avatarSource ? 'exposed' : 'unavailable';
    } catch (_) {
      avatarStatus = 'unavailable';
    }
    break;
  }
  return {
    profile_id: profileMatch[1], name: nameRead.value, name_status: nameRead.state,
    bio: bioRead.value === '还没有简介' ? '' : bioRead.value, bio_status: bioStatus,
    red_id: redIdRead.value,
    avatar: avatarSource, avatar_status: avatarStatus, interactions,
    interactions_status: interactionsStatus,
  };
}
"""


_PROFILE_NOTE_CANDIDATES_SCRIPT = """
(expectedProfileId) => {
  const expectedPath = `/user/profile/${expectedProfileId}`;
  if (location.protocol !== 'https:' || location.hostname !== 'www.xiaohongshu.com' ||
      location.port !== '' || location.pathname !== expectedPath) {
    return [];
  }
  const seen = new Set();
  const candidates = [];
  for (const card of document.querySelectorAll('section.note-item:not(.query-note-item)')) {
    for (const link of card.querySelectorAll('a[href]')) {
      let parsed;
      try {
        parsed = new URL(link.href);
      } catch {
        continue;
      }
      const match = /^\\/explore\\/([A-Za-z0-9_-]+)$/.exec(parsed.pathname);
      if (
        parsed.protocol !== 'https:' ||
        parsed.hostname !== 'www.xiaohongshu.com' ||
        parsed.port !== '' ||
        match === null ||
        seen.has(match[1])
      ) {
        continue;
      }
      const ownerLinks = Array.from(card.querySelectorAll('a[href*="/user/profile/"]'));
      const ownerMatches = ownerLinks.some((ownerLink) => {
        try {
          const owner = new URL(ownerLink.href);
          const ownerMatch = /^\\/user\\/profile\\/([A-Za-z0-9_-]+)(?:\\/([A-Za-z0-9_-]+))?$/.exec(owner.pathname);
          return owner.protocol === 'https:' && owner.hostname === 'www.xiaohongshu.com' &&
            owner.port === '' && ownerMatch !== null && ownerMatch[1] === expectedProfileId &&
            (ownerMatch[2] === undefined || ownerMatch[2] === match[1]);
        } catch {
          return false;
        }
      });
      if (!ownerMatches) {
        continue;
      }
      const coverImage = link.querySelector('img') || card.querySelector('a.cover img, img.cover');
      const title = card.querySelector('.title span, .title');
      seen.add(match[1]);
      candidates.push({
        profile_id: expectedProfileId,
        note_id: match[1],
        xsec_token: parsed.searchParams.get('xsec_token') || '',
        card_title: title?.innerText?.trim() || '',
        card_cover_url: coverImage?.currentSrc || coverImage?.src || '',
      });
      break;
    }
  }
  return candidates;
}
"""


_EXACT_VISIBLE_NOTE_CARD_HANDLE_SCRIPT = r"""
(expectedPath) => {
  const matchesExpectedExplorePath = (value) => {
    try {
      const parsed = new URL(value, window.location.href);
      return parsed.protocol === 'https:'
        && parsed.hostname === 'www.xiaohongshu.com'
        && parsed.port === ''
        && parsed.pathname === expectedPath;
    } catch {
      return false;
    }
  };
  const isCssVisible = (candidate) => {
    const style = window.getComputedStyle(candidate);
    return candidate.getClientRects().length > 0
      && style.display !== 'none' && style.visibility !== 'hidden';
  };
  for (const card of document.querySelectorAll('section.note-item:not(.query-note-item)')) {
    if (!isCssVisible(card)) continue;
    if (Array.from(card.querySelectorAll('a[href]')).some((link) => (
      matchesExpectedExplorePath(link.getAttribute('href') || '')
    ))) return card;
  }
  return null;
}
"""


_SAFE_CARD_SCROLL_INTO_VIEW_SCRIPT = r"""
(element, expectedPath) => {
  const matchesExpectedExplorePath = (value) => {
    try {
      const parsed = new URL(value, window.location.href);
      return parsed.protocol === 'https:'
        && parsed.hostname === 'www.xiaohongshu.com'
        && parsed.port === ''
        && parsed.pathname === expectedPath;
    } catch {
      return false;
    }
  };
  if (!Array.from(element.querySelectorAll('a[href]')).some((link) => (
    matchesExpectedExplorePath(link.href)
  ))) {
    return {state: 'not_matching'};
  }
  try {
    element.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'instant'});
  } catch {
    return {state: 'scroll_failed'};
  }
  return {state: 'scrolled'};
}
"""


_SAFE_CARD_CLICK_POINT_SCRIPT = r"""
(element, expectedPath) => {
  const matchesExpectedExplorePath = (value) => {
    try {
      const parsed = new URL(value, window.location.href);
      return parsed.protocol === 'https:'
        && parsed.hostname === 'www.xiaohongshu.com'
        && parsed.port === ''
        && parsed.pathname === expectedPath;
    } catch {
      return false;
    }
  };
  if (!Array.from(element.querySelectorAll('a[href]')).some((link) => (
    matchesExpectedExplorePath(link.href)
  ))) {
    return {state: 'not_matching'};
  }
  const rect = element.getBoundingClientRect();
  const isFullyInViewport = (candidate) => candidate.width > 0 && candidate.height > 0
    && candidate.top >= 0 && candidate.left >= 0
    && candidate.bottom <= window.innerHeight && candidate.right <= window.innerWidth;
  if (!isFullyInViewport(rect)) return {state: 'outside_viewport'};
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (!(hit instanceof Element) || !element.contains(hit)) return {state: 'unsafe'};
  const mutationPattern = /(?:like|collect|comment|follow|share|publish|delete|report)/i;
  const interactive = hit.closest(
    'a[href], button, [role="button"], form, input, select, textarea, option, [contenteditable="true"]'
  );
  if (interactive && !interactive.matches('a[href]')) return {state: 'unsafe'};
  const hitAnchor = hit.closest('a[href]');
  if (hitAnchor) {
    const isReadOnlyProfile = (rawHref) => {
      if (typeof rawHref !== 'string' || rawHref.length === 0 || rawHref !== rawHref.trim()) {
        return false;
      }
      if (rawHref.startsWith('#') || rawHref.startsWith('?') || rawHref.startsWith('//')) {
        return false;
      }
      let rawRoute = rawHref;
      if (!rawHref.startsWith('/')) {
        rawRoute = rawHref.replace(
          /^https:\/\/www\.xiaohongshu\.com(?::443)?(?=\/|$)/i, ''
        );
        if (rawRoute === rawHref) return false;
      }
      const rawPath = rawRoute.split(/[?#]/, 1)[0];
      const profileMatch = /^\/user\/profile\/([A-Za-z0-9_-]+)(?:\/([A-Za-z0-9_-]+))?$/.exec(rawPath);
      const expectedNoteMatch = /^\/explore\/([A-Za-z0-9_-]+)$/.exec(expectedPath);
      if (profileMatch === null || expectedNoteMatch === null) return false;
      try {
        const parsedAnchor = new URL(rawHref, window.location.href);
        return parsedAnchor.protocol === 'https:'
          && parsedAnchor.hostname === 'www.xiaohongshu.com'
          && parsedAnchor.port === ''
          && parsedAnchor.pathname === rawPath
          && (profileMatch[2] === undefined || profileMatch[2] === expectedNoteMatch[1]);
      } catch {
        return false;
      }
    };
    const rawHitAnchorHref = hitAnchor.getAttribute('href');
    if (!matchesExpectedExplorePath(hitAnchor.href) && !isReadOnlyProfile(rawHitAnchorHref)) {
      return {state: 'unsafe'};
    }
  }
  for (let current = hit; current && element.contains(current); current = current.parentElement) {
    const labels = [
      current.getAttribute('aria-label') || '',
      current.getAttribute('data-action') || '',
      current.getAttribute('data-testid') || '',
      typeof current.className === 'string' ? current.className : '',
    ].join(' ');
    if (mutationPattern.test(labels)) return {state: 'unsafe'};
  }
  return {state: 'safe', x, y};
}
"""


def _default_delegate_factory() -> _PinnedReadOnlyDelegate:
    module = import_module("xhs_cli" + ".client")
    return cast(
        _PinnedReadOnlyDelegate,
        cast(Callable[[dict[str, str]], object], module.XhsClient)({}),
    )
