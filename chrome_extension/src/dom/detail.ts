import { boundedStringList, boundedText, isSafeId } from "../security.js";
import { parseNativeRequest } from "../contracts.js";
import { detectBlockPage } from "./challenge.js";
import { observeBoundMedia, type MediaSourceDescriptor } from "./media.js";
import { detectPage } from "./route.js";
import { publishedTimestamp } from "./published-date.js";
import { readStructuredDetailState } from "./structured-state.js";
import { isVisible, visibleElements, visibleText } from "./visibility.js";

export type CandidateSnapshotInput = Readonly<{
  source_position: number;
  note_id: string;
  canonical_url: string;
  title?: string;
  body?: string;
  tags?: string[];
  note_type?: string;
  published_at?: string;
  time_evidence?: Readonly<{ kind: "published" | "edited" | "unknown"; raw_text: string }>;
  author_id: string;
  author_name?: string;
  author_profile_url: string;
  metrics: Readonly<Record<string, unknown>>;
  metric_provenance?: Readonly<Record<string, "detail_visible_count">>;
  media_slots: ReadonlyArray<Readonly<{ note_id: string; role: "image" | "video_cover" | "video"; position: number }>>;
}>;

export type DetailFieldSource = "structured_state" | "detail_dom" | "not_exposed";
export type DetailProjection = Readonly<{
  snapshot: CandidateSnapshotInput;
  media: readonly MediaSourceDescriptor[];
  field_provenance: Readonly<Record<string, DetailFieldSource>>;
  video?: Readonly<{durationMs?: number; unavailableReason?: "size_limit"; subtitle?: Readonly<{sourceUrl:string;sourceKind:"independent_srt"}>}>;
}>;

export type DetailProjectionStage = "route" | "detail_root" | "author_identity" | "structured_state" | "media_binding" | "snapshot_validation" | "unexpected";

export type AuthorIdentityReason = "no_visible_profile_anchor" | "duplicate_same_author" | "duplicate_different_author" | "malformed_profile_path" | "unsafe_profile_origin" | "unparseable_author_anchor";

export function isAuthorIdentityReason(value: unknown): value is AuthorIdentityReason {
  return value === "no_visible_profile_anchor" || value === "duplicate_same_author" || value === "duplicate_different_author" || value === "malformed_profile_path" || value === "unsafe_profile_origin" || value === "unparseable_author_anchor";
}

export class DetailProjectionError extends Error {
  readonly detail_reason?: AuthorIdentityReason;

  constructor(readonly stage: DetailProjectionStage, reason?: AuthorIdentityReason) {
    super(stage);
    this.name = "DetailProjectionError";
    if (stage === "author_identity" && isAuthorIdentityReason(reason)) this.detail_reason = reason;
  }
}

function requiredText(root: Element, selector: string, maximum: number, field: string): string | undefined {
  const text = visibleText(root.querySelector(selector));
  return text === undefined ? undefined : boundedText(text, maximum, field);
}

function uniqueDetailRoot(document: Document, expectedNoteId: string): Element {
  const roots = visibleElements<Element>(document, "#noteContainer, .note-detail-mask")
    .filter((root) => root.querySelector(".author-wrapper a[href]") !== null && root.querySelector("#detail-desc") !== null);
  const hasConflictingDeclaredIdentity = (root: Element): boolean => {
    for (let current: Element | null = root; current !== null; current = current.parentElement) {
      const declaredNoteId = current.getAttribute("data-note-id");
      if (declaredNoteId !== null && declaredNoteId !== expectedNoteId) return true;
    }
    return false;
  };
  if (roots.some(hasConflictingDeclaredIdentity)) throw new TypeError("exact visible detail root is unavailable");
  const leaves = roots.filter((root) => !roots.some((other) => other !== root && root.contains(other)));
  if (leaves.length !== 1 || leaves[0] === undefined) throw new TypeError("exact visible detail root is unavailable");
  return leaves[0];
}

