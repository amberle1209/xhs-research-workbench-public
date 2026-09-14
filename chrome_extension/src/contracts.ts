import { readVideoProcessing } from "./video-processing.js";
import {
  assertCanonicalXhsUrl,
  boundedStringList,
  boundedText,
  isSafeId,
  isSafeUrlValue,
  isValidBoundSrt
} from "./security.js";

const protocolVersion = "1.0";
const maxSnapshots = 100;
const maxRequested = 20;
const maxSlots = 22;
const maxMediaTransfersPerJob = maxRequested * maxSlots;
const maxChunkBytes = 256 * 1024;
const maxChunkBase64 = Math.ceil(maxChunkBytes / 3) * 4;
const maxSafeInteger = 9_007_199_254_740_991;
const offsetTime = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?[+-]\d{2}:\d{2}$/u;
const sha256 = /^[0-9a-f]{64}$/u;
const semver = /^\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.-]+)?$/u;
const canonicalDecimal = /^(?:0|[1-9][0-9]*)$/u;

const requestKinds = [
  "health", "begin_job", "candidate_snapshot", "candidate_unavailable", "finish_scan", "media_begin", "media_chunk",
  "media_end", "media_missing", "finish_job", "stop_job", "open_report", "video_status", "video_stop"
] as const;
const responseKinds = [
  "health_result", "job_started", "candidate_result", "selection_result", "progress", "media_result",
  "job_result", "report_result", "video_result", "error"
] as const;
const mediaRoles = ["image", "video_cover", "video"] as const;
const missingReasons = [
  "source_not_exposed", "unsupported_source", "unsafe_source", "download_failed", "mime_mismatch",
  "size_limit", "note_budget", "run_budget", "slot_limit", "discovery_limit"
] as const;

export type RequestKind = (typeof requestKinds)[number];
export type ResponseKind = (typeof responseKinds)[number];
export type NativeRequest = Readonly<Record<string, unknown>> & { readonly kind: RequestKind };
export type NativeResponse = Readonly<Record<string, unknown>> & { readonly kind: ResponseKind };

export const REQUEST_TO_ALLOWED_RESPONSE_KINDS: Readonly<Record<RequestKind, readonly ResponseKind[]>> = {
  health: ["health_result", "error"],
  begin_job: ["job_started", "error"],
  candidate_snapshot: ["candidate_result", "error"],
  candidate_unavailable: ["candidate_result", "error"],
  finish_scan: ["selection_result", "error"],
  media_begin: ["progress", "media_result", "error"],
  media_chunk: ["progress", "error"],
  media_end: ["media_result", "error"],
  media_missing: ["media_result", "error"],
  finish_job: ["job_result", "error"],
  stop_job: ["job_result", "error"],
  open_report: ["report_result", "error"],
  video_status: ["video_result", "error"],
  video_stop: ["video_result", "error"]
};

type WireRecord = Record<string, unknown>;

function fail(message: string): never {
  throw new TypeError(message);
}

function record(value: unknown, required: readonly string[], optional: readonly string[] = []): WireRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return fail("wire message must be an object");
  const result = value as WireRecord;
  const allowed = new Set([...required, ...optional]);
  for (const key of Object.keys(result)) if (!allowed.has(key)) fail("wire message contains an unknown field");
  for (const key of required) if (!Object.hasOwn(result, key) || result[key] === undefined) fail("wire message misses a required field");
  return result;
}

function basicRecord(value: unknown): WireRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return fail("wire message must be an object");
  const result = value as WireRecord;
  if (!Object.hasOwn(result, "protocol_version") || !Object.hasOwn(result, "kind")) fail("wire message misses a required field");
  return result;
}

function dictionaryRecord(value: unknown): WireRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return fail("wire dictionary must be an object");
  return value as WireRecord;
}

function string(value: unknown, field: string, maximum = 128): string {
  if (typeof value !== "string" || value.length > maximum) return fail(`${field} must be a bounded string`);
  return value;
}

