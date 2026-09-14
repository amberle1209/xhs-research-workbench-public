import { assertEphemeralSubtitleUrl, assertEphemeralVideoUrl, isSafeId } from "../security.js";

export type BoundSubtitle = Readonly<{ sourceUrl: string; sourceKind: "independent_srt" }>;
export type BoundVideoState = Readonly<{ sourceUrl?: string; expectedSizeBytes?: number; unavailableReason?: "size_limit"; durationMs?: number; subtitle?: BoundSubtitle }>;

const stateLimit = 512 * 1024;
const mediaV2Limit = 128 * 1024;
const videoLimit = 100 * 1024 * 1024;
const assignment = /^\s*window\.__INITIAL_STATE__\s*=\s*/u;

function record(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new TypeError("video page state is invalid");
  return value as Record<string, unknown>;
}

function boundedTree(value: unknown): void {
  const pending: { value: unknown; depth: number }[] = [{ value, depth: 0 }];
  let count = 0;
  while (pending.length > 0) {
    const next = pending.pop()!;
    if (++count > 30_000 || next.depth > 48) throw new TypeError("video page state exceeds bounds");
    if (next.value !== null && typeof next.value === "object") {
      for (const child of Object.values(next.value)) pending.push({ value: child, depth: next.depth + 1 });
    }
  }
}

/** Converts only known inert JSON-like values from the page assignment. No code executes. */
function inertJson(source: string): string {
  let result = "";
  let inString = false;
  let escaped = false;
  for (let index = 0; index < source.length; index++) {
    const char = source[index]!;
    if (inString) {
      result += char;
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
    } else if (char === '"') {
      inString = true;
      result += char;
    } else if (source.startsWith("new Map([])", index) && /[\s:,[\]]/u.test(source[index - 1] ?? "") && /[\s,}\]]/u.test(source[index + 11] ?? "")) {
      result += "null";
      index += 10;
    } else if (source.startsWith("undefined", index) && /[\s:,[\]]/u.test(source[index - 1] ?? "") && /[\s,}\]]/u.test(source[index + 9] ?? "")) {
      result += "null";
      index += 8;
    } else result += char;
  }
  return result;
}

function pageState(document: Document): Record<string, unknown> | undefined {
  const scripts = Array.from(document.querySelectorAll("script"))
    .map((script) => script.textContent ?? "")
    .filter((text) => assignment.test(text));
  if (scripts.length === 0) return undefined;
  if (scripts.length !== 1 || scripts[0]!.length > stateLimit) throw new TypeError("video page state is ambiguous or oversized");
  const body = scripts[0]!.replace(assignment, "").trim().replace(/;\s*$/u, "");
  let parsed: unknown;
  try { parsed = JSON.parse(inertJson(body)); } catch { throw new TypeError("video page state is malformed"); }
  boundedTree(parsed);
  return record(parsed);
}

function subtitleFromMediaV2(value: unknown): BoundSubtitle | undefined {
  if (typeof value !== "string" || value.length > mediaV2Limit) return undefined;
  let parsed: unknown;
  try { parsed = JSON.parse(value); boundedTree(parsed); } catch { return undefined; }
  try {
    const subtitles = record(record(parsed).video).subtitles;
    const source = record(subtitles);
    for (const key of ["source", "zh-CN"]) {
      const list = source[key];
      if (!Array.isArray(list) || list.length !== 1) continue;
      const item = record(list[0]);
      if (item.language !== "zh-CN" || item.format !== 0 || item.type !== 0) continue;
      return { sourceUrl: assertEphemeralSubtitleUrl(item.url), sourceKind: "independent_srt" };
    }
  } catch { /* Optional independent subtitles must not block the video. */ }
  return undefined;
}

function declaredDuration(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0 && value <= 86_400_000 ? value : undefined;
}

/** Reads only the exact note/author entry in a bounded inert page state. */
export function readBoundVideoState(document: Document, identity: Readonly<{ noteId: string; authorId: string }>): BoundVideoState | undefined {
  if (!isSafeId(identity.noteId) || !isSafeId(identity.authorId)) throw new TypeError("video identity is invalid");
  const state = pageState(document);
  if (state === undefined) return undefined;
  const map = record(record(state.note).noteDetailMap);
  const entry = record(map[identity.noteId]);
  const note = record(entry.note);
  if (note.noteId !== identity.noteId || record(note.user).userId !== identity.authorId) throw new TypeError("video page identity does not match the visible note");
  if (note.type !== "video") return undefined;
  const video = record(note.video);
  const stream = record(record(video.media).stream);
  if (stream.h264 !== undefined && (!Array.isArray(stream.h264) || stream.h264.length > 16)) return undefined;
  const candidates: { sourceUrl: string; size: number; durationMs?: number; position: number; familyRank: number }[] = [];
  const durations: number[] = [];
  let overLimit = false;
  // EF labels are opaque declared MP4 families, not aliases for h264.
  for (const family of ["h264", "EF4", "EF5", "EF6", "EF7"] as const) {
    const entries = stream[family];
    if (entries === undefined) continue;
    if (!Array.isArray(entries) || entries.length > 16) continue;
    for (const [position, raw] of entries.entries()) {
      let item: Record<string, unknown>;
      try { item = record(raw); } catch { continue; }
      const { size, width, height } = item;
      const durationMs = declaredDuration(item.duration);
      if (typeof size === "number" && Number.isInteger(size) && size > videoLimit) overLimit = true;
      if (durationMs !== undefined) durations.push(durationMs);
      if (item.videoCodec !== family || item.format !== "mp4" ||
        typeof size !== "number" || !Number.isInteger(size) || size < 1 || size > videoLimit ||
        typeof width !== "number" || !Number.isInteger(width) || width < 1 ||
        typeof height !== "number" || !Number.isInteger(height) || height < 1) continue;
      const urls = [item.masterUrl, ...(Array.isArray(item.backupUrls) && item.backupUrls.length <= 4 ? item.backupUrls : [])];
      for (const url of urls) {
        try {
          const sourceUrl = assertEphemeralVideoUrl(url);
          candidates.push({ sourceUrl, size, position, familyRank: family === "h264" ? 0 : 1, ...(durationMs === undefined ? {} : { durationMs }) });
          break;
        } catch { /* A bad mirror cannot authorize a different host or path. */ }
      }
    }
  }
  candidates.sort((a, b) => a.familyRank - b.familyRank || b.size - a.size || a.position - b.position);
  const selected = candidates[0];
  const subtitle = subtitleFromMediaV2(video.mediaV2);
  const commonDuration = durations.length > 0 && durations.every((value) => value === durations[0]) ? durations[0] : undefined;
  const durationMs = selected?.durationMs ?? commonDuration;
  if (selected === undefined && durationMs === undefined && subtitle === undefined && !overLimit) return undefined;
  return {
    ...(selected === undefined ? {} : { sourceUrl: selected.sourceUrl, expectedSizeBytes: selected.size }),
    ...(durationMs === undefined ? {} : { durationMs }),
    ...(subtitle === undefined ? {} : { subtitle }),
    ...(selected === undefined && overLimit ? { unavailableReason: "size_limit" as const } : {})
  };
}
