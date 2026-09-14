import { readVideoProcessing, type VideoProcessing } from "./video-processing.js";
import { defaultJobControllerDependencies, JobController, type BatchOptions } from "./job-controller.js";
import { NativeClient } from "./native-client.js";
import { isSafeId } from "./security.js";

const HEALTH_MESSAGE = "xhs-extension-health";
const offsetTimestamp = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?[+-]\d{2}:\d{2}$/u;

export type WorkerJobRunner = Readonly<{
  videoStatus?: (jobId:string, stop:boolean) => Promise<VideoProcessing>;
  collectCurrent: (tab: chrome.tabs.Tab) => Promise<void>;
  collectBatch: (tab: chrome.tabs.Tab, options: BatchOptions) => Promise<void>;
  stop: () => Promise<void>;
  inspectActivePage: (tab: chrome.tabs.Tab) => Promise<WorkerPageState>;
  openReport: (jobId: string) => Promise<Readonly<{ terminal_status?: "complete" | "partial" | "stopped" | "failed" }>>;
}>;

export type WorkerPageState =
  | Readonly<{ page: "current"; note_id: string }>
  | Readonly<{ page: "search" | "account" | "unsupported" }>
  | Readonly<{ page: "blocked"; reason: "login_required" | "challenge_detected" }>;

type WorkerMessage =
  | Readonly<{ kind: "collect_current"; tab: Readonly<{ id: number }> }>
  | Readonly<{ kind: "collect_batch"; tab: Readonly<{ id: number }>; options: BatchOptions }>
  | Readonly<{ kind: "stop_job" }>
  | Readonly<{ kind: "inspect_active_page"; tab: Readonly<{ id: number }> }>
  | Readonly<{ kind: "open_report"; job_id: string }>
  | Readonly<{ kind: "video_status"; job_id: string }>
  | Readonly<{ kind: "video_stop"; job_id: string }>;

function hasOnlyKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).every((key) => keys.includes(key)) && keys.every((key) => Object.hasOwn(value, key));
}

function isTab(value: unknown): value is Readonly<{ id: number }> {
  return value !== null && typeof value === "object" && !Array.isArray(value) && Number.isInteger((value as { id?: unknown }).id);
}

function isExactPopupTab(value: unknown): value is Readonly<{ id: number }> {
  return isTab(value) && hasOnlyKeys(value as Record<string, unknown>, ["id"]);
}

function reportTerminalStatus(value: unknown): "complete" | "partial" | "stopped" | "failed" | undefined {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
  const status = (value as Record<string, unknown>).terminal_status;
  return status === "complete" || status === "partial" || status === "stopped" || status === "failed" ? status : undefined;
}

function isBatchOptions(value: unknown): value is BatchOptions {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  if (!hasOnlyKeys(record, ["requestedCount", "candidateScanLimit", "publicationCutoff", "selectionOrder"].filter((key) => record[key] !== undefined))) return false;
  if (!Number.isInteger(record.requestedCount) || !Number.isInteger(record.candidateScanLimit)) return false;
  if (record.publicationCutoff !== undefined && (typeof record.publicationCutoff !== "string" || !offsetTimestamp.test(record.publicationCutoff))) return false;
  if (record.selectionOrder === "page_order") {
    return (record.requestedCount === 5 || record.requestedCount === 10) && record.candidateScanLimit === record.requestedCount && record.publicationCutoff === undefined;
  }
  return record.selectionOrder === undefined || record.selectionOrder === "exact_likes_desc";
}

function asWorkerMessage(value: unknown): WorkerMessage | undefined {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  if (record.kind === "collect_current" && hasOnlyKeys(record, ["kind", "tab"]) && isTab(record.tab)) return { kind: "collect_current", tab: { id: record.tab.id } };
  if (record.kind === "collect_batch" && hasOnlyKeys(record, ["kind", "tab", "options"]) && isTab(record.tab) && isBatchOptions(record.options)) {
    const options = record.options;
    return {
      kind: "collect_batch",
      tab: { id: record.tab.id },
      options: {
        requestedCount: options.requestedCount,
        candidateScanLimit: options.candidateScanLimit,
        ...(options.publicationCutoff === undefined ? {} : { publicationCutoff: options.publicationCutoff }),
        ...(options.selectionOrder === undefined ? {} : { selectionOrder: options.selectionOrder })
      }
    };
  }
  if (record.kind === "stop_job" && hasOnlyKeys(record, ["kind"])) return { kind: "stop_job" };
  if (record.kind === "inspect_active_page" && hasOnlyKeys(record, ["kind", "tab"]) && isExactPopupTab(record.tab)) return { kind: "inspect_active_page", tab: { id: record.tab.id } };
  if (record.kind === "open_report" && hasOnlyKeys(record, ["kind", "job_id"]) && isSafeId(record.job_id)) return { kind: "open_report", job_id: record.job_id };
  if ((record.kind === "video_status" || record.kind === "video_stop") && hasOnlyKeys(record,["kind","job_id"]) && isSafeId(record.job_id)) return {kind:record.kind,job_id:record.job_id};
  return undefined;
}

