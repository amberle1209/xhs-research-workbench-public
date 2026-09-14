import { JSDOM } from "jsdom";
import { afterEach, describe, expect, test, vi } from "vitest";

const sentinel = "__xhsResearchWorkbenchContentListenerV1";

afterEach(() => {
  delete (globalThis as Record<string, unknown>)[sentinel];
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("content-script registration", () => {
  test("accepts only an exact scan_search request and returns a bounded local projection", async () => {
    const dom = new JSDOM(`
      <main><div data-search-sort-label>综合</div><section class="note-item">
        <a href="/explore/note_123"><img class="cover"><span class="title">Visible card</span></a>
      </section></main>`, { url: "https://www.xiaohongshu.com/search_result" });
    Object.defineProperty(dom.window.HTMLElement.prototype, "getClientRects", {
      configurable: true, value: () => [{ width: 1, height: 1 }]
    });
    const listeners: Array<(message: unknown, sender: chrome.runtime.MessageSender, respond: (value: unknown) => void) => void> = [];
    vi.stubGlobal("document", dom.window.document);
    vi.stubGlobal("window", dom.window);
    vi.stubGlobal("chrome", {
      runtime: { id: "extension-id", onMessage: { addListener: vi.fn((listener) => listeners.push(listener)) } }
    } as unknown as typeof chrome);

    await import("../src/content-script.js");
    const respond = vi.fn();
    listeners[0]?.({ kind: "scan_search" }, { id: "extension-id" } as chrome.runtime.MessageSender, respond);
    const response = respond.mock.calls[0]?.[0] as Record<string, unknown>;

    expect(Object.keys(response).sort()).toEqual(["exclusions", "sort_label", "summaries"]);
    expect(response.summaries).toEqual([{
      note_id: "note_123", canonical_url: "https://www.xiaohongshu.com/explore/note_123", source_position: 1,
      title: "Visible card", cover: "visible", sponsorship_evidence: "unknown", summary_source: "search_card_visible_dom"
    }]);
    expect(response.exclusions).toEqual([]);
    expect(response.sort_label).toBe("综合");
    expect(JSON.stringify(response)).not.toContain("<section");
    expect(JSON.stringify(response)).not.toContain("href");

    const rejected = vi.fn();
    listeners[0]?.({ kind: "scan_search", extra: true }, { id: "extension-id" } as chrome.runtime.MessageSender, rejected);
    expect(rejected).not.toHaveBeenCalled();
  });

  test("reports a visible login block before projecting a detail", async () => {
    const dom = new JSDOM('<button id="login-btn">登录</button>', {
      url: "https://www.xiaohongshu.com/explore/note_123"
    });
    Object.defineProperty(dom.window.HTMLElement.prototype, "getClientRects", {
      configurable: true, value: () => [{ width: 1, height: 1 }]
    });
    const listeners: Array<(message: unknown, sender: chrome.runtime.MessageSender, respond: (value: unknown) => void) => void> = [];
    vi.stubGlobal("document", dom.window.document);
    vi.stubGlobal("window", dom.window);
    vi.stubGlobal("chrome", {
      runtime: { id: "extension-id", onMessage: { addListener: vi.fn((listener) => listeners.push(listener)) } }
    } as unknown as typeof chrome);

    await import("../src/content-script.js");
    const respond = vi.fn();
    listeners[0]?.(
      { kind: "project_detail", noteId: "note_123", sourcePosition: 1 },
      { id: "extension-id" } as chrome.runtime.MessageSender,
      respond
    );

    expect(respond).toHaveBeenCalledWith({ error: "login_required" });
  });

  test("returns a finite detail stage without exposing a projection exception", async () => {
    const dom = new JSDOM("<main></main>", {
      url: "https://www.xiaohongshu.com/explore/note_123"
    });
    Object.defineProperty(dom.window.HTMLElement.prototype, "getClientRects", {
      configurable: true, value: () => [{ width: 1, height: 1 }]
    });
    const listeners: Array<(message: unknown, sender: chrome.runtime.MessageSender, respond: (value: unknown) => void) => void> = [];
    vi.stubGlobal("document", dom.window.document);
    vi.stubGlobal("window", dom.window);
    vi.stubGlobal("chrome", {
      runtime: { id: "extension-id", onMessage: { addListener: vi.fn((listener) => listeners.push(listener)) } }
    } as unknown as typeof chrome);

    await import("../src/content-script.js");
    const respond = vi.fn();
    listeners[0]?.(
      { kind: "project_detail", noteId: "note_123", sourcePosition: 1 },
      { id: "extension-id" } as chrome.runtime.MessageSender,
      respond
    );

    expect(respond).toHaveBeenCalledWith({ error: "detail_unavailable", stage: "detail_root" });
    expect(JSON.stringify(respond.mock.calls)).not.toContain("exact visible detail root");
  });

  test.each([
    ['<a href="/follow">private text</a>', "no_visible_profile_anchor"],
    ['<a href="/user/profile/private_author">one</a><a href="/user/profile/other_author">two</a>', "duplicate_different_author"],
    ['<a href="/user/profile/private%2Fauthor">private text</a>', "malformed_profile_path"],
    ['<a href="https://unsafe.example/user/profile/private_author">private text</a>', "unsafe_profile_origin"],
    ['<a href="http://[invalid">private text</a>', "unparseable_author_anchor"]
  ])("forwards only the finite author reason from real DOM: %s -> %s", async (markup, reason) => {
    const dom = new JSDOM(`<main id="noteContainer"><div class="author-wrapper">${markup}</div><div id="detail-desc">private body</div></main>`, { url: "https://www.xiaohongshu.com/explore/note_123" });
    Object.defineProperty(dom.window.HTMLElement.prototype, "getClientRects", { configurable: true, value: () => [{ width: 1, height: 1 }] });
    const listeners: Array<(message: unknown, sender: chrome.runtime.MessageSender, respond: (value: unknown) => void) => void> = [];
    vi.stubGlobal("document", dom.window.document);
    vi.stubGlobal("window", dom.window);
    vi.stubGlobal("chrome", { runtime: { id: "extension-id", onMessage: { addListener: (listener: typeof listeners[number]) => listeners.push(listener) } } });
    await import("../src/content-script.js");
    const respond = vi.fn();
    listeners[0]?.({ kind: "project_detail", noteId: "note_123", sourcePosition: 1 }, { id: "extension-id" }, respond);
    expect(respond.mock.calls[0]?.[0]).toEqual({ error: "detail_unavailable", stage: "author_identity", detail_reason: reason });
  });

  test.each(["raw_error", "unknown_reason", "wrong_stage"])("drops unsafe projection diagnostics at the content response boundary: %s", async (scenario) => {
    const dom = new JSDOM("<main></main>", { url: "https://www.xiaohongshu.com/explore/note_123" });
    const listeners: Array<(message: unknown, sender: chrome.runtime.MessageSender, respond: (value: unknown) => void) => void> = [];
    vi.stubGlobal("document", dom.window.document);
    vi.stubGlobal("window", dom.window);
    vi.stubGlobal("chrome", { runtime: { id: "extension-id", onMessage: { addListener: (listener: typeof listeners[number]) => listeners.push(listener) } } });
    const projection = await import("../src/dom/detail.js");
    const failure = scenario === "raw_error" ? new Error("private-token raw error") : new projection.DetailProjectionError(scenario === "wrong_stage" ? "detail_root" : "author_identity");
    Object.defineProperty(failure, "detail_reason", { value: scenario === "wrong_stage" ? "duplicate_same_author" : "https://private.example/?xsec_token=private-token" });
    // Fault injection at the projection boundary exercises the content listener's own allowlist.
    const project = vi.spyOn(projection, "projectDetail").mockImplementation(() => { throw failure; });
    try {
      await import("../src/content-script.js");
      const respond = vi.fn();
      listeners[0]?.({ kind: "project_detail", noteId: "note_123", sourcePosition: 1 }, { id: "extension-id" }, respond);
      expect(respond.mock.calls[0]?.[0]).toEqual({ error: "detail_unavailable", stage: scenario === "raw_error" ? "unexpected" : scenario === "wrong_stage" ? "detail_root" : "author_identity" });
    } finally { project.mockRestore(); }
  });

  test("installs one listener across repeated isolated-world injections and scrolls once", async () => {
    const dom = new JSDOM("<main></main>", { url: "https://www.xiaohongshu.com/search_result" });
    const listeners: Array<(message: unknown, sender: chrome.runtime.MessageSender, respond: (value: unknown) => void) => void> = [];
    const scrollBy = vi.fn();
    Object.defineProperty(dom.window, "scrollBy", { value: scrollBy });
    vi.stubGlobal("document", dom.window.document);
    vi.stubGlobal("window", dom.window);
    vi.stubGlobal("chrome", {
      runtime: { id: "extension-id", onMessage: { addListener: vi.fn((listener) => listeners.push(listener)) } }
    } as unknown as typeof chrome);

    await import("../src/content-script.js");
    vi.resetModules();
    await import("../src/content-script.js");

    expect(listeners).toHaveLength(1);
    listeners[0]?.({ kind: "scroll_candidates" }, { id: "extension-id" } as chrome.runtime.MessageSender, vi.fn());
    expect(scrollBy).toHaveBeenCalledOnce();
  });

  test("retries registration after a listener-install failure", async () => {
    const dom = new JSDOM("<main></main>", { url: "https://www.xiaohongshu.com/search_result" });
    const listeners: unknown[] = [];
    const addListener = vi.fn((listener: unknown) => {
      if (addListener.mock.calls.length === 1) throw new Error("registration failed");
      listeners.push(listener);
    });
    vi.stubGlobal("document", dom.window.document);
    vi.stubGlobal("window", dom.window);
    vi.stubGlobal("chrome", {
      runtime: { id: "extension-id", onMessage: { addListener } }
    } as unknown as typeof chrome);

    await expect(import("../src/content-script.js")).rejects.toThrow("registration failed");
    expect((globalThis as Record<string, unknown>)[sentinel]).not.toBe(true);

    vi.resetModules();
    await import("../src/content-script.js");

    expect(addListener).toHaveBeenCalledTimes(2);
    expect(listeners).toHaveLength(1);
  });
});