function profileIdentity(root: Element): Readonly<{ authorId: string; canonicalUrl: string; authorRegion: Element }> {
  const allAnchors = visibleElements<HTMLAnchorElement>(root, ".author-wrapper a[href]");
  const titles = visibleElements<Element>(root, "#detail-title");
  const title = titles.length === 1 ? titles[0] : undefined;
  const followsTitle = title?.ownerDocument.defaultView?.Node.DOCUMENT_POSITION_FOLLOWING ?? 0;
  const anchors = title === undefined || followsTitle === 0
    ? allAnchors
    : allAnchors.filter((anchor) => (anchor.compareDocumentPosition(title) & followsTitle) !== 0);
  const profiles: Array<Readonly<{ authorId: string; canonicalUrl: string; authorRegion: Element }>> = [];
  for (const anchor of anchors) {
    let url: URL;
    try {
      url = new URL(anchor.getAttribute("href") ?? "", anchor.baseURI);
    } catch {
      throw new DetailProjectionError("author_identity", "unparseable_author_anchor");
    }
    if (!url.pathname.startsWith("/user/profile/")) continue;
    const match = url.pathname.match(/^\/user\/profile\/([A-Za-z0-9_-]{1,128})$/u);
    if (match === null || match[1] === undefined) throw new DetailProjectionError("author_identity", "malformed_profile_path");
    if (url.protocol !== "https:" || url.hostname !== "www.xiaohongshu.com" || url.port || url.username || url.password || !isSafeId(match[1])) {
      throw new DetailProjectionError("author_identity", "unsafe_profile_origin");
    }
    const authorRegion = anchor.closest(".author-wrapper");
    if (authorRegion === null) throw new DetailProjectionError("author_identity", "no_visible_profile_anchor");
    profiles.push({ authorId: match[1], canonicalUrl: `https://www.xiaohongshu.com${url.pathname}`, authorRegion });
  }
  if (profiles.length === 0 || profiles[0] === undefined) throw new DetailProjectionError("author_identity", "no_visible_profile_anchor");
  if (new Set(profiles.map((profile) => profile.authorId)).size !== 1) throw new DetailProjectionError("author_identity", "duplicate_different_author");
  return profiles[0];
}

function metric(raw: string | undefined): Readonly<Record<string, unknown>> {
  if (raw === undefined) return { precision: "not_exposed" };
  const exact = raw.replace(/,/gu, "");
  if (/^(?:0|[1-9][0-9]*)$/u.test(exact)) return { raw_value: exact, normalized_value: Number(exact), precision: "exact" };
  const rounded = /^(\d+(?:\.\d+)?)\s*(万|千)\+?$/u.exec(raw);
  if (rounded?.[1] !== undefined && rounded[2] !== undefined) {
    return { raw_value: raw, normalized_value: Math.round(Number(rounded[1]) * (rounded[2] === "万" ? 10_000 : 1_000)), precision: "display_rounded" };
  }
  return { precision: "not_exposed" };
}

function metricText(root: Element, selector: string): string | undefined {
  return requiredText(root, selector, 100, "metric");
}

function timeEvidence(root: Element): { published_at?: string; time_evidence?: { kind: "published" | "edited" | "unknown"; raw_text: string } } {
  const raw = requiredText(root, ".date", 200, "time evidence");
  if (raw === undefined) return {};
  const timestamp = publishedTimestamp(raw);
  const kind = /^(?:编辑|更新)于/u.test(raw) ? "edited" : timestamp !== undefined || raw.startsWith("发布于") ? "published" : "unknown";
  const result: { published_at?: string; time_evidence?: { kind: "published" | "edited" | "unknown"; raw_text: string } } = { time_evidence: { kind, raw_text: raw } };
  if (timestamp !== undefined) result.published_at = timestamp;
  return result;
}

function validateSnapshot(snapshot: CandidateSnapshotInput): CandidateSnapshotInput {
  parseNativeRequest({ protocol_version: "1.0", kind: "candidate_snapshot", job_id: "projection", ...snapshot });
  return snapshot;
}

