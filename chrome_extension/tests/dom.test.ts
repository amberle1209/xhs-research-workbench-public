import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { JSDOM } from "jsdom";
import { describe, expect, test, vi } from "vitest";

import { discoverCandidates } from "../src/dom/candidates.js";
import { detectBlockPage } from "../src/dom/challenge.js";
import { projectDetail } from "../src/dom/detail.js";
import { fetchBoundMedia } from "../src/dom/media.js";
import { detectPage } from "../src/dom/route.js";
import { scanSearchCards } from "../src/dom/search-scan.js";
import { scrollCandidates } from "../src/dom/scroll.js";
import { isVisible } from "../src/dom/visibility.js";

const fixture = (name: string): string =>
  readFileSync(resolve(import.meta.dirname, "fixtures", name), "utf8");

function page(name: string, url: string): JSDOM {
  const dom = new JSDOM(fixture(name), { url });
  Object.defineProperty(dom.window.HTMLElement.prototype, "getClientRects", {
    configurable: true,
    value: () => [{ width: 1, height: 1 }]
  });
  return dom;
}

describe("visibility", () => {
  test("rejects transparent and zero-sized nodes even when their text is in the DOM", () => {
    const dom = page("search-page.html", "https://www.xiaohongshu.com/search_result");
    const transparent = dom.window.document.createElement("div");
    transparent.style.opacity = "0";
    const zeroSized = dom.window.document.createElement("div");
    Object.defineProperty(zeroSized, "getClientRects", { value: () => [] });

    expect(isVisible(transparent)).toBe(false);
    expect(isVisible(zeroSized)).toBe(false);
  });
});

describe("route", () => {
  test("classifies exact supported routes and strips search queries from the retained identity", () => {
    const current = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123?xsec_token=discard");
    const search = page("search-page.html", "https://www.xiaohongshu.com/search_result?keyword=AI&xsec_token=discard");
    const account = page("account-page.html", "https://www.xiaohongshu.com/user/profile/author_123?tab=note");

    expect(detectPage(current.window.document, current.window.location)).toEqual({
      kind: "current", noteId: "note_123", canonicalUrl: "https://www.xiaohongshu.com/explore/note_123"
    });
    expect(detectPage(search.window.document, search.window.location)).toEqual({
      kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result"
    });
    expect(detectPage(account.window.document, account.window.location)).toEqual({
      kind: "account", profileId: "author_123", canonicalUrl: "https://www.xiaohongshu.com/user/profile/author_123"
    });
  });

  test("rejects unsupported, non-exact, or unsafe routes", () => {
    const unsupported = page("search-page.html", "https://www.xiaohongshu.com/explore/note_123/extra");
    const unsafe = page("search-page.html", "https://evil.test/explore/note_123");

    expect(detectPage(unsupported.window.document, unsupported.window.location)).toEqual({ kind: "unsupported" });
    expect(detectPage(unsafe.window.document, unsafe.window.location)).toEqual({ kind: "unsupported" });
  });
});

describe("candidate", () => {
  test("keeps only visible unique safe note anchors in DOM order without hrefs or queries", () => {
    const dom = page("search-page.html", "https://www.xiaohongshu.com/search_result?keyword=AI");
    const context = detectPage(dom.window.document, dom.window.location);
    const result = discoverCandidates(dom.window.document, context, 20);

    expect(result).toEqual([
      { sourcePosition: 1, noteId: "note_first", canonicalUrl: "https://www.xiaohongshu.com/explore/note_first" },
      { sourcePosition: 2, noteId: "note_second", canonicalUrl: "https://www.xiaohongshu.com/explore/note_second" }
    ]);
    expect(JSON.stringify(result)).not.toContain("href");
    expect(JSON.stringify(result)).not.toContain("?");
    expect(JSON.stringify(result)).not.toContain("xsec_token");
  });

  test("requires an exact visible account-card owner and applies the M cap", () => {
    const dom = page("account-page.html", "https://www.xiaohongshu.com/user/profile/author_123");
    const context = detectPage(dom.window.document, dom.window.location);

    expect(discoverCandidates(dom.window.document, context, 1)).toEqual([
      { sourcePosition: 1, noteId: "note_owner", canonicalUrl: "https://www.xiaohongshu.com/explore/note_owner" }
    ]);
  });

  test("does not treat an unrelated visible profile link as a card owner", () => {
    const dom = page("account-page.html", "https://www.xiaohongshu.com/user/profile/author_123");
    const unowned = dom.window.document.querySelectorAll("section.note-item")[1] as Element;
    unowned.insertAdjacentHTML("beforeend", '<a href="/user/profile/author_123">unrelated profile</a>');
    const context = detectPage(dom.window.document, dom.window.location);

    expect(discoverCandidates(dom.window.document, context, 20)).toEqual([
      { sourcePosition: 1, noteId: "note_owner", canonicalUrl: "https://www.xiaohongshu.com/explore/note_owner" }
    ]);
  });
});

