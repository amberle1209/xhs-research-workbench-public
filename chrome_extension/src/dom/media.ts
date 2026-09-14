import { assertAllowedMediaUrl, assertEphemeralSubtitleUrl, assertEphemeralVideoUrl, isSafeId, isValidBoundSrt } from "../security.js";
import { readBoundVideoState, type BoundSubtitle } from "./video-state.js";
import { isVisible, visibleElements } from "./visibility.js";

export type MediaRole = "image" | "video_cover" | "video";

export type MediaSourceDescriptor = Readonly<{
  note_id: string;
  role: MediaRole;
  position: number;
  sourceUrl: string;
  expectedSizeBytes?: number;
}>;

export type MediaSlot = Readonly<{ note_id: string; role: MediaRole; position: number }>;

export type BoundMediaObservation = Readonly<{
  family: "none" | "image" | "video";
  slots: readonly MediaSlot[];
  sources: readonly MediaSourceDescriptor[];
  durationMs?: number;
  unavailableReason?: "size_limit";
  subtitle?: BoundSubtitle;
}>;

export type MediaFetchResult = Readonly<{
  mimeType: "image/jpeg" | "image/png" | "image/webp" | "video/mp4" | "video/webm";
  sizeBytes: number;
  sha256: string;
  bytes: Uint8Array;
}>;

export class MediaFetchError extends Error {
  constructor(readonly code: "source_not_exposed" | "download_failed" | "mime_mismatch" | "size_limit" | "login_required" | "challenge_detected") {
    super(code);
  }
}

const imageLimit = 15 * 1024 * 1024;
const videoLimit = 100 * 1024 * 1024;
const directVideo = /\.(?:mp4|webm)$/iu;

function approvedVideoSource(sourceUrl: string): string {
  try { return assertAllowedMediaUrl(sourceUrl); }
  catch { return assertEphemeralVideoUrl(sourceUrl); }
}

function descriptor(noteId: string, role: MediaRole, position: number, sourceUrl: string): MediaSourceDescriptor {
  if (!isSafeId(noteId)) throw new TypeError("media note id is invalid");
  return { note_id: noteId, role, position, sourceUrl: role === "video" ? approvedVideoSource(sourceUrl) : assertAllowedMediaUrl(sourceUrl) };
}

function unique<T>(items: readonly T[]): T[] {
  return Array.from(new Set(items));
}

function slots(noteId: string, family: "image" | "video", count: number): MediaSlot[] {
  return family === "image"
    ? Array.from({ length: count }, (_, index) => ({ note_id: noteId, role: "image" as const, position: index + 1 }))
    : [{ note_id: noteId, role: "video_cover" as const, position: 1 }, { note_id: noteId, role: "video" as const, position: 1 }];
}

