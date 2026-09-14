import { readVideoProcessing, videoIsRunning, videoProcessingCopy, type VideoProcessing } from "../video-processing.js";
import { isAuthorIdentityReason, type AuthorIdentityReason } from "../dom/detail.js";

declare global {
  var __XHS_BUILD_REVISION__: string | undefined;
}

const PREFERENCES_KEY = "xhs_popup_preferences";
const PROGRESS_KEY = "xhs_job_progress";
const SEARCH_RISK_KEY = "xhs_search_risk";
const BATCH_COOLDOWN_MS = 60_000;
const MAX_COUNT = 100;
const MAX_SAVED_MEDIA_COUNT = 20 * 22;
const safeId = /^[A-Za-z0-9_-]{1,128}$/u;

type PageState = Readonly<{ page: "current"; note_id: string }> | Readonly<{ page: "search" | "account" | "unsupported" }> | Readonly<{ page: "blocked"; reason: "login_required" | "challenge_detected" }>;
type ProgressPhase = "starting" | "scanning" | "ranking" | "downloading" | "complete" | "partial" | "stopped" | "error";
type DetailStage = "route" | "detail_root" | "author_identity" | "structured_state" | "media_binding" | "snapshot_validation" | "unexpected";
type Preferences = Readonly<{ requestedCount: 5 | 10 }>;
type Progress = Readonly<{ job_id: string; phase: ProgressPhase; discovered: number; inspected: number; eligible: number; selected: number; saved: number; report_available: boolean; video_processing?:VideoProcessing; current_source_position?: number; detail_stage?: DetailStage; detail_reason?: AuthorIdentityReason; error?: string }>;

export type PopupChromeApi = Readonly<{
  runtime: Readonly<{ sendMessage: (message: unknown) => Promise<unknown> }>;
  permissions: Readonly<{ request: (permissions: chrome.permissions.Permissions) => Promise<boolean> }>;
  storage: Readonly<{ local: Readonly<{ get: (keys: string[]) => Promise<Record<string, unknown>>; set: (items: Record<string, unknown>) => Promise<void> }>; onChanged: Readonly<{ addListener: (listener: (changes: Record<string, chrome.storage.StorageChange>, areaName: string) => void) => void }> }>;
  tabs: Readonly<{ query: (queryInfo: chrome.tabs.QueryInfo) => Promise<chrome.tabs.Tab[]> }>;
}>;

const defaultPreferences: Preferences = { requestedCount: 5 };
const stopStateUnconfirmedCopy = "停止状态尚未确认，采集可能已中断。请等待已持久化的状态更新。";