describe("search scan", () => {
  test("projects twenty visible cards in DOM order and skips duplicate, sponsored, module, and loading entries", () => {
    const dom = page("search-scan.html", "https://www.xiaohongshu.com/search_result");
    const context = detectPage(dom.window.document, dom.window.location);

    const result = scanSearchCards(dom.window.document, context);

    expect(result.summaries.map((summary) => [summary.note_id, summary.source_position])).toEqual([
      ...Array.from({ length: 20 }, (_, index) => [`note_${String(index + 1).padStart(2, "0")}`, index + 1]),
      ["note_missing", 22],
      ["note_attribute_only", 24],
      ["note_hidden_sponsorship", 25]
    ]);
    expect(result.summaries[0]).toEqual({
      note_id: "note_01",
      canonical_url: "https://www.xiaohongshu.com/explore/note_01",
      source_position: 1,
      title: "Title 01",
      cover: "visible",
      likes: "1",
      note_type: "图文",
      sponsorship_evidence: "unknown",
      summary_source: "search_card_visible_dom"
    });
    expect(result.summaries.at(-5)?.source_position).toBe(19);
    expect(result.exclusions).toEqual([
      {
        note_id: "note_sponsored",
        canonical_url: "https://www.xiaohongshu.com/explore/note_sponsored",
        source_position: 21,
        reason: "sponsored",
        is_sponsored: true,
        sponsorship_evidence: "visible_sponsored_label"
      },
      {
        note_id: "note_repeated",
        canonical_url: "https://www.xiaohongshu.com/explore/note_repeated",
        source_position: 23,
        reason: "sponsored",
        is_sponsored: true,
        sponsorship_evidence: "visible_sponsored_label"
      }
    ]);
    expect(result.sort_label).toBe("综合");
  });

  test("omits missing card facts without navigation, fetch, or detail projection", async () => {
    const dom = page("search-scan.html", "https://www.xiaohongshu.com/search_result");
    const context = detectPage(dom.window.document, dom.window.location);
    const fetchStub = vi.fn(() => { throw new Error("scan must not fetch"); });
    const tabs = new Proxy({}, { get: () => { throw new Error("scan must not access chrome.tabs"); } });
    vi.stubGlobal("fetch", fetchStub);
    vi.stubGlobal("chrome", { tabs } as unknown as typeof chrome);
    vi.resetModules();
    vi.doMock("../src/dom/detail.js", () => { throw new Error("scan must not import detail projection"); });
    const { scanSearchCards: isolatedScan } = await import("../src/dom/search-scan.js");

    const result = isolatedScan(dom.window.document, context);
    const missing = result.summaries.find((summary) => summary.note_id === "note_missing");

    expect(missing).toEqual({
      note_id: "note_missing",
      canonical_url: "https://www.xiaohongshu.com/explore/note_missing",
      source_position: 22,
      cover: "visible",
      note_type: "图文",
      sponsorship_evidence: "unknown",
      summary_source: "search_card_visible_dom"
    });
    expect(fetchStub).not.toHaveBeenCalled();
  });

  test("fails closed for a stale route context and does not retain an unsafe sort label", () => {
    const dom = page("search-scan.html", "https://www.xiaohongshu.com/search_result");
    const stale = detectPage(dom.window.document, new URL("https://www.xiaohongshu.com/explore/note_01") as unknown as Location);
    (dom.window.document.querySelector("[data-search-sort-label]") as Element).textContent = "https://unsafe.example/sort";

    expect(() => scanSearchCards(dom.window.document, stale)).toThrow("search scan requires the canonical search route");
    expect(scanSearchCards(dom.window.document, detectPage(dom.window.document, dom.window.location)).sort_label).toBeUndefined();
  });
});