function nullableString(value: unknown, field: string, maximum = 128): void {
  if (value !== null && value !== undefined) string(value, field, maximum);
}

function exactInteger(value: unknown, field: string, minimum: number, maximum: number): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum || value > maximum) {
    return fail(`${field} must be an exact bounded integer`);
  }
  return value;
}

function boolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") return fail(`${field} must be boolean`);
  return value;
}

function oneOf<T extends readonly string[]>(value: unknown, choices: T, field: string): T[number] {
  if (typeof value !== "string" || !choices.includes(value)) return fail(`${field} is invalid`);
  return value as T[number];
}

function safeId(value: unknown, field: string): string {
  if (!isSafeId(value)) return fail(`${field} must be a bounded opaque identifier`);
  return value;
}

function optionalSafeId(value: unknown, field: string): void {
  if (value !== undefined && value !== null) safeId(value, field);
}

function requiredProtocol(value: unknown, kind: string, required: readonly string[], optional: readonly string[] = []): WireRecord {
  const message = record(value, ["protocol_version", "kind", ...required], optional);
  if (message.protocol_version !== protocolVersion || message.kind !== kind) fail("protocol version or kind is invalid");
  return message;
}

function jobProtocol(value: unknown, kind: string, required: readonly string[], optional: readonly string[] = []): WireRecord {
  const message = requiredProtocol(value, kind, ["job_id", ...required], optional);
  safeId(message.job_id, "job_id");
  return message;
}

function offsetTimestamp(value: unknown, field: string): void {
  if (value !== null && value !== undefined && (typeof value !== "string" || !offsetTime.test(value))) fail(`${field} is not an offset timestamp`);
}

function mediaRoleAndPosition(role: unknown, position: unknown): void {
  const validatedRole = oneOf(role, mediaRoles, "role");
  const validatedPosition = exactInteger(position, "position", 1, 20);
  if (validatedRole !== "image" && validatedPosition !== 1) fail("video roles use position one");
}

