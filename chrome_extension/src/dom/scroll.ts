import { detectBlockPage, type BlockPageReason } from "./challenge.js";
import { detectPage } from "./route.js";

export type ScrollCandidatesResult = Readonly<{ scrolled: true }> | Readonly<{
  error: Exclude<BlockPageReason, undefined> | "unsupported_page" | "scroll_limit";
}>;

export function scrollCandidates(document: Document, location: Location, scroll: () => void, allowed = true): ScrollCandidatesResult {
  if (!allowed) return { error: "scroll_limit" };
  const block = detectBlockPage(document);
  if (block !== undefined) return { error: block };
  const context = detectPage(document, location);
  if (context.kind !== "search" && context.kind !== "account") return { error: "unsupported_page" };
  scroll();
  return { scrolled: true };
}
