import { describe, expect, test, vi } from "vitest";

import {
  JobController,
  type JobControllerDependencies,
  type NativeTransport
} from "../src/job-controller.js";
import type { NativeResponse } from "../src/contracts.js";
import { fetchBoundMedia } from "../src/dom/media.js";
import { NativeClientError } from "../src/native-client.js";

type RemovedListener = (tabId: number) => void;

class FakeNativeTransport implements NativeTransport {
  readonly requests: Record<string, unknown>[] = [];
  readonly completed = vi.fn();
  private readonly candidates: Record<string, unknown>[] = [];
  private unavailableCount = 0;

  constructor(private readonly selectedIds: readonly string[] = ["note_123"]) {}

  async connect(): Promise<Readonly<{ hostVersion: string }>> {
    return { hostVersion: "0.1.0" };
  }

  interrupt(): void {}

  completeTerminal(): void { this.completed(); }

  async request(value: unknown): Promise<NativeResponse> {
    const request = value as Record<string, unknown>;
    this.requests.push(request);
    const jobId = request.job_id as string | undefined;
    switch (request.kind) {
      case "begin_job": return { protocol_version: "1.0", kind: "job_started", job_id: jobId as string, status: "started" } as NativeResponse;
      case "candidate_snapshot":
        this.candidates.push(request);
        return { protocol_version: "1.0", kind: "candidate_result", job_id: jobId as string, note_id: request.note_id as string, source_position: request.source_position as number, outcome: "recorded" } as NativeResponse;
      case "candidate_unavailable":
        this.unavailableCount += 1;
        return { protocol_version: "1.0", kind: "candidate_result", job_id: jobId as string, note_id: request.note_id as string, source_position: request.source_position as number, outcome: "unavailable" } as NativeResponse;
      case "finish_scan":
        return {
          protocol_version: "1.0",
          kind: "selection_result",
          job_id: jobId as string,
          scanned_count: this.candidates.length + this.unavailableCount,
          eligible_count: this.candidates.length,
          selected_count: this.selectedIds.length,
          status: this.selectedIds.length === this.candidates.length ? "complete" : "partial",
          selected: this.selectedIds.map((note_id, index) => ({ note_id, selection_rank: index + 1 }))
        } as NativeResponse;
      case "media_begin": case "media_chunk":
        return {
          protocol_version: "1.0", kind: "progress", job_id: jobId as string, phase: "downloading",
          discovered: this.candidates.length + this.unavailableCount, inspected: this.candidates.length + this.unavailableCount, eligible: this.candidates.length,
          selected: this.selectedIds.length, saved: 0, current_source_position: this.candidates.length
        } as NativeResponse;
      case "media_end": return { protocol_version: "1.0", kind: "media_result", job_id: jobId as string, note_id: request.note_id as string, role: request.role as "image", position: request.position as number, outcome: "downloaded" } as NativeResponse;
      case "media_missing": return { protocol_version: "1.0", kind: "media_result", job_id: jobId as string, note_id: request.note_id as string, role: request.role as "image", position: request.position as number, outcome: "missing", reason: request.reason as "download_failed" } as NativeResponse;
      case "finish_job": return { protocol_version: "1.0", kind: "job_result", job_id: jobId as string, status: "complete", retained_count: this.selectedIds.length, report_available: true, report_file: "index.html" } as NativeResponse;
      case "stop_job": return { protocol_version: "1.0", kind: "job_result", job_id: jobId as string, status: "stopped", retained_count: 0, report_available: false } as NativeResponse;
      default: throw new Error("unexpected native request");
    }
  }
}

class InterruptingMediaNativeTransport extends FakeNativeTransport {
  readonly interrupted = vi.fn(() => {
    this.pendingReject?.(new NativeClientError("native_host_interrupted"));
    this.pendingReject = undefined;
  });
  private pendingReject: ((error: unknown) => void) | undefined;

  override interrupt(): void {
    this.interrupted();
  }

  override async request(value: unknown): Promise<NativeResponse> {
    const request = value as Record<string, unknown>;
    if (request.kind !== "media_chunk") return super.request(value);
    this.requests.push(request);
    return new Promise<NativeResponse>((_resolve, reject) => {
      this.pendingReject = reject;
    });
  }
}

class DelayedConnectNativeTransport extends FakeNativeTransport {
  readonly connectStarted = vi.fn();
  private releaseConnection: (() => void) | undefined;
  private readonly connection = new Promise<void>((resolve) => { this.releaseConnection = resolve; });

  override async connect(): Promise<Readonly<{ hostVersion: string }>> {
    this.connectStarted();
    await this.connection;
    return { hostVersion: "0.1.0" };
  }

  release(): void {
    this.releaseConnection?.();
  }
}

class TerminalClosingNativeTransport extends FakeNativeTransport {
  private closed = false;

  override completeTerminal(): void {
    this.completed();
    this.closed = true;
  }

  override async request(value: unknown): Promise<NativeResponse> {
    if (this.closed) throw new NativeClientError("native_host_interrupted");
    return super.request(value);
  }
}

class PartialStopNativeTransport extends FakeNativeTransport {
  override async request(value: unknown): Promise<NativeResponse> {
    const request = value as Record<string, unknown>;
    if (request.kind === "stop_job") {
      this.requests.push(request);
      return {
        protocol_version: "1.0", kind: "job_result", job_id: request.job_id as string,
        status: "partial", retained_count: 1, report_available: true, report_file: "index.html"
      } as NativeResponse;
    }
    return super.request(value);
  }
}

class TerminalFinishNativeTransport extends FakeNativeTransport {
  constructor(private readonly terminalStatus: "stopped" | "failed") { super(); }

  override async request(value: unknown): Promise<NativeResponse> {
    const request = value as Record<string, unknown>;
    if (request.kind === "finish_job") {
      this.requests.push(request);
      return {
        protocol_version: "1.0", kind: "job_result", job_id: request.job_id as string,
        status: this.terminalStatus, retained_count: 0, report_available: false
      } as NativeResponse;
    }
    return super.request(value);
  }
}

const imageBytes = new Uint8Array([1, 2, 3]);
const imageResult = {
  bytes: imageBytes,
  sizeBytes: imageBytes.byteLength,
  mimeType: "image/webp" as const,
  sha256: "a".repeat(64)
};

function projection(noteId: string, sourcePosition: number): Record<string, unknown> {
  return {
    snapshot: {
      source_position: sourcePosition,
      note_id: noteId,
      canonical_url: `https://www.xiaohongshu.com/explore/${noteId}`,
      title: "Fixture detail title",
      body: "Fixture detail body",
      note_type: "normal",
      author_id: "author_123",
      author_profile_url: "https://www.xiaohongshu.com/user/profile/author_123",
      metrics: {},
      media_slots: [{ note_id: noteId, role: "image", position: 1 }]
    },
    media: [{ note_id: noteId, role: "image", position: 1, sourceUrl: "https://ci.xhscdn.com/image.webp" }]
  };
}

function searchScan(noteIds: readonly string[]): Record<string, unknown> {
  return {
    summaries: noteIds.map((noteId, index) => ({
      note_id: noteId,
      canonical_url: `https://www.xiaohongshu.com/explore/${noteId}`,
      source_position: index + 1,
      sponsorship_evidence: "unknown",
      summary_source: "search_card_visible_dom"
    })),
    exclusions: []
  };
}

