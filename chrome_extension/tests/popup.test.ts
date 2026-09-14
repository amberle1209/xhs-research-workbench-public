// @vitest-environment jsdom

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeEach, describe, expect, test, vi } from "vitest";

import { mountPopup } from "../src/popup/popup.js";

type PageState =
  | { page: "current"; note_id?: string }
  | { page: "search" }
  | { page: "account" }
  | { page: "unsupported" }
  | { page: "blocked"; reason: "login_required" | "challenge_detected" };

type Progress = {
  job_id: string;
  phase: "starting" | "scanning" | "ranking" | "downloading" | "complete" | "partial" | "stopped" | "error";
  discovered: number;
  inspected: number;
  eligible: number;
  selected: number;
  saved: number;
  error?: string;
  detail_stage?: string;
  detail_reason?: unknown;
  report_available?: boolean;
  video_processing?: {status:string;reason?:string};
  current_source_position?: number;
};

type PopupScenario = {
  page?: PageState;
  progress?: Progress;
  preferences?: { requestedCount: number; candidateScanLimit: number; windowDays: "all" | "30" | "90" | "180" | "365" };
  permission?: boolean | Promise<boolean>;
  inspectFails?: boolean;
  collectionFails?: boolean;
  stopFails?: boolean;
  stopResponse?: unknown;
  reportFails?: boolean;
  reportResponse?: unknown | Promise<unknown>;
  searchRisk?: { last_search_batch_started_at: number };
  buildRevision?: string;
  videoResponse?: unknown;
  videoStopResponse?: unknown;
};

const root = resolve(import.meta.dirname, "../..");
const popupTemplate = () => readFileSync(resolve(root, "chrome_extension/src/popup/popup.html"), "utf8");

function loadTemplate(): void {
  document.documentElement.innerHTML = popupTemplate();
}

function field<T extends HTMLElement>(selector: string): T {
  const found = document.querySelector<T>(selector);
  if (found === null) throw new Error(`missing ${selector}`);
  return found;
}

function collectionCommands(send: ReturnType<typeof vi.fn>): unknown[] {
  return send.mock.calls.map(([message]) => message).filter((message) => {
    return message !== null && typeof message === "object" && ["collect_current", "collect_batch"].includes((message as { kind?: string }).kind ?? "");
  });
}

function setup(scenario: PopupScenario = {}) {
  if (scenario.buildRevision !== undefined) {
    (globalThis as typeof globalThis & { __XHS_BUILD_REVISION__?: string }).__XHS_BUILD_REVISION__ = scenario.buildRevision;
  }
  const changeListeners: Array<(changes: Record<string, { newValue?: unknown }>, areaName: string) => void> = [];
  const send = vi.fn(async (message: unknown) => {
    if (scenario.inspectFails === true && (message as { kind?: string }).kind === "inspect_active_page") throw new Error("host unavailable");
    if (scenario.collectionFails === true && ["collect_current", "collect_batch"].includes((message as { kind?: string }).kind ?? "")) throw new Error("host unavailable");
    if (scenario.stopFails === true && (message as { kind?: string }).kind === "stop_job") throw new Error("stop unavailable");
    if (scenario.reportFails === true && (message as { kind?: string }).kind === "open_report") throw new Error("report unavailable");
    if ((message as { kind?: string }).kind === "inspect_active_page") return scenario.page ?? { page: "unsupported" };
    if ((message as { kind?: string }).kind === "stop_job") return scenario.stopResponse ?? { status: "stopped" };
    if ((message as { kind?: string }).kind === "open_report") return scenario.reportResponse ?? { status: "opened" };
    if ((message as {kind?:string}).kind === "video_status") return scenario.videoResponse ?? {status:"ok",job_id:"job_video",processing:{status:"running"}};
    if ((message as {kind?:string}).kind === "video_stop") return scenario.videoStopResponse ?? {status:"ok",job_id:"job_video",processing:{status:"failed",reason:"stopped"}};
    return { status: "accepted" };
  });
  const request = vi.fn(() => scenario.permission ?? true);
  const set = vi.fn(async () => undefined);
  const remove = vi.fn(async () => undefined);
  const api = {
    runtime: { sendMessage: send },
    permissions: { request },
    storage: { local: {
      get: vi.fn(async () => ({
        ...(scenario.preferences === undefined ? {} : { xhs_popup_preferences: scenario.preferences }),
        ...(scenario.progress === undefined ? {} : { xhs_job_progress: scenario.progress }),
        ...(scenario.searchRisk === undefined ? {} : { xhs_search_risk: scenario.searchRisk })
      })),
      set,
      remove
    }, onChanged: { addListener: (listener: (changes: Record<string, { newValue?: unknown }>, areaName: string) => void) => changeListeners.push(listener) }},
    tabs: { query: vi.fn(async () => [{ id: 17 }]) }
  };
  const emitProgress = (progress: unknown): void => {
    for (const listener of changeListeners) listener({ xhs_job_progress: { newValue: progress } }, "local");
  };
  const emitSearchRisk = (risk: unknown): void => {
    for (const listener of changeListeners) listener({ xhs_search_risk: { newValue: risk } }, "local");
  };
  return { api, send, request, set, emitProgress, emitSearchRisk };
}