describe("candidate scrolling", () => {
  test("scrolls only an unblocked search or account page", () => {
    const search = page("search-page.html", "https://www.xiaohongshu.com/search_result");
    const account = page("account-page.html", "https://www.xiaohongshu.com/user/profile/author_123");
    const calls: string[] = [];

    expect(scrollCandidates(search.window.document, search.window.location, () => calls.push("search"))).toEqual({ scrolled: true });
    expect(scrollCandidates(account.window.document, account.window.location, () => calls.push("account"))).toEqual({ scrolled: true });
    expect(calls).toEqual(["search", "account"]);
  });

  test("does not scroll blocked or unsupported contexts", () => {
    const detail = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const blocked = page("challenge-page.html", "https://www.xiaohongshu.com/search_result");
    const emptyChallenge = page("challenge-empty-page.html", "https://www.xiaohongshu.com/search_result");
    const hiddenChallenge = page("challenge-hidden-page.html", "https://www.xiaohongshu.com/search_result");
    const calls: string[] = [];

    expect(scrollCandidates(detail.window.document, detail.window.location, () => calls.push("detail"))).toEqual({ error: "unsupported_page" });
    expect(scrollCandidates(blocked.window.document, blocked.window.location, () => calls.push("blocked"))).toEqual({ error: "challenge_detected" });
    expect(detectBlockPage(emptyChallenge.window.document)).toBe("challenge_detected");
    expect(scrollCandidates(emptyChallenge.window.document, emptyChallenge.window.location, () => calls.push("empty-challenge"))).toEqual({ error: "challenge_detected" });
    expect(detectBlockPage(hiddenChallenge.window.document)).toBeUndefined();
    expect(scrollCandidates(hiddenChallenge.window.document, hiddenChallenge.window.location, () => calls.push("hidden-challenge"))).toEqual({ scrolled: true });

    const emptyLogin = page("search-page.html", "https://www.xiaohongshu.com/search_result");
    emptyLogin.window.document.body.insertAdjacentHTML("beforeend", '<div data-login></div>');
    expect(detectBlockPage(emptyLogin.window.document)).toBe("login_required");
    expect(scrollCandidates(emptyLogin.window.document, emptyLogin.window.location, () => calls.push("empty-login"))).toEqual({ error: "login_required" });

    const hiddenLogin = page("search-page.html", "https://www.xiaohongshu.com/search_result");
    hiddenLogin.window.document.body.insertAdjacentHTML("beforeend", '<div data-login hidden></div>');
    expect(detectBlockPage(hiddenLogin.window.document)).toBeUndefined();
    expect(scrollCandidates(hiddenLogin.window.document, hiddenLogin.window.location, () => calls.push("hidden-login"))).toEqual({ scrolled: true });

    const headerLogin = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    headerLogin.window.document.body.insertAdjacentHTML("afterbegin", '<button id="login-btn">登录</button>');
    expect(detectBlockPage(headerLogin.window.document)).toBe("login_required");

    expect(calls).toEqual(["hidden-challenge", "hidden-login"]);
  });
});