function fakeChrome(options: Readonly<{
  optionalPermissions?: boolean;
  permissionsContains?: () => Promise<boolean>;
  sourceKind?: "current" | "search" | "account";
  onMessage?: (tabId: number, message: Record<string, unknown>) => unknown;
  tabsCreate?: () => Promise<chrome.tabs.Tab>;
  tabsUpdate?: (tabId: number, changes: { url: string }) => Promise<chrome.tabs.Tab>;
  executeScript?: (details: { target: { tabId: number } }) => Promise<unknown>;
  storageSet?: (value: Record<string, unknown>) => Promise<void>;
}> = {}) {
  const removedListeners: RemovedListener[] = [];
  const storageSet = vi.fn(options.storageSet ?? (async () => undefined));
  const storageGet = vi.fn(async () => ({}));
  const executeScript = vi.fn(options.executeScript ?? (async () => []));
  const create = vi.fn(options.tabsCreate ?? (async () => ({ id: 99, url: "about:blank" })));
  const update = vi.fn(options.tabsUpdate ?? (async (tabId: number, changes: { url: string }) => ({ id: tabId, url: changes.url })));
  const remove = vi.fn(async () => undefined);
  const sendMessage = vi.fn(async (tabId: number, message: Record<string, unknown>) => {
    if (options.onMessage !== undefined) return options.onMessage(tabId, message);
    if (message.kind === "inspect_page") {
      const kind = options.sourceKind ?? "current";
      return kind === "current"
        ? { context: { kind, noteId: "note_123", canonicalUrl: "https://www.xiaohongshu.com/explore/note_123" } }
        : { context: { kind, canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
    }
    if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
    if (message.kind === "scan_search") return searchScan([]);
    if (message.kind === "discover_candidates") return { candidates: [] };
    if (message.kind === "scroll_candidates") return { scrolled: true };
    throw new Error("unexpected content message");
  });
  vi.stubGlobal("chrome", {
    permissions: { contains: vi.fn(options.permissionsContains ?? (async () => options.optionalPermissions ?? true)) },
    scripting: { executeScript },
    tabs: {
      create,
      update,
      remove,
      sendMessage,
      onRemoved: {
        addListener: (listener: RemovedListener) => removedListeners.push(listener),
        removeListener: (listener: RemovedListener) => {
          const index = removedListeners.indexOf(listener);
          if (index >= 0) removedListeners.splice(index, 1);
        }
      }
    },
    storage: { local: { get: storageGet, set: storageSet } }
  } as unknown as typeof chrome);
  return { storageSet, executeScript, create, update, remove, sendMessage, emitRemoved: (tabId: number) => removedListeners.forEach((listener) => listener(tabId)) };
}

function dependencies(native: NativeTransport, overrides: Partial<JobControllerDependencies> = {}): JobControllerDependencies {
  return {
    native,
    sleep: async () => undefined,
    delay: (minimum) => minimum,
    newJobId: () => "job_123",
    fetchMedia: async () => imageResult,
    ...overrides
  };
}

const currentTab = { id: 1, url: "https://www.xiaohongshu.com/explore/note_123?xsec_token=discard" } as chrome.tabs.Tab;
const searchTab = { id: 1, url: "https://www.xiaohongshu.com/search_result?keyword=AI&xsec_token=discard" } as chrome.tabs.Tab;

describe("JobController current-note collection", () => {
  test("runs only after the explicit current-tab command, injects that tab only, and never queues or filters it", async () => {
    const chromeMock = fakeChrome();
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native));

    await controller.collectCurrent(currentTab);

    expect(chromeMock.executeScript).toHaveBeenCalledExactlyOnceWith({ target: { tabId: 1 }, files: ["content-script.js"] });
    expect(chromeMock.create).not.toHaveBeenCalled();
    expect(native.requests.map((request) => request.kind)).toEqual([
      "begin_job", "candidate_snapshot", "finish_scan", "media_begin", "media_chunk", "media_end", "finish_job"
    ]);
    expect(native.requests[0]).toMatchObject({ collection_surface: "extension_current", requested_count: 1, candidate_scan_limit: 1 });
    expect(native.requests[0]).not.toHaveProperty("publication_cutoff");
    expect(native.completed).toHaveBeenCalledOnce();
    expect(chromeMock.storageSet.mock.invocationCallOrder.at(-1)).toBeLessThan(native.completed.mock.invocationCallOrder[0] ?? Number.POSITIVE_INFINITY);
  });

  test("releases the completed native session when its final progress write fails", async () => {
    const chromeMock = fakeChrome({
      storageSet: async (value) => {
        const progress = value.xhs_job_progress as { phase?: string } | undefined;
        if (progress?.phase === "complete") throw new Error("terminal storage unavailable");
      }
    });
    const native = new TerminalClosingNativeTransport();
    const controller = new JobController(dependencies(native));

    await expect(controller.collectCurrent(currentTab)).rejects.toMatchObject({ code: "native_host_error" });

    expect(native.completed).toHaveBeenCalledOnce();
    expect(native.requests.map((request) => request.kind)).not.toContain("stop_job");
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({ phase: "error", error: "native_host_error" })
    });
  });

  test.each(["stopped", "failed"] as const)("closes a %s finish response instead of relaying it as partial", async (terminalStatus) => {
    const chromeMock = fakeChrome();
    const native = new TerminalFinishNativeTransport(terminalStatus);
    const controller = new JobController(dependencies(native));

    await expect(controller.collectCurrent(currentTab)).rejects.toMatchObject({ code: "native_host_error" });

    expect(native.completed).toHaveBeenCalledOnce();
    expect(native.requests.map((request) => request.kind)).not.toContain("stop_job");
    const storedPhases = chromeMock.storageSet.mock.calls.map(([value]) => (value.xhs_job_progress as { phase: string }).phase);
    expect(storedPhases).not.toContain("complete");
    expect(storedPhases).not.toContain("partial");
    expect(storedPhases.at(-1)).toBe("error");
  });

  test("a stop while optional permission is pending cannot later connect or inject", async () => {
    let allowPermission: (() => void) | undefined;
    const permission = new Promise<boolean>((resolve) => { allowPermission = () => resolve(true); });
    const chromeMock = fakeChrome({ permissionsContains: () => permission });
    const native = new FakeNativeTransport();
    const connect = vi.spyOn(native, "connect");
    const controller = new JobController(dependencies(native));

    const run = controller.collectCurrent(currentTab);
    await vi.waitFor(() => expect(allowPermission).toBeTypeOf("function"));
    await controller.stop();
    allowPermission?.();
    await run;

    expect(connect).not.toHaveBeenCalled();
    expect(chromeMock.executeScript).not.toHaveBeenCalled();
    expect(chromeMock.create).not.toHaveBeenCalled();
  });

  test("rejects a selection whose scanned count differs from submitted snapshots", async () => {
    fakeChrome();
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        const request = value as Record<string, unknown>;
        if (request.kind === "finish_scan") {
          this.requests.push(request);
          return {
            protocol_version: "1.0", kind: "selection_result", job_id: request.job_id as string,
            scanned_count: 0, eligible_count: 0, selected_count: 0, status: "partial", selected: []
          } as NativeResponse;
        }
        return super.request(value);
      }
    }([]);
    const controller = new JobController(dependencies(native));

    await expect(controller.collectCurrent(currentTab)).rejects.toMatchObject({ code: "native_host_error" });

    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("media_begin");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
  });

  test("records no body, HTML, media URL, token, selector, or raw error in storage progress", async () => {
    const chromeMock = fakeChrome();
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native));

    await controller.collectCurrent(currentTab);

    const retained = JSON.stringify(chromeMock.storageSet.mock.calls);
    expect(retained).not.toMatch(/Fixture detail|xhscdn|sourceUrl|xsec|token|selector|https?:\/\//i);
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({
        job_id: "job_123", phase: "complete", eligible: 1, current_source_position: 1
      })
    });
  });

  test("sends media_missing for a declared slot that cannot transfer and still closes the job finitely", async () => {
    fakeChrome();
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native, {
      fetchMedia: async () => { throw Object.assign(new Error("https://ci.xhscdn.com/private?token=secret"), { code: "download_failed" }); }
    }));

    await controller.collectCurrent(currentTab);

    expect(native.requests.map((request) => request.kind)).toContain("media_missing");
    expect(native.requests.find((request) => request.kind === "media_missing")).toMatchObject({ reason: "download_failed" });
  });

  test("treats a login or challenge response while fetching media as a terminal stop, not a missing slot", async () => {
    fakeChrome();
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native, {
      fetchMedia: async () => { throw Object.assign(new Error("login page"), { code: "login_required" }); }
    }));

    await expect(controller.collectCurrent(currentTab)).rejects.toMatchObject({ code: "login_required" });

    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("media_missing");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
  });

  test("treats a login block that appears before detail projection as a terminal stop", async () => {
    fakeChrome({ onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") {
        return { context: { kind: "current", noteId: "note_123", canonicalUrl: "https://www.xiaohongshu.com/explore/note_123" } };
      }
      if (message.kind === "project_detail") return { error: "login_required" };
      throw new Error("detail projection must not continue after a login block");
    } });
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native));

    await expect(controller.collectCurrent(currentTab)).rejects.toMatchObject({ code: "login_required" });

    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("candidate_snapshot");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
  });

  test.each([
    ["author_identity", "no_visible_profile_anchor", "no_visible_profile_anchor"],
    ["author_identity", "duplicate_same_author", "duplicate_same_author"],
    ["author_identity", "duplicate_different_author", "duplicate_different_author"],
    ["author_identity", "malformed_profile_path", "malformed_profile_path"],
    ["author_identity", "unsafe_profile_origin", "unsafe_profile_origin"],
    ["author_identity", "unparseable_author_anchor", "unparseable_author_anchor"],
    ["author_identity", "https://private.example/?xsec_token=private-token", undefined],
    ["author_identity", { href: "private-token" }, undefined],
    ["detail_root", "duplicate_same_author", undefined],
    ["unknown_stage", "duplicate_same_author", undefined]
  ])("stores only an allowlisted author diagnostic: %s / %s", async (stage, reason, expected) => {
    const chromeMock = fakeChrome({ onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") return { context: { kind: "current", noteId: "note_123", canonicalUrl: "https://www.xiaohongshu.com/explore/note_123" } };
      return { error: "detail_unavailable", stage, detail_reason: reason, href: "private-token", message: "private error" };
    } });
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        const request = value as Record<string, unknown>;
        if (request.kind === "stop_job") return { protocol_version: "1.0", kind: "job_result", job_id: request.job_id as string, status: "failed", retained_count: 0, report_available: false };
        return super.request(value);
      }
    };
    await expect(new JobController(dependencies(native)).collectCurrent(currentTab)).rejects.toMatchObject({ code: "detail_unavailable" });
    const progress = chromeMock.storageSet.mock.calls.at(-1)?.[0].xhs_job_progress;
    expect(progress?.detail_stage).toBe(stage === "unknown_stage" ? "unexpected" : stage);
    if (expected === undefined) expect(progress).not.toHaveProperty("detail_reason");
    else expect(progress).toHaveProperty("detail_reason", expected);
    expect(JSON.stringify(chromeMock.storageSet.mock.calls)).not.toMatch(/private-token|private\.example|private error|href/u);
    expect(native.requests.some((request) => request.kind === "candidate_snapshot")).toBe(false);
  });

  test.each(["partial", "disconnect"])("retains author diagnostics on terminal %s", async (terminal) => {
    const chromeMock = fakeChrome({ onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") return { context: { kind: "current", noteId: "note_123", canonicalUrl: "https://www.xiaohongshu.com/explore/note_123" } };
      return { error: "detail_unavailable", stage: "author_identity", detail_reason: "duplicate_same_author" };
    } });
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        const request = value as Record<string, unknown>;
        if (request.kind !== "stop_job") return super.request(value);
        if (terminal === "disconnect") throw new Error("private native failure");
        return { protocol_version: "1.0", kind: "job_result", job_id: request.job_id as string, status: "partial", retained_count: 0, report_available: false };
      }
    };
    await expect(new JobController(dependencies(native)).collectCurrent(currentTab)).rejects.toMatchObject({ code: "detail_unavailable" });
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({ xhs_job_progress: expect.objectContaining({ phase: terminal === "partial" ? "partial" : "error", detail_stage: "author_identity", detail_reason: "duplicate_same_author" }) });
    expect(JSON.stringify(chromeMock.storageSet.mock.calls)).not.toContain("private native failure");
  });

  test("retains a finite detail stage when current-note projection cannot produce a snapshot", async () => {
    const chromeMock = fakeChrome({ onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") {
        return { context: { kind: "current", noteId: "note_123", canonicalUrl: "https://www.xiaohongshu.com/explore/note_123" } };
      }
      if (message.kind === "project_detail") return { error: "detail_unavailable", stage: "author_identity" };
      throw new Error("detail projection must not continue after a staged failure");
    } });
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        const request = value as Record<string, unknown>;
        if (request.kind === "stop_job") {
          this.requests.push(request);
          return { protocol_version: "1.0", kind: "job_result", job_id: request.job_id as string, status: "failed", retained_count: 0, report_available: false } as NativeResponse;
        }
        return super.request(value);
      }
    };
    const controller = new JobController(dependencies(native));

    await expect(controller.collectCurrent(currentTab)).rejects.toMatchObject({ code: "detail_unavailable" });

    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("candidate_snapshot");
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({ phase: "error", error: "detail_unavailable", detail_stage: "author_identity" })
    });
  });

  test("splits every transferred media payload at the 256 KiB protocol boundary", async () => {
    fakeChrome();
    const native = new FakeNativeTransport();
    const bytes = new Uint8Array((256 * 1024) + 1).fill(0x61);
    const controller = new JobController(dependencies(native, {
      fetchMedia: async () => ({ ...imageResult, bytes, sizeBytes: bytes.byteLength })
    }));

    await controller.collectCurrent(currentTab);

    const chunks = native.requests.filter((request) => request.kind === "media_chunk");
    expect(chunks).toHaveLength(2);
    for (const chunk of chunks) expect(atob(chunk.data_base64 as string).length).toBeLessThanOrEqual(256 * 1024);
  });

  test("closes an immediate host-side budget rejection without chunks and preserves partial saved progress", async () => {
    const chromeMock = fakeChrome();
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        const request = value as Record<string, unknown>;
        if (request.kind === "media_begin") {
          this.requests.push(request);
          return { protocol_version: "1.0", kind: "media_result", job_id: request.job_id as string, note_id: request.note_id as string, role: request.role as "image", position: request.position as number, outcome: "rejected", reason: "run_budget" } as NativeResponse;
        }
        if (request.kind === "finish_job") {
          this.requests.push(request);
          return { protocol_version: "1.0", kind: "job_result", job_id: request.job_id as string, status: "partial", retained_count: 0, report_available: true, report_file: "index.html" } as NativeResponse;
        }
        return super.request(value);
      }
    }();
    const controller = new JobController(dependencies(native));

    await controller.collectCurrent(currentTab);

    expect(native.requests.map((request) => request.kind)).toContain("media_begin");
    expect(native.requests.map((request) => request.kind)).not.toContain("media_chunk");
    expect(native.requests.map((request) => request.kind)).not.toContain("media_end");
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({ phase: "partial", saved: 0, report_available: true })
    });
  });

  test("fails closed when media_begin returns missing instead of an immediate rejection", async () => {
    fakeChrome();
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        const request = value as Record<string, unknown>;
        if (request.kind === "media_begin") {
          this.requests.push(request);
          return { protocol_version: "1.0", kind: "media_result", job_id: request.job_id as string, note_id: request.note_id as string, role: request.role as "image", position: request.position as number, outcome: "missing", reason: "source_not_exposed" } as NativeResponse;
        }
        return super.request(value);
      }
    }();
    const controller = new JobController(dependencies(native));

    await expect(controller.collectCurrent(currentTab)).rejects.toMatchObject({ code: "native_host_error" });

    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("media_chunk");
    expect(native.requests.map((request) => request.kind)).not.toContain("media_end");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
  });

  test("does not count a rejected media_end as saved", async () => {
    const chromeMock = fakeChrome();
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        const request = value as Record<string, unknown>;
        if (request.kind === "media_end") {
          this.requests.push(request);
          return { protocol_version: "1.0", kind: "media_result", job_id: request.job_id as string, note_id: request.note_id as string, role: request.role as "image", position: request.position as number, outcome: "rejected", reason: "mime_mismatch" } as NativeResponse;
        }
        if (request.kind === "finish_job") {
          this.requests.push(request);
          return { protocol_version: "1.0", kind: "job_result", job_id: request.job_id as string, status: "partial", retained_count: 0, report_available: true, report_file: "index.html" } as NativeResponse;
        }
        return super.request(value);
      }
    }();
    const controller = new JobController(dependencies(native));

    await controller.collectCurrent(currentTab);

    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({ phase: "partial", saved: 0 })
    });
  });

  test("interrupts a pending native media chunk when the invoking source tab closes", async () => {
    const chromeMock = fakeChrome();
    const native = new InterruptingMediaNativeTransport();
    const controller = new JobController(dependencies(native));

    const run = controller.collectCurrent(currentTab);
    await vi.waitFor(() => expect(native.requests.map((request) => request.kind)).toContain("media_chunk"));
    chromeMock.emitRemoved(1);
    await run;

    expect(native.interrupted).toHaveBeenCalledExactlyOnceWith();
    expect(native.requests.map((request) => request.kind)).not.toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({ phase: "stopped", error: "source_tab_closed" })
    });
  });

  test("defers unaccepted current-job progress and persists Stop without a late starting write", async () => {
    let releaseInitial: (() => void) | undefined;
    const initialWrite = new Promise<void>((resolve) => { releaseInitial = resolve; });
    let firstWrite = true;
    const committed: Record<string, unknown>[] = [];
    let allowPermissions: (() => void) | undefined;
    const permissions = new Promise<boolean>((resolve) => { allowPermissions = () => resolve(true); });
    const chromeMock = fakeChrome({
      permissionsContains: () => permissions,
      storageSet: async (value) => {
        if (firstWrite) {
          firstWrite = false;
          await initialWrite;
        }
        committed.push(value);
      }
    });
    const controller = new JobController(dependencies(new FakeNativeTransport()));

    const run = controller.collectCurrent(currentTab);
    await vi.waitFor(() => expect(releaseInitial).toBeTypeOf("function"));
    const stopping = controller.stop();
    releaseInitial?.();
    await stopping;

    expect(committed).toHaveLength(1);
    expect(committed.at(-1)).toEqual({ xhs_job_progress: expect.objectContaining({ phase: "stopped", error: "stopped" }) });
    allowPermissions?.();
    await run;
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({ xhs_job_progress: expect.objectContaining({ phase: "stopped" }) });
  });
});