async function render(scenario: PopupScenario = {}) {
  loadTemplate();
  const controls = setup(scenario);
  await mountPopup(document, controls.api);
  return controls;
}

beforeEach(() => {
  document.documentElement.innerHTML = "";
  Reflect.deleteProperty(globalThis, "__XHS_BUILD_REVISION__");
});

describe("extension collection popup", () => {
  test("renders the injected content-script revision as fixed safe Chinese copy", async () => {
    await render({ buildRevision: "9a12b34c56d7" });

    expect(field<HTMLElement>("#build-revision").textContent).toBe("构建版本：9a12b34c56d7");
  });

  test("keeps every actionable control hidden and disabled before asynchronous page inspection", () => {
    loadTemplate();

    expect(field<HTMLButtonElement>("#current-action").hidden).toBe(true);
    expect(field<HTMLButtonElement>("#current-action").disabled).toBe(true);
    expect(field<HTMLButtonElement>("#batch-action").hidden).toBe(true);
    expect(field<HTMLButtonElement>("#batch-action").disabled).toBe(true);
    expect(field<HTMLElement>("#collection-options").hidden).toBe(true);
  });

  test("uses an accessible Chinese search form with only 5 and 10 page-order choices", async () => {
    await render({ page: { page: "search" } });

    expect(field<HTMLElement>("main").getAttribute("aria-labelledby")).toBe("popup-title");
    expect(field<HTMLElement>("#status").getAttribute("aria-live")).toBe("polite");
    expect(field<HTMLLabelElement>("label[for='requested-count']").textContent).toContain("采集数量");
    expect(field<HTMLSelectElement>("#requested-count").options).toHaveLength(2);
    expect([...field<HTMLSelectElement>("#requested-count").options].map((option) => option.value)).toEqual(["5", "10"]);
    expect(document.querySelector("#candidate-limit")).toBeNull();
    expect(document.querySelector("#time-window")).toBeNull();
    expect(document.body.textContent).not.toMatch(/候选扫描上限|发布时间范围|点赞数|按点赞/u);
    expect(field<HTMLElement>("#order").textContent).toContain("将按当前小红书页面的展示顺序采集；若你已调整排序，插件会保留当前结果顺序。");
    expect(field<HTMLElement>("#risk-note").textContent).toContain("批量采集会依次打开帖子详情；一次最多 10 篇，并限制访问频率。");
    expect(field<HTMLButtonElement>("#batch-action").textContent).toContain("按当前页面顺序采集");
  });

  test("sends the selected page-order count without ranking or time-window options", async () => {
    const { send } = await render({ page: { page: "search" } });
    send.mockClear();
    field<HTMLSelectElement>("#requested-count").value = "10";
    field<HTMLSelectElement>("#requested-count").dispatchEvent(new Event("change", { bubbles: true }));

    field<HTMLButtonElement>("#batch-action").click();

    await vi.waitFor(() => expect(collectionCommands(send)).toEqual([{
      kind: "collect_batch", tab: { id: 17 }, options: { requestedCount: 10, candidateScanLimit: 10, selectionOrder: "page_order" }
    }]));
  });

  test.each([
    [{ page: "current", note_id: "note_123" } as const, "当前为帖子详情页（编号：note_123），可提取这篇帖子。", "#current-action"],
    [{ page: "search" } as const, "当前为搜索结果页，可按当前页面顺序采集。", "#batch-action"],
    [{ page: "account" } as const, "账号主页不在本次搜索采集范围内。", "#batch-action"],
    [{ page: "unsupported" } as const, "请打开帖子详情或搜索结果页后再试。", "#current-action"],
    [{ page: "blocked", reason: "login_required" } as const, "请先登录小红书后再试。", "#current-action"],
    [{ page: "blocked", reason: "challenge_detected" } as const, "检测到验证页，请先在当前 Chrome 页面完成验证后再试。", "#current-action"]
  ])("renders the finite %o page state without page content", async (page, copy, action) => {
    await render({ page });

    expect(field<HTMLElement>("#status").textContent).toContain(copy);
    expect(field<HTMLButtonElement>(action).disabled).toBe(page.page !== "search" && page.page !== "current");
    expect(field<HTMLElement>("#collection-options").hidden).toBe(page.page !== "search");
  });

  test.each([
    [{ page: "current", note_id: "note_123" } as const, "将请求媒体访问权限，仅用于提取当前帖子。"],
    [{ page: "search" } as const, "将请求小红书页面与媒体访问权限，用于按当前页面顺序采集。"],
    [{ page: "unsupported" } as const, "当前页面不支持采集，不会请求额外权限。"]
  ])("renders state-specific permission explanation", async (page, copy) => {
    await render({ page });
    expect(field<HTMLElement>("#permission-copy").textContent).toContain(copy);
  });

  test("shows a finite local-service failure without exposing an error", async () => {
    await render({ inspectFails: true });

    expect(field<HTMLElement>("#status").textContent).toContain("本地采集服务暂不可用，请检查安装状态。");
    expect(field<HTMLElement>("#status").textContent).not.toContain("host unavailable");
  });

  test.each([
    [{ job_id: "job_run", phase: "scanning", discovered: 8, inspected: 3, eligible: 8, selected: 5, saved: 0, current_source_position: 3 } as Progress, "已发现 8 篇；已完成详情 3 / 5。", true, false],
    [{ job_id: "job_run", phase: "partial", discovered: 8, inspected: 2, eligible: 2, selected: 2, saved: 2, error: "detail_unavailable", report_available: true } as Progress, "已完成部分采集。", false, true],
    [{ job_id: "job_run", phase: "stopped", discovered: 8, inspected: 3, eligible: 2, selected: 0, saved: 0 } as Progress, "采集已停止。", false, false],
    [{ job_id: "job_run", phase: "complete", discovered: 8, inspected: 5, eligible: 5, selected: 5, saved: 5, report_available: true } as Progress, "本地结果包已写入", false, true],
    [{ job_id: "job_run", phase: "partial", discovered: 0, inspected: 0, eligible: 0, selected: 0, saved: 0, error: "detail_unavailable" } as Progress, "已完成部分采集。", false, false]
  ])("renders progress and terminal state safely", async (progress, copy, stopVisible, reportVisible) => {
    await render({ page: { page: "search" }, progress });

    expect(field<HTMLElement>("#status").textContent).toContain(copy);
    expect(field<HTMLButtonElement>("#stop-action").hidden).toBe(!stopVisible);
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(!reportVisible);
    expect(field<HTMLElement>("#progress-counts").hidden).toBe(false);
    expect(field<HTMLElement>("#count-discovered").textContent).toBe(String(progress.discovered));
    expect(field<HTMLElement>("#count-inspected").textContent).toBe(String(progress.inspected));
    expect(field<HTMLElement>("#count-eligible").textContent).toBe(String(progress.eligible));
    expect(field<HTMLElement>("#count-selected").textContent).toBe(String(progress.selected));
    expect(field<HTMLElement>("#count-saved").textContent).toBe(String(progress.saved));
  });

  test("renders discovered, detail progress, current item, cooldown, partial, login, challenge, and stop states truthfully", async () => {
    const running: Progress = { job_id: "job_run", phase: "scanning", discovered: 5, inspected: 2, eligible: 5, selected: 5, saved: 1, current_source_position: 2 };
    await render({ page: { page: "search" }, progress: running });
    expect(field<HTMLElement>("#status").textContent).toContain("已发现 5 篇");
    expect(field<HTMLElement>("#status").textContent).toContain("已完成详情 2 / 5");
    expect(field<HTMLElement>("#current-candidate").textContent).toContain("当前帖子：第 2 篇");
    expect(field<HTMLButtonElement>("#stop-action").hidden).toBe(false);

    for (const [error, copy] of [
      ["cooldown", "请在 60 秒冷却结束后再开始新的批量采集。"],
      ["detail_unavailable", "已完成部分采集"],
      ["login_required", "当前页面需要登录"],
      ["challenge_detected", "当前页面需要完成验证"],
      ["stopped", "采集已停止"]
    ] as const) {
      await render({ page: { page: "search" }, progress: { ...running, phase: error === "detail_unavailable" ? "partial" : error === "stopped" ? "stopped" : "error", error } });
      expect(field<HTMLElement>("#status").textContent).toContain(copy);
      expect(field<HTMLButtonElement>("#batch-action").disabled).toBe(false);
    }
  });

  test.each(["complete", "partial", "stopped", "login_required", "challenge_detected", "native_host_interrupted"] as const)("allows a later user-initiated search after the %s terminal record", async (terminal) => {
    const phase = terminal === "complete" ? "complete" : terminal === "partial" ? "partial" : terminal === "stopped" ? "stopped" : "error";
    const progress: Progress = { job_id: "job_terminal", phase, discovered: 5, inspected: 2, eligible: 5, selected: 5, saved: 1, error: terminal === "complete" || terminal === "partial" ? "detail_unavailable" : terminal };
    await render({ page: { page: "search" }, progress });

    expect(field<HTMLButtonElement>("#batch-action").disabled).toBe(false);
  });

  test("keeps a new search disabled only through the persisted 60-second cooldown, then dispatches page order", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(10_000));
    const progress: Progress = { job_id: "job_cooldown", phase: "error", discovered: 0, inspected: 0, eligible: 0, selected: 0, saved: 0, error: "cooldown" };
    const { send } = await render({ page: { page: "search" }, progress, searchRisk: { last_search_batch_started_at: 10_000 } });
    send.mockClear();

    expect(field<HTMLButtonElement>("#batch-action").disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(field<HTMLButtonElement>("#batch-action").disabled).toBe(false);
    field<HTMLButtonElement>("#batch-action").click();
    await vi.waitFor(() => expect(collectionCommands(send)).toEqual([{
      kind: "collect_batch", tab: { id: 17 }, options: { requestedCount: 5, candidateScanLimit: 5, selectionOrder: "page_order" }
    }]));
    vi.useRealTimers();
  });

  test("an open popup observes its own search cooldown through terminal progress", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(20_000));
    const { send, emitProgress, emitSearchRisk } = await render({ page: { page: "search" } });
    send.mockClear();

    field<HTMLButtonElement>("#batch-action").click();
    await vi.waitFor(() => expect(collectionCommands(send)).toHaveLength(1));
    emitSearchRisk({ last_search_batch_started_at: 20_000 });
    emitProgress({ job_id: "job_open", phase: "complete", discovered: 5, inspected: 5, eligible: 5, selected: 5, saved: 5, report_available: true });
    expect(field<HTMLButtonElement>("#batch-action").disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(field<HTMLButtonElement>("#batch-action").disabled).toBe(false);
    vi.useRealTimers();
  });

  test("an active search cooldown never disables current-note capture", async () => {
    const { send } = await render({
      page: { page: "current", note_id: "note_123" },
      searchRisk: { last_search_batch_started_at: Date.now() }
    });
    send.mockClear();

    expect(field<HTMLButtonElement>("#current-action").disabled).toBe(false);
    field<HTMLButtonElement>("#current-action").click();
    await vi.waitFor(() => expect(collectionCommands(send)).toEqual([{ kind: "collect_current", tab: { id: 17 } }]));
  });

  test("renders inconsistent legacy progress as checked candidates rather than impossible detail completion", async () => {
    const progress: Progress = { job_id: "job_legacy", phase: "scanning", discovered: 4, inspected: 4, eligible: 2, selected: 2, saved: 0 };
    await render({ page: { page: "search" }, progress });

    expect(field<HTMLElement>("#status").textContent).toContain("已检查 4 篇候选帖子；已选择 2 篇");
    expect(field<HTMLElement>("#status").textContent).not.toContain("已完成详情 4 / 2");
    expect(field<HTMLElement>("#count-inspected-label").textContent).toBe("已检查");
  });

  test("does not claim a local bundle exists until the host confirms it", async () => {
    const progress: Progress = { job_id: "job_unconfirmed", phase: "complete", discovered: 5, inspected: 5, eligible: 5, selected: 5, saved: 5, report_available: false };
    await render({ page: { page: "search" }, progress });

    expect(field<HTMLElement>("#status").textContent).toContain("尚未收到本地结果包写入确认");
    expect(field<HTMLElement>("#status").textContent).not.toContain("本地结果包已写入");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(true);
  });

  test.each([
    ["native_host_interrupted", "采集已中断，未完成。本地采集服务连接已中断，未继续处理。"],
    ["source_tab_closed", "采集已中断，未完成。原始页面已关闭，未继续处理。"],
    ["queue_tab_closed", "采集已中断，未完成。采集队列页面已关闭，未继续处理。"],
    ["identity_mismatch", "采集已中断，未完成。页面身份与开始时不一致，未继续处理。"]
  ])("renders bounded interruption %s without a retry, profile, or permission instruction", async (error, copy) => {
    const progress: Progress = { job_id: "job_interrupted", phase: "error", discovered: 4, inspected: 2, eligible: 1, selected: 0, saved: 0, error, report_available: true };
    await render({ page: { page: "search" }, progress });

    const status = field<HTMLElement>("#status").textContent ?? "";
    expect(status).toContain(copy);
    expect(status).not.toMatch(/重试|切换|权限/u);
    expect(field<HTMLElement>("#report-identifier").textContent).toBe("本地结果包已确认：可打开本地报告");
    expect(field<HTMLElement>("#report-identifier").hidden).toBe(false);
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(false);
  });

  test.each([
    ["cooldown", "请在 60 秒冷却结束后再开始新的批量采集。诊断代码：cooldown。"],
    ["detail_unavailable", "当前帖子详情暂不可用，未继续处理。诊断代码：detail_unavailable。"],
    ["login_required", "当前页面需要登录，采集已停止。诊断代码：login_required。"],
    ["challenge_detected", "当前页面需要完成验证，采集已停止。诊断代码：challenge_detected。"],
    ["stopped", "采集已停止。诊断代码：stopped。"],
    ["native_host_interrupted", "本地采集服务连接已中断，未继续处理。诊断代码：native_host_interrupted。"],
    ["source_tab_closed", "原始页面已关闭，未继续处理。诊断代码：source_tab_closed。"],
    ["queue_tab_closed", "采集队列页面已关闭，未继续处理。诊断代码：queue_tab_closed。"],
    ["identity_mismatch", "页面身份与开始时不一致，未继续处理。诊断代码：identity_mismatch。"],
    ["optional_permission_required", "未获得所需媒体访问权限。诊断代码：optional_permission_required。"],
    ["invalid_batch_bounds", "批量采集设置无效。诊断代码：invalid_batch_bounds。"],
    ["unsupported_current_tab", "当前页面未识别为帖子详情。诊断代码：unsupported_current_tab。"],
    ["unsupported_batch_tab", "当前页面不支持批量采集。诊断代码：unsupported_batch_tab。"],
    ["job_in_progress", "已有采集任务正在处理。诊断代码：job_in_progress。"],
    ["native_host_unavailable", "本地采集服务未连接。诊断代码：native_host_unavailable。"],
    ["native_host_version_mismatch", "本地采集服务版本不匹配。诊断代码：native_host_version_mismatch。"],
    ["native_response_invalid", "本地采集服务返回无效响应。诊断代码：native_response_invalid。"],
    ["native_host_error", "本地采集服务发生受限错误。诊断代码：native_host_error。"],
    ["media_transfer_failed", "媒体传输失败，未继续处理。诊断代码：media_transfer_failed。"]
  ])("renders the finite native diagnostic %s instead of collapsing it into generic copy", async (error, copy) => {
    const progress: Progress = { job_id: "job_native_diagnostic", phase: "error", discovered: 0, inspected: 0, eligible: 0, selected: 0, saved: 0, error };
    await render({ page: { page: "current", note_id: "note_123" }, progress });

    const status = field<HTMLElement>("#status").textContent ?? "";
    expect(status).toContain(copy);
    expect(status).not.toContain("采集过程发生受限错误");
  });

  test("does not claim retained output when current-note detail projection fails before saving", async () => {
    const progress: Progress = { job_id: "job_empty_detail", phase: "error", discovered: 0, inspected: 0, eligible: 0, selected: 0, saved: 0, error: "detail_unavailable", report_available: false };
    await render({ page: { page: "current", note_id: "note_123" }, progress });

    const status = field<HTMLElement>("#status").textContent ?? "";
    expect(status).toContain("当前帖子详情暂不可用，未继续处理。诊断代码：detail_unavailable。");
    expect(status).not.toContain("已保留可验证的结果");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(true);
  });

  test("renders a fixed detail stage without displaying page-derived failure text", async () => {
    const progress: Progress = { job_id: "job_author_stage", phase: "error", discovered: 0, inspected: 0, eligible: 0, selected: 0, saved: 0, error: "detail_unavailable", detail_stage: "author_identity" };
    await render({ page: { page: "current", note_id: "note_123" }, progress });

    const status = field<HTMLElement>("#status").textContent ?? "";
    expect(status).toContain("详情检查阶段：作者身份。");
    expect(status).not.toContain("Fixture detail");
  });

  test.each([
    ["no_visible_profile_anchor", "未找到可见的作者主页链接。"],
    ["duplicate_same_author", "检测到多个指向同一作者的主页链接。"],
    ["duplicate_different_author", "检测到指向不同作者的主页链接。"],
    ["malformed_profile_path", "作者主页链接路径格式不符合要求。"],
    ["unsafe_profile_origin", "作者主页链接来源不符合安全要求。"],
    ["unparseable_author_anchor", "作者区域存在无法解析的链接。"]
  ])("renders fixed Chinese copy for %s from storage and live updates", async (reason, copy) => {
    const progress: Progress = { job_id: "job_author_reason", phase: "error", discovered: 0, inspected: 0, eligible: 0, selected: 0, saved: 0, error: "detail_unavailable", detail_stage: "author_identity", detail_reason: reason };
    const { emitProgress } = await render({ page: { page: "current", note_id: "note_123" }, progress });
    expect(field<HTMLElement>("#status").textContent).toContain(copy);
    expect(field<HTMLElement>("#status").textContent).not.toContain(reason);
    emitProgress({ ...progress, detail_reason: "https://private.example/?xsec_token=private-token" });
    expect(field<HTMLElement>("#status").textContent).toContain("详情检查阶段：作者身份。");
    expect(field<HTMLElement>("#status").textContent).not.toMatch(/private-token|private\.example/u);
    expect(field<HTMLElement>("#status").textContent).not.toContain(copy);
    emitProgress({ ...progress, phase: "partial" });
    expect(field<HTMLElement>("#status").textContent).toContain(copy);
    emitProgress({ ...progress, detail_stage: "detail_root" });
    expect(field<HTMLElement>("#status").textContent).not.toContain(copy);
  });

  test("does not echo an unrecognized persisted error value", async () => {
    const unknown = "unexpected xsec_token=private-value";
    const progress: Progress = { job_id: "job_unknown_error", phase: "error", discovered: 0, inspected: 0, eligible: 0, selected: 0, saved: 0, error: unknown };
    await render({ page: { page: "current", note_id: "note_123" }, progress });

    const status = field<HTMLElement>("#status").textContent ?? "";
    expect(status).toContain("发生未识别的本地错误，未继续处理");
    expect(status).not.toContain(unknown);
  });

  test("requests the current-note media origin synchronously before dispatching collection", async () => {
    let resolvePermission: ((value: boolean) => void) | undefined;
    const permission = new Promise<boolean>((resolve) => { resolvePermission = resolve; });
    const { send, request } = await render({ page: { page: "current", note_id: "note_123" }, permission });
    send.mockClear();

    field<HTMLButtonElement>("#current-action").click();
    expect(request).toHaveBeenCalledWith({ origins: ["https://*.xhscdn.com/*"] });
    expect(collectionCommands(send)).toEqual([]);

    resolvePermission?.(true);
    await vi.waitFor(() => expect(collectionCommands(send)).toEqual([{ kind: "collect_current", tab: { id: 17 } }]));
  });

  test("requests both batch origins synchronously and sends bounded options only after approval", async () => {
    let resolvePermission: ((value: boolean) => void) | undefined;
    const permission = new Promise<boolean>((resolve) => { resolvePermission = resolve; });
    const { send, request, set } = await render({ page: { page: "search" }, permission });
    send.mockClear();
    field<HTMLSelectElement>("#requested-count").value = "10";

    field<HTMLButtonElement>("#batch-action").click();
    expect(request).toHaveBeenCalledWith({ origins: ["https://www.xiaohongshu.com/*", "https://*.xhscdn.com/*"] });
    expect(collectionCommands(send)).toEqual([]);

    resolvePermission?.(true);
    await vi.waitFor(() => expect(collectionCommands(send)).toEqual([{
      kind: "collect_batch", tab: { id: 17 }, options: { requestedCount: 10, candidateScanLimit: 10, selectionOrder: "page_order" }
    }]));
    expect(set).toHaveBeenCalledWith({ xhs_popup_preferences: { requestedCount: 10 } });
  });

  test("does not start collection after a permission denial", async () => {
    const { send, request } = await render({ page: { page: "search" }, permission: false });
    send.mockClear();

    field<HTMLButtonElement>("#batch-action").click();
    await vi.waitFor(() => expect(request).toHaveBeenCalledOnce());
    expect(collectionCommands(send)).toEqual([]);
    expect(field<HTMLElement>("#status").textContent).toContain("未授予所需权限，采集未开始。");
  });

  test("blocks duplicate starts while the synchronous permission request is pending", async () => {
    let resolvePermission: ((value: boolean) => void) | undefined;
    const permission = new Promise<boolean>((resolve) => { resolvePermission = resolve; });
    const { request, send } = await render({ page: { page: "current", note_id: "note_123" }, permission });
    send.mockClear();

    field<HTMLButtonElement>("#current-action").click();
    field<HTMLButtonElement>("#current-action").click();
    expect(request).toHaveBeenCalledOnce();
    expect(field<HTMLButtonElement>("#current-action").disabled).toBe(true);
    expect(field<HTMLElement>("#status").textContent).toContain("正在等待权限确认。");

    resolvePermission?.(true);
    await vi.waitFor(() => expect(collectionCommands(send)).toHaveLength(1));
  });

  test("sends a stop command only while a job is running and gives immediate finite feedback", async () => {
    const progress: Progress = { job_id: "job_run", phase: "downloading", discovered: 5, inspected: 5, eligible: 3, selected: 3, saved: 1 };
    const { send } = await render({ page: { page: "search" }, progress });
    send.mockClear();

    field<HTMLButtonElement>("#stop-action").click();
    expect(send).toHaveBeenCalledWith({ kind: "stop_job" });
    expect(field<HTMLElement>("#status").textContent).toContain("正在停止采集，请等待当前步骤结束。");
    expect(field<HTMLButtonElement>("#stop-action").disabled).toBe(true);
  });

  test("opens a local report with its opaque job id only", async () => {
    const progress: Progress = { job_id: "job_report", phase: "complete", discovered: 1, inspected: 1, eligible: 1, selected: 1, saved: 1, report_available: true };
    const { send } = await render({ page: { page: "current", note_id: "note_123" }, progress });
    send.mockClear();

    field<HTMLButtonElement>("#report-action").click();
    await vi.waitFor(() => expect(send).toHaveBeenCalledWith({ kind: "open_report", job_id: "job_report" }));
    expect(send.mock.calls[0]?.[0]).toEqual({ kind: "open_report", job_id: "job_report" });
    await vi.waitFor(() => expect(field<HTMLElement>("#status").textContent).toContain("本地报告已打开。"));
  });

  test.each([
    ["消息拒绝", { stopFails: true }],
    ["worker 未确认响应", { stopResponse: { status: "unconfirmed" } }],
    ["worker 失败响应", { stopResponse: { status: "failed" } }]
  ])("does not offer a stop retry when %s leaves the stop state unconfirmed", async (_label, failure) => {
    const progress: Progress = { job_id: "job_run", phase: "scanning", discovered: 1, inspected: 1, eligible: 1, selected: 0, saved: 0 };
    await render({ page: { page: "search" }, progress, ...failure });

    field<HTMLButtonElement>("#stop-action").click();
    await vi.waitFor(() => expect(field<HTMLElement>("#status").textContent).toContain("停止状态尚未确认，采集可能已中断。请等待已持久化的状态更新。"));
    expect(field<HTMLElement>("#status").textContent).not.toMatch(/重试|切换|权限/u);
  });

  test("keeps report-open failures separate from stop acknowledgement feedback", async () => {
    const progress: Progress = { job_id: "job_report", phase: "complete", discovered: 1, inspected: 1, eligible: 1, selected: 1, saved: 1, report_available: true };
    await render({ page: { page: "current", note_id: "note_123" }, progress, reportFails: true });

    field<HTMLButtonElement>("#report-action").click();
    await vi.waitFor(() => expect(field<HTMLElement>("#status").textContent).toContain("本地报告暂时无法打开，请稍后重试。"));
  });

  test("rejects malformed stored progress rather than turning it into a report command", async () => {
    const progress = { job_id: 123, phase: "complete", discovered: 1, inspected: 1, eligible: 1, selected: 1, saved: 1, report_available: true } as unknown as Progress;
    await render({ page: { page: "current", note_id: "note_123" }, progress });

    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(true);
  });

  test("updates the open popup from validated sanitized storage progress", async () => {
    loadTemplate();
    const controls = setup({ page: { page: "search" } });
    await mountPopup(document, controls.api);

    controls.emitProgress({ job_id: "job_updated", phase: "complete", discovered: 4, inspected: 4, eligible: 2, selected: 2, saved: 2, report_available: true });

    expect(field<HTMLElement>("#status").textContent).toBe("采集完成。已发现 4 篇；已检查 4 篇候选帖子；已选择 2 篇。本地结果包已写入，可打开本地报告。");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(false);
    expect(field<HTMLElement>("#count-saved").textContent).toBe("2");
  });

  test("keeps a completed one-post multi-media run as a verified terminal state", async () => {
    const { emitProgress } = await render({
      page: { page: "current", note_id: "note_123" },
      progress: { job_id: "job_media", phase: "downloading", discovered: 1, inspected: 1, eligible: 1, selected: 1, saved: 1 }
    });

    emitProgress({ job_id: "job_media", phase: "complete", discovered: 1, inspected: 1, eligible: 1, selected: 1, saved: 3, report_available: true });

    expect(field<HTMLElement>("#status").textContent).toContain("采集完成。");
    expect(field<HTMLElement>("#status").textContent).not.toContain("采集状态未返回");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(false);
    expect(field<HTMLElement>("#count-saved").textContent).toBe("3");
  });

  test("maps a post-permission collection dispatch failure to a finite recoverable state", async () => {
    const { send } = await render({ page: { page: "current", note_id: "note_123" }, collectionFails: true });
    send.mockClear();

    field<HTMLButtonElement>("#current-action").click();
    await vi.waitFor(() => expect(field<HTMLElement>("#status").textContent).toContain("本地采集服务暂不可用，请检查安装状态。"));
    expect(field<HTMLButtonElement>("#current-action").disabled).toBe(false);
  });

  test.each([
    ["complete", "complete", "本地报告已验证：采集完成。"],
    ["partial", "partial", "本地报告已验证：已完成部分采集。"],
    ["stopped", "stopped", "本地报告已验证：采集已停止。"],
    ["failed", "error", "本地报告已验证：采集未完成。"]
  ])("restores the %s terminal receipt only after the local report verifies it", async (terminalStatus, expectedPhase, copy) => {
    const { emitProgress, send, set } = await render({
      page: { page: "current", note_id: "note_123" },
      reportResponse: { status: "opened", terminal_status: terminalStatus }
    });
    const running: Progress = {
      job_id: "job_current", phase: "ranking", discovered: 1, inspected: 1,
      eligible: 1, selected: 1, saved: 0
    };

    emitProgress(running);
    emitProgress(undefined);

    expect(field<HTMLElement>("#status").textContent).toContain("采集状态未返回");
    expect(field<HTMLElement>("#status").textContent).not.toContain("已验证");
    expect(field<HTMLElement>("#status").textContent).not.toContain("当前为帖子详情页");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(false);

    field<HTMLButtonElement>("#report-action").click();

    await vi.waitFor(() => expect(send).toHaveBeenCalledWith({ kind: "open_report", job_id: "job_current" }));
    await vi.waitFor(() => expect(field<HTMLElement>("#status").textContent).toContain(copy));
    expect(set).not.toHaveBeenCalled();
  });

  test("keeps a missing terminal receipt unconfirmed for a legacy opened-only report response", async () => {
    const { emitProgress, send } = await render({ page: { page: "current", note_id: "note_123" } });
    const running: Progress = {
      job_id: "job_current", phase: "ranking", discovered: 1, inspected: 1,
      eligible: 1, selected: 1, saved: 0
    };

    emitProgress(running);
    emitProgress(undefined);
    field<HTMLButtonElement>("#report-action").click();

    await vi.waitFor(() => expect(send).toHaveBeenCalledWith({ kind: "open_report", job_id: "job_current" }));
    expect(field<HTMLElement>("#status").textContent).toContain("本地报告已打开，采集终态仍未返回。");
  });

  test("keeps a verified report terminal state over late storage events from the same job, but accepts a new job", async () => {
    const { emitProgress, send } = await render({
      page: { page: "current", note_id: "note_123" },
      reportResponse: { status: "opened", terminal_status: "complete" }
    });
    const previous: Progress = {
      job_id: "job_previous", phase: "ranking", discovered: 1, inspected: 1,
      eligible: 1, selected: 1, saved: 0
    };

    emitProgress(previous);
    emitProgress(undefined);
    field<HTMLButtonElement>("#report-action").click();
    await vi.waitFor(() => expect(send).toHaveBeenCalledWith({ kind: "open_report", job_id: "job_previous" }));
    await vi.waitFor(() => expect(field<HTMLElement>("#status").textContent).toContain("本地报告已验证：采集完成。"));

    emitProgress(undefined);
    expect(field<HTMLElement>("#status").textContent).toContain("本地报告已验证：采集完成。");
    emitProgress(previous);
    expect(field<HTMLElement>("#status").textContent).toContain("本地报告已验证：采集完成。");

    emitProgress({ job_id: "job_new", phase: "scanning", discovered: 1, inspected: 0, eligible: 0, selected: 0, saved: 0 });
    expect(field<HTMLElement>("#status").textContent).toContain("正在采集。已发现 1 篇；已完成详情 0 / 0。");
  });

  test("clears a recovered job before a newly accepted collection can receive its first progress", async () => {
    const { emitProgress, send } = await render({
      page: { page: "current", note_id: "note_123" },
      reportResponse: { status: "opened", terminal_status: "complete" }
    });
    const previous: Progress = {
      job_id: "job_previous", phase: "ranking", discovered: 1, inspected: 1,
      eligible: 1, selected: 1, saved: 0
    };

    emitProgress(previous);
    emitProgress(undefined);
    field<HTMLButtonElement>("#report-action").click();
    await vi.waitFor(() => expect(field<HTMLElement>("#status").textContent).toContain("本地报告已验证：采集完成。"));

    field<HTMLButtonElement>("#current-action").click();
    await vi.waitFor(() => expect(send).toHaveBeenCalledWith({ kind: "collect_current", tab: { id: 17 } }));
    expect(field<HTMLElement>("#status").textContent).not.toContain("本地报告已验证");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(true);

    emitProgress(undefined);
    expect(field<HTMLElement>("#status").textContent).not.toContain("本地报告已验证");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(true);
  });

  test("does not let a stale report response overwrite a newer job progress state", async () => {
    let resolveReport: ((response: unknown) => void) | undefined;
    const reportResponse = new Promise<unknown>((resolve) => { resolveReport = resolve; });
    const { emitProgress } = await render({ page: { page: "current", note_id: "note_123" }, reportResponse });
    emitProgress({ job_id: "job_previous", phase: "ranking", discovered: 1, inspected: 1, eligible: 1, selected: 1, saved: 0 });
    emitProgress(undefined);
    field<HTMLButtonElement>("#report-action").click();
    emitProgress({ job_id: "job_new", phase: "scanning", discovered: 1, inspected: 0, eligible: 0, selected: 0, saved: 0 });

    resolveReport?.({ status: "opened" });
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(field<HTMLElement>("#status").textContent).toContain("正在采集。已发现 1 篇；已完成详情 0 / 0。");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(true);
  });
});

