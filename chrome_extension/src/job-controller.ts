import { readVideoProcessing, type VideoProcessing } from "./video-processing.js";
import { type NativeRequest, type NativeResponse, parseNativeRequest } from "./contracts.js";
import { MediaFetchError, fetchBoundMedia, fetchBoundSubtitle, type MediaFetchResult, type MediaRole } from "./dom/media.js";
import { isAuthorIdentityReason, type AuthorIdentityReason, type DetailProjectionStage } from "./dom/detail.js";
import {
  DEFAULT_SEARCH_RISK_POLICY,
  canScroll,
  canStart,
  canVisitDetail,
  completeVisit,
  createGuardState,
  halt,
  recordScroll,
  recordVisit,
  type GuardState
} from "./risk/guard.js";
import { assertAllowedMediaUrl, assertEphemeralVideoUrl, assertEphemeralSubtitleUrl, assertCanonicalXhsUrl, isSafeId } from "./security.js";
import { NativeClientError } from "./native-client.js";

const PROGRESS_STORAGE_KEY = "xhs_job_progress";
const SEARCH_RISK_STORAGE_KEY = "xhs_search_risk";
const MAX_REQUESTED_COUNT = 20;
const MAX_CANDIDATE_SCAN_LIMIT = 100;
const MAX_MEDIA_TRANSFERS_PER_JOB = MAX_REQUESTED_COUNT * 22;
const MAX_MEDIA_CHUNK_BYTES = 256 * 1024;

type JobErrorCode =
  | "optional_permission_required"
  | "invalid_batch_bounds"
  | "unsupported_current_tab"
  | "unsupported_batch_tab"
  | "job_in_progress"
  | "detail_unavailable"
  | "identity_mismatch"
  | "native_host_error"
  | "native_host_unavailable"
  | "native_host_interrupted"
  | "native_host_version_mismatch"
  | "native_response_invalid"
  | "media_transfer_failed"
  | "login_required"
  | "challenge_detected"
  | "cooldown"
  | "source_tab_closed"
  | "queue_tab_closed"
  | "stopped";

export type JobProgress = Readonly<{
  job_id: string;
  phase: "starting" | "scanning" | "ranking" | "downloading" | "complete" | "partial" | "stopped" | "error";
  discovered: number;
  inspected: number;
  eligible: number;
  selected: number;
  saved: number;
  report_available?: boolean;
  video_processing?: VideoProcessing;
  current_source_position?: number;
  detail_stage?: DetailProjectionStage;
  detail_reason?: AuthorIdentityReason;
  error?: JobErrorCode;
}>;

export type BatchOptions = Readonly<{
  requestedCount: number;
  candidateScanLimit: number;
  publicationCutoff?: string;
  selectionOrder?: "exact_likes_desc" | "page_order";
}>;

export interface NativeTransport {
  connect(): Promise<Readonly<{ hostVersion: string }>>;
  request(value: unknown): Promise<NativeResponse>;
  interrupt(): void;
  completeTerminal(): void;
}

export interface SearchRiskStateStore {
  readLastSearchBatchStartedAt(): Promise<number | undefined>;
  writeLastSearchBatchStartedAt(value: number): Promise<void>;
}

export type JobControllerDependencies = Readonly<{
  native: NativeTransport;
  sleep: (durationMs: number) => Promise<void>;
  delay: (minimumMs: number, maximumMs: number) => number;
  now?: () => number;
  searchRiskState?: SearchRiskStateStore;
  newJobId: () => string;
  fetchSubtitle?: (sourceUrl:string, signal:AbortSignal) => Promise<Readonly<{text:string}>>;
  fetchMedia: (sourceUrl: string, role: MediaRole, signal: AbortSignal) => Promise<MediaFetchResult>;
}>;

type ActiveJob = {
  jobId: string;
  sourceTabId: number;
  queueTabId: number | undefined;
  begun: boolean;
  currentNote: boolean;
  sourceIndependent: boolean;
  nativeInterrupted: boolean;
  nativePending: Promise<NativeResponse> | undefined;
  stopAcknowledged: boolean;
  stopReportAvailable: boolean | undefined;
  pageOrder: boolean;
  stopPromise: Promise<void> | undefined;
  stopCode: "stopped" | "source_tab_closed" | "queue_tab_closed" | undefined;
  progress: JobProgress;
  nextMediaSequence: number;
  injectedTabIds: Set<number>;
  abort: AbortController;
  removeListener: ((tabId: number) => void) | undefined;
};

type PageContext =
  | Readonly<{ kind: "current"; noteId: string; canonicalUrl: string }>
  | Readonly<{ kind: "search" | "account"; canonicalUrl: string }>;
type SourcePageContext = Extract<PageContext, Readonly<{ kind: "search" | "account"; canonicalUrl: string }>>;
type SearchPageContext = Readonly<{ kind: "search"; canonicalUrl: string }>;

type VideoProjection = Readonly<{durationMs?:number;unavailableReason?:"size_limit";subtitle?:Readonly<{sourceUrl:string;sourceKind:"independent_srt"}>}>;
type VideoMetadata = Readonly<{note_id:string;duration_ms?:number;subtitle_srt?:string;subtitle_status:"available"|"not_exposed"|"failed"}>;
type DetailProjection = Readonly<{
  video?: VideoProjection;
  snapshot: Record<string, unknown>;
  media: readonly Readonly<{ note_id: string; role: MediaRole; position: number; sourceUrl: string; expectedSizeBytes?:number }>[];
}>;

type Candidate = Readonly<{ sourcePosition: number; noteId: string; canonicalUrl: string }>;
type SearchScanCandidate = Candidate & Readonly<{
  title?: string;
  likes?: string;
  noteType?: string;
}>;
type SearchScanExclusion = Candidate;
type SearchScan = Readonly<{
  candidates: readonly SearchScanCandidate[];
  exclusions: readonly SearchScanExclusion[];
  scrollRounds: number;
  sortLabel?: string;
}>;
type Selection = Readonly<{ eligible: number; selected: readonly string[] }>;
type RetainedProjection = Readonly<{
  video?: VideoProjection;
  snapshot: Record<string, unknown>;
  descriptors: ReadonlyMap<string, Readonly<{ note_id: string; role: MediaRole; position: number; sourceUrl: string; expectedSizeBytes?:number }>>;
}>;
type MediaResultResponse = NativeResponse & Readonly<{
  kind: "media_result";
  note_id: string;
  role: MediaRole;
  position: number;
  outcome: "downloaded" | "missing" | "rejected";
}>;

export class JobControllerError extends Error {
  readonly detailReason?: AuthorIdentityReason;

  constructor(readonly code: JobErrorCode, readonly detailStage?: DetailProjectionStage, reason?: AuthorIdentityReason) {
    super(code);
    this.name = "JobControllerError";
    if (detailStage === "author_identity" && isAuthorIdentityReason(reason)) this.detailReason = reason;
  }
}

function isDetailProjectionStage(value: unknown): value is DetailProjectionStage {
  return value === "route" || value === "detail_root" || value === "author_identity" || value === "structured_state" || value === "media_binding" || value === "snapshot_validation" || value === "unexpected";
}

class StopRequested extends Error {
  constructor(readonly code: "stopped" | "source_tab_closed" | "queue_tab_closed") {
    super(code);
  }
}

function defaultDelay(minimumMs: number, maximumMs: number): number {
  const span = maximumMs - minimumMs + 1;
  if (!Number.isInteger(minimumMs) || !Number.isInteger(maximumMs) || span <= 0) throw new TypeError("delay range is invalid");
  const values = new Uint32Array(1);
  const limit = Math.floor(0x1_0000_0000 / span) * span;
  do crypto.getRandomValues(values); while ((values[0] ?? 0) >= limit);
  return minimumMs + ((values[0] ?? 0) % span);
}