describe("JobController batch collection", () => {
  test("Simple Search freezes page order before detail navigation and never visits N plus one", async () => {
    const noteIds = ["n1", "n2", "n3", "n4", "n5", "n6"];
    let activeDetailId: string | undefined;
    const fakeTabs = fakeChrome({
      sourceKind: "search",
      tabsUpdate: async (tabId, changes) => {
        activeDetailId = changes.url.slice(changes.url.lastIndexOf("/") + 1);
        return { id: tabId, url: changes.url } as chrome.tabs.Tab;
      },
      onMessage: (tabId, message) => {
        if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
        if (message.kind === "scan_search") return {
          summaries: noteIds.map((noteId, index) => ({
            note_id: noteId,
            canonical_url: `https://www.xiaohongshu.com/explore/${noteId}`,
            source_position: index + 1,
            sponsorship_evidence: "unknown",
            summary_source: "search_card_visible_dom"
          })),
          exclusions: []
        };
        if (message.kind === "inspect_page" && tabId === 99 && activeDetailId !== undefined) {
          return { context: { kind: "current", noteId: activeDetailId, canonicalUrl: `https://www.xiaohongshu.com/explore/${activeDetailId}` } };
        }
        if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
        throw new Error("Simple Search must scan before opening detail routes");
      }
    });
    const native = new FakeNativeTransport(noteIds.slice(0, 5));
    let nowMs = 0;
    const controller = new JobController(dependencies(native, {
      now: () => nowMs,
      sleep: async (duration) => { nowMs += duration; },
      searchRiskState: { readLastSearchBatchStartedAt: async () => undefined, writeLastSearchBatchStartedAt: async () => undefined }
    }));

    await controller.collectBatch(searchTab, { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" });

    const detailVisits = fakeTabs.update.mock.calls.map((call) => (call[1] as { url: string }).url.slice((call[1] as { url: string }).url.lastIndexOf("/") + 1));
    expect(detailVisits).toEqual(["n1", "n2", "n3", "n4", "n5"]);
    expect(detailVisits).not.toContain("n6");
    expect(native.requests.map((request) => request.kind)).toEqual(expect.arrayContaining(["finish_scan", "finish_job"]));
  });

  test("Simple Search detects a source route change before it freezes a selection", async () => {
    let sourceInspections = 0;
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) {
        sourceInspections += 1;
        return sourceInspections === 1
          ? { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } }
          : { context: { kind: "account", canonicalUrl: "https://www.xiaohongshu.com/user/profile/changed_route" } };
      }
      if (message.kind === "scan_search") return { summaries: [], exclusions: [] };
      throw new Error("source route drift must prevent freeze and detail navigation");
    } });
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native, {
      searchRiskState: { readLastSearchBatchStartedAt: async () => undefined, writeLastSearchBatchStartedAt: async () => undefined }
    }));

    await expect(controller.collectBatch(searchTab, { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" })).rejects.toMatchObject({ code: "identity_mismatch" });

    expect(native.requests.map((request) => request.kind)).not.toContain("candidate_snapshot");
    expect(chromeMock.create).not.toHaveBeenCalled();
    expect(chromeMock.update).not.toHaveBeenCalled();
  });

  test("Simple Search records one unavailable frozen detail and continues later frozen ranks", async () => {
    const noteIds = ["note_one", "note_two", "note_three", "note_four", "note_five"];
    let activeDetailId: string | undefined;
    const chromeMock = fakeChrome({
      sourceKind: "search",
      tabsUpdate: async (tabId, changes) => {
        activeDetailId = changes.url.slice(changes.url.lastIndexOf("/") + 1);
        return { id: tabId, url: changes.url } as chrome.tabs.Tab;
      },
      onMessage: (tabId, message) => {
        if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
        if (message.kind === "scan_search") return searchScan(noteIds);
        if (message.kind === "inspect_page" && tabId === 99) {
          if (activeDetailId === "note_two") return { context: null };
          return { context: { kind: "current", noteId: activeDetailId, canonicalUrl: `https://www.xiaohongshu.com/explore/${activeDetailId}` } };
        }
        if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
        throw new Error("unexpected Simple Search message");
      }
    });
    let nowMs = 0;
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        if ((value as { kind?: unknown }).kind === "finish_job") {
          const request = value as Record<string, unknown>;
          this.requests.push(request);
          return { protocol_version: "1.0", kind: "job_result", job_id: request.job_id as string, status: "partial", retained_count: 4, report_available: true, report_file: "index.html" } as NativeResponse;
        }
        return super.request(value);
      }
    }(noteIds);
    const controller = new JobController(dependencies(native, {
      now: () => nowMs,
      sleep: async (duration) => { nowMs += duration; },
      searchRiskState: { readLastSearchBatchStartedAt: async () => undefined, writeLastSearchBatchStartedAt: async () => undefined }
    }));

    await controller.collectBatch(searchTab, { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" });

    expect(chromeMock.update.mock.calls.map((call) => (call[1] as { url: string }).url.slice((call[1] as { url: string }).url.lastIndexOf("/") + 1))).toEqual(noteIds);
    expect(native.requests.filter((request) => request.kind === "candidate_unavailable")).toEqual([
      expect.objectContaining({ note_id: "note_two", source_position: 2, reason: "detail_unavailable" })
    ]);
    expect(native.requests.filter((request) => request.kind === "candidate_snapshot").map((request) => request.note_id)).toEqual([
      ...noteIds, "note_one", "note_three", "note_four", "note_five"
    ]);
  });

  test("rejects page-order collection from an account page without opening a native job or queue", async () => {
    const chromeMock = fakeChrome({ onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") return { context: { kind: "account", canonicalUrl: "https://www.xiaohongshu.com/user/profile/author_123" } };
      throw new Error("page-order must not scan an account page");
    } });
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native));

    await expect(controller.collectBatch({ id: 1 } as chrome.tabs.Tab, {
      requestedCount: 5,
      candidateScanLimit: 5,
      selectionOrder: "page_order"
    })).rejects.toMatchObject({ code: "unsupported_batch_tab" });

    expect(native.requests).toHaveLength(0);
    expect(chromeMock.create).not.toHaveBeenCalled();
    expect(chromeMock.sendMessage).not.toHaveBeenCalledWith(1, { kind: "scroll_candidates" });
  });

  test("rejects a page-order publication cutoff before it sends begin_job", async () => {
    const chromeMock = fakeChrome({ sourceKind: "search" });
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native));

    await expect(controller.collectBatch(searchTab, {
      requestedCount: 5,
      candidateScanLimit: 5,
      publicationCutoff: "2026-09-01T00:00:00+08:00",
      selectionOrder: "page_order"
    })).rejects.toMatchObject({ code: "invalid_batch_bounds" });

    expect(native.requests).toHaveLength(0);
    expect(chromeMock.create).not.toHaveBeenCalled();
  });

  test("persists the Simple Search start across controller reconstruction for the sixty-second cooldown", async () => {
    let nowMs = 0;
    let lastStartedAt: number | undefined;
    const searchRiskState = {
      readLastSearchBatchStartedAt: async () => lastStartedAt,
      writeLastSearchBatchStartedAt: async (value: number) => { lastStartedAt = value; }
    };
    fakeChrome({ sourceKind: "search" });
    const first = new JobController(dependencies(new FakeNativeTransport([]), {
      now: () => nowMs,
      sleep: async (duration) => { nowMs += duration; },
      searchRiskState
    }));

    await first.collectBatch(searchTab, { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" });

    const secondNative = new FakeNativeTransport([]);
    const second = new JobController(dependencies(secondNative, {
      now: () => nowMs,
      searchRiskState
    }));
    await expect(second.collectBatch(searchTab, { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" })).rejects.toMatchObject({ code: "cooldown" });
    expect(secondNative.requests).toHaveLength(0);
  });

  test("Simple Search rejects counts other than five or ten before it starts a native job", async () => {
    const chromeMock = fakeChrome({ sourceKind: "search" });
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native));

    await expect(controller.collectBatch(searchTab, {
      requestedCount: 4,
      candidateScanLimit: 4,
      selectionOrder: "page_order"
    })).rejects.toMatchObject({ code: "invalid_batch_bounds" });

    expect(native.requests).toHaveLength(0);
    expect(chromeMock.create).not.toHaveBeenCalled();
  });

  test("Simple Search uses the injected clock to keep detail navigations at least six seconds apart", async () => {
    let nowMs = 0;
    let queueInspection = 0;
    const noteIds = ["note_one", "note_two", "note_three", "note_four", "note_five"];
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "scan_search") return searchScan(noteIds);
      if (message.kind === "inspect_page" && tabId === 99) {
        const noteId = noteIds[queueInspection++] as string;
        return { context: { kind: "current", noteId, canonicalUrl: `https://www.xiaohongshu.com/explore/${noteId}` } };
      }
      if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
      throw new Error("unexpected content message");
    } });
    const navigationTimes: number[] = [];
    chromeMock.update.mockImplementation(async (tabId, changes) => {
      navigationTimes.push(nowMs);
      return { id: tabId, url: changes.url };
    });
    const native = new FakeNativeTransport(noteIds);
    const controller = new JobController(dependencies(native, {
      now: () => nowMs,
      delay: (minimum) => minimum,
      sleep: async (duration) => { nowMs += duration; }
    }));

    await controller.collectBatch(searchTab, {
      requestedCount: 5,
      candidateScanLimit: 5,
      selectionOrder: "page_order"
    });

    expect(navigationTimes).toEqual([0, 6_000, 12_000, 18_000, 24_000]);
  });

  test("Simple Search makes exactly two source scroll attempts when fewer than five cards are available", async () => {
    let scrolls = 0;
    let scans = 0;
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "scan_search") {
        scans += 1;
        if (scans === 1) return searchScan(["note_first", "note_second"]);
        if (scans === 2) return searchScan(["note_first", "note_virtualized"]);
        return searchScan([]);
      }
      if (message.kind === "scroll_candidates") {
        scrolls += 1;
        return { scrolled: true };
      }
      throw new Error("no detail navigation is allowed without candidates");
    } });
    const native = new FakeNativeTransport([]);
    const controller = new JobController(dependencies(native, {
      searchRiskState: { readLastSearchBatchStartedAt: async () => undefined, writeLastSearchBatchStartedAt: async () => undefined }
    }));

    await controller.collectBatch(searchTab, { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" });

    expect(scrolls).toBe(2);
    expect(chromeMock.update).not.toHaveBeenCalled();
    expect(native.requests.filter((request) => request.kind === "candidate_snapshot").map((request) => request.source_position)).toEqual([1, 2, 3]);
  });

  test("Simple Search visits no more than ten frozen detail routes", async () => {
    let queueInspection = 0;
    const noteIds = Array.from({ length: 11 }, (_, index) => `note_${index + 1}`);
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "scan_search") return searchScan(noteIds);
      if (message.kind === "inspect_page" && tabId === 99) {
        const noteId = noteIds[queueInspection++] as string;
        return { context: { kind: "current", noteId, canonicalUrl: `https://www.xiaohongshu.com/explore/${noteId}` } };
      }
      if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
      throw new Error("unexpected content message");
    } });
    let nowMs = 0;
    const controller = new JobController(dependencies(new FakeNativeTransport(noteIds.slice(0, 10)), {
      now: () => nowMs,
      sleep: async (duration) => { nowMs += duration; },
      searchRiskState: { readLastSearchBatchStartedAt: async () => undefined, writeLastSearchBatchStartedAt: async () => undefined }
    }));

    await controller.collectBatch(searchTab, { requestedCount: 10, candidateScanLimit: 10, selectionOrder: "page_order" });

    expect(chromeMock.update).toHaveBeenCalledTimes(10);
    expect(chromeMock.update).not.toHaveBeenCalledWith(99, { url: "https://www.xiaohongshu.com/explore/note_11" });
  });

  test.each(["login_required", "challenge_detected"] as const)("Simple Search %s halts before a second detail navigation", async (block) => {
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "scan_search") return searchScan(["note_one", "note_two", "note_three", "note_four", "note_five"]);
      if (message.kind === "inspect_page" && tabId === 99) return { block, context: { kind: "current", noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" } };
      throw new Error("detail projection must not follow a terminal block");
    } });
    const native = new FakeNativeTransport(["note_one", "note_two"]);
    const controller = new JobController(dependencies(native, {
      searchRiskState: { readLastSearchBatchStartedAt: async () => undefined, writeLastSearchBatchStartedAt: async () => undefined }
    }));

    await expect(controller.collectBatch(searchTab, { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" })).rejects.toMatchObject({ code: block });

    expect(chromeMock.update).toHaveBeenCalledExactlyOnceWith(99, { url: "https://www.xiaohongshu.com/explore/note_one" });
    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
  });

  test("Simple Search keeps one detail worker and a user stop prevents the second navigation", async () => {
    let releaseNavigation: ((tab: chrome.tabs.Tab) => void) | undefined;
    const pendingNavigation = new Promise<chrome.tabs.Tab>((resolve) => { releaseNavigation = resolve; });
    const chromeMock = fakeChrome({
      sourceKind: "search",
      tabsUpdate: () => pendingNavigation,
      onMessage: (_tabId, message) => {
        if (message.kind === "inspect_page") return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
        if (message.kind === "scan_search") return searchScan(["note_one", "note_two", "note_three", "note_four", "note_five"]);
        throw new Error("a stopped first navigation must not inspect or project a detail page");
      }
    });
    const native = new FakeNativeTransport(["note_one", "note_two", "note_three", "note_four", "note_five"]);
    const controller = new JobController(dependencies(native, {
      searchRiskState: { readLastSearchBatchStartedAt: async () => undefined, writeLastSearchBatchStartedAt: async () => undefined }
    }));

    const run = controller.collectBatch(searchTab, { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" });
    await vi.waitFor(() => expect(chromeMock.update).toHaveBeenCalledExactlyOnceWith(99, { url: "https://www.xiaohongshu.com/explore/note_one" }));
    await controller.stop();
    expect(chromeMock.update).toHaveBeenCalledTimes(1);
    releaseNavigation?.({ id: 99, url: "https://www.xiaohongshu.com/explore/note_one" } as chrome.tabs.Tab);
    await run;

    expect(chromeMock.update).toHaveBeenCalledTimes(1);
    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
  });

  test("Simple Search accepts a frozen stopped receipt and exposes its report", async () => {
    let releaseNavigation: ((tab: chrome.tabs.Tab) => void) | undefined;
    const pendingNavigation = new Promise<chrome.tabs.Tab>((resolve) => { releaseNavigation = resolve; });
    const chromeMock = fakeChrome({
      sourceKind: "search", tabsUpdate: () => pendingNavigation,
      onMessage: (_tabId, message) => {
        if (message.kind === "inspect_page") return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
        if (message.kind === "scan_search") return searchScan(["note_one", "note_two", "note_three", "note_four", "note_five"]);
        throw new Error("a stopped run must not inspect a detail");
      }
    });
    const native = new FakeNativeTransport(["note_one", "note_two", "note_three", "note_four", "note_five"]);
    const request = native.request.bind(native);
    vi.spyOn(native, "request").mockImplementation(async (value) => {
      if ((value as { kind?: unknown }).kind === "stop_job") return { protocol_version: "1.0", kind: "job_result", job_id: "job_123", status: "stopped", retained_count: 5, report_available: true, report_file: "index.html" } as NativeResponse;
      return request(value);
    });
    const controller = new JobController(dependencies(native, { searchRiskState: { readLastSearchBatchStartedAt: async () => undefined, writeLastSearchBatchStartedAt: async () => undefined } }));
    const run = controller.collectBatch(searchTab, { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" });
    await vi.waitFor(() => expect(chromeMock.update).toHaveBeenCalledOnce());
    await expect(controller.stop()).resolves.toBeUndefined();
    releaseNavigation?.({ id: 99, url: "https://www.xiaohongshu.com/explore/note_one" } as chrome.tabs.Tab);
    await run;
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({ xhs_job_progress: expect.objectContaining({ phase: "stopped", report_available: true }) });
  });

  test("requires pregranted origins and rejects N/M values outside the fixed bounds", async () => {
    fakeChrome({ optionalPermissions: false, sourceKind: "search" });
    const controller = new JobController(dependencies(new FakeNativeTransport()));

    await expect(controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 })).rejects.toMatchObject({ code: "optional_permission_required" });
    await expect(controller.collectBatch(searchTab, { requestedCount: 21, candidateScanLimit: 100 })).rejects.toMatchObject({ code: "invalid_batch_bounds" });
    await expect(controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 101 })).rejects.toMatchObject({ code: "invalid_batch_bounds" });
  });

  test("a source-tab close while native connect is pending cannot inject or navigate afterward", async () => {
    const chromeMock = fakeChrome({ sourceKind: "search" });
    const native = new DelayedConnectNativeTransport();
    const controller = new JobController(dependencies(native));

    const run = controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 });
    await vi.waitFor(() => expect(native.connectStarted).toHaveBeenCalledExactlyOnceWith());
    chromeMock.emitRemoved(1);
    native.release();
    await run;

    expect(chromeMock.executeScript).not.toHaveBeenCalled();
    expect(chromeMock.create).not.toHaveBeenCalled();
    expect(chromeMock.update).not.toHaveBeenCalled();
  });

  test("a stop during pending queue creation records and cleans only the resolved owned tab", async () => {
    let releaseCreate: ((tab: chrome.tabs.Tab) => void) | undefined;
    const pendingCreate = new Promise<chrome.tabs.Tab>((resolve) => { releaseCreate = resolve; });
    const chromeMock = fakeChrome({
      tabsCreate: () => pendingCreate,
      onMessage: (_tabId, message) => {
        if (message.kind === "inspect_page") return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
        if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }] };
        throw new Error("no detail step may begin after pending queue creation is stopped");
      }
    });
    const native = new FakeNativeTransport(["note_one"]);
    const controller = new JobController(dependencies(native));

    const run = controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 });
    await vi.waitFor(() => expect(chromeMock.create).toHaveBeenCalledExactlyOnceWith({ url: "about:blank", active: false }));
    await controller.stop();
    releaseCreate?.({ id: 99, url: "about:blank" } as chrome.tabs.Tab);
    await run;

    expect(chromeMock.update).not.toHaveBeenCalled();
    expect(chromeMock.executeScript).toHaveBeenCalledExactlyOnceWith({ target: { tabId: 1 }, files: ["content-script.js"] });
    expect(chromeMock.remove).toHaveBeenCalledExactlyOnceWith(99);
    expect(chromeMock.remove).not.toHaveBeenCalledWith(1);
    expect(native.requests.map((request) => request.kind)).not.toContain("candidate_snapshot");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
  });

  test("a source close during pending queue navigation performs no detail step and cleans only its queue", async () => {
    let releaseUpdate: ((tab: chrome.tabs.Tab) => void) | undefined;
    const pendingUpdate = new Promise<chrome.tabs.Tab>((resolve) => { releaseUpdate = resolve; });
    const chromeMock = fakeChrome({
      tabsUpdate: () => pendingUpdate,
      onMessage: (_tabId, message) => {
        if (message.kind === "inspect_page") return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
        if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }] };
        throw new Error("no queue inspection or detail projection may follow a stopped navigation");
      }
    });
    const native = new FakeNativeTransport(["note_one"]);
    const controller = new JobController(dependencies(native));

    const run = controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 });
    await vi.waitFor(() => expect(chromeMock.update).toHaveBeenCalledExactlyOnceWith(99, { url: "https://www.xiaohongshu.com/explore/note_one" }));
    chromeMock.emitRemoved(1);
    releaseUpdate?.({ id: 99, url: "https://www.xiaohongshu.com/explore/note_one" } as chrome.tabs.Tab);
    await run;

    expect(chromeMock.executeScript).toHaveBeenCalledExactlyOnceWith({ target: { tabId: 1 }, files: ["content-script.js"] });
    expect(chromeMock.remove).toHaveBeenCalledExactlyOnceWith(99);
    expect(chromeMock.remove).not.toHaveBeenCalledWith(1);
    expect(native.requests.map((request) => request.kind)).not.toContain("candidate_snapshot");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
  });

  test("rejects a native selection that exceeds requested N before any media transfer", async () => {
    let queueInspection = 0;
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "inspect_page" && tabId === 99) {
        const noteId = ["note_one", "note_two"][queueInspection++];
        return { context: { kind: "current", noteId, canonicalUrl: `https://www.xiaohongshu.com/explore/${noteId}` } };
      }
      if (message.kind === "discover_candidates") return { candidates: [
        { sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" },
        { sourcePosition: 2, noteId: "note_two", canonicalUrl: "https://www.xiaohongshu.com/explore/note_two" }
      ] };
      if (message.kind === "scroll_candidates") return { scrolled: true };
      if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
      throw new Error("unexpected content message");
    } });
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        const request = value as Record<string, unknown>;
        if (request.kind === "finish_scan") {
          this.requests.push(request);
          return {
            protocol_version: "1.0", kind: "selection_result", job_id: request.job_id as string,
            scanned_count: 2, eligible_count: 2, selected_count: 2, status: "complete",
            selected: [{ note_id: "note_one", selection_rank: 1 }, { note_id: "note_two", selection_rank: 2 }]
          } as NativeResponse;
        }
        return super.request(value);
      }
    }(["note_one", "note_two"]);
    const controller = new JobController(dependencies(native));

    await expect(controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 2 })).rejects.toMatchObject({ code: "native_host_error" });

    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("media_begin");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
    expect(chromeMock.remove).toHaveBeenCalledExactlyOnceWith(99);
  });

  test("maps a source-page login/challenge scroll response to its finite terminal code", async () => {
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }] };
      if (message.kind === "scroll_candidates") return { error: "challenge_detected" };
      throw new Error("detail projection must not run after a source-page block");
    } });
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native));

    await expect(controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 2 })).rejects.toMatchObject({ code: "challenge_detected" });

    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
    expect(chromeMock.create).not.toHaveBeenCalled();
  });

  test("revalidates source search and account identity after discovery without reinjecting", async () => {
    for (const source of [
      { kind: "search" as const, canonicalUrl: "https://www.xiaohongshu.com/search_result", driftedKind: "account" as const, driftedUrl: "https://www.xiaohongshu.com/user/profile/author_other", tab: searchTab },
      { kind: "account" as const, canonicalUrl: "https://www.xiaohongshu.com/user/profile/author_123", driftedKind: "account" as const, driftedUrl: "https://www.xiaohongshu.com/user/profile/author_other", tab: { id: 1 } as chrome.tabs.Tab }
    ]) {
      let inspections = 0;
      const chromeMock = fakeChrome({ onMessage: (_tabId, message) => {
        if (message.kind === "inspect_page") {
          inspections += 1;
          return inspections < 3
            ? { context: { kind: source.kind, canonicalUrl: source.canonicalUrl } }
            : { context: { kind: source.driftedKind, canonicalUrl: source.driftedUrl } };
        }
        if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }] };
        if (message.kind === "scroll_candidates") return { scrolled: true };
        throw new Error("detail projection is unreachable after source identity drift");
      } });
      const controller = new JobController(dependencies(new FakeNativeTransport([])));

      await expect(controller.collectBatch(source.tab, { requestedCount: 1, candidateScanLimit: 2 })).rejects.toMatchObject({ code: "identity_mismatch" });

      expect(chromeMock.executeScript).toHaveBeenCalledExactlyOnceWith({ target: { tabId: 1 }, files: ["content-script.js"] });
      expect(chromeMock.create).not.toHaveBeenCalled();
    }
  });

  test("deduplicates bounded candidates, uses one inactive owned tab, serially navigates canonical URLs, and waits within both ranges", async () => {
    let discoveryRound = 0;
    let queueInspection = 0;
    const chromeMock = fakeChrome({
      sourceKind: "search",
      onMessage: (tabId, message) => {
        if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
        if (message.kind === "inspect_page" && tabId === 99) {
          const noteId = ["note_one", "note_two", "note_three"][queueInspection++];
          return { context: { kind: "current", noteId, canonicalUrl: `https://www.xiaohongshu.com/explore/${noteId}` } };
        }
        if (message.kind === "discover_candidates") {
          discoveryRound += 1;
          return { candidates: discoveryRound === 1
            ? [
              { sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" },
              { sourcePosition: 2, noteId: "note_two", canonicalUrl: "https://www.xiaohongshu.com/explore/note_two" }
            ]
            : [
              { sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" },
              { sourcePosition: 19, noteId: "note_three", canonicalUrl: "https://www.xiaohongshu.com/explore/note_three" }
            ] };
        }
        if (message.kind === "scroll_candidates") return { scrolled: true };
        if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
        throw new Error("unexpected content message");
      }
    });
    const waits: number[] = [];
    const native = new FakeNativeTransport(["note_two"]);
    const controller = new JobController(dependencies(native, {
      delay: (minimum, maximum) => {
        expect([[2000, 5000], [3000, 7000]]).toContainEqual([minimum, maximum]);
        return minimum;
      },
      sleep: async (duration) => { waits.push(duration); }
    }));

    await controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 3 });

    expect(chromeMock.create).toHaveBeenCalledExactlyOnceWith({ url: "about:blank", active: false });
    expect(chromeMock.update.mock.calls.map((call) => call[1])).toEqual([
      { url: "https://www.xiaohongshu.com/explore/note_one" },
      { url: "https://www.xiaohongshu.com/explore/note_two" },
      { url: "https://www.xiaohongshu.com/explore/note_three" }
    ]);
    expect(waits).toEqual([2000, 3000, 3000, 3000]);
    expect(chromeMock.remove).toHaveBeenCalledExactlyOnceWith(99);
    expect(native.requests.filter((request) => request.kind === "candidate_snapshot")).toHaveLength(3);
    expect(native.requests.filter((request) => request.kind === "candidate_snapshot").map((request) => request.source_position)).toEqual([1, 2, 19]);
    expect(native.requests.filter((request) => request.kind === "media_begin")).toHaveLength(1);
  });

  test("stops source-page scrolling after exactly three no-new candidate rounds", async () => {
    let discovers = 0;
    let scrolls = 0;
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "discover_candidates") {
        discovers += 1;
        return { candidates: [] };
      }
      if (message.kind === "scroll_candidates") {
        scrolls += 1;
        return { scrolled: true };
      }
      throw new Error("detail projection is unreachable without candidates");
    } });
    const controller = new JobController(dependencies(new FakeNativeTransport([])));

    await controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 });

    expect(discovers).toBe(3);
    expect(scrolls).toBe(2);
    expect(chromeMock.create).toHaveBeenCalledExactlyOnceWith({ url: "about:blank", active: false });
    expect(chromeMock.remove).toHaveBeenCalledExactlyOnceWith(99);
  });

  test("a user stop during a queue wait stays stopped, sends no finish_job, and removes only the owned queue tab", async () => {
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }] };
      if (message.kind === "scroll_candidates") return { scrolled: true };
      if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
      throw new Error("unexpected content message");
    } });
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native, {
      sleep: () => new Promise<void>(() => undefined)
    }));

    const run = controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 });
    await vi.waitFor(() => expect(native.requests.map((request) => request.kind)).toContain("begin_job"));
    await controller.stop();
    const settled = await Promise.race([run.then(() => true), new Promise<false>((resolve) => setTimeout(() => resolve(false), 25))]);
    expect(settled).toBe(true);

    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
    expect(chromeMock.remove).toHaveBeenCalledExactlyOnceWith(99);
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({ phase: "stopped" })
    });
  });

  test("closes a terminal native session when a user stop receives a partial result", async () => {
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }] };
      if (message.kind === "scroll_candidates") return { scrolled: true };
      throw new Error("detail projection is unreachable without candidates");
    } });
    const native = new PartialStopNativeTransport();
    const controller = new JobController(dependencies(native, { sleep: () => new Promise<void>(() => undefined) }));

    const run = controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 });
    await vi.waitFor(() => expect(native.requests.map((request) => request.kind)).toContain("begin_job"));
    await expect(controller.stop()).rejects.toMatchObject({ code: "native_host_error" });
    await run;

    expect(native.completed).toHaveBeenCalledOnce();
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({ phase: "error", error: "native_host_interrupted" })
    });
  });

  test("aborts an in-flight media download on user stop, confirms it, and never finishes afterward", async () => {
    fakeChrome();
    const native = new FakeNativeTransport();
    let signal: AbortSignal | undefined;
    const controller = new JobController(dependencies(native, {
      fetchMedia: async (_sourceUrl, _role, receivedSignal) => {
        signal = receivedSignal;
        return new Promise(() => undefined);
      }
    }));

    const run = controller.collectCurrent(currentTab);
    await vi.waitFor(() => expect(signal).toBeInstanceOf(AbortSignal));
    await controller.stop();
    const settled = await Promise.race([run.then(() => true), new Promise<false>((resolve) => setTimeout(() => resolve(false), 25))]);

    expect(settled).toBe(true);
    expect(signal?.aborted).toBe(true);
    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
  });

  test("a user stop with a lost pending native request reports interruption instead of stopped", async () => {
    const chromeMock = fakeChrome();
    const native = new InterruptingMediaNativeTransport();
    const controller = new JobController(dependencies(native));

    const run = controller.collectCurrent(currentTab);
    await vi.waitFor(() => expect(native.requests.map((request) => request.kind)).toContain("media_chunk"));
    await expect(controller.stop()).rejects.toMatchObject({ code: "native_host_interrupted" });
    await run;

    expect(native.interrupted).toHaveBeenCalledExactlyOnceWith();
    expect(native.requests.map((request) => request.kind)).not.toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({ phase: "error", error: "native_host_interrupted" })
    });
  });

  test("retains one bounded detail-unavailable candidate and continues the remaining batch", async () => {
    let queueInspection = 0;
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "discover_candidates") return { candidates: [
        { sourcePosition: 1, noteId: "note_unavailable", canonicalUrl: "https://www.xiaohongshu.com/explore/note_unavailable" },
        { sourcePosition: 2, noteId: "note_selected", canonicalUrl: "https://www.xiaohongshu.com/explore/note_selected" }
      ] };
      if (message.kind === "inspect_page" && tabId === 99) {
        queueInspection += 1;
        if (queueInspection === 1) return { context: null };
        return { context: { kind: "current", noteId: "note_selected", canonicalUrl: "https://www.xiaohongshu.com/explore/note_selected" } };
      }
      if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
      throw new Error("unexpected content message");
    } });
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        const request = value as Record<string, unknown>;
        if (request.kind === "finish_scan") {
          this.requests.push(request);
          return {
            protocol_version: "1.0", kind: "selection_result", job_id: request.job_id as string,
            scanned_count: 2, eligible_count: 1, selected_count: 1, status: "partial",
            selected: [{ note_id: "note_selected", selection_rank: 1 }]
          } as NativeResponse;
        }
        if (request.kind === "finish_job") {
          this.requests.push(request);
          return { protocol_version: "1.0", kind: "job_result", job_id: request.job_id as string, status: "partial", retained_count: 1, report_available: true, report_file: "index.html" } as NativeResponse;
        }
        return super.request(value);
      }
    }(["note_selected"]);
    const controller = new JobController(dependencies(native));

    await controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 2 });

    expect(chromeMock.update.mock.calls.map((call) => call[1])).toEqual([
      { url: "https://www.xiaohongshu.com/explore/note_unavailable" },
      { url: "https://www.xiaohongshu.com/explore/note_selected" }
    ]);
    expect(native.requests.filter((request) => request.kind === "candidate_unavailable")).toEqual([
      expect.objectContaining({ source_position: 1, note_id: "note_unavailable", reason: "detail_unavailable" })
    ]);
    expect(native.requests.filter((request) => request.kind === "candidate_snapshot")).toEqual([
      expect.objectContaining({ source_position: 2, note_id: "note_selected" })
    ]);
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({ phase: "partial", inspected: 2, selected: 1 })
    });
  });

  test.each(["tabs.update", "scripting.executeScript", "tabs.sendMessage"] as const)(
    "normalizes one rejected queue %s operation into an unavailable candidate and continues", async (operation) => {
      let queueInspection = 0;
      let failed = false;
      const chromeMock = fakeChrome({
        sourceKind: "search",
        tabsUpdate: async (tabId, changes) => {
          if (operation === "tabs.update" && !failed) {
            failed = true;
            throw new Error("queue navigation rejected");
          }
          return { id: tabId, url: changes.url } as chrome.tabs.Tab;
        },
        executeScript: async (details) => {
          if (operation === "scripting.executeScript" && details.target.tabId === 99 && !failed) {
            failed = true;
            throw new Error("queue injection rejected");
          }
          return [];
        },
        onMessage: (tabId, message) => {
          if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
          if (message.kind === "discover_candidates") return { candidates: [
            { sourcePosition: 1, noteId: "note_unavailable", canonicalUrl: "https://www.xiaohongshu.com/explore/note_unavailable" },
            { sourcePosition: 2, noteId: "note_selected", canonicalUrl: "https://www.xiaohongshu.com/explore/note_selected" }
          ] };
          if (message.kind === "inspect_page" && tabId === 99) {
            if (operation === "tabs.sendMessage" && !failed) {
              failed = true;
              throw new Error("queue message rejected");
            }
            queueInspection += 1;
            return { context: { kind: "current", noteId: "note_selected", canonicalUrl: "https://www.xiaohongshu.com/explore/note_selected" } };
          }
          if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
          throw new Error("unexpected content message");
        }
      });
      const native = new class extends FakeNativeTransport {
        override async request(value: unknown): Promise<NativeResponse> {
          const request = value as Record<string, unknown>;
          if (request.kind === "finish_scan") {
            this.requests.push(request);
            return {
              protocol_version: "1.0", kind: "selection_result", job_id: request.job_id as string,
              scanned_count: 2, eligible_count: 1, selected_count: 1, status: "partial",
              selected: [{ note_id: "note_selected", selection_rank: 1 }]
            } as NativeResponse;
          }
          if (request.kind === "finish_job") {
            this.requests.push(request);
            return { protocol_version: "1.0", kind: "job_result", job_id: request.job_id as string, status: "partial", retained_count: 1, report_available: true, report_file: "index.html" } as NativeResponse;
          }
          return super.request(value);
        }
      }(["note_selected"]);
      const controller = new JobController(dependencies(native));

      await controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 2 });

      expect(failed).toBe(true);
      expect(native.requests.filter((request) => request.kind === "candidate_unavailable")).toEqual([
        expect.objectContaining({ source_position: 1, note_id: "note_unavailable", reason: "detail_unavailable" })
      ]);
      expect(native.requests.filter((request) => request.kind === "candidate_snapshot")).toEqual([
        expect.objectContaining({ source_position: 2, note_id: "note_selected" })
      ]);
      expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
        xhs_job_progress: expect.objectContaining({ phase: "partial", inspected: 2, selected: 1 })
      });
      expect(queueInspection).toBe(1);
    }
  );

  test("persists only finite native ranking/progress counts and transfers selected notes by selection rank", async () => {
    let queueInspection = 0;
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "inspect_page" && tabId === 99) {
        const noteId = ["note_one", "note_two", "note_three"][queueInspection++];
        return { context: { kind: "current", noteId, canonicalUrl: `https://www.xiaohongshu.com/explore/${noteId}` } };
      }
      if (message.kind === "discover_candidates") return { candidates: [
        { sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" },
        { sourcePosition: 2, noteId: "note_two", canonicalUrl: "https://www.xiaohongshu.com/explore/note_two" },
        { sourcePosition: 3, noteId: "note_three", canonicalUrl: "https://www.xiaohongshu.com/explore/note_three" }
      ] };
      if (message.kind === "scroll_candidates") return { scrolled: true };
      if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
      throw new Error("unexpected content message");
    } });
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        const request = value as Record<string, unknown>;
        if (request.kind === "finish_scan") {
          this.requests.push(request);
          return {
            protocol_version: "1.0", kind: "selection_result", job_id: request.job_id as string,
            scanned_count: 3, eligible_count: 3, selected_count: 3, status: "complete",
            selected: [
              { note_id: "note_two", selection_rank: 2 },
              { note_id: "note_three", selection_rank: 3 },
              { note_id: "note_one", selection_rank: 1 }
            ]
          } as NativeResponse;
        }
        return super.request(value);
      }
    }(["note_one", "note_two", "note_three"]);
    const controller = new JobController(dependencies(native));

    await controller.collectBatch(searchTab, { requestedCount: 3, candidateScanLimit: 3 });

    expect(native.requests.filter((request) => request.kind === "media_begin").map((request) => request.note_id)).toEqual(["note_one", "note_two", "note_three"]);
    expect(native.requests.filter((request) => request.kind === "media_begin").map((request) => request.sequence)).toEqual([1, 2, 3]);
    expect(chromeMock.storageSet).toHaveBeenCalledWith({
      xhs_job_progress: expect.objectContaining({ eligible: 3, selected: 3 })
    });
    expect(JSON.stringify(chromeMock.storageSet.mock.calls)).not.toMatch(/https?:\/\/|sourceUrl|token|selector|Fixture detail/i);
  });

  test("an owned queue tab closing is finite and never removes the invoking source tab", async () => {
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (_tabId, message) => {
      if (message.kind === "inspect_page") return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }] };
      if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
      throw new Error("unexpected content message");
    } });
    let release: (() => void) | undefined;
    const controller = new JobController(dependencies(new FakeNativeTransport(), { sleep: () => new Promise<void>((resolve) => { release = resolve; }) }));

    const run = controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 });
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    chromeMock.emitRemoved(99);
    release?.();
    await run;

    expect(chromeMock.remove).not.toHaveBeenCalledWith(1);
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({ xhs_job_progress: expect.objectContaining({ phase: "stopped" }) });
  });

  test("an owned queue tab closing interrupts a pending native media chunk and does not delete the source", async () => {
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "inspect_page" && tabId === 99) return { context: { kind: "current", noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" } };
      if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }] };
      if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
      throw new Error("unexpected content message");
    } });
    const native = new InterruptingMediaNativeTransport(["note_one"]);
    const controller = new JobController(dependencies(native));

    const run = controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 });
    await vi.waitFor(() => expect(native.requests.map((request) => request.kind)).toContain("media_chunk"));
    chromeMock.emitRemoved(99);
    await run;

    expect(native.interrupted).toHaveBeenCalledExactlyOnceWith();
    expect(native.requests.map((request) => request.kind)).not.toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
    expect(chromeMock.remove).not.toHaveBeenCalledWith(1);
    expect(chromeMock.storageSet).toHaveBeenLastCalledWith({
      xhs_job_progress: expect.objectContaining({ phase: "stopped", error: "queue_tab_closed" })
    });
  });

  test("a login or challenge observed after queue navigation stops before detail projection or finish", async () => {
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }] };
      if (message.kind === "inspect_page" && tabId === 99) return { context: { kind: "current", noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }, block: "login_required" };
      throw new Error("detail projection must not run after a login block");
    } });
    const native = new FakeNativeTransport();
    const controller = new JobController(dependencies(native));

    await expect(controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 })).rejects.toMatchObject({ code: "login_required" });

    expect(native.requests.map((request) => request.kind)).toContain("stop_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
    expect(chromeMock.remove).toHaveBeenCalledExactlyOnceWith(99);
  });

  test("a native-port interruption during batch projection is finite, does not retry, and never finishes", async () => {
    const chromeMock = fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" }] };
      if (message.kind === "inspect_page" && tabId === 99) return { context: { kind: "current", noteId: "note_one", canonicalUrl: "https://www.xiaohongshu.com/explore/note_one" } };
      if (message.kind === "project_detail") return projection(message.noteId as string, message.sourcePosition as number);
      throw new Error("unexpected content message");
    } });
    const native = new class extends FakeNativeTransport {
      override async request(value: unknown): Promise<NativeResponse> {
        if ((value as { kind?: unknown }).kind === "candidate_snapshot") throw new NativeClientError("native_host_interrupted");
        return super.request(value);
      }
    }();
    const controller = new JobController(dependencies(native));

    await expect(controller.collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 })).rejects.toMatchObject({ code: "native_host_interrupted" });

    expect(native.requests.map((request) => request.kind)).not.toContain("finish_job");
    expect(native.requests.map((request) => request.kind)).not.toContain("stop_job");
    expect(chromeMock.remove).toHaveBeenCalledExactlyOnceWith(99);
  });
});

