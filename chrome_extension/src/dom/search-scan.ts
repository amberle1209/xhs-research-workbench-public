import { boundedText } from "../security.js";
import { canonicalNoteAnchor } from "./candidates.js";
import type { PageContext } from "./route.js";
import { isVisible, visibleElements, visibleText } from "./visibility.js";

export type NoteSummary = Readonly<{
  note_id: string;
  canonical_url: string;
  source_position: number;
  title?: string;
  cover?: "visible";
  likes?: string;
  note_type?: string;
  sponsorship_evidence: "unknown";
  summary_source: "search_card_visible_dom";
}>;

export type SearchScanExclusion = Readonly<{
  note_id: string;
  canonical_url: string;
  source_position: number;
  reason: "sponsored";
  is_sponsored: true;
  sponsorship_evidence: "visible_sponsored_label";
}>;

export type SearchScanResult = Readonly<{
  summaries: readonly NoteSummary[];
  exclusions: readonly SearchScanExclusion[];
  sort_label?: string;
}>;

function boundedVisibleText(card: Element, selector: string, maximum: number): string | undefined {
  try {
    const text = visibleText(card.querySelector(selector));
    return text === undefined ? undefined : boundedText(text, maximum, selector);
  } catch {
    return undefined;
  }
}

function hasVisibleCover(card: Element): boolean {
  return visibleElements<HTMLImageElement>(card, "img.cover, img[data-card-cover]").length > 0;
}

function hasVisibleSponsorshipEvidence(card: Element): boolean {
  return visibleElements(card, ".sponsored, .ad-label, [data-sponsored-label]").length > 0;
}

function loading(card: Element): boolean {
  return card.hasAttribute("data-skeleton") || card.getAttribute("aria-busy") === "true" ||
    card.classList.contains("skeleton") || card.classList.contains("loading");
}

function sortLabel(document: Document): string | undefined {
  return boundedVisibleText(document.documentElement, "[data-search-sort-label]", 100);
}

/** Reads only currently visible search-card DOM facts; it never opens, fetches, or projects a detail page. */
export function scanSearchCards(document: Document, pageContext: PageContext): SearchScanResult {
  if (pageContext.kind !== "search") throw new TypeError("search scan requires the canonical search route");

  const seenNoteIds = new Set<string>();
  const summaries: NoteSummary[] = [];
  const exclusions: SearchScanExclusion[] = [];
  let sourcePosition = 0;
  for (const card of Array.from(document.querySelectorAll("section.note-item:not(.query-note-item)"))) {
    if (!isVisible(card) || loading(card)) continue;
    const anchor = visibleElements<HTMLAnchorElement>(card, "a[href]").find((item) => canonicalNoteAnchor(item) !== undefined);
    if (anchor === undefined) continue;
    const identity = canonicalNoteAnchor(anchor);
    if (identity === undefined || seenNoteIds.has(identity.noteId)) continue;
    seenNoteIds.add(identity.noteId);
    sourcePosition += 1;
    if (hasVisibleSponsorshipEvidence(card)) {
      exclusions.push({
        note_id: identity.noteId,
        canonical_url: identity.canonicalUrl,
        source_position: sourcePosition,
        reason: "sponsored",
        is_sponsored: true,
        sponsorship_evidence: "visible_sponsored_label"
      });
      continue;
    }
    const title = boundedVisibleText(card, ".title, [data-card-title]", 200);
    const likes = boundedVisibleText(card, ".likes, [data-card-likes]", 100);
    const noteType = boundedVisibleText(card, ".type, [data-card-type]", 100);
    summaries.push({
      note_id: identity.noteId,
      canonical_url: identity.canonicalUrl,
      source_position: sourcePosition,
      ...(title === undefined ? {} : { title }),
      ...(hasVisibleCover(card) ? { cover: "visible" as const } : {}),
      ...(likes === undefined ? {} : { likes }),
      ...(noteType === undefined ? {} : { note_type: noteType }),
      sponsorship_evidence: "unknown",
      summary_source: "search_card_visible_dom"
    });
  }
  const label = sortLabel(document);
  return { summaries, exclusions, ...(label === undefined ? {} : { sort_label: label }) };
}