function defaultJobId(): string {
  return `job_${crypto.randomUUID().replaceAll("-", "")}`;
}

function defaultSleep(durationMs: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, durationMs));
}

const defaultSearchRiskState: SearchRiskStateStore = {
  async readLastSearchBatchStartedAt(): Promise<number | undefined> {
    const stored = await chrome.storage.local.get(SEARCH_RISK_STORAGE_KEY) as unknown;
    if (stored === null || typeof stored !== "object" || Array.isArray(stored)) return undefined;
    const state = (stored as Record<string, unknown>)[SEARCH_RISK_STORAGE_KEY];
    if (state === null || typeof state !== "object" || Array.isArray(state)) return undefined;
    const value = (state as Record<string, unknown>).last_search_batch_started_at;
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
  },
  async writeLastSearchBatchStartedAt(value: number): Promise<void> {
    if (!Number.isSafeInteger(value) || value < 0) throw new JobControllerError("detail_unavailable");
    await chrome.storage.local.set({ [SEARCH_RISK_STORAGE_KEY]: { last_search_batch_started_at: value } });
  }
};

function slotKey(noteId: string, role: MediaRole, position: number): string {
  return `${noteId}:${role}:${position}`;
}

function approvedProjectedMediaUrl(sourceUrl: string, role: MediaRole): string {
  if (role !== "video") return assertAllowedMediaUrl(sourceUrl);
  try { return assertAllowedMediaUrl(sourceUrl); }
  catch { return assertEphemeralVideoUrl(sourceUrl); }
}

function mediaMissingReason(error: unknown): "source_not_exposed" | "unsupported_source" | "unsafe_source" | "download_failed" | "mime_mismatch" | "size_limit" {
  const code = error instanceof MediaFetchError ? error.code : (error as { code?: unknown } | undefined)?.code;
  if (code === "source_not_exposed" || code === "unsupported_source" || code === "unsafe_source" || code === "download_failed" || code === "mime_mismatch" || code === "size_limit") return code;
  return "download_failed";
}

function mediaBlockCode(error: unknown): "login_required" | "challenge_detected" | undefined {
  const code = error instanceof MediaFetchError ? error.code : (error as { code?: unknown } | undefined)?.code;
  return code === "login_required" || code === "challenge_detected" ? code : undefined;
}

function finiteError(error: unknown): JobErrorCode {
  if (error instanceof JobControllerError) return error.code;
  if (error instanceof StopRequested) return error.code;
  if (error instanceof NativeClientError) {
    switch (error.code) {
      case "native_host_unavailable": case "native_host_interrupted": case "native_host_version_mismatch": case "native_response_invalid":
        return error.code;
      default: return "native_host_error";
    }
  }
  return "native_host_error";
}

function terminalCause(code: JobErrorCode): "stopped" | "login_required" | "challenge_detected" | "structural_error" | "route_mismatch" | "identity_mismatch" {
  if (code === "login_required" || code === "challenge_detected" || code === "identity_mismatch") return code;
  if (code === "source_tab_closed" || code === "queue_tab_closed") return "route_mismatch";
  return "structural_error";
}

function boundedCount(value: number): number {
  return Number.isInteger(value) && value >= 0 && value <= MAX_CANDIDATE_SCAN_LIMIT ? value : 0;
}

function boundedMediaCount(value: number): number {
  return Number.isInteger(value) && value >= 0 && value <= MAX_MEDIA_TRANSFERS_PER_JOB ? value : 0;
}

function isBoundedCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= MAX_CANDIDATE_SCAN_LIMIT;
}

function isBoundedMediaCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= MAX_MEDIA_TRANSFERS_PER_JOB;
}

function searchLikesMetric(raw: string): Readonly<Record<string, unknown>> | undefined {
  if (/^\d+$/.test(raw)) return { raw_value: raw, normalized_value: Number(raw), precision: "exact" };
  const match = /^(\d+(?:\.\d+)?)\s*(万|千)$/.exec(raw);
  if (match === null) return undefined;
  const normalized = Math.round(Number(match[1]) * (match[2] === "万" ? 10_000 : 1_000));
  return Number.isSafeInteger(normalized) ? { raw_value: raw, normalized_value: normalized, precision: "display_rounded" } : undefined;
}

function base64(bytes: Uint8Array): string {
  let text = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    text += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(text);
}

/** User-commanded jobs with no popup-owned state and no automatic injection. */
export class JobController {
  private active: ActiveJob | undefined;
  private storageWrites: Promise<void> = Promise.resolve();

  constructor(private readonly dependencies: JobControllerDependencies) {}

  async collectCurrent(tab: chrome.tabs.Tab): Promise<void> {
    const state = this.begin(tab, true);
    try {
      await this.requireOrigins(["https://*.xhscdn.com/*"]);
      this.checkStopped(state);
      await this.dependencies.native.connect();
      this.checkStopped(state);
      const context = await this.inspect(state, tab.id as number);
      if (context.kind !== "current") throw new JobControllerError("unsupported_current_tab");
      await this.startNativeJob(state, "extension_current", context.canonicalUrl, 1, 1);
      const projection = await this.project(state, tab.id as number, context.noteId, 1);
      const snapshot = this.validateProjection(projection, context.noteId, context.canonicalUrl, 1);
      await this.request(state, snapshot);
      const selection = await this.request(state, { protocol_version: "1.0", kind: "finish_scan", job_id: state.jobId });
      const selectionResult = this.expectSelection(selection, [context.noteId], 1);
      await this.persist(state, { phase: "ranking", discovered: 1, inspected: 1, eligible: selectionResult.eligible, selected: selectionResult.selected.length });
      const videoMetadata = await this.videoMetadata(state, context.noteId, projection);
      await this.transferSelected(state, new Map([[context.noteId, projection]]), selectionResult.selected);
      await this.finish(state, videoMetadata);
    } catch (error) {
      await this.handleTerminal(state, error);
      if (!(error instanceof StopRequested) && state.stopCode === undefined) throw this.publicError(error);
    } finally {
      await this.cleanup(state);
    }
  }