describe("video report progress", () => {
  const videoProgress: Progress = {job_id:"job_video",phase:"complete",discovered:1,inspected:1,eligible:1,selected:1,saved:1,report_available:true,video_processing:{status:"running"}};
  test("reopens a published video job, keeps report available and disables duplicate collection", async () => {
    vi.useFakeTimers();
    const controls = await render({page:{page:"current",note_id:"n1"},progress:videoProgress});
    await Promise.resolve(); await Promise.resolve();
    expect(controls.send).toHaveBeenCalledWith({kind:"video_status",job_id:"job_video"});
    expect(field<HTMLButtonElement>("#current-action").disabled).toBe(true);
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(false);
    expect(field<HTMLElement>("#status").textContent).toContain("音频转录中");
    field<HTMLButtonElement>("#stop-action").click();
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(controls.send).toHaveBeenCalledWith({kind:"video_stop",job_id:"job_video"});
    controls.emitProgress(videoProgress);
    expect(field<HTMLElement>("#status").textContent).toContain("已停止");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(false);
    window.dispatchEvent(new Event("pagehide"));
    vi.useRealTimers();
  });
  test("an older pending status response cannot undo a newer Stop", async () => {
    vi.useFakeTimers();
    let release: ((value:unknown)=>void) | undefined;
    const pending = new Promise<unknown>(resolve => {release=resolve;});
    await render({progress:videoProgress,videoResponse:pending});
    field<HTMLButtonElement>("#stop-action").click();
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    release?.({status:"ok",job_id:"job_video",processing:{status:"running"}});
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(field<HTMLElement>("#status").textContent).toContain("已停止");
    expect(field<HTMLButtonElement>("#stop-action").hidden).toBe(true);
    window.dispatchEvent(new Event("pagehide"));
    vi.useRealTimers();
  });
  test("recovers a host-published report when the MV3 terminal receipt was lost", async () => {
    vi.useFakeTimers();
    const {video_processing: _ignored,...withoutSummary} = videoProgress;
    const controls = await render({progress:{...withoutSummary,phase:"downloading",report_available:false}});
    await Promise.resolve(); await Promise.resolve();
    expect(controls.send).toHaveBeenCalledWith({kind:"video_status",job_id:"job_video"});
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(false);
    expect(field<HTMLElement>("#status").textContent).toContain("音频转录中");
    window.dispatchEvent(new Event("pagehide"));
    vi.useRealTimers();
  });
  test("recovers completed worker status after popup closure", async () => {
    await render({page:{page:"current",note_id:"n1"},progress:videoProgress,videoResponse:{status:"ok",job_id:"job_video",processing:{status:"complete"}}});
    await Promise.resolve(); await Promise.resolve();
    expect(field<HTMLElement>("#status").textContent).toContain("音频转录已完成");
    expect(field<HTMLButtonElement>("#stop-action").hidden).toBe(true);
    expect(field<HTMLButtonElement>("#current-action").disabled).toBe(false);
  });
  test("report update failure is visible while saved report stays available", async () => {
    await render({progress:videoProgress,videoResponse:{status:"ok",job_id:"job_video",processing:{status:"complete",report_update_failed:true}}});
    await Promise.resolve(); await Promise.resolve();
    expect(field<HTMLElement>("#status").textContent).toContain("文字稿已生成，报告更新失败");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(false);
  });
  test("status connection failure cannot invent a terminal ASR result or hide the report", async () => {
    await render({progress:videoProgress,videoResponse:{status:"failed"}});
    await Promise.resolve(); await Promise.resolve();
    expect(field<HTMLElement>("#status").textContent).toContain("暂时无法确认转录状态");
    expect(field<HTMLButtonElement>("#report-action").hidden).toBe(false);
    window.dispatchEvent(new Event("pagehide"));
  });
});