describe("detail", () => {
  test("retains structured facts before semantic DOM fallbacks and marks each retained source", () => {
    const dom = page("detail-structured-state.html", "https://www.xiaohongshu.com/explore/note_123");

    const projection = projectDetail(dom.window.document, dom.window.location, "note_123", 1);

    expect(projection.snapshot).toMatchObject({
      note_id: "note_123",
      title: "Structured title",
      body: "DOM body fallback",
      author_id: "author_123",
      author_name: "DOM author",
      metrics: {
        likes: { raw_value: "456", normalized_value: 456, precision: "exact" },
        collects: { raw_value: "1.5万", normalized_value: 15000, precision: "display_rounded" }
      }
    });
    expect(projection.field_provenance).toEqual({
      title: "structured_state",
      body: "detail_dom",
      author_id: "structured_state",
      author_name: "detail_dom",
      tags: "not_exposed",
      note_type: "detail_dom",
      published_at: "not_exposed",
      time_evidence: "not_exposed",
      author_profile_url: "detail_dom",
      media: "not_exposed",
      likes: "structured_state",
      collects: "structured_state",
      comments: "not_exposed",
      shares: "not_exposed"
    });
    expect(projection.snapshot.note_type).toBe("video");
    expect(projection.snapshot.media_slots).toEqual([
      { note_id: "note_123", role: "video_cover", position: 1 },
      { note_id: "note_123", role: "video", position: 1 }
    ]);
    expect(projection.media).toEqual([{ note_id: "note_123", role: "video_cover", position: 1, sourceUrl: "https://ci.xhscdn.com/cover.webp" }]);
  });

  test("rejects a structured identity conflict instead of blending another note into the detail", () => {
    const dom = page("detail-structured-state.html", "https://www.xiaohongshu.com/explore/note_123");
    const state = dom.window.document.querySelector("#xhs-note-state") as Element;
    state.textContent = '{"note_id":"other_note","author_id":"author_123","title":"Wrong note"}';

    expect(() => projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toThrow("structured_state");

    state.textContent = '{"note_id":"note_123","author_id":"other_author","title":"Wrong author"}';
    expect(() => projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toThrow("structured_state");
  });

  test("rejects malformed or duplicate explicit structured state islands", () => {
    const malformed = page("detail-structured-state.html", "https://www.xiaohongshu.com/explore/note_123");
    (malformed.window.document.querySelector("#xhs-note-state") as Element).textContent = "{not json";
    expect(() => projectDetail(malformed.window.document, malformed.window.location, "note_123", 1)).toThrow("structured_state");

    const duplicate = page("detail-structured-state.html", "https://www.xiaohongshu.com/explore/note_123");
    duplicate.window.document.body.insertAdjacentHTML("beforeend", '<script id="xhs-note-state" type="application/json">{"note_id":"note_123"}</script>');
    expect(() => projectDetail(duplicate.window.document, duplicate.window.location, "note_123", 1)).toThrow("structured_state");
  });

  test("omits unavailable detail fields and keeps only the bound cover when direct video is ambiguous", () => {
    const dom = page("detail-structured-state.html", "https://www.xiaohongshu.com/explore/note_123");
    const state = dom.window.document.querySelector("#xhs-note-state") as Element;
    state.textContent = '{"note_id":"note_123","author_id":"author_123"}';
    dom.window.document.querySelector("#detail-title")?.remove();
    dom.window.document.querySelector(".engage-bar")?.remove();

    const projection = projectDetail(dom.window.document, dom.window.location, "note_123", 1);

    expect(projection.snapshot).not.toHaveProperty("title");
    expect(projection.snapshot.metrics).toEqual({
      likes: { precision: "not_exposed" }, collects: { precision: "not_exposed" },
      comments: { precision: "not_exposed" }, shares: { precision: "not_exposed" }
    });
    expect(projection.field_provenance).toMatchObject({ title: "not_exposed", likes: "not_exposed" });
    expect(projection.snapshot.note_type).toBe("video");
    expect(projection.snapshot.media_slots).toEqual([
      { note_id: "note_123", role: "video_cover", position: 1 },
      { note_id: "note_123", role: "video", position: 1 }
    ]);
    expect(projection.media).toEqual([{ note_id: "note_123", role: "video_cover", position: 1, sourceUrl: "https://ci.xhscdn.com/cover.webp" }]);
  });

  test("does not block a valid detail for ordinary visible login or verification language", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    expect(detectBlockPage(dom.window.document)).toBeUndefined();

    const detailText = dom.window.document.querySelector("#noteContainer #detail-desc") as Element;
    detailText.insertAdjacentHTML("beforeend", "<span> 登录后验证内容</span>");

    expect(detectBlockPage(dom.window.document)).toBeUndefined();
    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1).snapshot.note_id).toBe("note_123");
  });

  test("projects one identity-bound visible detail root without media sources in the native snapshot", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const projection = projectDetail(dom.window.document, dom.window.location, "note_123", 1);

    expect(projection.snapshot).toEqual({
      source_position: 1,
      note_id: "note_123",
      canonical_url: "https://www.xiaohongshu.com/explore/note_123",
      title: "Fixture detail title",
      body: "Fixture detail body #AI",
      tags: ["#AI"],
      note_type: "normal",
      published_at: "2026-08-01T10:00:00+08:00",
      time_evidence: { kind: "published", raw_text: "发布于 2026-08-01 10:00" },
      author_id: "author_123",
      author_name: "Fixture author",
      author_profile_url: "https://www.xiaohongshu.com/user/profile/author_123",
      metrics: {
        likes: { raw_value: "12", normalized_value: 12, precision: "exact" },
        collects: { raw_value: "1.2万", normalized_value: 12000, precision: "display_rounded" },
        comments: { raw_value: "3", normalized_value: 3, precision: "exact" },
        shares: { precision: "not_exposed" }
      },
      metric_provenance: {
        likes: "detail_visible_count", collects: "detail_visible_count", comments: "detail_visible_count"
      },
      media_slots: [
        { note_id: "note_123", role: "image", position: 1 },
        { note_id: "note_123", role: "image", position: 2 }
      ]
    });
    expect(projection.media).toHaveLength(2);
    expect(JSON.stringify(projection.snapshot)).not.toContain("xhscdn");
    expect(JSON.stringify(projection.snapshot)).not.toContain("sourceUrl");
  });

  test("fails closed for a route mismatch, duplicate exact roots, and challenge/login markers", () => {
    const mismatch = page("current-note.html", "https://www.xiaohongshu.com/explore/not_the_note");
    expect(() => projectDetail(mismatch.window.document, mismatch.window.location, "note_123", 1)).toThrow();

    const duplicate = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    duplicate.window.document.body.insertAdjacentHTML("beforeend", fixture("current-note.html"));
    expect(() => projectDetail(duplicate.window.document, duplicate.window.location, "note_123", 1)).toThrow();

    const challenge = page("challenge-page.html", "https://www.xiaohongshu.com/explore/note_123");
    expect(detectBlockPage(challenge.window.document)).toBe("challenge_detected");

    const login = page("search-page.html", "https://www.xiaohongshu.com/search_result");
    login.window.document.body.insertAdjacentHTML("beforeend", '<div class="login-container">请登录继续</div>');
    expect(detectBlockPage(login.window.document)).toBe("login_required");
  });

  test("uses the route-bound unique detail root when the live page omits data-note-id", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const root = dom.window.document.querySelector("#noteContainer") as Element;
    root.querySelector("#detail-desc")?.insertAdjacentHTML("beforeend", '<span style="display:none"> hidden secret</span>');
    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1).snapshot.body).toBe("Fixture detail body #AI");

    root.removeAttribute("data-note-id");
    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1).snapshot.note_id).toBe("note_123");

    root.setAttribute("data-note-id", "other_note");
    expect(() => projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toThrow();
  });

  test("uses one valid author profile link even when the author region has a separate action link", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const author = dom.window.document.querySelector("#noteContainer .author-wrapper") as Element;
    author.insertAdjacentHTML("beforeend", '<a href="/follow">关注</a>');

    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1).snapshot.author_id).toBe("author_123");

    author.insertAdjacentHTML("beforeend", '<a href="/user/profile/other_author">其他作者</a>');
    expect(() => projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toThrow("author_identity");
  });

  test("uses the title-header author and ignores a later comment author", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const root = dom.window.document.querySelector("#noteContainer") as Element;
    root.insertAdjacentHTML("beforeend", '<section class="comments"><div class="author-wrapper"><a href="/user/profile/comment_author"><span class="name">Comment author</span></a></div></section>');

    const projection = projectDetail(dom.window.document, dom.window.location, "note_123", 1);

    expect(projection.snapshot.author_id).toBe("author_123");
    expect(projection.snapshot.author_name).toBe("Fixture author");
    expect(projection.snapshot.author_profile_url).toBe("https://www.xiaohongshu.com/user/profile/author_123");
  });

  test("collapses duplicate title-header links to the same author", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const author = dom.window.document.querySelector("#noteContainer .author-wrapper") as Element;
    author.insertAdjacentHTML("beforeend", '<a href="/user/profile/author_123"><span>Same author second link</span></a>');

    const projection = projectDetail(dom.window.document, dom.window.location, "note_123", 1);

    expect(projection.snapshot.author_id).toBe("author_123");
    expect(projection.snapshot.author_profile_url).toBe("https://www.xiaohongshu.com/user/profile/author_123");
  });

  test("canonicalizes same-author title-header links with distinct queries", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const author = dom.window.document.querySelector("#noteContainer .author-wrapper") as Element;
    author.insertAdjacentHTML("beforeend", '<a href="/user/profile/author_123?from=second_header_link"><span>Same author second link</span></a>');

    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1).snapshot.author_profile_url)
      .toBe("https://www.xiaohongshu.com/user/profile/author_123");
  });

  test("uses the first selected author region for an optional name when same-author regions repeat", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const root = dom.window.document.querySelector("#noteContainer") as Element;
    root.querySelector("#detail-title")?.insertAdjacentHTML("beforebegin", '<div class="author-wrapper"><a href="/user/profile/author_123"><span class="name">Second region</span></a></div>');

    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1).snapshot.author_name).toBe("Fixture author");
  });

  test("rejects different title-header author regions", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const root = dom.window.document.querySelector("#noteContainer") as Element;
    root.querySelector("#detail-title")?.insertAdjacentHTML("beforebegin", '<div class="author-wrapper"><a href="/user/profile/other_author"><span class="name">Other author</span></a></div>');

    let failure: unknown;
    try { projectDetail(dom.window.document, dom.window.location, "note_123", 1); } catch (error) { failure = error; }
    expect(failure).toMatchObject({ name: "DetailProjectionError", stage: "author_identity", detail_reason: "duplicate_different_author" });
  });

  test("keeps the prior unique-author behavior when the title is not exposed", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    dom.window.document.querySelector("#detail-title")?.remove();

    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1).snapshot.author_id).toBe("author_123");
  });

  test("keeps the all-author ambiguity check when more than one visible title exists", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const root = dom.window.document.querySelector("#noteContainer") as Element;
    root.insertAdjacentHTML("afterbegin", '<h1 id="detail-title">Another visible title</h1>');
    root.insertAdjacentHTML("beforeend", '<section class="comments"><div class="author-wrapper"><a href="/user/profile/comment_author"><span class="name">Comment author</span></a></div></section>');

    let failure: unknown;
    try { projectDetail(dom.window.document, dom.window.location, "note_123", 1); } catch (error) { failure = error; }
    expect(failure).toMatchObject({ name: "DetailProjectionError", stage: "author_identity", detail_reason: "duplicate_different_author" });
  });

  test.each([
    ['<a href="/follow">private author text</a>', "no_visible_profile_anchor"],
    ['<a hidden href="/user/profile/private_author">private author text</a>', "no_visible_profile_anchor"],
    ['<a href="/user/profile/private_author">one</a><a href="/user/profile/other_author">two</a>', "duplicate_different_author"],
    ['<a href="/user/profile/private%2Fauthor">private author text</a>', "malformed_profile_path"],
    ['<a href="https://unsafe.example/user/profile/private_author">private author text</a>', "unsafe_profile_origin"],
    ['<a href="http://[invalid">private author text</a>', "unparseable_author_anchor"]
  ])("rejects author markup with only the finite reason %s -> %s", (markup, reason) => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const author = dom.window.document.querySelector("#noteContainer .author-wrapper") as Element;
    author.innerHTML = markup;
    let failure: unknown;
    try { projectDetail(dom.window.document, dom.window.location, "note_123", 1); } catch (error) { failure = error; }
    expect(failure).toMatchObject({ name: "DetailProjectionError", stage: "author_identity", detail_reason: reason, message: "author_identity" });
    expect(JSON.stringify(failure)).not.toMatch(/private_author|private-token|unsafe\.example|private author text|href/u);
  });

  test("rejects a malformed profile-like link beside the valid author identity", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const author = dom.window.document.querySelector("#noteContainer .author-wrapper") as Element;
    author.insertAdjacentHTML("beforeend", '<a href="/user/profile/not%2Fsafe">异常作者</a>');

    expect(() => projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toThrow("author_identity");
  });

  test("rejects a missing detail-root identity when another visible candidate explicitly names a different note", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const root = dom.window.document.querySelector("#noteContainer") as Element;
    root.removeAttribute("data-note-id");
    dom.window.document.body.insertAdjacentHTML("beforeend", fixture("current-note.html").replace('data-note-id="note_123"', 'data-note-id="other_note"'));

    expect(() => projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toThrow();
  });

  test("rejects a missing detail-root identity nested inside an explicit conflicting ancestor", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const root = dom.window.document.querySelector("#noteContainer") as Element;
    root.removeAttribute("data-note-id");
    const outer = dom.window.document.createElement("section");
    outer.className = "note-detail-mask";
    outer.setAttribute("data-note-id", "other_note");
    root.replaceWith(outer);
    outer.append(root);

    expect(() => projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toThrow();
  });

  test("preserves visible video slots when a direct source is absent, ambiguous, HLS, or posterless", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const root = dom.window.document.querySelector("#noteContainer") as Element;
    root.querySelector(".note-slider")?.remove();
    root.insertAdjacentHTML("beforeend", `
      <div class="media-container"><video poster="https://ci.xhscdn.com/cover.webp">
        <source src="https://ci.xhscdn.com/video.mp4" type="video/mp4">
      </video></div>`);
    const source = root.querySelector("source") as HTMLElement;
    Object.defineProperty(source, "getClientRects", { configurable: true, value: () => [] });

    const projection = projectDetail(dom.window.document, dom.window.location, "note_123", 1);
    expect(projection.snapshot.media_slots).toEqual([
      { note_id: "note_123", role: "video_cover", position: 1 },
      { note_id: "note_123", role: "video", position: 1 }
    ]);
    expect(projection.media.map(({ sourceUrl, ...slot }) => slot)).toEqual(projection.snapshot.media_slots);
    expect(projection.media.map((item) => item.sourceUrl)).toEqual([
      "https://ci.xhscdn.com/cover.webp", "https://ci.xhscdn.com/video.mp4"
    ]);

    root.querySelector("source")?.setAttribute("src", "blob:https://www.xiaohongshu.com/opaque");
    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toMatchObject({
      snapshot: { note_type: "video", media_slots: [
        { note_id: "note_123", role: "video_cover", position: 1 },
        { note_id: "note_123", role: "video", position: 1 }
      ] }, media: [{ note_id: "note_123", role: "video_cover", position: 1, sourceUrl: "https://ci.xhscdn.com/cover.webp" }]
    });

    root.querySelector("source")?.setAttribute("src", "https://ci.xhscdn.com/video.mp4");
    root.querySelector("video")?.insertAdjacentHTML("beforeend", '<source src="https://ci.xhscdn.com/other.mp4">');
    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toMatchObject({
      snapshot: { note_type: "video", media_slots: [
        { note_id: "note_123", role: "video_cover", position: 1 },
        { note_id: "note_123", role: "video", position: 1 }
      ] }, media: [{ note_id: "note_123", role: "video_cover", position: 1, sourceUrl: "https://ci.xhscdn.com/cover.webp" }]
    });

    (root.querySelector("video") as HTMLVideoElement).innerHTML = '<source src="https://ci.xhscdn.com/playlist.m3u8">';
    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toMatchObject({
      snapshot: { note_type: "video", media_slots: [
        { note_id: "note_123", role: "video_cover", position: 1 },
        { note_id: "note_123", role: "video", position: 1 }
      ] }, media: [{ note_id: "note_123", role: "video_cover", position: 1, sourceUrl: "https://ci.xhscdn.com/cover.webp" }]
    });

    root.querySelector("source")?.setAttribute("src", "https://ci.xhscdn.com/video.mp4");
    root.querySelector("video")?.removeAttribute("poster");
    expect(projectDetail(dom.window.document, dom.window.location, "note_123", 1)).toMatchObject({
      snapshot: { note_type: "video", media_slots: [
        { note_id: "note_123", role: "video", position: 1 }
      ] }, media: [{ note_id: "note_123", role: "video", position: 1, sourceUrl: "https://ci.xhscdn.com/video.mp4" }]
    });
  });

  test("projects a hydrated exact-note EF5 MP4 while ignoring unrelated empty Map state", () => {
    const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
    const root = dom.window.document.querySelector("#noteContainer")!;
    root.querySelector(".note-slider")?.remove();
    root.insertAdjacentHTML("beforeend", '<div class="media-container"><video poster="https://ci.xhscdn.com/cover.webp" src="blob:https://www.xiaohongshu.com/opaque"></video></div>');
    const script = dom.window.document.createElement("script");
    script.textContent = 'window.__INITIAL_STATE__={"unrelated":new Map([]),"note":{"noteDetailMap":{"note_123":{"note":{"noteId":"note_123","user":{"userId":"author_123"},"type":"video","video":{"media":{"stream":{"EF5":[{"videoCodec":"EF5","format":"mp4","size":16641333,"width":1706,"height":1080,"duration":204499,"masterUrl":"https://sns-video-v4.xhscdn.com/stream/a/larger.mp4?sign=abc123&t=123456"}],"EF7":[]}}}}}}}};';
    dom.window.document.body.append(script);

    const projection = projectDetail(dom.window.document, dom.window.location, "note_123", 1);
    expect(projection.snapshot.media_slots).toEqual([
      { note_id: "note_123", role: "video_cover", position: 1 },
      { note_id: "note_123", role: "video", position: 1 }
    ]);
    expect(projection.media).toEqual([
      { note_id: "note_123", role: "video_cover", position: 1, sourceUrl: "https://ci.xhscdn.com/cover.webp" },
      { note_id: "note_123", role: "video", position: 1, expectedSizeBytes: 16641333, sourceUrl: "https://sns-video-v4.xhscdn.com/stream/a/larger.mp4?sign=abc123&t=123456" }
    ]);
    expect(projection.video?.durationMs).toBe(204499);
  });
});