  async collectBatch(tab: chrome.tabs.Tab, options: BatchOptions): Promise<void> {
    if (!Number.isInteger(options.requestedCount) || !Number.isInteger(options.candidateScanLimit) || options.requestedCount < 1 || options.requestedCount > MAX_REQUESTED_COUNT || options.candidateScanLimit < options.requestedCount || options.candidateScanLimit > MAX_CANDIDATE_SCAN_LIMIT) {
      throw new JobControllerError("invalid_batch_bounds");
    }
    const state = this.begin(tab);
    try {
      await this.requireOrigins(["https://www.xiaohongshu.com/*", "https://*.xhscdn.com/*"]);
      this.checkStopped(state);
      await this.dependencies.native.connect();
      this.checkStopped(state);
      const context = await this.inspect(state, tab.id as number);
      if (context.kind !== "search" && context.kind !== "account") throw new JobControllerError("unsupported_batch_tab");
      const requestedPageOrder = options.selectionOrder === "page_order";
      if (requestedPageOrder && context.kind !== "search") throw new JobControllerError("unsupported_batch_tab");
      const simpleSearch = requestedPageOrder;
      let guard: GuardState | undefined;
      if (simpleSearch) {
        if (options.candidateScanLimit !== options.requestedCount || options.publicationCutoff !== undefined) throw new JobControllerError("invalid_batch_bounds");
        const lastBatchStartedAt = await this.searchRiskState().readLastSearchBatchStartedAt();
        guard = lastBatchStartedAt === undefined
          ? createGuardState(DEFAULT_SEARCH_RISK_POLICY)
          : createGuardState(DEFAULT_SEARCH_RISK_POLICY, { lastBatchStartedAt });
        const nowMs = this.now();
        const decision = canStart(guard, options.requestedCount, nowMs);
        if (!decision.allowed) throw new JobControllerError(decision.reason === "cooldown" ? "cooldown" : "invalid_batch_bounds");
        await this.searchRiskState().writeLastSearchBatchStartedAt(nowMs);
      }
      await this.startNativeJob(state, context.kind === "search" ? "extension_search" : "extension_account", context.canonicalUrl, options.requestedCount, simpleSearch ? MAX_CANDIDATE_SCAN_LIMIT : options.candidateScanLimit, options.publicationCutoff, simpleSearch ? "page_order" : "exact_likes_desc");
      if (simpleSearch) {
        await this.collectSimpleSearch(state, tab.id as number, { kind: "search", canonicalUrl: context.canonicalUrl }, options, guard as GuardState);
        return;
      }
      const candidates = await this.discover(state, tab.id as number, simpleSearch ? options.requestedCount : options.candidateScanLimit, context, guard);
      this.checkStopped(state);
      const queue = await chrome.tabs.create({ url: "about:blank", active: false });
      const queueTabId = queue.id;
      if (typeof queueTabId !== "number" || !Number.isInteger(queueTabId)) throw new JobControllerError("detail_unavailable");
      state.queueTabId = queueTabId;
      this.checkStopped(state);
      const projections = new Map<string, RetainedProjection>();
      for (const candidate of candidates) {
        let detailVisitStarted = false;
        try {
          this.checkStopped(state);
          if (guard !== undefined) guard = await this.waitForDetailPermission(state, guard);
          if (guard !== undefined) {
            guard = recordVisit(guard, this.now());
            detailVisitStarted = true;
          }
          await this.queueBrowserOperation(() => chrome.tabs.update(queueTabId, { url: candidate.canonicalUrl }));
          this.checkStopped(state);
          state.injectedTabIds.delete(queueTabId);
          await this.wait(state, 3000, 7000);
          const queueContext = await this.inspect(state, queueTabId, true);
          if (queueContext.kind !== "current" || queueContext.noteId !== candidate.noteId || queueContext.canonicalUrl !== candidate.canonicalUrl) throw new JobControllerError("identity_mismatch");
          const projection = await this.project(state, queueTabId, candidate.noteId, candidate.sourcePosition, true);
          const snapshot = this.validateProjection(projection, candidate.noteId, candidate.canonicalUrl, candidate.sourcePosition);
          await this.request(state, snapshot);
          projections.set(candidate.noteId, projection);
        } catch (error) {
          if (guard !== undefined && error instanceof JobControllerError && (error.code === "login_required" || error.code === "challenge_detected")) {
            guard = halt(guard, error.code);
          }
          if (!(error instanceof JobControllerError) || error.code !== "detail_unavailable") throw error;
          await this.request(state, {
            protocol_version: "1.0", kind: "candidate_unavailable", job_id: state.jobId,
            source_position: candidate.sourcePosition, note_id: candidate.noteId, reason: "detail_unavailable"
          });
        } finally {
          if (guard !== undefined && detailVisitStarted) guard = completeVisit(guard);
        }
        await this.persist(state, {
          phase: "scanning", inspected: state.progress.inspected + 1, discovered: candidates.length,
          current_source_position: candidate.sourcePosition
        });
      }
      const selection = await this.request(state, { protocol_version: "1.0", kind: "finish_scan", job_id: state.jobId });
      const selectionResult = this.expectSelection(selection, candidates.map((candidate) => candidate.noteId), options.requestedCount);
      await this.persist(state, { phase: "ranking", eligible: selectionResult.eligible, selected: selectionResult.selected.length, inspected: candidates.length, discovered: candidates.length });
      await this.transferSelected(state, projections, selectionResult.selected);
      await this.finish(state);
    } catch (error) {
      await this.handleTerminal(state, error);
      if (!(error instanceof StopRequested) && state.stopCode === undefined) throw this.publicError(error);
    } finally {
      await this.cleanup(state);
    }
  }

  async stop(): Promise<void> {
    const state = this.active;
    if (state === undefined || state.stopCode !== undefined) return;
    state.stopCode = "stopped";
    state.stopPromise = this.acknowledgeUserStop(state);
    state.abort.abort();
    await state.stopPromise;
    if (!state.begun) await this.persist(state, { phase: "stopped", error: "stopped" });
  }

  private begin(tab: chrome.tabs.Tab, currentNote = false): ActiveJob {
    if (this.active !== undefined) throw new JobControllerError("job_in_progress");
    const sourceTabId = tab.id;
    if (typeof sourceTabId !== "number" || !Number.isInteger(sourceTabId)) throw new JobControllerError("detail_unavailable");
    const jobId = this.dependencies.newJobId();
    if (!isSafeId(jobId)) throw new JobControllerError("detail_unavailable");
    const state: ActiveJob = {
      jobId,
      sourceTabId,
      queueTabId: undefined,
      begun: false,
      currentNote,
      sourceIndependent: false,
      nativeInterrupted: false,
      nativePending: undefined,
      stopAcknowledged: false,
      stopReportAvailable: undefined,
      pageOrder: false,
      stopPromise: undefined,
      stopCode: undefined,
      progress: { job_id: jobId, phase: "starting", discovered: 0, inspected: 0, eligible: 0, selected: 0, saved: 0 },
      nextMediaSequence: 1,
      injectedTabIds: new Set(),
      abort: new AbortController(),
      removeListener: undefined
    };
    const onRemoved = (tabId: number): void => {
      if (tabId === state.sourceTabId && !state.sourceIndependent) {
        state.stopCode = "source_tab_closed";
        state.abort.abort();
        this.interruptNative(state);
      }
      if (tabId === state.queueTabId) {
        state.queueTabId = undefined;
        state.stopCode = "queue_tab_closed";
        state.abort.abort();
        this.interruptNative(state);
      }
    };
    state.removeListener = onRemoved;
    chrome.tabs.onRemoved.addListener(onRemoved);
    this.active = state;
    if (!currentNote) void this.persist(state, {}).catch(() => undefined);
    return state;
  }

  private async requireOrigins(origins: string[]): Promise<void> {
    if (!(await chrome.permissions.contains({ origins }))) throw new JobControllerError("optional_permission_required");
  }

  private async inspect(state: ActiveJob, tabId: number, normalizeQueueBrowserFailure = false): Promise<PageContext> {
    await this.inject(state, tabId, normalizeQueueBrowserFailure);
    this.checkStopped(state);
    const response = await this.queueBrowserOperation(
      () => chrome.tabs.sendMessage(tabId, { kind: "inspect_page" }),
      normalizeQueueBrowserFailure
    ) as unknown;
    if (response === null || typeof response !== "object" || Array.isArray(response)) throw new JobControllerError("detail_unavailable");
    const block = (response as { block?: unknown }).block;
    if (block === "login_required" || block === "challenge_detected") throw new JobControllerError(block);
    const context = (response as { context?: unknown }).context;
    if (context === null || typeof context !== "object" || Array.isArray(context)) throw new JobControllerError("detail_unavailable");
    const record = context as Record<string, unknown>;
    if (record.kind === "current" && isSafeId(record.noteId)) {
      const canonicalUrl = assertCanonicalXhsUrl(record.canonicalUrl, "note");
      if (!canonicalUrl.endsWith(`/${record.noteId}`)) throw new JobControllerError("identity_mismatch");
      return { kind: "current", noteId: record.noteId, canonicalUrl };
    }
    if (record.kind === "search" && record.canonicalUrl === "https://www.xiaohongshu.com/search_result") {
      return { kind: "search", canonicalUrl: record.canonicalUrl };
    }
    if (record.kind === "account" && typeof record.canonicalUrl === "string") {
      return { kind: "account", canonicalUrl: assertCanonicalXhsUrl(record.canonicalUrl, "profile") };
    }
    throw new JobControllerError("detail_unavailable");
  }

