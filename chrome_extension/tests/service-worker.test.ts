import { describe, expect, test, vi } from "vitest";

type Runner = {
  videoStatus: ReturnType<typeof vi.fn>;
  collectCurrent: ReturnType<typeof vi.fn>;
  collectBatch: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
  inspectActivePage: ReturnType<typeof vi.fn>;
  openReport: ReturnType<typeof vi.fn>;
};

async function messageHandler() {
  vi.resetModules();
  vi.stubGlobal("chrome", {
    runtime: {
      id: "extension-id",
      getURL: vi.fn((path: string) => `chrome-extension://extension-id/${path}`),
      onMessage: { addListener: vi.fn() },
      connectNative: vi.fn()
    }
  } as unknown as typeof chrome);
  const module = await import("../src/service-worker.js");
  const runner: Runner = {
    videoStatus:vi.fn(async () => ({status:"running" as const})),
    collectCurrent: vi.fn(async () => undefined),
    collectBatch: vi.fn(async () => undefined),
    stop: vi.fn(async () => undefined),
    inspectActivePage: vi.fn(async () => ({ page: "search" })),
    openReport: vi.fn(async () => undefined)
  };
  return { handler: module.createServiceWorkerMessageHandler(runner), runner };
}

function popupSender(): chrome.runtime.MessageSender {
  return { id: "extension-id", url: "chrome-extension://extension-id/popup.html" } as chrome.runtime.MessageSender;
}