describe("video current-note integration", () => {
  function videoChrome(expectedSize = 3, unavailable = false) {
    return fakeChrome({onMessage: (_tab, message) => {
      if (message.kind === "inspect_page") return {context:{kind:"current",noteId:"note_123",canonicalUrl:"https://www.xiaohongshu.com/explore/note_123"}};
      const base = projection("note_123",1);
      return {...base,snapshot:{...(base.snapshot as object),note_type:"video",media_slots:[{note_id:"note_123",role:"video",position:1}]},media:unavailable ? [] : [{note_id:"note_123",role:"video",position:1,sourceUrl:"https://sns-video-v6.xhscdn.com/stream/a/movie.mp4?sign=abc&t=1",expectedSizeBytes:expectedSize}],video:{durationMs:unavailable ? 409110 : 135867, ...(unavailable ? {unavailableReason:"size_limit"} : {}),subtitle:{sourceKind:"independent_srt",sourceUrl:"https://sns-subtitle-s2.xhscdn.com/subtitle/a/caption.srt"}}};
    }});
  }
  test.each(["media_end","finish_job"])("source closure cannot interrupt local %s after all video bytes are acknowledged", async phase => {
    const browser = videoChrome();
    let release: (()=>void) | undefined;
    let finishing = false;
    const barrier = new Promise<void>(resolve => {release=resolve;});
    const native = new class extends FakeNativeTransport {
      override async request(value:unknown):Promise<NativeResponse> {
        if ((value as {kind:string}).kind === phase) {finishing=true;await barrier;}
        return super.request(value);
      }
    }();
    const controller = new JobController(dependencies(native,{fetchMedia:async () => ({...imageResult,mimeType:"video/mp4"})}));
    const run = controller.collectCurrent(currentTab);
    await vi.waitFor(() => expect(finishing).toBe(true));
    browser.emitRemoved(1);
    release?.();
    await run;
    expect(native.requests.some(x=>x.kind === "stop_job")).toBe(false);
    expect(browser.storageSet).toHaveBeenLastCalledWith({xhs_job_progress:expect.objectContaining({report_available:true})});
  });
  test("busy worker rejection never replaces the previously stored report pointer", async () => {
    const browser = videoChrome();
    const native = new class extends FakeNativeTransport {
      override async request(value:unknown):Promise<NativeResponse> {return {protocol_version:"1.0",kind:"error",job_id:(value as {job_id:string}).job_id,code:"job_in_progress",fatal:false};}
    }();
    await expect(new JobController(dependencies(native)).collectCurrent(currentTab)).rejects.toMatchObject({code:"job_in_progress"});
    expect(browser.storageSet).not.toHaveBeenCalled();
  });
  test("transfers signed video, captures independent SRT, persists report before ASR, and detaches source listener", async () => {
    const browser = videoChrome();
    const native = new class extends FakeNativeTransport {
      override async request(value:unknown):Promise<NativeResponse> {
        const response = await super.request(value);
        return response.kind === "job_result" ? {...response,video_processing:{status:"running"}} : response;
      }
    }();
    const srt = "1\n00:00:00,000 --> 00:00:01,000\n你好\n";
    const fetchMedia = vi.fn(async () => ({...imageResult,mimeType:"video/mp4" as const}));
    const controller = new JobController(dependencies(native,{fetchMedia,fetchSubtitle:async () => ({text:srt})}));
    await controller.collectCurrent(currentTab);
    expect(fetchMedia).toHaveBeenCalledWith(expect.stringContaining("?sign="),"video",expect.any(AbortSignal));
    expect(native.requests.find(x => x.kind === "finish_job")?.video_metadata).toEqual({note_id:"note_123",duration_ms:135867,subtitle_status:"available",subtitle_srt:srt});
    expect(browser.storageSet).toHaveBeenLastCalledWith({xhs_job_progress:expect.objectContaining({report_available:true,video_processing:{status:"running"}})});
    browser.emitRemoved(1);
    expect(native.requests.some(x => x.kind === "stop_job")).toBe(false);
    expect(JSON.stringify(browser.storageSet.mock.calls)).not.toMatch(/sign=|你好|xhscdn/);
  });
  test("rejects a short body against exact-bound declared size before publishing a fake video", async () => {
    videoChrome(100);
    const native = new FakeNativeTransport();
    await new JobController(dependencies(native,{fetchMedia:async () => ({...imageResult,mimeType:"video/mp4"})})).collectCurrent(currentTab);
    expect(native.requests.some(x => x.kind === "media_begin")).toBe(false);
    expect(native.requests).toContainEqual(expect.objectContaining({kind:"media_missing",reason:"download_failed"}));
    expect(native.requests.some(x => x.kind === "finish_job")).toBe(true);
  });
  test("keeps known long duration and explicit size limit when no video can be fetched", async () => {
    videoChrome(3,true);
    const native = new FakeNativeTransport();
    await new JobController(dependencies(native)).collectCurrent(currentTab);
    expect(native.requests).toContainEqual(expect.objectContaining({kind:"media_missing",reason:"size_limit"}));
    expect(native.requests.find(x => x.kind === "finish_job")?.video_metadata).toMatchObject({duration_ms:409110});
  });
  test("subtitle failure cannot abort the video report", async () => {
    videoChrome();
    const native = new FakeNativeTransport();
    await new JobController(dependencies(native,{fetchMedia:async () => ({...imageResult,mimeType:"video/mp4"}),fetchSubtitle:async () => {throw new Error("unavailable");}})).collectCurrent(currentTab);
    expect(native.requests.find(x => x.kind === "finish_job")?.video_metadata).toMatchObject({subtitle_status:"failed"});
    expect(native.requests.some(x => x.kind === "media_end")).toBe(true);
  });
  test.each([
    "00:00:00,000 --> 00:00:01,000\n你好\n",
    "2\n00:00:00,000 --> 00:00:01,000\n你好\n"
  ])("Python-incompatible optional SRT degrades to failed without losing the video report", async (text) => {
    videoChrome();
    const native = new FakeNativeTransport();
    await new JobController(dependencies(native, {
      fetchMedia: async () => ({ ...imageResult, mimeType: "video/mp4" }),
      fetchSubtitle: async () => ({ text })
    })).collectCurrent(currentTab);
    expect(native.requests.find(x => x.kind === "finish_job")?.video_metadata).toMatchObject({ subtitle_status: "failed" });
    expect(native.requests.some(x => x.kind === "media_end" && x.role === "video")).toBe(true);
  });
});