function element<T extends HTMLElement>(documentRef: Document, selector: string): T {
  const found = documentRef.querySelector<T>(selector);
  if (found === null) throw new Error(`popup element ${selector} is missing`);
  return found;
}
function isCount(value: unknown, minimum: number, maximum: number): value is number { return typeof value === "number" && Number.isInteger(value) && value >= minimum && value <= maximum; }
function isDetailStage(value: unknown): value is DetailStage { return value === "route" || value === "detail_root" || value === "author_identity" || value === "structured_state" || value === "media_binding" || value === "snapshot_validation" || value === "unexpected"; }
function readPreferences(value: unknown): Preferences {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return defaultPreferences;
  const requestedCount = (value as Record<string, unknown>).requestedCount;
  return requestedCount === 5 || requestedCount === 10 ? { requestedCount } : defaultPreferences;
}
function optionalVideo(value:unknown): VideoProcessing | undefined { try { return readVideoProcessing(value); } catch { return undefined; } }
function readProgress(value: unknown): Progress | undefined {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  if (typeof record.job_id !== "string" || !safeId.test(record.job_id) || !["starting", "scanning", "ranking", "downloading", "complete", "partial", "stopped", "error"].includes(String(record.phase)) || !isCount(record.discovered, 0, MAX_COUNT) || !isCount(record.inspected, 0, MAX_COUNT) || !isCount(record.eligible, 0, MAX_COUNT) || !isCount(record.selected, 0, MAX_COUNT) || !isCount(record.saved, 0, MAX_SAVED_MEDIA_COUNT) || record.inspected > record.discovered || record.eligible > record.discovered || record.selected > record.eligible) return undefined;
  return { job_id: record.job_id, phase: record.phase as ProgressPhase, discovered: record.discovered, inspected: record.inspected, eligible: record.eligible, selected: record.selected, saved: record.saved, report_available: record.report_available === true, ...(optionalVideo(record.video_processing) === undefined ? {} : {video_processing:optionalVideo(record.video_processing)!}), ...(isCount(record.current_source_position, 1, MAX_COUNT) ? { current_source_position: record.current_source_position } : {}), ...(isDetailStage(record.detail_stage) ? { detail_stage: record.detail_stage } : {}), ...(record.detail_stage === "author_identity" && isAuthorIdentityReason(record.detail_reason) ? { detail_reason: record.detail_reason } : {}), ...(typeof record.error === "string" ? { error: record.error } : {}) };
}
function readCooldownUntil(value: unknown): number | undefined {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
  const startedAt = (value as Record<string, unknown>).last_search_batch_started_at;
  if (typeof startedAt !== "number" || !Number.isSafeInteger(startedAt) || startedAt < 0) return undefined;
  return startedAt + BATCH_COOLDOWN_MS;
}
function pageCopy(page: PageState): string {
  if (page.page === "current") return `当前为帖子详情页（编号：${page.note_id}），可提取这篇帖子。`;
  if (page.page === "search") return "当前为搜索结果页，可按当前页面顺序采集。";
  if (page.page === "account") return "账号主页不在本次搜索采集范围内。";
  if (page.page === "blocked") return page.reason === "login_required" ? "请先登录小红书后再试。" : "检测到验证页，请先在当前 Chrome 页面完成验证后再试。";
  return "请打开帖子详情或搜索结果页后再试。";
}
function permissionCopy(page: PageState): string { return page.page === "current" ? "将请求媒体访问权限，仅用于提取当前帖子。" : page.page === "search" ? "将请求小红书页面与媒体访问权限，用于按当前页面顺序采集。" : "当前页面不支持采集，不会请求额外权限。"; }
function commandStatus(value: unknown, expected: "accepted" | "stopped" | "opened"): boolean { return value !== null && typeof value === "object" && !Array.isArray(value) && (value as Record<string, unknown>).status === expected; }
function reportTerminalStatus(value: unknown): "complete" | "partial" | "stopped" | "failed" | undefined { if (!commandStatus(value, "opened")) return undefined; const status = (value as Record<string, unknown>).terminal_status; return status === "complete" || status === "partial" || status === "stopped" || status === "failed" ? status : undefined; }
function recoveredPhase(status: "complete" | "partial" | "stopped" | "failed"): ProgressPhase { return status === "failed" ? "error" : status; }
function recoveredStatusCopy(status: "complete" | "partial" | "stopped" | "failed"): string { return status === "complete" ? "本地报告已验证：采集完成。" : status === "partial" ? "本地报告已验证：已完成部分采集。" : status === "stopped" ? "本地报告已验证：采集已停止。" : "本地报告已验证：采集未完成。"; }
function isRunning(progress: Progress | undefined): boolean { return progress?.phase === "starting" || progress?.phase === "scanning" || progress?.phase === "ranking" || progress?.phase === "downloading"; }
function isTerminal(progress: Progress | undefined): boolean { return progress !== undefined && !isRunning(progress); }
function hasWrittenBundle(progress: Progress | undefined): boolean { return progress?.report_available === true && isTerminal(progress); }
function buildRevisionCopy(revision: string | undefined): string {
  return /^[0-9a-f]{12}$/u.test(revision ?? "") ? `构建版本：${revision}` : "构建版本：未识别";
}
function detailStageCopy(stage: DetailStage | undefined): string {
  switch (stage) {
    case "route": return "详情检查阶段：页面路由。";
    case "detail_root": return "详情检查阶段：详情根节点。";
    case "author_identity": return "详情检查阶段：作者身份。";
    case "structured_state": return "详情检查阶段：页面结构化状态。";
    case "media_binding": return "详情检查阶段：媒体绑定。";
    case "snapshot_validation": return "详情检查阶段：结果校验。";
    case "unexpected": return "详情检查阶段：未分类本地检查。";
    default: return "";
  }
}
const authorReasonCopy: Readonly<Record<AuthorIdentityReason, string>> = {
  no_visible_profile_anchor: "未找到可见的作者主页链接。",
  duplicate_same_author: "检测到多个指向同一作者的主页链接。",
  duplicate_different_author: "检测到指向不同作者的主页链接。",
  malformed_profile_path: "作者主页链接路径格式不符合要求。",
  unsafe_profile_origin: "作者主页链接来源不符合安全要求。",
  unparseable_author_anchor: "作者区域存在无法解析的链接。"
};
function errorCopy(error: string | undefined, detailStage?: DetailStage, detailReason?: AuthorIdentityReason): string {
  switch (error) {
    case "cooldown": return "请在 60 秒冷却结束后再开始新的批量采集。诊断代码：cooldown。";
    case "detail_unavailable": return `当前帖子详情暂不可用，未继续处理。诊断代码：detail_unavailable。${detailStageCopy(detailStage)}${detailStage === "author_identity" && isAuthorIdentityReason(detailReason) ? authorReasonCopy[detailReason] : ""}`;
    case "login_required": return "当前页面需要登录，采集已停止。诊断代码：login_required。";
    case "challenge_detected": return "当前页面需要完成验证，采集已停止。诊断代码：challenge_detected。";
    case "stopped": return "采集已停止。诊断代码：stopped。";
    case "optional_permission_required": return "未获得所需媒体访问权限。诊断代码：optional_permission_required。";
    case "invalid_batch_bounds": return "批量采集设置无效。诊断代码：invalid_batch_bounds。";
    case "unsupported_current_tab": return "当前页面未识别为帖子详情。诊断代码：unsupported_current_tab。";
    case "unsupported_batch_tab": return "当前页面不支持批量采集。诊断代码：unsupported_batch_tab。";
    case "job_in_progress": return "已有采集任务正在处理。诊断代码：job_in_progress。";
    case "native_host_unavailable": return "本地采集服务未连接。诊断代码：native_host_unavailable。";
    case "native_host_version_mismatch": return "本地采集服务版本不匹配。诊断代码：native_host_version_mismatch。";
    case "native_response_invalid": return "本地采集服务返回无效响应。诊断代码：native_response_invalid。";
    case "native_host_error": return "本地采集服务发生受限错误。诊断代码：native_host_error。";
    case "media_transfer_failed": return "媒体传输失败，未继续处理。诊断代码：media_transfer_failed。";
    case "native_host_interrupted": return "采集已中断，未完成。本地采集服务连接已中断，未继续处理。诊断代码：native_host_interrupted。";
    case "source_tab_closed": return "采集已中断，未完成。原始页面已关闭，未继续处理。诊断代码：source_tab_closed。";
    case "queue_tab_closed": return "采集已中断，未完成。采集队列页面已关闭，未继续处理。诊断代码：queue_tab_closed。";
    case "identity_mismatch": return "采集已中断，未完成。页面身份与开始时不一致，未继续处理。诊断代码：identity_mismatch。";
    default: return "采集已中断，未完成。发生未识别的本地错误，未继续处理。";
  }
}
function hasDetailProgress(progress: Progress): boolean { return progress.inspected <= progress.selected; }
function progressSummary(progress: Progress): string {
  return hasDetailProgress(progress)
    ? `已发现 ${progress.discovered} 篇；已完成详情 ${progress.inspected} / ${progress.selected}。`
    : `已发现 ${progress.discovered} 篇；已检查 ${progress.inspected} 篇候选帖子；已选择 ${progress.selected} 篇。`;
}
function progressCopy(progress: Progress): string {
  const detail = progressSummary(progress);
  if (progress.report_available && progress.video_processing?.status !== undefined && progress.video_processing.status !== "none") return videoProcessingCopy(progress.video_processing);
  if (progress.phase === "starting") return "正在准备采集。";
  if (progress.phase === "scanning") return `正在采集。${detail}`;
  if (progress.phase === "ranking") return `已按当前页面顺序冻结采集范围。${detail}`;
  if (progress.phase === "downloading") return `正在整理已验证的采集结果。${detail}`;
  if (progress.phase === "complete") return hasWrittenBundle(progress) ? `采集完成。${detail}本地结果包已写入，可打开本地报告。` : `采集完成。${detail}尚未收到本地结果包写入确认。`;
  if (progress.phase === "partial") return `已完成部分采集。${detail}${errorCopy(progress.error, progress.detail_stage, progress.detail_reason)}`;
  if (progress.phase === "stopped") return `采集已停止。${detail}`;
  return errorCopy(progress.error, progress.detail_stage, progress.detail_reason);
}