describe("service worker command boundary", () => {
  test("does not start a job during service-worker initialization", async () => {
    const { runner } = await messageHandler();

    expect(runner.collectCurrent).not.toHaveBeenCalled();
    expect(runner.collectBatch).not.toHaveBeenCalled();
    expect(runner.stop).not.toHaveBeenCalled();
  });

  test("accepts only an exact popup current-tab command and discards the raw tab URL", async () => {
    const { handler, runner } = await messageHandler();
    const respond = vi.fn();

    expect(handler(
      { kind: "collect_current", tab: { id: 7, url: "https://www.xiaohongshu.com/explore/note_123?xsec_token=discard" } },
      popupSender(),
      respond
    )).toBe(false);
    await vi.waitFor(() => expect(runner.collectCurrent).toHaveBeenCalledOnce());
    expect(runner.collectCurrent).toHaveBeenCalledWith({ id: 7 });
    expect(respond).toHaveBeenCalledWith({ status: "accepted" });
  });

  test("rejects content-page and malformed commands without dispatching collection", async () => {
    const { handler, runner } = await messageHandler();
    const respond = vi.fn();

    expect(handler(
      { kind: "collect_batch", tab: { id: 7 }, options: { requestedCount: 1, candidateScanLimit: 1 } },
      { id: "extension-id", tab: { id: 7 } } as chrome.runtime.MessageSender,
      respond
    )).toBe(false);
    expect(handler(
      { kind: "collect_batch", tab: { id: 7 }, options: { requestedCount: 1, candidateScanLimit: 1, publicationCutoff: "token=secret" } },
      popupSender(),
      respond
    )).toBe(false);
    await Promise.resolve();

    expect(runner.collectCurrent).not.toHaveBeenCalled();
    expect(runner.collectBatch).not.toHaveBeenCalled();
    expect(respond).not.toHaveBeenCalled();
  });

  test("dispatches a valid batch command without retaining popup state", async () => {
    const { handler, runner } = await messageHandler();
    const respond = vi.fn();

    handler(
      { kind: "collect_batch", tab: { id: 7 }, options: { requestedCount: 2, candidateScanLimit: 5 } },
      popupSender(),
      respond
    );
    await vi.waitFor(() => expect(runner.collectBatch).toHaveBeenCalledOnce());

    expect(runner.collectBatch).toHaveBeenCalledWith({ id: 7 }, { requestedCount: 2, candidateScanLimit: 5 });
    expect(respond).toHaveBeenCalledWith({ status: "accepted" });
  });

  test("preserves an explicit page-order batch mode for the Simple Search runner", async () => {
    const { handler, runner } = await messageHandler();
    const respond = vi.fn();

    handler(
      { kind: "collect_batch", tab: { id: 7 }, options: { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" } },
      popupSender(),
      respond
    );
    await vi.waitFor(() => expect(runner.collectBatch).toHaveBeenCalledOnce());

    expect(runner.collectBatch).toHaveBeenCalledWith({ id: 7 }, { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" });
  });

  test("rejects popup page-order commands that are not the fixed 5 or 10 current-page choices", async () => {
    const { handler, runner } = await messageHandler();
    const respond = vi.fn();

    handler(
      { kind: "collect_batch", tab: { id: 7 }, options: { requestedCount: 6, candidateScanLimit: 6, selectionOrder: "page_order" } },
      popupSender(),
      respond
    );
    await Promise.resolve();

    expect(runner.collectBatch).not.toHaveBeenCalled();
    expect(respond).not.toHaveBeenCalled();
  });

  test("accepts only the strict tab-less popup stop command", async () => {
    const { handler, runner } = await messageHandler();
    const respond = vi.fn();

    expect(handler({ kind: "stop_job" }, popupSender(), respond)).toBe(true);
    await vi.waitFor(() => expect(runner.stop).toHaveBeenCalledOnce());
    expect(respond).toHaveBeenCalledWith({ status: "stopped" });

    handler({ kind: "stop_job", job_id: "job_123" }, popupSender(), respond);
    handler({ kind: "stop_job" }, { id: "extension-id", tab: { id: 7 } } as chrome.runtime.MessageSender, respond);
    await Promise.resolve();
    expect(runner.stop).toHaveBeenCalledOnce();
  });

  test("inspects a popup-selected tab through a finite page-state response", async () => {
    const { handler, runner } = await messageHandler();
    const respond = vi.fn();

    expect(handler(
      { kind: "inspect_active_page", tab: { id: 7 } },
      popupSender(),
      respond
    )).toBe(true);
    await vi.waitFor(() => expect(runner.inspectActivePage).toHaveBeenCalledWith({ id: 7 }));
    expect(respond).toHaveBeenCalledWith({ page: "search" });

    handler(
      { kind: "inspect_active_page", tab: { id: 7, url: "https://www.xiaohongshu.com/explore/note?token=hidden" } },
      popupSender(),
      respond
    );
    await Promise.resolve();
    expect(runner.inspectActivePage).toHaveBeenCalledOnce();
  });

  test("returns only the safe detected current-note id to the popup", async () => {
    const { handler, runner } = await messageHandler();
    runner.inspectActivePage.mockResolvedValueOnce({ page: "current", note_id: "note_123" });
    const respond = vi.fn();

    handler({ kind: "inspect_active_page", tab: { id: 7 } }, popupSender(), respond);
    await vi.waitFor(() => expect(respond).toHaveBeenCalledWith({ page: "current", note_id: "note_123" }));
    expect(JSON.stringify(respond.mock.calls)).not.toMatch(/https?:\/\/|token|selector|body/i);
  });

  test("opens a report using only its opaque job id", async () => {
    const { handler, runner } = await messageHandler();
    runner.openReport.mockResolvedValueOnce({ terminal_status: "complete" });
    const respond = vi.fn();

    expect(handler(
      { kind: "open_report", job_id: "job_123" },
      popupSender(),
      respond
    )).toBe(true);
    await vi.waitFor(() => expect(runner.openReport).toHaveBeenCalledWith("job_123"));
    expect(respond).toHaveBeenCalledWith({ status: "opened", terminal_status: "complete" });

    handler(
      { kind: "open_report", job_id: "job_123", report_file: "/private/report.html" },
      popupSender(),
      respond
    );
    await Promise.resolve();
    expect(runner.openReport).toHaveBeenCalledOnce();
  });

  test("rejects same-extension commands from a tab-less non-popup page", async () => {
    const { handler, runner } = await messageHandler();
    const respond = vi.fn();

    expect(handler(
      { kind: "collect_current", tab: { id: 7 } },
      { id: "extension-id", url: "chrome-extension://extension-id/other.html" } as chrome.runtime.MessageSender,
      respond
    )).toBe(false);
    await Promise.resolve();

    expect(runner.collectCurrent).not.toHaveBeenCalled();
    expect(respond).not.toHaveBeenCalled();
  });

  test("returns an unconfirmed stop state without conflating it with report failure", async () => {
    const { handler, runner } = await messageHandler();
    runner.stop.mockRejectedValueOnce(new Error("raw stop failure"));
    runner.openReport.mockRejectedValueOnce(new Error("raw report failure"));
    const stopResponse = vi.fn();
    const reportResponse = vi.fn();

    handler({ kind: "stop_job" }, popupSender(), stopResponse);
    handler({ kind: "open_report", job_id: "job_123" }, popupSender(), reportResponse);
    await vi.waitFor(() => expect(stopResponse).toHaveBeenCalledWith({ status: "unconfirmed" }));
    expect(reportResponse).toHaveBeenCalledWith({ status: "failed" });
  });
});

test.each(["video_status","video_stop"])("%s accepts only an exact job-scoped popup command",async kind => {
  const {handler,runner}=await messageHandler();
  const respond=vi.fn();
  expect(handler({kind,job_id:"job_video"},popupSender(),respond)).toBe(true);
  await Promise.resolve();
  expect(runner.videoStatus).toHaveBeenCalledWith("job_video",kind==="video_stop");
  expect(respond).toHaveBeenCalledWith({status:"ok",job_id:"job_video",processing:{status:"running"}});
  runner.videoStatus.mockClear();
  expect(handler({kind,job_id:"job_video"},{...popupSender(),tab:{id:7}},respond)).toBe(false);
  expect(handler({kind,job_id:"../../other"},popupSender(),respond)).toBe(false);
  expect(handler({kind,job_id:"job_video",path:"/private"},popupSender(),respond)).toBe(false);
  expect(runner.videoStatus).not.toHaveBeenCalled();
});
