import { boundedStringList, boundedText, isSafeId } from "../security.js";

export type StructuredDetailState = Readonly<{
  noteId: string;
  authorId?: string;
  title?: string;
  body?: string;
  tags?: readonly string[];
  noteType?: string;
  publishedAt?: string;
  authorName?: string;
  metrics: Readonly<Partial<Record<"likes" | "collects" | "comments" | "shares", string>>>;
}>;

function optionalText(value: unknown, maximum: number, field: string): string | undefined {
  if (value === undefined || value === null) return undefined;
  return boundedText(value, maximum, field);
}

function optionalId(value: unknown, field: string): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (!isSafeId(value)) throw new TypeError(`${field} is invalid`);
  return value;
}

function stateRecord(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new TypeError("structured detail state is invalid");
  return value as Record<string, unknown>;
}

function stateMetrics(value: unknown): StructuredDetailState["metrics"] {
  if (value === undefined || value === null) return {};
  const record = stateRecord(value);
  const result: Partial<Record<"likes" | "collects" | "comments" | "shares", string>> = {};
  for (const name of ["likes", "collects", "comments", "shares"] as const) {
    const metric = optionalText(record[name], 100, `structured ${name}`);
    if (metric !== undefined) result[name] = metric;
  }
  return result;
}

/** Reads only an explicit, inert, sanitized detail-state island; unknown page state is not inferred. */
export function readStructuredDetailState(document: Document): StructuredDetailState | undefined {
  const nodes = Array.from(document.querySelectorAll<HTMLScriptElement>('script#xhs-note-state[type="application/json"]'));
  if (nodes.length === 0) return undefined;
  if (nodes.length !== 1 || nodes[0] === undefined) throw new TypeError("structured detail state is ambiguous");
  let parsed: unknown;
  try { parsed = JSON.parse(nodes[0].textContent ?? ""); } catch { throw new TypeError("structured detail state is invalid"); }
  const state = stateRecord(parsed);
  const noteId = optionalId(state.note_id, "structured note id");
  if (noteId === undefined) throw new TypeError("structured note id is missing");
  const authorId = optionalId(state.author_id, "structured author id");
  const title = optionalText(state.title, 200, "structured title");
  const body = optionalText(state.body, 20_000, "structured body");
  const tags = state.tags === undefined || state.tags === null ? undefined : boundedStringList(state.tags, 100, 100, "structured tags");
  const noteType = optionalText(state.note_type, 100, "structured note type");
  const publishedAt = optionalText(state.published_at, 100, "structured published at");
  const authorName = optionalText(state.author_name, 100, "structured author name");
  return {
    noteId,
    ...(authorId === undefined ? {} : { authorId }),
    ...(title === undefined ? {} : { title }),
    ...(body === undefined ? {} : { body }),
    ...(tags === undefined ? {} : { tags }),
    ...(noteType === undefined ? {} : { noteType }),
    ...(publishedAt === undefined ? {} : { publishedAt }),
    ...(authorName === undefined ? {} : { authorName }),
    metrics: stateMetrics(state.metrics)
  };
}