  private async inject(state: ActiveJob, tabId: number, normalizeQueueBrowserFailure = false): Promise<void> {
    this.checkStopped(state);
    if (state.injectedTabIds.has(tabId)) return;
    await this.queueBrowserOperation(
      () => chrome.scripting.executeScript({ target: { tabId }, files: ["content-script.js"] }),
      normalizeQueueBrowserFailure
    );
    this.checkStopped(state);
    state.injectedTabIds.add(tabId);
  }

  private async project(state: ActiveJob, tabId: number, noteId: string, sourcePosition: number, normalizeQueueBrowserFailure = false): Promise<RetainedProjection> {
    await this.inject(state, tabId, normalizeQueueBrowserFailure);
    this.checkStopped(state);
    const response = await this.queueBrowserOperation(
      () => chrome.tabs.sendMessage(tabId, { kind: "project_detail", noteId, sourcePosition }),
      normalizeQueueBrowserFailure
    ) as unknown;
    if (response === null || typeof response !== "object" || Array.isArray(response)) throw new JobControllerError("detail_unavailable");
    const error = (response as { error?: unknown }).error;
    if (error === "login_required" || error === "challenge_detected") throw new JobControllerError(error);
    if (error === "detail_unavailable") {
      const stage = (response as { stage?: unknown }).stage;
      const reason = (response as { detail_reason?: unknown }).detail_reason;
      throw new JobControllerError("detail_unavailable", isDetailProjectionStage(stage) ? stage : "unexpected", stage === "author_identity" && isAuthorIdentityReason(reason) ? reason : undefined);
    }
    const value = response as Partial<DetailProjection>;
    if (value.snapshot === undefined || value.media === undefined || !Array.isArray(value.media)) throw new JobControllerError("detail_unavailable");
    const descriptors = new Map<string, Readonly<{ note_id: string; role: MediaRole; position: number; sourceUrl: string; expectedSizeBytes?:number }>>();
    for (const item of value.media) {
      if (!isSafeId(item.note_id) || !Number.isInteger(item.position) || item.position < 1 || item.position > 20 || (item.role !== "image" && item.role !== "video_cover" && item.role !== "video")) continue;
      try {
        descriptors.set(slotKey(item.note_id, item.role, item.position), { ...item, sourceUrl: approvedProjectedMediaUrl(item.sourceUrl, item.role) });
      } catch {
        // A declared slot without a usable ephemeral source becomes media_missing.
      }
    }
    let video: VideoProjection | undefined;
    if (value.video !== undefined && value.video !== null && typeof value.video === "object") {
      const candidate = value.video;
      let subtitle: VideoProjection["subtitle"];
      try { if (candidate.subtitle?.sourceKind === "independent_srt") subtitle = {sourceKind:"independent_srt",sourceUrl:assertEphemeralSubtitleUrl(candidate.subtitle.sourceUrl)}; } catch { /* Optional source remains unavailable. */ }
      video = {
        ...(typeof candidate.durationMs === "number" && Number.isInteger(candidate.durationMs) && candidate.durationMs > 0 && candidate.durationMs <= 86_400_000 ? {durationMs:candidate.durationMs} : {}),
        ...(candidate.unavailableReason === "size_limit" ? {unavailableReason:"size_limit" as const} : {}),
        ...(subtitle === undefined ? {} : {subtitle})
      };
    }
    return { snapshot: value.snapshot, descriptors, ...(video === undefined ? {} : {video}) };
  }

  private async queueBrowserOperation<T>(operation: () => Promise<T>, normalize = true): Promise<T> {
    if (!normalize) return operation();
    try {
      return await operation();
    } catch (error) {
      if (error instanceof JobControllerError || error instanceof StopRequested || error instanceof NativeClientError) throw error;
      throw new JobControllerError("detail_unavailable");
    }
  }

  private validateProjection(projection: RetainedProjection, noteId: string, canonicalUrl: string, sourcePosition: number): NativeRequest {
    try {
      const candidate = parseNativeRequest({ protocol_version: "1.0", kind: "candidate_snapshot", job_id: this.active?.jobId, ...projection.snapshot });
      if (candidate.kind !== "candidate_snapshot" || candidate.note_id !== noteId || candidate.canonical_url !== canonicalUrl || candidate.source_position !== sourcePosition) throw new JobControllerError("identity_mismatch");
      return candidate;
    } catch (error) {
      if (error instanceof JobControllerError) throw error;
      throw new JobControllerError("detail_unavailable");
    }
  }

  private async startNativeJob(state: ActiveJob, collectionSurface: "extension_current" | "extension_search" | "extension_account", sourcePageUrl: string, requestedCount: number, candidateScanLimit: number, publicationCutoff?: string, selectionOrder: "exact_likes_desc" | "page_order" = "exact_likes_desc"): Promise<void> {
    this.checkStopped(state);
    const response = await this.request(state, {
      protocol_version: "1.0",
      kind: "begin_job",
      job_id: state.jobId,
      collection_surface: collectionSurface,
      source_page_url: sourcePageUrl,
      requested_count: requestedCount,
      candidate_scan_limit: candidateScanLimit,
      ...(publicationCutoff === undefined ? {} : { publication_cutoff: publicationCutoff }),
      selection_order: selectionOrder
    });
    if (response.kind !== "job_started") throw new JobControllerError("native_host_error");
    state.begun = true;
    state.pageOrder = selectionOrder === "page_order";
    await this.persist(state, { phase: "scanning" });
  }

  private async discover(state: ActiveJob, sourceTabId: number, maximum: number, expectedSource: SourcePageContext, guard?: GuardState): Promise<Candidate[]> {
    const candidates: Candidate[] = [];
    const seen = new Set<string>();
    const sourcePositions = new Set<number>();
    let noNewRounds = 0;
    while (candidates.length < maximum && noNewRounds < 3) {
      this.checkStopped(state);
      await this.assertSourceIdentity(state, sourceTabId, expectedSource);
      this.checkStopped(state);
      const raw = await chrome.tabs.sendMessage(sourceTabId, { kind: "discover_candidates", maximum }) as unknown;
      this.checkStopped(state);
      await this.assertSourceIdentity(state, sourceTabId, expectedSource);
      this.checkStopped(state);
      const list = raw !== null && typeof raw === "object" && !Array.isArray(raw) ? (raw as { candidates?: unknown }).candidates : undefined;
      if (!Array.isArray(list)) throw new JobControllerError("detail_unavailable");
      let added = 0;
      for (const item of list) {
        if (item === null || typeof item !== "object" || Array.isArray(item)) continue;
        const record = item as Record<string, unknown>;
        if (typeof record.sourcePosition !== "number" || !Number.isInteger(record.sourcePosition) || record.sourcePosition < 1 || record.sourcePosition > MAX_CANDIDATE_SCAN_LIMIT || !isSafeId(record.noteId)) continue;
        let canonicalUrl: string;
        try { canonicalUrl = assertCanonicalXhsUrl(record.canonicalUrl, "note"); } catch { continue; }
        if (!canonicalUrl.endsWith(`/${record.noteId}`) || seen.has(record.noteId) || sourcePositions.has(record.sourcePosition)) continue;
        seen.add(record.noteId);
        sourcePositions.add(record.sourcePosition);
        candidates.push({ sourcePosition: record.sourcePosition, noteId: record.noteId, canonicalUrl });
        added += 1;
        if (candidates.length === maximum) break;
      }
      this.checkStopped(state);
      await this.persist(state, { phase: "scanning", discovered: candidates.length });
      if (candidates.length === maximum) break;
      if (added === 0) noNewRounds += 1; else noNewRounds = 0;
      if (noNewRounds === 3) break;
      if (guard !== undefined) {
        const decision = canScroll(guard);
        if (!decision.allowed) break;
      }
      const scroll = await chrome.tabs.sendMessage(sourceTabId, { kind: "scroll_candidates" }) as unknown;
      this.checkStopped(state);
      if (scroll === null || typeof scroll !== "object" || Array.isArray(scroll)) throw new JobControllerError("detail_unavailable");
      const scrollRecord = scroll as { scrolled?: unknown; error?: unknown };
      if (scrollRecord.error === "login_required" || scrollRecord.error === "challenge_detected") {
        if (guard !== undefined) guard = halt(guard, scrollRecord.error);
        throw new JobControllerError(scrollRecord.error);
      }
      if (scrollRecord.scrolled !== true) throw new JobControllerError("detail_unavailable");
      if (guard !== undefined) guard = recordScroll(guard);
      await this.wait(state, 2000, 5000);
    }
    return candidates;
  }