function validateSourcePageUrl(value: unknown): void {
  if (!isSafeUrlValue(value, 512)) fail("source page URL is unsafe");
  let parsed: URL;
  try { parsed = new URL(value); } catch { return fail("source page URL is invalid"); }
  const authority = value.slice(value.indexOf("://") + 3).split(/[/?#]/u)[0] ?? "";
  if (
    parsed.protocol !== "https:" ||
    !["www.xiaohongshu.com", "xiaohongshu.com"].includes(parsed.hostname) ||
    authority.includes(":") ||
    parsed.port || parsed.username || parsed.password || parsed.search || parsed.hash
  ) fail("source page URL must be query-free XHS HTTPS");
}

function isCanonicalSearchRoute(value: unknown): boolean {
  return value === "https://www.xiaohongshu.com/search_result";
}

function validateMetric(value: unknown): void {
  const metric = record(value, ["precision"], ["raw_value", "normalized_value"]);
  const precision = oneOf(metric.precision, ["exact", "display_rounded", "not_exposed"] as const, "precision");
  if (metric.raw_value !== undefined && metric.raw_value !== null) boundedText(metric.raw_value, 100, "raw_value");
  if (metric.normalized_value !== undefined && metric.normalized_value !== null) exactInteger(metric.normalized_value, "normalized_value", 0, maxSafeInteger);
  const raw = metric.raw_value;
  const normalized = metric.normalized_value;
  if (precision === "not_exposed" && (raw !== undefined && raw !== null || normalized !== undefined && normalized !== null)) fail("not-exposed metric has values");
  if (precision === "exact") {
    if (typeof normalized !== "number") fail("exact metric needs normalized value");
    if (raw !== undefined && raw !== null && (typeof raw !== "string" || !canonicalDecimal.test(raw) || Number(raw) !== normalized)) fail("exact metric raw value does not match");
  }
  if (precision === "display_rounded" && (typeof raw !== "string" || typeof normalized !== "number")) fail("rounded metric needs both values");
}

function validateMetrics(value: unknown): WireRecord {
  const metrics = record(value, [], ["likes", "collects", "comments", "shares"]);
  for (const metric of Object.values(metrics)) if (metric !== null && metric !== undefined) validateMetric(metric);
  return metrics;
}

function validateCandidate(value: unknown): void {
  const message = jobProtocol(value, "candidate_snapshot", ["source_position", "note_id", "canonical_url", "metrics"], [
    "title", "body", "tags", "note_type", "published_at", "time_evidence", "author_id", "author_name",
    "author_profile_url", "metric_provenance", "media_slots"
  ]);
  exactInteger(message.source_position, "source_position", 1, maxSnapshots);
  const noteId = safeId(message.note_id, "note_id");
  const canonical = assertCanonicalXhsUrl(message.canonical_url, "note");
  if (!canonical.endsWith(`/${noteId}`)) fail("note identity does not match canonical URL");
  if (message.title !== undefined && message.title !== null) boundedText(message.title, 200, "title");
  if (message.body !== undefined && message.body !== null) boundedText(message.body, 20_000, "body");
  if (message.tags !== undefined) boundedStringList(message.tags, 100, 100, "tags");
  if (message.note_type !== undefined && message.note_type !== null) boundedText(message.note_type, 100, "note_type");
  offsetTimestamp(message.published_at, "published_at");
  if (message.time_evidence !== undefined && message.time_evidence !== null) {
    const evidence = record(message.time_evidence, ["kind", "raw_text"]);
    oneOf(evidence.kind, ["published", "edited", "unknown"] as const, "time evidence kind");
    boundedText(evidence.raw_text, 200, "time evidence raw text");
  }
  optionalSafeId(message.author_id, "author_id");
  if (message.author_name !== undefined && message.author_name !== null) boundedText(message.author_name, 100, "author_name");
  if (message.author_profile_url !== undefined && message.author_profile_url !== null) {
    const profile = assertCanonicalXhsUrl(message.author_profile_url, "profile");
    if (typeof message.author_id === "string" && !profile.endsWith(`/${message.author_id}`)) fail("author identity mismatch");
  }
  const metrics = validateMetrics(message.metrics);
  if (message.metric_provenance !== undefined) {
    const provenance = dictionaryRecord(message.metric_provenance);
    for (const [name, source] of Object.entries(provenance)) {
      if (!["likes", "collects", "comments", "shares"].includes(name)) fail("unknown metric provenance");
      oneOf(source, ["detail_visible_count", "search_card_interface", "account_card_interface"] as const, "metric provenance");
      const metric = metrics[name];
      if (metric === null || metric === undefined || (metric as WireRecord).precision === "not_exposed") fail("provenance needs an exposed metric");
    }
  }
  const slots = message.media_slots === undefined ? [] : message.media_slots;
  if (!Array.isArray(slots) || slots.length > maxSlots) fail("media slots are invalid");
  const seen = new Set<string>();
  const images: number[] = [];
  let hasVideo = false;
  for (const item of slots) {
    const slot = record(item, ["note_id", "role", "position"]);
    if (safeId(slot.note_id, "slot note_id") !== noteId) fail("media slot note mismatch");
    mediaRoleAndPosition(slot.role, slot.position);
    const key = `${String(slot.role)}:${String(slot.position)}`;
    if (seen.has(key)) fail("duplicate media slot");
    seen.add(key);
    if (slot.role === "image") images.push(slot.position as number); else hasVideo = true;
  }
  if (images.length && hasVideo) fail("image and video slots cannot mix");
  if (images.sort((a, b) => a - b).some((position, index) => position !== index + 1)) fail("image positions are not contiguous");
}

function validateCandidateUnavailable(value: unknown): void {
  const message = jobProtocol(value, "candidate_unavailable", ["source_position", "note_id", "reason"]);
  exactInteger(message.source_position, "source_position", 1, maxSnapshots);
  safeId(message.note_id, "note_id");
  oneOf(message.reason, ["detail_unavailable", "sponsored", "invalid_card"] as const, "candidate unavailable reason");
}

function validateMediaBase(message: WireRecord, includeSequence: boolean): void {
  safeId(message.note_id, "note_id");
  mediaRoleAndPosition(message.role, message.position);
  if (includeSequence) exactInteger(message.sequence, "sequence", 1, maxMediaTransfersPerJob);
}

export function parseNativeRequest(value: unknown): NativeRequest {
  const basic = basicRecord(value);
  if (basic.protocol_version !== protocolVersion) fail("unsupported protocol");
  const kind = oneOf(basic.kind, requestKinds, "request kind");
  switch (kind) {
    case "health": requiredProtocol(value, kind, []); break;
    case "begin_job": {
      const message = jobProtocol(value, kind, ["collection_surface", "source_page_url", "requested_count", "candidate_scan_limit"], ["publication_cutoff", "selection_order"]);
      const surface = oneOf(message.collection_surface, ["extension_current", "extension_search", "extension_account"] as const, "collection surface");
      validateSourcePageUrl(message.source_page_url);
      const requested = exactInteger(message.requested_count, "requested_count", 1, maxRequested);
      const scanLimit = exactInteger(message.candidate_scan_limit, "candidate_scan_limit", 1, maxSnapshots);
      if (requested > scanLimit || surface === "extension_current" && requested !== 1) fail("request counts are invalid");
      offsetTimestamp(message.publication_cutoff, "publication_cutoff");
      const selectionOrder = message.selection_order === undefined
        ? "exact_likes_desc"
        : oneOf(message.selection_order, ["exact_likes_desc", "page_order"] as const, "selection order");
      if (selectionOrder === "page_order" && (
        surface !== "extension_search" || ![5, 10].includes(requested) ||
        (message.publication_cutoff !== undefined && message.publication_cutoff !== null) ||
        !isCanonicalSearchRoute(message.source_page_url)
      )) fail("page-order search request is invalid");
      break;
    }
    case "candidate_snapshot": validateCandidate(value); break;
    case "candidate_unavailable": validateCandidateUnavailable(value); break;
    case "finish_scan": {
      const message = jobProtocol(value, kind, [], ["scroll_rounds", "sort_label"]);
      if (message.scroll_rounds !== undefined && message.scroll_rounds !== null) exactInteger(message.scroll_rounds, "scroll_rounds", 0, 2);
      if (message.sort_label !== undefined && message.sort_label !== null) boundedText(message.sort_label, 100, "sort_label");
      break;
    }
    case "media_begin": {
      const message = jobProtocol(value, kind, ["note_id", "role", "position", "sequence", "size_limit_bytes"]);
      validateMediaBase(message, true); exactInteger(message.size_limit_bytes, "size_limit_bytes", 1, 100 * 1024 * 1024); break;
    }
    case "media_chunk": {
      const message = jobProtocol(value, kind, ["note_id", "role", "position", "sequence", "chunk_index", "data_base64"]);
      validateMediaBase(message, true); exactInteger(message.chunk_index, "chunk_index", 0, maxSafeInteger);
      const encoded = string(message.data_base64, "data_base64", maxChunkBase64);
      let decoded: string;
      try { decoded = atob(encoded); } catch { return fail("data_base64 is invalid"); }
      if (!decoded.length || decoded.length > maxChunkBytes || btoa(decoded) !== encoded) fail("data_base64 is invalid");
      break;
    }
    case "media_end": {
      const message = jobProtocol(value, kind, ["note_id", "role", "position", "sequence", "mime_type", "sha256"]);
      validateMediaBase(message, true); oneOf(message.mime_type, ["image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm"] as const, "mime type");
      if (typeof message.sha256 !== "string" || !sha256.test(message.sha256)) fail("sha256 is invalid"); break;
    }
    case "media_missing": {
      const message = jobProtocol(value, kind, ["note_id", "role", "position", "reason"]);
      validateMediaBase(message, false); oneOf(message.reason, missingReasons, "missing reason"); break;
    }
    case "stop_job": {
      const message = jobProtocol(value, kind, [], ["terminal_cause"]);
      if (message.terminal_cause !== undefined && message.terminal_cause !== null) oneOf(message.terminal_cause, ["stopped", "login_required", "challenge_detected", "structural_error", "route_mismatch", "identity_mismatch"] as const, "terminal cause");
      break;
    }
    case "finish_job": {
      const message = jobProtocol(value, kind, [], ["video_metadata"]);
      if (message.video_metadata !== undefined && message.video_metadata !== null) {
        const metadata = record(message.video_metadata, ["note_id"], ["duration_ms", "subtitle_srt", "subtitle_status"]);
        safeId(metadata.note_id, "video note id");
        if (metadata.duration_ms !== undefined && metadata.duration_ms !== null) exactInteger(metadata.duration_ms, "duration_ms", 1, 86_400_000);
        if (metadata.subtitle_status !== undefined && metadata.subtitle_status !== null) oneOf(metadata.subtitle_status, ["available", "not_exposed", "failed"] as const, "subtitle status");
        if (metadata.subtitle_srt !== undefined && metadata.subtitle_srt !== null) {
          const text = string(metadata.subtitle_srt, "subtitle", 512 * 1024);
          if (!isValidBoundSrt(text)) fail("invalid subtitle");
          if (metadata.subtitle_status !== "available") fail("subtitle status mismatch");
        } else if (metadata.subtitle_status === "available") fail("subtitle text missing");
      }
      break;
    }
    case "video_status": case "video_stop": case "open_report": jobProtocol(value, kind, []); break;
  }
  return value as NativeRequest;
}

function validateResponseError(value: unknown): void {
  const message = requiredProtocol(value, "error", ["code", "fatal"], ["job_id"]);
  optionalSafeId(message.job_id, "job_id");
  oneOf(message.code, ["invalid_request", "unsupported_protocol", "message_limit_exceeded", "invalid_frame", "invalid_state", "identity_mismatch", "media_rejected", "internal_error", "job_in_progress"] as const, "error code");
  boolean(message.fatal, "fatal");
}

export function parseNativeResponse(value: unknown): NativeResponse {
  const basic = basicRecord(value);
  if (basic.protocol_version !== protocolVersion) fail("unsupported protocol");
  const kind = oneOf(basic.kind, responseKinds, "response kind");
  switch (kind) {
    case "health_result": {
      const message = requiredProtocol(value, kind, ["status", "host_version"]);
      if (message.status !== "ready" || typeof message.host_version !== "string" || message.host_version.length > 64 || !semver.test(message.host_version)) fail("health result is invalid"); break;
    }
    case "job_started": { const message = jobProtocol(value, kind, ["status"]); if (message.status !== "started") fail("job start is invalid"); break; }
    case "candidate_result": { const message = jobProtocol(value, kind, ["note_id", "source_position", "outcome"]); safeId(message.note_id, "note_id"); exactInteger(message.source_position, "source_position", 1, maxSnapshots); oneOf(message.outcome, ["recorded", "unavailable"] as const, "candidate result outcome"); break; }
    case "selection_result": {
      const message = jobProtocol(value, kind, ["scanned_count", "eligible_count", "selected_count", "status", "selected"]);
      const scanned = exactInteger(message.scanned_count, "scanned_count", 0, maxSnapshots); const eligible = exactInteger(message.eligible_count, "eligible_count", 0, maxSnapshots); const selectedCount = exactInteger(message.selected_count, "selected_count", 0, maxRequested);
      oneOf(message.status, ["complete", "partial"] as const, "selection status");
      if (!Array.isArray(message.selected) || message.selected.length !== selectedCount || selectedCount > eligible || eligible > scanned) fail("selection counts are invalid");
      const ids = new Set<string>(); const ranks: number[] = [];
      for (const item of message.selected) { const selected = record(item, ["note_id", "selection_rank"]); ids.add(safeId(selected.note_id, "selected note")); ranks.push(exactInteger(selected.selection_rank, "selection rank", 1, maxRequested)); }
      if (ids.size !== message.selected.length || ranks.sort((a, b) => a - b).some((rank, index) => rank !== index + 1)) fail("selected ranks are invalid"); break;
    }
    case "progress": {
      const message = jobProtocol(value, kind, ["phase", "discovered", "inspected", "eligible", "selected", "saved"], ["current_source_position"]);
      oneOf(message.phase, ["started", "scanning", "ranking", "downloading", "saving", "stopped"] as const, "progress phase");
      const counts = ["discovered", "inspected", "eligible", "selected"].map((field) => exactInteger(message[field], field, 0, maxSnapshots));
      exactInteger(message.saved, "saved", 0, maxMediaTransfersPerJob);
      if (counts.some((count, index) => index > 0 && count > (counts[index - 1] ?? 0))) fail("progress counts are invalid");
      if (message.current_source_position !== undefined && message.current_source_position !== null) exactInteger(message.current_source_position, "current_source_position", 1, maxSnapshots); break;
    }
    case "media_result": {
      const message = jobProtocol(value, kind, ["note_id", "role", "position", "outcome"], ["reason"]); validateMediaBase(message, false);
      const outcome = oneOf(message.outcome, ["downloaded", "missing", "rejected"] as const, "media outcome");
      if ((outcome === "downloaded") !== (message.reason === undefined || message.reason === null)) fail("media reason is invalid");
      if (message.reason !== undefined && message.reason !== null) oneOf(message.reason, missingReasons, "media reason"); break;
    }
    case "job_result": {
      const message = jobProtocol(value, kind, ["status", "retained_count", "report_available"], ["report_file", "video_processing"]);
      oneOf(message.status, ["complete", "partial", "stopped", "failed"] as const, "job status"); exactInteger(message.retained_count, "retained_count", 0, maxRequested); const available = boolean(message.report_available, "report_available");
      if (available !== (message.report_file === "index.html")) fail("report file is invalid");
      if (message.video_processing != null) readVideoProcessing(message.video_processing); break;
    }
    case "report_result": {
      const message = jobProtocol(value, kind, ["opened"], ["terminal_status"]);
      if (message.opened !== true) fail("report result is invalid");
      if (message.terminal_status !== undefined && message.terminal_status !== null) {
        oneOf(message.terminal_status, ["complete", "partial", "stopped", "failed"] as const, "report terminal status");
      }
      break;
    }
    case "video_result": { const message = jobProtocol(value, kind, ["processing"]); readVideoProcessing(message.processing); break; }
    case "error": validateResponseError(value); break;
  }
  return value as NativeResponse;
}

export function assertResponseForRequest(request: NativeRequest, response: NativeResponse): NativeResponse {
  if (!REQUEST_TO_ALLOWED_RESPONSE_KINDS[request.kind].includes(response.kind)) fail("response kind is not allowed for request");
  const requestJobId = request.job_id;
  const responseJobId = response.job_id;
  if (requestJobId === undefined) {
    if (responseJobId !== undefined) fail("health response cannot have job id");
  } else if (responseJobId !== requestJobId) fail("job response must carry matching job id");
  if ((request.kind === "candidate_snapshot" || request.kind === "candidate_unavailable") && response.kind === "candidate_result") {
    if (request.note_id !== response.note_id || request.source_position !== response.source_position) fail("candidate response does not echo request identity");
    if ((request.kind === "candidate_snapshot" && response.outcome !== "recorded") || (request.kind === "candidate_unavailable" && response.outcome !== "unavailable")) {
      fail("candidate response outcome does not match request kind");
    }
  }
  if ((request.kind === "media_begin" || request.kind === "media_end" || request.kind === "media_missing") && response.kind === "media_result") {
    if (request.note_id !== response.note_id || request.role !== response.role || request.position !== response.position) fail("media response does not echo request identity");
  }
  return response;
}