/** Mount the compact popup; collection results remain authoritative in the native host. */
export async function mountPopup(documentRef: Document, api: PopupChromeApi): Promise<void> {
  const status = element<HTMLElement>(documentRef, "#status"); const currentAction = element<HTMLButtonElement>(documentRef, "#current-action"); const batchAction = element<HTMLButtonElement>(documentRef, "#batch-action"); const stopAction = element<HTMLButtonElement>(documentRef, "#stop-action"); const reportAction = element<HTMLButtonElement>(documentRef, "#report-action"); const collectionOptions = element<HTMLElement>(documentRef, "#collection-options"); const permission = element<HTMLElement>(documentRef, "#permission-copy"); const progressCounts = element<HTMLElement>(documentRef, "#progress-counts"); const currentCandidate = element<HTMLElement>(documentRef, "#current-candidate"); const requested = element<HTMLSelectElement>(documentRef, "#requested-count"); const reportIdentifier = element<HTMLElement>(documentRef, "#report-identifier"); const buildRevision = element<HTMLElement>(documentRef, "#build-revision");
  let activeTab: Readonly<{ id: number }> | undefined; let page: PageState = { page: "unsupported" }; let progress: Progress | undefined; let cooldownUntil: number | undefined; let pendingPermission = false; let dispatching = false; let stopping = false; let reporting = false; let terminalReceiptMissing = false; let verifiedTerminalJobId: string | undefined; let statusOverride: string | undefined;
  const preference = (): Preferences | undefined => requested.value === "5" ? { requestedCount: 5 } : requested.value === "10" ? { requestedCount: 10 } : undefined;
  const updateCooldown = (value: unknown): void => {
    cooldownUntil = readCooldownUntil(value);
    if (cooldownUntil !== undefined && Date.now() < cooldownUntil) setTimeout(render, cooldownUntil - Date.now());
  };
  const render = (): void => {
    const running = (isRunning(progress) && !terminalReceiptMissing) || videoIsRunning(progress?.video_processing); const reportRecoverable = terminalReceiptMissing && progress !== undefined; const reportAvailable = hasWrittenBundle(progress) || reportRecoverable; const cooldownActive = cooldownUntil !== undefined && Date.now() < cooldownUntil; const canStart = activeTab !== undefined && !running && !terminalReceiptMissing && !pendingPermission && !dispatching;
    status.textContent = statusOverride ?? (progress === undefined ? pageCopy(page) : progressCopy(progress)); collectionOptions.hidden = page.page !== "search"; currentAction.hidden = page.page !== "current"; batchAction.hidden = page.page !== "search"; currentAction.disabled = !(page.page === "current" && canStart); batchAction.disabled = !(page.page === "search" && canStart && !cooldownActive && preference() !== undefined); requested.disabled = !canStart || cooldownActive || page.page !== "search"; stopAction.hidden = !running; stopAction.disabled = stopping; reportAction.hidden = !reportAvailable; reportAction.disabled = reporting; reportIdentifier.hidden = !reportAvailable; reportIdentifier.textContent = hasWrittenBundle(progress) ? "本地结果包已确认：可打开本地报告" : reportRecoverable ? "采集状态未返回：可尝试验证并打开本次本地报告" : ""; progressCounts.hidden = progress === undefined;
    element<HTMLElement>(documentRef, "#count-discovered").textContent = String(progress?.discovered ?? 0); element<HTMLElement>(documentRef, "#count-inspected").textContent = String(progress?.inspected ?? 0); element<HTMLElement>(documentRef, "#count-inspected-label").textContent = progress !== undefined && !hasDetailProgress(progress) ? "已检查" : "已完成详情"; element<HTMLElement>(documentRef, "#count-eligible").textContent = String(progress?.eligible ?? 0); element<HTMLElement>(documentRef, "#count-selected").textContent = String(progress?.selected ?? 0); element<HTMLElement>(documentRef, "#count-saved").textContent = String(progress?.saved ?? 0);
    const position = progress?.current_source_position; currentCandidate.hidden = !(running && page.page === "search" && position !== undefined && progress !== undefined && hasDetailProgress(progress)); currentCandidate.textContent = position === undefined ? "" : `当前帖子：第 ${position} 篇`; permission.textContent = permissionCopy(page);
    buildRevision.textContent = buildRevisionCopy(globalThis.__XHS_BUILD_REVISION__);
  };
  const start = async (kind: "collect_current" | "collect_batch"): Promise<void> => {
    if (activeTab === undefined || pendingPermission || dispatching || isRunning(progress) || videoIsRunning(progress?.video_processing) || (kind === "collect_batch" && cooldownUntil !== undefined && Date.now() < cooldownUntil)) return;
    const choice = preference(); if (kind === "collect_batch" && choice === undefined) return;
    try {
      const origins = kind === "collect_current" ? ["https://*.xhscdn.com/*"] : ["https://www.xiaohongshu.com/*", "https://*.xhscdn.com/*"]; const granted = api.permissions.request({ origins }); pendingPermission = true; statusOverride = "正在等待权限确认。"; render();
      if (!await granted) { pendingPermission = false; statusOverride = "未授予所需权限，采集未开始。"; render(); return; }
      pendingPermission = false; dispatching = true; await api.storage.local.set({ [PREFERENCES_KEY]: choice ?? defaultPreferences });
      const response = kind === "collect_current" ? await api.runtime.sendMessage({ kind, tab: activeTab }) : await api.runtime.sendMessage({ kind, tab: activeTab, options: { requestedCount: choice?.requestedCount, candidateScanLimit: choice?.requestedCount, selectionOrder: "page_order" } });
      if (!commandStatus(response, "accepted")) throw new TypeError("collection command rejected"); dispatching = false; if (verifiedTerminalJobId !== undefined && progress?.job_id === verifiedTerminalJobId) { progress = undefined; terminalReceiptMissing = false; verifiedTerminalJobId = undefined; } statusOverride = undefined; render();
    } catch { pendingPermission = false; dispatching = false; statusOverride = "本地采集服务暂不可用，请检查安装状态。"; render(); }
  };
  let videoTimer: ReturnType<typeof setTimeout> | undefined;
  let videoQueryPending = false;
  let videoQueryEpoch = 0;
  let verifiedVideo: Readonly<{jobId:string;processing:VideoProcessing}> | undefined;
  let popupClosed = false;
  documentRef.defaultView?.addEventListener("pagehide", () => {popupClosed = true; if (videoTimer !== undefined) clearTimeout(videoTimer);});
  const queryVideo = async (stop = false, recover = false): Promise<void> => {
    if (popupClosed || (videoQueryPending && !stop) || progress === undefined || (!progress.report_available && !recover)) return;
    const jobId = progress.job_id;
    const epoch = ++videoQueryEpoch;
    let querySucceeded = false;
    videoQueryPending = true;
    if (videoTimer !== undefined) clearTimeout(videoTimer);
    try {
      const response = await api.runtime.sendMessage({kind:stop ? "video_stop" : "video_status",job_id:jobId});
      if (response === null || typeof response !== "object" || Array.isArray(response)) throw new TypeError("video state unavailable");
      const result = response as Record<string,unknown>;
      if (result.status !== "ok" || result.job_id !== jobId) throw new TypeError("video state unavailable");
      const processing = readVideoProcessing(result.processing);
      querySucceeded = true;
      if (epoch === videoQueryEpoch && progress?.job_id === jobId) {
        if (recover && !progress.report_available) terminalReceiptMissing = isRunning(progress);
        verifiedVideo = {jobId,processing};
        progress = {...progress,report_available:true,video_processing:processing};
        statusOverride = processing.status === "none" && terminalReceiptMissing ? "本地报告已找到，请打开报告核对采集结果。" : undefined;
      }
    } catch {
      if (epoch === videoQueryEpoch && (!recover || progress?.report_available) && progress?.job_id === jobId) statusOverride = "暂时无法确认转录状态；已保存的报告仍可打开。稍后重新打开插件查看。";
    } finally {
      if (epoch === videoQueryEpoch) videoQueryPending = false;
      render();
      if (epoch === videoQueryEpoch && querySucceeded && !popupClosed && videoIsRunning(progress?.video_processing)) videoTimer = setTimeout(() => {void queryVideo();},2000);
    }
  };
  requested.addEventListener("change", render); currentAction.addEventListener("click", () => { void start("collect_current"); }); batchAction.addEventListener("click", () => { void start("collect_batch"); });
  stopAction.addEventListener("click", () => { if ((!isRunning(progress) && !videoIsRunning(progress?.video_processing)) || stopping) return;
    if (videoIsRunning(progress?.video_processing) && progress !== undefined) { stopping = true; statusOverride = "正在停止转录，已保存的视频和报告会保留。"; render(); void queryVideo(true).finally(() => {stopping = false; render();}); return; } stopping = true; statusOverride = "正在停止采集，请等待当前步骤结束。"; render(); void api.runtime.sendMessage({ kind: "stop_job" }).then((response) => { stopping = false; if (!commandStatus(response, "stopped")) statusOverride = stopStateUnconfirmedCopy; else if (progress !== undefined) progress = { ...progress, phase: "stopped", error: "stopped" }; render(); }, () => { stopping = false; statusOverride = stopStateUnconfirmedCopy; render(); }); });
  reportAction.addEventListener("click", () => { if (!(hasWrittenBundle(progress) || (terminalReceiptMissing && progress !== undefined)) || reporting || progress === undefined) return; const reportJobId = progress.job_id; reporting = true; statusOverride = "正在打开本地报告。"; render(); void api.runtime.sendMessage({ kind: "open_report", job_id: reportJobId }).then((response) => { reporting = false; if (progress?.job_id !== reportJobId) { render(); return; } const terminalStatus = reportTerminalStatus(response); if (terminalReceiptMissing && terminalStatus !== undefined && progress !== undefined) { progress = { ...progress, phase: recoveredPhase(terminalStatus), report_available: true }; terminalReceiptMissing = false; verifiedTerminalJobId = reportJobId; statusOverride = recoveredStatusCopy(terminalStatus); } else if (commandStatus(response, "opened")) statusOverride = terminalReceiptMissing ? "本地报告已打开，采集终态仍未返回。" : "本地报告已打开。"; else statusOverride = "本地报告暂时无法打开，请稍后重试。"; render(); }, () => { reporting = false; if (progress?.job_id === reportJobId) statusOverride = "本地报告暂时无法打开，请稍后重试。"; render(); }); });
  api.storage.onChanged.addListener((changes, areaName) => { if (areaName !== "local") return; if (Object.hasOwn(changes, SEARCH_RISK_KEY)) updateCooldown(changes[SEARCH_RISK_KEY]?.newValue); if (!Object.hasOwn(changes, PROGRESS_KEY) && !Object.hasOwn(changes, SEARCH_RISK_KEY)) return; if (Object.hasOwn(changes, PROGRESS_KEY)) { let next = readProgress(changes[PROGRESS_KEY]?.newValue); if (next !== undefined && next.job_id === verifiedVideo?.jobId) next = {...next,report_available:true,video_processing:verifiedVideo.processing}; const ignoresVerifiedTerminal = verifiedTerminalJobId !== undefined && (next === undefined || next.job_id === verifiedTerminalJobId); if (!ignoresVerifiedTerminal) { if (next === undefined && progress !== undefined) { terminalReceiptMissing = true; statusOverride = "采集状态未返回，尚不能确认完成；可尝试验证并打开本次本地报告。"; } else { progress = next; terminalReceiptMissing = false; statusOverride = undefined; } if (next?.job_id !== verifiedTerminalJobId) verifiedTerminalJobId = undefined; } stopping = false; } render(); if (videoIsRunning(progress?.video_processing)) void queryVideo(); });
  try {
    const stored = await api.storage.local.get([PREFERENCES_KEY, PROGRESS_KEY, SEARCH_RISK_KEY]); requested.value = String(readPreferences(stored[PREFERENCES_KEY]).requestedCount); progress = readProgress(stored[PROGRESS_KEY]); updateCooldown(stored[SEARCH_RISK_KEY]); const tab = (await api.tabs.query({ active: true, currentWindow: true }))[0];
    if (tab !== undefined && typeof tab.id === "number" && Number.isInteger(tab.id)) { activeTab = { id: tab.id }; const inspected = await api.runtime.sendMessage({ kind: "inspect_active_page", tab: activeTab }); if (inspected !== null && typeof inspected === "object" && !Array.isArray(inspected)) { const result = inspected as Record<string, unknown>; if (result.page === "current" && typeof result.note_id === "string" && safeId.test(result.note_id)) page = { page: "current", note_id: result.note_id }; else if (result.page === "search" || result.page === "account" || result.page === "unsupported") page = { page: result.page }; else if (result.page === "blocked" && (result.reason === "login_required" || result.reason === "challenge_detected")) page = { page: "blocked", reason: result.reason }; } }
    render();
    if (progress !== undefined && (progress.video_processing !== undefined || isRunning(progress))) void queryVideo(false, true);
  } catch { progress = undefined; page = { page: "unsupported" }; statusOverride = "本地采集服务暂不可用，请检查安装状态。"; render(); }
}
if (typeof chrome !== "undefined" && typeof document !== "undefined") void mountPopup(document, chrome as unknown as PopupChromeApi);