  /** Scan current rendered cards, freeze the native page-order ledger, then visit only that prefix. */
  private async collectSimpleSearch(state: ActiveJob, sourceTabId: number, source: SearchPageContext, options: BatchOptions, guard: GuardState): Promise<void> {
    const scan = await this.scanSearchUntilRequestedCount(state, sourceTabId, source, options.requestedCount, guard);
    const candidates = scan.candidates;
    for (const candidate of candidates) {
      await this.request(state, this.searchSummarySnapshot(state, candidate));
    }
    for (const exclusion of scan.exclusions) {
      await this.request(state, {
        protocol_version: "1.0", kind: "candidate_unavailable", job_id: state.jobId,
        source_position: exclusion.sourcePosition, note_id: exclusion.noteId, reason: "sponsored"
      });
    }
    const selection = await this.request(state, {
      protocol_version: "1.0", kind: "finish_scan", job_id: state.jobId,
      scroll_rounds: scan.scrollRounds,
      ...(scan.sortLabel === undefined ? {} : { sort_label: scan.sortLabel })
    });
    const selectionResult = this.expectSelection(selection, candidates.map((candidate) => candidate.noteId), options.requestedCount, candidates.length + scan.exclusions.length);
    const frozen = candidates.filter((candidate) => selectionResult.selected.includes(candidate.noteId));
    if (
      frozen.length !== selectionResult.selected.length ||
      frozen.some((candidate, index) => candidate.noteId !== selectionResult.selected[index])
    ) throw new JobControllerError("identity_mismatch");
    await this.persist(state, {
      phase: "ranking", discovered: candidates.length, inspected: 0,
      eligible: selectionResult.eligible, selected: frozen.length
    });
    this.checkStopped(state);
    const queue = await chrome.tabs.create({ url: "about:blank", active: false });
    const queueTabId = queue.id;
    if (typeof queueTabId !== "number" || !Number.isInteger(queueTabId)) throw new JobControllerError("detail_unavailable");
    state.queueTabId = queueTabId;

    for (const candidate of frozen) {
      let detailVisitStarted = false;
      try {
        this.checkStopped(state);
        guard = await this.waitForDetailPermission(state, guard);
        guard = recordVisit(guard, this.now());
        detailVisitStarted = true;
        await this.queueBrowserOperation(() => chrome.tabs.update(queueTabId, { url: candidate.canonicalUrl }));
        this.checkStopped(state);
        state.injectedTabIds.delete(queueTabId);
        await this.wait(state, 3000, 7000);
        const queueContext = await this.inspect(state, queueTabId, true);
        if (queueContext.kind !== "current" || queueContext.noteId !== candidate.noteId || queueContext.canonicalUrl !== candidate.canonicalUrl) throw new JobControllerError("identity_mismatch");
        const projection = await this.project(state, queueTabId, candidate.noteId, candidate.sourcePosition, true);
        await this.request(state, this.validateProjection(projection, candidate.noteId, candidate.canonicalUrl, candidate.sourcePosition));
      } catch (error) {
        if (error instanceof JobControllerError && (error.code === "login_required" || error.code === "challenge_detected")) {
          guard = halt(guard, error.code);
        }
        if (!(error instanceof JobControllerError) || error.code !== "detail_unavailable") throw error;
        await this.request(state, {
          protocol_version: "1.0", kind: "candidate_unavailable", job_id: state.jobId,
          source_position: candidate.sourcePosition, note_id: candidate.noteId, reason: "detail_unavailable"
        });
      } finally {
        if (detailVisitStarted) guard = completeVisit(guard);
      }
      await this.persist(state, {
        phase: "scanning", discovered: candidates.length, inspected: state.progress.inspected + 1,
        current_source_position: candidate.sourcePosition
      });
    }
    await this.finish(state);
  }

  private async scanSearchUntilRequestedCount(state: ActiveJob, sourceTabId: number, expectedSource: SearchPageContext, requestedCount: number, guard: GuardState): Promise<SearchScan> {
    const candidates: SearchScanCandidate[] = [];
    const exclusions: SearchScanExclusion[] = [];
    const seen = new Set<string>();
    let nextSourcePosition = 1;
    let sortLabel: string | undefined;
    for (let scrollRound = 0; candidates.length < requestedCount; ) {
      this.checkStopped(state);
      await this.assertSourceIdentity(state, sourceTabId, expectedSource);
      const raw = await chrome.tabs.sendMessage(sourceTabId, { kind: "scan_search" }) as unknown;
      this.checkStopped(state);
      await this.assertSourceIdentity(state, sourceTabId, expectedSource);
      const scan = raw !== null && typeof raw === "object" && !Array.isArray(raw)
        ? raw as { summaries?: unknown; exclusions?: unknown; sort_label?: unknown } : undefined;
      const summaries = scan?.summaries;
      if (!Array.isArray(summaries)) throw new JobControllerError("detail_unavailable");
      if (typeof scan?.sort_label === "string" && scan.sort_label.length <= 100) sortLabel ??= scan.sort_label;
      const rawExclusions = scan?.exclusions;
      if (!Array.isArray(rawExclusions)) throw new JobControllerError("detail_unavailable");
      const orderedCards = [
        ...rawExclusions.map((value) => ({ value, kind: "exclusion" as const })),
        ...summaries.map((value) => ({ value, kind: "summary" as const }))
      ].sort((left, right) => {
        const leftPosition = left.value !== null && typeof left.value === "object" && !Array.isArray(left.value) ? (left.value as { source_position?: unknown }).source_position : undefined;
        const rightPosition = right.value !== null && typeof right.value === "object" && !Array.isArray(right.value) ? (right.value as { source_position?: unknown }).source_position : undefined;
        return (typeof leftPosition === "number" ? leftPosition : Number.MAX_SAFE_INTEGER) - (typeof rightPosition === "number" ? rightPosition : Number.MAX_SAFE_INTEGER);
      });
      for (const card of orderedCards) {
        if (card.value === null || typeof card.value !== "object" || Array.isArray(card.value)) continue;
        const record = card.value as Record<string, unknown>;
        if (!isSafeId(record.note_id) || seen.has(record.note_id) || !Number.isInteger(record.source_position) || typeof record.source_position !== "number" || record.source_position < 1 || record.source_position > MAX_CANDIDATE_SCAN_LIMIT) continue;
        let canonicalUrl: string;
        try { canonicalUrl = assertCanonicalXhsUrl(record.canonical_url, "note"); } catch { continue; }
        if (!canonicalUrl.endsWith(`/${record.note_id}`)) continue;
        if (nextSourcePosition > MAX_CANDIDATE_SCAN_LIMIT) throw new JobControllerError("detail_unavailable");
        if (card.kind === "exclusion") {
          if (record.reason !== "sponsored" || record.is_sponsored !== true || record.sponsorship_evidence !== "visible_sponsored_label") continue;
          seen.add(record.note_id);
          exclusions.push({ sourcePosition: nextSourcePosition++, noteId: record.note_id, canonicalUrl });
          continue;
        }
        if (record.sponsorship_evidence !== "unknown" || record.summary_source !== "search_card_visible_dom") continue;
        seen.add(record.note_id);
        candidates.push({
          sourcePosition: nextSourcePosition++, noteId: record.note_id, canonicalUrl,
          ...(typeof record.title === "string" && record.title.length <= 200 ? { title: record.title } : {}),
          ...(typeof record.likes === "string" && record.likes.length <= 100 ? { likes: record.likes } : {}),
          ...(typeof record.note_type === "string" && record.note_type.length <= 100 ? { noteType: record.note_type } : {})
        });
        if (candidates.length === requestedCount) break;
      }
      await this.persist(state, { phase: "scanning", discovered: candidates.length });
      if (candidates.length === requestedCount || scrollRound === DEFAULT_SEARCH_RISK_POLICY.maxScrollRounds) break;
      const decision = canScroll(guard);
      if (!decision.allowed) break;
      const scroll = await chrome.tabs.sendMessage(sourceTabId, { kind: "scroll_candidates" }) as unknown;
      this.checkStopped(state);
      if (scroll === null || typeof scroll !== "object" || Array.isArray(scroll)) throw new JobControllerError("detail_unavailable");
      const record = scroll as { scrolled?: unknown; error?: unknown };
      if (record.error === "login_required" || record.error === "challenge_detected") throw new JobControllerError(record.error);
      if (record.scrolled !== true) throw new JobControllerError("detail_unavailable");
      guard = recordScroll(guard);
      scrollRound += 1;
      await this.wait(state, 2000, 5000);
    }
    return { candidates, exclusions, scrollRounds: guard.scrollRounds, ...(sortLabel === undefined ? {} : { sortLabel }) };
  }