/** Binds explicit popup commands; popup inspection injects idempotently, while JobController owns collection injection. */
export function createServiceWorkerMessageHandler(runner: WorkerJobRunner): (
  message: unknown,
  sender: chrome.runtime.MessageSender,
  sendResponse: (response?: unknown) => void
) => boolean {
  return (message, sender, sendResponse) => {
    if (message === HEALTH_MESSAGE) {
      sendResponse({ status: "ready" });
      return false;
    }
    if (sender.id !== chrome.runtime.id || sender.tab !== undefined || sender.url !== chrome.runtime.getURL("popup.html")) return false;
    const command = asWorkerMessage(message);
    if (command === undefined) return false;
    if (command.kind === "collect_current") void runner.collectCurrent(command.tab as chrome.tabs.Tab).catch(() => undefined);
    else if (command.kind === "collect_batch") void runner.collectBatch(command.tab as chrome.tabs.Tab, command.options).catch(() => undefined);
    else if (command.kind === "video_status" || command.kind === "video_stop") {
      if (runner.videoStatus === undefined) { sendResponse({status:"failed"}); return false; }
      void runner.videoStatus(command.job_id, command.kind === "video_stop").then(
        processing => sendResponse({status:"ok",job_id:command.job_id,processing}),
        () => sendResponse({status:"failed"})
      );
      return true;
    }
    else if (command.kind === "stop_job") {
      void runner.stop().then(
        () => sendResponse({ status: "stopped" }),
        () => sendResponse({ status: "unconfirmed" })
      );
      return true;
    } else if (command.kind === "open_report") {
      void runner.openReport(command.job_id).then(
        (result) => {
          const terminalStatus = reportTerminalStatus(result);
          sendResponse({ status: "opened", ...(terminalStatus === undefined ? {} : { terminal_status: terminalStatus }) });
        },
        () => sendResponse({ status: "failed" })
      );
      return true;
    }
    else {
      void runner.inspectActivePage(command.tab as chrome.tabs.Tab).then(
        (state) => sendResponse(state),
        () => sendResponse({ page: "unsupported" })
      );
      return true;
    }
    sendResponse({ status: "accepted" });
    return false;
  };
}

const native = new NativeClient();
const jobs = new JobController(defaultJobControllerDependencies(native));

async function inspectActivePage(tab: chrome.tabs.Tab): Promise<WorkerPageState> {
  if (typeof tab.id !== "number" || !Number.isInteger(tab.id)) return { page: "unsupported" };
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content-script.js"] });
    const response = await chrome.tabs.sendMessage(tab.id, { kind: "inspect_page" }) as unknown;
    if (response === null || typeof response !== "object" || Array.isArray(response)) return { page: "unsupported" };
    const record = response as Record<string, unknown>;
    if (record.block === "login_required" || record.block === "challenge_detected") return { page: "blocked", reason: record.block };
    const context = record.context;
    if (context === null || typeof context !== "object" || Array.isArray(context)) return { page: "unsupported" };
    const contextRecord = context as Record<string, unknown>;
    const kind = contextRecord.kind;
    const noteId = contextRecord.noteId;
    if (kind === "current" && isSafeId(noteId)) return { page: "current", note_id: noteId };
    if (kind === "search" || kind === "account") return { page: kind };
  } catch {
    // Popup state deliberately remains finite and URL-free.
  }
  return { page: "unsupported" };
}

async function openReport(jobId: string): Promise<Readonly<{ terminal_status?: "complete" | "partial" | "stopped" | "failed" }>> {
  const reportClient = new NativeClient();
  try {
    await reportClient.connect();
    const response = await reportClient.request({ protocol_version: "1.0", kind: "open_report", job_id: jobId });
    if (response.kind !== "report_result") throw new TypeError("report did not open");
    const terminalStatus = reportTerminalStatus(response);
    return terminalStatus === undefined ? {} : { terminal_status: terminalStatus };
  } finally {
    reportClient.interrupt();
  }
}

async function videoStatus(jobId:string, stop:boolean): Promise<VideoProcessing> {
  const client = new NativeClient();
  try {
    await client.connect();
    const response = await client.request({protocol_version:"1.0",kind:stop ? "video_stop" : "video_status",job_id:jobId});
    if (response.kind !== "video_result" || response.job_id !== jobId) throw new TypeError("video state unavailable");
    return readVideoProcessing(response.processing);
  } finally { client.interrupt(); }
}

chrome.runtime.onMessage.addListener(createServiceWorkerMessageHandler({
  videoStatus,
  collectCurrent: (tab) => jobs.collectCurrent(tab),
  collectBatch: (tab, options) => jobs.collectBatch(tab, options),
  stop: () => jobs.stop(),
  inspectActivePage,
  openReport
}));