/** Observes the exact-root media family separately from optional, safe ephemeral sources. */
export function observeBoundMedia(detailRoot: Element, identity: Readonly<{ noteId: string; authorId: string }>): BoundMediaObservation {
  if (!isVisible(detailRoot) || !isSafeId(identity.noteId) || !isSafeId(identity.authorId)) throw new TypeError("detail identity is invalid");
  const imageNodes = unique(visibleElements<HTMLImageElement>(detailRoot,
    ".note-slider .swiper-slide:not(.swiper-slide-duplicate) img.note-slider-img, .note-slider .swiper-slide:not(.swiper-slide-duplicate) .note-slider-img img"
  ));
  const players = visibleElements<HTMLVideoElement>(detailRoot, ".media-container video, xg-player video");
  if (imageNodes.length > 0 && players.length > 0) throw new TypeError("mixed media families are not bound");
  if (imageNodes.length > 0) {
    return {
      family: "image",
      slots: slots(identity.noteId, "image", imageNodes.length),
      sources: imageNodes.map((image, index) => descriptor(identity.noteId, "image", index + 1, image.currentSrc || image.src || image.getAttribute("src") || ""))
    };
  }
  if (players.length === 0) return { family: "none", slots: [], sources: [] };
  if (players.length !== 1) throw new TypeError("video source is ambiguous");
  const video = players[0];
  if (video === undefined || !video.closest(".media-container, xg-player")) throw new TypeError("video is outside the detail media root");
  let videoSlots = slots(identity.noteId, "video", 1);
  const poster = video.poster || video.getAttribute("poster") || "";
  const cover = (() => {
    try { return poster ? descriptor(identity.noteId, "video_cover", 1, poster) : undefined; }
    catch { return undefined; }
  })();
  if (cover === undefined) videoSlots = videoSlots.filter(slot => slot.role !== "video_cover");
  const coverSources = cover === undefined ? [] : [cover];
  let boundState: ReturnType<typeof readBoundVideoState>;
  try { boundState = readBoundVideoState(detailRoot.ownerDocument, identity); }
  catch { return { family: "video", slots: videoSlots, sources: coverSources }; }
  if (boundState?.sourceUrl !== undefined) return {
    family: "video", slots: videoSlots,
    sources: [...coverSources, {...descriptor(identity.noteId, "video", 1, boundState.sourceUrl), ...(boundState.expectedSizeBytes === undefined ? {} : {expectedSizeBytes:boundState.expectedSizeBytes})}],
    ...(boundState.durationMs === undefined ? {} : { durationMs: boundState.durationMs }),
    ...(boundState.subtitle === undefined ? {} : { subtitle: boundState.subtitle })
  };
  if (boundState !== undefined) return {
    family: "video", slots: videoSlots, sources: coverSources,
    ...(boundState.unavailableReason === undefined ? {} : {unavailableReason:boundState.unavailableReason}),
    ...(boundState.durationMs === undefined ? {} : { durationMs: boundState.durationMs }),
    ...(boundState.subtitle === undefined ? {} : { subtitle: boundState.subtitle })
  };
  const sources = unique([
    video.getAttribute("src") ?? "",
    ...Array.from(video.querySelectorAll<HTMLSourceElement>("source[src]")).map((source) => source.getAttribute("src") ?? "")
  ].filter((source) => source.length > 0).map((source) => new URL(source, video.baseURI).toString()));
  if (sources.length !== 1 || !directVideo.test(new URL(sources[0] ?? "", video.baseURI).pathname)) return { family: "video", slots: videoSlots, sources: coverSources };
  try {
    assertAllowedMediaUrl(sources[0] ?? "");
    return {
      family: "video",
      slots: videoSlots,
      sources: [
        ...coverSources,
        descriptor(identity.noteId, "video", 1, sources[0] ?? "")
      ]
    };
  } catch {
    return { family: "video", slots: videoSlots, sources: coverSources };
  }
}

/** Compatibility helper for callers that only need fetchable sources. */
export function boundMediaSources(detailRoot: Element, identity: Readonly<{ noteId: string; authorId: string }>): MediaSourceDescriptor[] {
  return [...observeBoundMedia(detailRoot, identity).sources];
}

function maximumFor(role: MediaRole): number {
  return role === "video" ? videoLimit : imageLimit;
}

function expectedMime(role: MediaRole, mime: string): mime is MediaFetchResult["mimeType"] {
  return role === "video" ? mime === "video/mp4" || mime === "video/webm" : mime === "image/jpeg" || mime === "image/png" || mime === "image/webp";
}

function finiteContentLength(response: Response): number | undefined {
  const header = response.headers.get("content-length");
  if (header === null) return undefined;
  if (!/^(?:0|[1-9][0-9]*)$/u.test(header)) throw new MediaFetchError("size_limit");
  return Number(header);
}

async function responseBytes(response: Response, maximum: number, signal?: AbortSignal): Promise<Uint8Array> {
  const contentLength = finiteContentLength(response);
  if (contentLength !== undefined && contentLength > maximum) throw new MediaFetchError("size_limit");
  if (response.body === null) throw new MediaFetchError("download_failed");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      if (signal?.aborted) throw new DOMException("media fetch aborted", "AbortError");
      const next = await reader.read();
      if (next.done) break;
      size += next.value.byteLength;
      if (size > maximum) {
        await reader.cancel();
        throw new MediaFetchError("size_limit");
      }
      chunks.push(next.value);
    }
  } finally {
    reader.releaseLock();
  }
  if (size === 0 || contentLength !== undefined && size !== contentLength) throw new MediaFetchError("download_failed");
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