  private searchSummarySnapshot(state: ActiveJob, candidate: SearchScanCandidate): NativeRequest {
    try {
      const likes = candidate.likes === undefined ? undefined : searchLikesMetric(candidate.likes);
      return parseNativeRequest({
        protocol_version: "1.0", kind: "candidate_snapshot", job_id: state.jobId,
        source_position: candidate.sourcePosition, note_id: candidate.noteId,
        canonical_url: candidate.canonicalUrl,
        ...(candidate.title === undefined ? {} : { title: candidate.title }),
        ...(candidate.noteType === undefined ? {} : { note_type: candidate.noteType }),
        ...(likes === undefined ? { metrics: {} } : { metrics: { likes }, metric_provenance: { likes: "search_card_interface" } }),
        media_slots: []
      });
    } catch {
      throw new JobControllerError("detail_unavailable");
    }
  }

  private async assertSourceIdentity(state: ActiveJob, sourceTabId: number, expected: SourcePageContext): Promise<void> {
    const context = await this.inspect(state, sourceTabId);
    if (context.kind !== expected.kind || context.canonicalUrl !== expected.canonicalUrl) throw new JobControllerError("identity_mismatch");
  }

  private expectSelection(response: NativeResponse, candidates: readonly string[], requestedCount: number, expectedScanned = candidates.length): Selection {
    if (response.kind !== "selection_result") throw new JobControllerError("native_host_error");
    const result = response as Readonly<Record<string, unknown>>;
    const selectedItems = result.selected;
    const scanned = result.scanned_count;
    const eligible = result.eligible_count;
    const selectedCount = result.selected_count;
    if (!Array.isArray(selectedItems) || !isBoundedCount(scanned) || !isBoundedCount(eligible) || !Number.isInteger(selectedCount) || typeof selectedCount !== "number" || scanned !== expectedScanned || selectedCount < 0 || selectedCount > requestedCount || selectedCount > MAX_REQUESTED_COUNT || eligible > scanned || selectedCount > eligible || selectedCount !== selectedItems.length) {
      throw new JobControllerError("native_host_error");
    }
    const selected: Array<Readonly<{ noteId: string; rank: number }>> = [];
    for (const item of selectedItems) {
      if (item === null || typeof item !== "object" || Array.isArray(item)) throw new JobControllerError("identity_mismatch");
      const noteId = (item as { note_id?: unknown }).note_id;
      const rank = (item as { selection_rank?: unknown }).selection_rank;
      if (!isSafeId(noteId) || typeof rank !== "number" || !Number.isInteger(rank) || rank < 1 || rank > MAX_REQUESTED_COUNT || !candidates.includes(noteId) || selected.some((value) => value.noteId === noteId) || selected.some((value) => value.rank === rank)) {
        throw new JobControllerError("identity_mismatch");
      }
      selected.push({ noteId, rank });
    }
    selected.sort((left, right) => left.rank - right.rank);
    if (selected.some((value, index) => value.rank !== index + 1)) throw new JobControllerError("identity_mismatch");
    return { eligible, selected: selected.map((value) => value.noteId) };
  }

  private async transferSelected(state: ActiveJob, projections: ReadonlyMap<string, RetainedProjection>, selected: readonly string[]): Promise<void> {
    for (const noteId of selected) {
      this.checkStopped(state);
      const projection = projections.get(noteId);
      if (projection === undefined) throw new JobControllerError("identity_mismatch");
      const candidate = this.validateProjection(projection, noteId, (projection.snapshot.canonical_url as string), projection.snapshot.source_position as number);
      if (candidate.kind !== "candidate_snapshot" || !Array.isArray(candidate.media_slots)) throw new JobControllerError("identity_mismatch");
      for (const item of candidate.media_slots) {
        if (item === null || typeof item !== "object" || Array.isArray(item)) throw new JobControllerError("identity_mismatch");
        const slot = item as { note_id: string; role: MediaRole; position: number };
        if (!isSafeId(slot.note_id) || !Number.isInteger(slot.position) || slot.position < 1 || slot.position > 20 || (slot.role !== "image" && slot.role !== "video_cover" && slot.role !== "video")) throw new JobControllerError("identity_mismatch");
        this.checkStopped(state);
        const descriptor = projection.descriptors.get(slotKey(slot.note_id, slot.role, slot.position));
        if (descriptor === undefined) {
          await this.request(state, { protocol_version: "1.0", kind: "media_missing", job_id: state.jobId, note_id: slot.note_id, role: slot.role, position: slot.position, reason: slot.role === "video" ? projection.video?.unavailableReason ?? "source_not_exposed" : "source_not_exposed" });
          continue;
        }
        let media: MediaFetchResult;
        try {
          media = await this.abortable(state, this.dependencies.fetchMedia(descriptor.sourceUrl, slot.role, state.abort.signal));
          if (descriptor.expectedSizeBytes !== undefined && descriptor.expectedSizeBytes !== media.sizeBytes) throw new MediaFetchError("download_failed");
        } catch (error) {
          this.checkStopped(state);
          const block = mediaBlockCode(error);
          if (block !== undefined) throw new JobControllerError(block);
          await this.request(state, { protocol_version: "1.0", kind: "media_missing", job_id: state.jobId, note_id: slot.note_id, role: slot.role, position: slot.position, reason: mediaMissingReason(error) });
          continue;
        }
        this.checkStopped(state);
        if (
          media.bytes.byteLength === 0 ||
          media.bytes.byteLength !== media.sizeBytes ||
          !/^[0-9a-f]{64}$/u.test(media.sha256) ||
          (slot.role === "video" && media.mimeType !== "video/mp4" && media.mimeType !== "video/webm") ||
          (slot.role !== "video" && media.mimeType !== "image/jpeg" && media.mimeType !== "image/png" && media.mimeType !== "image/webp")
        ) {
          await this.request(state, { protocol_version: "1.0", kind: "media_missing", job_id: state.jobId, note_id: slot.note_id, role: slot.role, position: slot.position, reason: "mime_mismatch" });
          continue;
        }
        const sequence = state.nextMediaSequence;
        const sizeLimit = slot.role === "video" ? 100 * 1024 * 1024 : 15 * 1024 * 1024;
        const begin = await this.request(state, { protocol_version: "1.0", kind: "media_begin", job_id: state.jobId, note_id: slot.note_id, role: slot.role, position: slot.position, sequence, size_limit_bytes: sizeLimit });
        state.nextMediaSequence += 1;
        if (begin.kind === "media_result") {
          this.expectMediaResult(begin, slot.note_id, slot.role, slot.position);
          if (begin.outcome !== "rejected") throw new JobControllerError("native_host_error");
          continue;
        }
        if (begin.kind !== "progress") throw new JobControllerError("native_host_error");
        for (let offset = 0, chunkIndex = 0; offset < media.bytes.length; offset += MAX_MEDIA_CHUNK_BYTES, chunkIndex += 1) {
          this.checkStopped(state);
          const chunk = media.bytes.subarray(offset, Math.min(offset + MAX_MEDIA_CHUNK_BYTES, media.bytes.length));
          await this.request(state, { protocol_version: "1.0", kind: "media_chunk", job_id: state.jobId, note_id: slot.note_id, role: slot.role, position: slot.position, sequence, chunk_index: chunkIndex, data_base64: base64(chunk) });
        }
        this.checkStopped(state);
        // All video bytes are now acknowledged locally; finalization no longer needs the page.
        if (state.currentNote && slot.role === "video") state.sourceIndependent = true;
        const completed = await this.request(state, { protocol_version: "1.0", kind: "media_end", job_id: state.jobId, note_id: slot.note_id, role: slot.role, position: slot.position, sequence, mime_type: media.mimeType, sha256: media.sha256 });
        this.expectMediaResult(completed, slot.note_id, slot.role, slot.position);
        if (completed.outcome !== "downloaded") continue;
        await this.persist(state, { phase: "downloading", saved: state.progress.saved + 1 });
      }
    }
  }