describe("media fetch", () => {
  test("uses credential-free, redirect-free bounded fetches and keeps the source in the caller closure", async () => {
    const calls: RequestInit[] = [];
    const response = new Response(new Uint8Array([1, 2, 3]), { headers: { "content-type": "image/webp", "content-length": "3" } });
    const result = await fetchBoundMedia("https://ci.xhscdn.com/image.webp", "image", async (_url, init) => {
      calls.push(init as RequestInit);
      return response;
    });

    expect(calls).toEqual([{ credentials: "omit", redirect: "error", referrerPolicy: "no-referrer", cache: "no-store" }]);
    expect(result).toMatchObject({ mimeType: "image/webp", sizeBytes: 3 });
    expect(result).not.toHaveProperty("sourceUrl");
  });

  test("forwards the active job abort signal into the credential-free fetch", async () => {
    const controller = new AbortController();
    const calls: RequestInit[] = [];
    const response = new Response(new Uint8Array([1]), { headers: { "content-type": "image/webp", "content-length": "1" } });

    await fetchBoundMedia("https://ci.xhscdn.com/image.webp", "image", async (_url, init) => {
      calls.push(init as RequestInit);
      return response;
    }, controller.signal);

    expect(calls).toHaveLength(1);
    expect(calls[0]?.signal).toBe(controller.signal);
  });

  test("rejects an empty approved response before it can become a native media slot", async () => {
    const response = new Response(new Uint8Array(), { headers: { "content-type": "image/webp", "content-length": "0" } });
    await expect(fetchBoundMedia("https://ci.xhscdn.com/image.webp", "image", async () => response)).rejects.toMatchObject({ code: "download_failed" });
  });

  test("detects a challenge body even when it lies about an allowed media MIME", async () => {
    const response = new Response("请完成安全验证", { headers: { "content-type": "image/webp" } });
    await expect(fetchBoundMedia("https://ci.xhscdn.com/image.webp", "image", async () => response)).rejects.toMatchObject({ code: "challenge_detected" });
  });
});


describe("publication dates in the detail collection path", () => {
  test("uses the page date and keeps its original evidence", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-14T00:30:00+08:00"));
    try {
      const dom = page("current-note.html", "https://www.xiaohongshu.com/explore/note_123");
      dom.window.document.querySelector("#noteContainer .date")!.textContent = "05-26";
      const projection = projectDetail(dom.window.document, dom.window.location, "note_123", 1);
      expect(projection.snapshot.published_at).toBe("2026-05-26T00:00:00+08:00");
      expect(projection.snapshot.time_evidence).toEqual({ kind: "published", raw_text: "05-26" });
      expect(projection.field_provenance.published_at).toBe("detail_dom");
    } finally { vi.useRealTimers(); }
  });
});