export async function fetchBoundMedia(
  sourceUrl: string,
  role: MediaRole,
  request: typeof fetch = fetch,
  signal?: AbortSignal
): Promise<MediaFetchResult> {
  const approvedSource = role === "video" ? approvedVideoSource(sourceUrl) : assertAllowedMediaUrl(sourceUrl);
  let response: Response;
  try {
    response = await request(approvedSource, {
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
      cache: "no-store",
      ...(signal === undefined ? {} : { signal })
    });
  } catch {
    throw new MediaFetchError("download_failed");
  }
  if (!response.ok || response.redirected) throw new MediaFetchError("download_failed");
  const declaredMime = (response.headers.get("content-type") ?? "").split(";", 1)[0]?.trim().toLowerCase() ?? "";
  const bytes = await responseBytes(response, maximumFor(role), signal);
  const mp4 = bytes.byteLength >= 16 && new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(0) >= 16 &&
    bytes[4] === 0x66 && bytes[5] === 0x74 && bytes[6] === 0x79 && bytes[7] === 0x70;
  const webm = bytes.byteLength >= 4 &&
    bytes[0] === 0x1a && bytes[1] === 0x45 && bytes[2] === 0xdf && bytes[3] === 0xa3;
  const genericBinary = declaredMime === "application/octet-stream" || declaredMime === "binary/octet-stream";
  let mime = declaredMime;
  if (role === "video" && genericBinary) {
    if (mp4) mime = "video/mp4";
    else if (webm) mime = "video/webm";
  }
  const validVideo = (mime === "video/mp4" && mp4) || (mime === "video/webm" && webm);
  if (role !== "video" || !validVideo) {
    const visibleResponseText = new TextDecoder().decode(bytes.subarray(0, 64 * 1024));
    if (/(?:验证码|captcha|安全验证|security\s*check|验证)/iu.test(visibleResponseText)) throw new MediaFetchError("challenge_detected");
    if (/(?:登录|登入|log\s*in|sign\s*in)/iu.test(visibleResponseText)) throw new MediaFetchError("login_required");
  }
  if (role === "video" && !validVideo) throw new MediaFetchError("mime_mismatch");
  if (mime === "text/html" || mime === "application/xhtml+xml") {
    throw new MediaFetchError("mime_mismatch");
  }
  if (!expectedMime(role, mime)) throw new MediaFetchError("mime_mismatch");
  const digestInput = new Uint8Array(bytes.byteLength);
  digestInput.set(bytes);
  const hash = await crypto.subtle.digest("SHA-256", digestInput);
  const sha256 = Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
  return { mimeType: mime, sizeBytes: bytes.byteLength, sha256, bytes };
}

/** Fetches a supplementary SRT through the same credential-free, redirect-free boundary. */
export async function fetchBoundSubtitle(sourceUrl: string, request: typeof fetch = fetch, signal?: AbortSignal): Promise<Readonly<{ text: string }>> {
  const approvedSource = assertEphemeralSubtitleUrl(sourceUrl);
  let response: Response;
  try {
    response = await request(approvedSource, {
      credentials: "omit", redirect: "error", referrerPolicy: "no-referrer", cache: "no-store",
      ...(signal === undefined ? {} : { signal })
    });
  } catch { throw new MediaFetchError("download_failed"); }
  if (!response.ok || response.redirected) throw new MediaFetchError("download_failed");
  const mime = (response.headers.get("content-type") ?? "").split(";", 1)[0]?.trim().toLowerCase() ?? "";
  if (!["application/x-subrip", "text/plain", "application/octet-stream", ""].includes(mime)) throw new MediaFetchError("mime_mismatch");
  const bytes = await responseBytes(response, 512 * 1024, signal);
  let text: string;
  try { text = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
  catch { throw new MediaFetchError("mime_mismatch"); }
  if (!isValidBoundSrt(text)) throw new MediaFetchError("mime_mismatch");
  return { text };
}