  private async videoMetadata(state: ActiveJob, noteId:string, projection:RetainedProjection): Promise<VideoMetadata | undefined> {
    if (!Array.isArray(projection.snapshot.media_slots) || !projection.snapshot.media_slots.some((slot: {role?:unknown}) => slot.role === "video")) return undefined;
    const common = {note_id:noteId, ...(projection.video?.durationMs === undefined ? {} : {duration_ms:projection.video.durationMs})};
    if (projection.video?.subtitle === undefined || this.dependencies.fetchSubtitle === undefined) return {...common,subtitle_status:"not_exposed"};
    try {
      const subtitle = await this.abortable(state, this.dependencies.fetchSubtitle(projection.video.subtitle.sourceUrl, AbortSignal.any([state.abort.signal, AbortSignal.timeout(8000)])));
      const metadata = {...common,subtitle_status:"available" as const,subtitle_srt:subtitle.text};
      parseNativeRequest({protocol_version:"1.0",kind:"finish_job",job_id:state.jobId,video_metadata:metadata});
      return metadata;
    } catch { this.checkStopped(state); return {...common,subtitle_status:"failed"}; }
  }

  private async finish(state: ActiveJob, videoMetadata?:VideoMetadata): Promise<void> {
    this.checkStopped(state);
    const response = await this.request(state, { protocol_version: "1.0", kind: "finish_job", job_id: state.jobId, ...(videoMetadata === undefined ? {} : {video_metadata:videoMetadata}) });
    if (response.kind === "job_result" && (
      response.job_id !== state.jobId || (response.status !== "complete" && response.status !== "partial")
    )) {
      this.completeTerminal(state);
    }
    if (
      response.kind !== "job_result" || response.job_id !== state.jobId ||
      (response.status !== "complete" && response.status !== "partial")
    ) {
      throw new JobControllerError("native_host_error");
    }
    if (state.removeListener !== undefined) { chrome.tabs.onRemoved.removeListener(state.removeListener); state.removeListener = undefined; }
    await this.persistTerminal(state, { phase: response.status, report_available: response.report_available === true, ...(response.video_processing == null ? {} : {video_processing:readVideoProcessing(response.video_processing)}) });
  }

  private expectMediaResult(response: NativeResponse, noteId: string, role: MediaRole, position: number): MediaResultResponse {
    if (response.kind !== "media_result" || response.note_id !== noteId || response.role !== role || response.position !== position) {
      throw new JobControllerError("native_host_error");
    }
    return response as MediaResultResponse;
  }

  private async request(state: ActiveJob, request: unknown): Promise<NativeResponse> {
    this.checkStopped(state);
    const pending = this.dependencies.native.request(request);
    state.nativePending = pending;
    let response: NativeResponse;
    try {
      response = await pending;
    } finally {
      if (state.nativePending === pending) state.nativePending = undefined;
    }
    this.checkStopped(state);
    if (response.kind === "error" && response.code === "job_in_progress") throw new JobControllerError("job_in_progress");
    if (response.kind === "error") throw new JobControllerError("native_host_error");
    if (response.kind === "progress") await this.persistNativeProgress(state, response);
    return response;
  }

  private async wait(state: ActiveJob, minimumMs: number, maximumMs: number): Promise<void> {
    const duration = this.dependencies.delay(minimumMs, maximumMs);
    if (!Number.isInteger(duration) || duration < minimumMs || duration > maximumMs) throw new JobControllerError("detail_unavailable");
    await this.abortable(state, this.dependencies.sleep(duration));
    this.checkStopped(state);
  }

  private now(): number {
    return (this.dependencies.now ?? Date.now)();
  }

  private searchRiskState(): SearchRiskStateStore {
    return this.dependencies.searchRiskState ?? defaultSearchRiskState;
  }

  private async waitForDetailPermission(state: ActiveJob, guard: GuardState): Promise<GuardState> {
    let decision = canVisitDetail(guard, this.now());
    if (!decision.allowed && decision.reason === "pacing" && guard.lastVisitAt !== undefined) {
      await this.abortable(state, this.dependencies.sleep(guard.lastVisitAt + guard.policy.minimumDetailIntervalMs - this.now()));
      this.checkStopped(state);
      decision = canVisitDetail(guard, this.now());
    }
    if (!decision.allowed) throw new JobControllerError(decision.reason === "login_required" || decision.reason === "challenge_detected" ? decision.reason : "detail_unavailable");
    return guard;
  }

  private abortable<T>(state: ActiveJob, operation: Promise<T>): Promise<T> {
    if (state.abort.signal.aborted) return Promise.reject(new StopRequested(state.stopCode ?? "stopped"));
    return new Promise<T>((resolve, reject) => {
      let settled = false;
      const settle = (callback: (value: T) => void, value: T): void => {
        if (settled) return;
        settled = true;
        state.abort.signal.removeEventListener("abort", onAbort);
        callback(value);
      };
      const fail = (error: unknown): void => {
        if (settled) return;
        settled = true;
        state.abort.signal.removeEventListener("abort", onAbort);
        reject(error);
      };
      const onAbort = (): void => fail(new StopRequested(state.stopCode ?? "stopped"));
      state.abort.signal.addEventListener("abort", onAbort, { once: true });
      void operation.then((value) => settle(resolve, value), fail);
    });
  }

  private checkStopped(state: ActiveJob): void {
    if (state.stopCode !== undefined) throw new StopRequested(state.stopCode);
  }