describe("legacy direct-video source regression", () => {
  function directVideoProjection(noteId: string, position: number, sourceUrl: string): Record<string, unknown> {
    const base = projection(noteId, position);
    return {
      ...base,
      snapshot: { ...(base.snapshot as object), note_type: "video", media_slots: [{ note_id: noteId, role: "video", position: 1 }] },
      media: [{ note_id: noteId, role: "video", position: 1, sourceUrl }]
    };
  }

  test("current-note collection transfers an existing query-free CDN MP4", async () => {
    const sourceUrl = "https://ci.xhscdn.com/video.mp4";
    fakeChrome({ onMessage: (_tab, message) => message.kind === "project_detail"
      ? directVideoProjection("note_123", 1, sourceUrl)
      : { context: { kind: "current", noteId: "note_123", canonicalUrl: "https://www.xiaohongshu.com/explore/note_123" } } });
    const native = new FakeNativeTransport();
    const fetchMedia = vi.fn((url: string, role: "image" | "video_cover" | "video", signal: AbortSignal) =>
      fetchBoundMedia(url, role, async () => new Response(new Uint8Array([0, 0, 0, 16, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d, 0, 0, 0, 0]), { headers: { "content-type": "video/mp4" } }), signal));

    await new JobController(dependencies(native, { fetchMedia })).collectCurrent(currentTab);

    expect(fetchMedia).toHaveBeenCalledWith(sourceUrl, "video", expect.any(AbortSignal));
    expect(native.requests).toContainEqual(expect.objectContaining({ kind: "media_end", note_id: "note_123", role: "video" }));
    expect(native.requests.some((request) => request.kind === "media_missing")).toBe(false);
  });

  test("batch collection transfers an existing query-free CDN WebM", async () => {
    const noteId = "note_batch";
    const sourceUrl = "https://ci.xhscdn.com/video.webm";
    fakeChrome({ sourceKind: "search", onMessage: (tabId, message) => {
      if (message.kind === "inspect_page" && tabId === 1) return { context: { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" } };
      if (message.kind === "discover_candidates") return { candidates: [{ sourcePosition: 1, noteId, canonicalUrl: `https://www.xiaohongshu.com/explore/${noteId}` }] };
      if (message.kind === "inspect_page" && tabId === 99) return { context: { kind: "current", noteId, canonicalUrl: `https://www.xiaohongshu.com/explore/${noteId}` } };
      if (message.kind === "project_detail") return directVideoProjection(noteId, 1, sourceUrl);
      throw new Error("unexpected content message");
    } });
    const native = new FakeNativeTransport([noteId]);
    const fetchMedia = vi.fn((url: string, role: "image" | "video_cover" | "video", signal: AbortSignal) =>
      fetchBoundMedia(url, role, async () => new Response(new Uint8Array([0x1a, 0x45, 0xdf, 0xa3]), { headers: { "content-type": "video/webm" } }), signal));

    await new JobController(dependencies(native, { fetchMedia })).collectBatch(searchTab, { requestedCount: 1, candidateScanLimit: 1 });

    expect(fetchMedia).toHaveBeenCalledWith(sourceUrl, "video", expect.any(AbortSignal));
    expect(native.requests).toContainEqual(expect.objectContaining({ kind: "media_end", note_id: noteId, role: "video" }));
    expect(native.requests.some((request) => request.kind === "media_missing")).toBe(false);
  });
});
