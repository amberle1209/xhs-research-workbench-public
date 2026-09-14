import { assertCanonicalXhsUrl } from "../security.js";
import type { PageContext } from "./route.js";
import { isVisible, visibleElements } from "./visibility.js";

export type CandidateLink = Readonly<{
  sourcePosition: number;
  noteId: string;
  canonicalUrl: string;
}>;

export function canonicalNoteAnchor(anchor: HTMLAnchorElement): CandidateLink | undefined {
  let canonicalUrl: string;
  try {
    canonicalUrl = assertCanonicalXhsUrl(new URL(anchor.getAttribute("href") ?? "", anchor.baseURI).toString().replace(/[?#].*$/u, ""), "note");
  } catch {
    return undefined;
  }
  const noteId = canonicalUrl.slice(canonicalUrl.lastIndexOf("/") + 1);
  return { sourcePosition: 0, noteId, canonicalUrl };
}

function cardHasExpectedOwner(card: Element, profileId: string): boolean {
  // A note anchor is not proof of account ownership. Check only canonical profile anchors separately.
  return visibleElements<HTMLAnchorElement>(card, ".card-owner[href], .author-wrapper a[href]").some((anchor) => {
    try {
      const profile = assertCanonicalXhsUrl(new URL(anchor.getAttribute("href") ?? "", anchor.baseURI).toString().replace(/[?#].*$/u, ""), "profile");
      return profile.endsWith(`/${profileId}`);
    } catch {
      return false;
    }
  });
}

export function discoverCandidates(document: Document, pageContext: PageContext, maximum: number): CandidateLink[] {
  if (!Number.isInteger(maximum) || maximum < 1 || maximum > 100) throw new TypeError("candidate scan limit is invalid");
  if (pageContext.kind !== "search" && pageContext.kind !== "account") return [];

  const seen = new Set<string>();
  const candidates: CandidateLink[] = [];
  for (const card of Array.from(document.querySelectorAll("section.note-item:not(.query-note-item)"))) {
    if (!isVisible(card)) continue;
    if (pageContext.kind === "account" && !cardHasExpectedOwner(card, pageContext.profileId)) continue;
    for (const anchor of visibleElements<HTMLAnchorElement>(card, "a[href]")) {
      const candidate = canonicalNoteAnchor(anchor);
      if (candidate === undefined || seen.has(candidate.noteId)) continue;
      seen.add(candidate.noteId);
      candidates.push({ ...candidate, sourcePosition: candidates.length + 1 });
      break;
    }
    if (candidates.length === maximum) break;
  }
  return candidates;
}