  private async persistNativeProgress(state: ActiveJob, response: NativeResponse): Promise<void> {
    if (response.kind !== "progress") return;
    const result = response as Readonly<Record<string, unknown>>;
    const discovered = result.discovered;
    const inspected = result.inspected;
    const eligible = result.eligible;
    const selected = result.selected;
    const saved = result.saved;
    const current = result.current_source_position;
    if (
      !isBoundedCount(discovered) || !isBoundedCount(inspected) || !isBoundedCount(eligible) || !isBoundedCount(selected) || !isBoundedMediaCount(saved) ||
      inspected > discovered || eligible > inspected || selected > eligible ||
      (current !== undefined && current !== null && (typeof current !== "number" || !Number.isInteger(current) || current < 1 || current > MAX_CANDIDATE_SCAN_LIMIT))
    ) throw new JobControllerError("native_host_error");
    const phase: JobProgress["phase"] = result.phase === "ranking" ? "ranking" : result.phase === "downloading" || result.phase === "saving" ? "downloading" : "scanning";
    await this.persist(state, {
      phase,
      discovered,
      inspected,
      eligible,
      selected,
      saved: Math.max(state.progress.saved, saved),
      ...(typeof current === "number" ? { current_source_position: current } : {})
    });
  }

  private async handleTerminal(state: ActiveJob, error: unknown): Promise<void> {
    if (state.stopCode === "stopped") {
      try {
        await state.stopPromise;
      } catch {
        await this.persist(state, { phase: "error", error: "native_host_interrupted" });
        return;
      }
      await this.persistTerminal(state, { phase: "stopped", error: "stopped", ...(state.stopReportAvailable === undefined ? {} : { report_available: state.stopReportAvailable }) });
      return;
    }
    const code = state.stopCode ?? finiteError(error);
    if (code === "job_in_progress" && !state.begun) { this.completeTerminal(state); return; }
    const detailStage = error instanceof JobControllerError ? error.detailStage : undefined;
    const detailReason = error instanceof JobControllerError ? error.detailReason : undefined;
    const diagnostic = {
      ...(detailStage === undefined ? {} : { detail_stage: detailStage }),
      ...(detailStage === "author_identity" && isAuthorIdentityReason(detailReason) ? { detail_reason: detailReason } : {})
    };
    if (state.begun && !state.nativeInterrupted && !state.stopAcknowledged && !(error instanceof NativeClientError)) {
      try {
        const response = await this.dependencies.native.request({
          protocol_version: "1.0", kind: "stop_job", job_id: state.jobId,
          ...(state.pageOrder ? { terminal_cause: terminalCause(code) } : {})
        });
        if (response.kind === "job_result" && response.job_id === state.jobId && typeof response.report_available === "boolean") {
          if (response.status === "partial") {
            await this.persistTerminal(state, { phase: "partial", error: code, report_available: response.report_available, ...diagnostic });
            return;
          }
          if (response.status === "failed") {
            await this.persistTerminal(state, { phase: "error", error: code, report_available: response.report_available, ...diagnostic });
            return;
          }
          if (response.status === "stopped") {
            await this.persistTerminal(state, { phase: "stopped", error: "stopped", report_available: response.report_available });
            return;
          }
        }
      } catch { /* host loss remains finite */ }
    }
    if (error instanceof StopRequested || state.stopCode !== undefined) {
      await this.persist(state, { phase: "stopped", error: code });
      return;
    }
    await this.persist(state, { phase: "error", error: code, ...diagnostic });
  }

  private publicError(error: unknown): JobControllerError {
    return error instanceof JobControllerError ? error : new JobControllerError(finiteError(error));
  }

  private async acknowledgeUserStop(state: ActiveJob): Promise<void> {
    try {
      const pending = state.nativePending;
      if (pending !== undefined) {
        this.interruptNative(state);
        throw new NativeClientError("native_host_interrupted");
      }
      if (!state.begun) return;
      const response = await this.dependencies.native.request({
        protocol_version: "1.0", kind: "stop_job", job_id: state.jobId,
        ...(state.pageOrder ? { terminal_cause: "stopped" } : {})
      });
      if (response.kind === "job_result" && response.job_id === state.jobId && response.status !== "stopped") {
        // The native host has already reached a terminal result and is now
        // waiting for EOF. Keep the UI failure state, but never reuse this
        // completed transport for another job.
        this.completeTerminal(state);
      }
      if (
        response.kind !== "job_result" || response.job_id !== state.jobId ||
        response.status !== "stopped" || typeof response.report_available !== "boolean"
      ) throw new JobControllerError("native_host_error");
      state.stopAcknowledged = true;
      state.stopReportAvailable = response.report_available;
    } catch (error) {
      if (error instanceof JobControllerError) throw error;
      throw new JobControllerError("native_host_interrupted");
    }
  }

  private interruptNative(state: ActiveJob): void {
    if (state.nativeInterrupted) return;
    state.nativeInterrupted = true;
    try {
      this.dependencies.native.interrupt();
    } catch {
      // Local stop remains finite even when a transport disconnect throws.
    }
  }

  private async persist(state: ActiveJob, update: Partial<Omit<JobProgress, "job_id">>): Promise<void> {
    const detailStage = update.detail_stage ?? state.progress.detail_stage;
    const detailReason = update.detail_stage === undefined ? update.detail_reason ?? state.progress.detail_reason : update.detail_reason;
    const progress: JobProgress = {
      job_id: state.jobId,
      phase: update.phase ?? state.progress.phase,
      discovered: boundedCount(update.discovered ?? state.progress.discovered),
      inspected: boundedCount(update.inspected ?? state.progress.inspected),
      eligible: boundedCount(update.eligible ?? state.progress.eligible),
      selected: boundedCount(update.selected ?? state.progress.selected),
      saved: boundedMediaCount(update.saved ?? state.progress.saved),
      ...(update.video_processing === undefined && state.progress.video_processing === undefined ? {} : {video_processing:update.video_processing ?? state.progress.video_processing}),
      ...(update.report_available === undefined && state.progress.report_available === undefined
        ? {}
        : { report_available: update.report_available ?? state.progress.report_available }),
      ...(update.current_source_position === undefined && state.progress.current_source_position === undefined
        ? {}
        : { current_source_position: update.current_source_position ?? state.progress.current_source_position }),
      ...(isDetailProjectionStage(detailStage) ? { detail_stage: detailStage } : {}),
      ...(detailStage === "author_identity" && isAuthorIdentityReason(detailReason) ? { detail_reason: detailReason } : {}),
      ...(update.error === undefined ? {} : { error: update.error })
    };
    state.progress = progress;
    const write = this.storageWrites.then(() => chrome.storage.local.set({ [PROGRESS_STORAGE_KEY]: progress }));
    this.storageWrites = write.catch(() => undefined);
    await write;
  }

  private async persistTerminal(state: ActiveJob, update: Partial<Omit<JobProgress, "job_id">>): Promise<void> {
    try {
      await this.persist(state, update);
    } finally {
      // A failed final write must not leave the MV3 keepalive and native host
      // waiting on one another forever. The caller still receives the write
      // failure and records its normal recoverable error state when possible.
      this.completeTerminal(state);
    }
  }

  private completeTerminal(state: ActiveJob): void {
    state.nativeInterrupted = true;
    this.dependencies.native.completeTerminal();
  }

  private async cleanup(state: ActiveJob): Promise<void> {
    state.abort.abort();
    if (state.removeListener !== undefined) chrome.tabs.onRemoved.removeListener(state.removeListener);
    const queueTabId = state.queueTabId;
    state.queueTabId = undefined;
    if (queueTabId !== undefined) {
      try { await chrome.tabs.remove(queueTabId); } catch { /* only the owned ID is ever removed */ }
    }
    if (this.active === state) this.active = undefined;
  }
}

export const defaultJobControllerDependencies = (native: NativeTransport): JobControllerDependencies => ({
  native,
  sleep: defaultSleep,
  delay: defaultDelay,
  searchRiskState: defaultSearchRiskState,
  newJobId: defaultJobId,
  fetchMedia: (sourceUrl, role, signal) => fetchBoundMedia(sourceUrl, role, fetch, signal)
});