export function projectDetail(document: Document, location: Location, expectedNoteId: string, sourcePosition: number): DetailProjection {
  if (detectBlockPage(document) !== undefined) throw new DetailProjectionError("route");
  const context = detectPage(document, location);
  if (context.kind !== "current" || context.noteId !== expectedNoteId || !isSafeId(expectedNoteId) || !Number.isInteger(sourcePosition) || sourcePosition < 1 || sourcePosition > 100) {
    throw new DetailProjectionError("route");
  }
  let detailRoot: Element;
  try {
    detailRoot = uniqueDetailRoot(document, expectedNoteId);
  } catch {
    throw new DetailProjectionError("detail_root");
  }
  let author: ReturnType<typeof profileIdentity>;
  try {
    author = profileIdentity(detailRoot);
  } catch (error) {
    throw new DetailProjectionError("author_identity", error instanceof DetailProjectionError ? error.detail_reason : undefined);
  }
  let state: ReturnType<typeof readStructuredDetailState>;
  try {
    state = readStructuredDetailState(document);
  } catch {
    throw new DetailProjectionError("structured_state");
  }
  if (state !== undefined && (state.noteId !== expectedNoteId || state.authorId !== undefined && state.authorId !== author.authorId)) {
    throw new DetailProjectionError("structured_state");
  }
  let mediaObservation: ReturnType<typeof observeBoundMedia>;
  try {
    mediaObservation = observeBoundMedia(detailRoot, { noteId: expectedNoteId, authorId: author.authorId });
  } catch {
    throw new DetailProjectionError("media_binding");
  }
  try {
    const semanticTitle = requiredText(detailRoot, "#detail-title", 200, "title");
    const semanticBody = requiredText(detailRoot, "#detail-desc", 20_000, "body");
    const semanticAuthorName = requiredText(author.authorRegion, ".name", 100, "author name");
    const semanticTags = boundedStringList(visibleElements<HTMLAnchorElement>(detailRoot, "#detail-desc a[href*='search']")
      .map((anchor) => visibleText(anchor)).filter((tag): tag is string => tag !== undefined), 100, 100, "tags");
    const title = state?.title ?? semanticTitle;
    const body = state?.body ?? semanticBody;
    const authorName = state?.authorName ?? semanticAuthorName;
    const tags = state?.tags ?? semanticTags;
    const semanticTime = timeEvidence(detailRoot);
    const media = mediaObservation.sources;
  const metricSources: Record<string, DetailFieldSource> = {};
  const stateMetric = (name: "likes" | "collects" | "comments" | "shares", selector: string): Readonly<Record<string, unknown>> => {
    const raw = state?.metrics[name];
    const semanticRaw = raw === undefined ? metricText(detailRoot, selector) : undefined;
    const value = metric(raw ?? semanticRaw);
    metricSources[name] = value.precision === "not_exposed" ? "not_exposed" : raw === undefined ? "detail_dom" : "structured_state";
    return value;
  };
  const metrics = {
    likes: stateMetric("likes", ".engage-bar .like-wrapper .count"),
    collects: stateMetric("collects", ".engage-bar .collect-wrapper .count"),
    comments: stateMetric("comments", ".engage-bar .chat-wrapper .count"),
    shares: stateMetric("shares", ".engage-bar .share-wrapper .count")
  };
  const metric_provenance = Object.fromEntries(Object.entries(metrics)
    .filter(([name, value]) => value.precision !== "not_exposed" && metricSources[name] === "detail_dom")
    .map(([name]) => [name, "detail_visible_count" as const]));
  const field_provenance: Record<string, DetailFieldSource> = {
    title: state?.title !== undefined ? "structured_state" : semanticTitle === undefined ? "not_exposed" : "detail_dom",
    body: state?.body !== undefined ? "structured_state" : semanticBody === undefined ? "not_exposed" : "detail_dom",
    author_id: state?.authorId !== undefined ? "structured_state" : "detail_dom",
    author_name: state?.authorName !== undefined ? "structured_state" : semanticAuthorName === undefined ? "not_exposed" : "detail_dom",
    tags: state?.tags !== undefined ? "structured_state" : semanticTags.length === 0 ? "not_exposed" : "detail_dom",
    note_type: state?.noteType !== undefined ? "structured_state" : "detail_dom",
    published_at: state?.publishedAt !== undefined ? "structured_state" : semanticTime.published_at === undefined ? "not_exposed" : "detail_dom",
    time_evidence: state?.publishedAt !== undefined ? "not_exposed" : semanticTime.time_evidence === undefined ? "not_exposed" : "detail_dom",
    author_profile_url: "detail_dom",
    media: mediaObservation.slots.length === 0 || media.length !== mediaObservation.slots.length ? "not_exposed" : "detail_dom",
    ...metricSources
  };
    const snapshot = validateSnapshot({
    source_position: sourcePosition,
    note_id: expectedNoteId,
    canonical_url: context.canonicalUrl,
    ...(title === undefined ? {} : { title }),
    ...(body === undefined ? {} : { body }),
    ...(tags.length === 0 ? {} : { tags: Array.from(tags) }),
    note_type: state?.noteType ?? (mediaObservation.family === "video" ? "video" : "normal"),
    ...(state?.publishedAt === undefined ? semanticTime : { published_at: state.publishedAt }),
    author_id: author.authorId,
    ...(authorName === undefined ? {} : { author_name: authorName }),
    author_profile_url: author.canonicalUrl,
    metrics,
    ...(Object.keys(metric_provenance).length === 0 ? {} : { metric_provenance }),
    media_slots: mediaObservation.slots
  });
    return { snapshot, media, field_provenance, ...(mediaObservation.family !== "video" ? {} : {video: {
      ...(mediaObservation.durationMs === undefined ? {} : {durationMs:mediaObservation.durationMs}),
      ...(mediaObservation.unavailableReason === undefined ? {} : {unavailableReason:mediaObservation.unavailableReason}),
      ...(mediaObservation.subtitle === undefined ? {} : {subtitle:mediaObservation.subtitle})
    }}) };
  } catch {
    throw new DetailProjectionError("snapshot_validation");
  }
}
