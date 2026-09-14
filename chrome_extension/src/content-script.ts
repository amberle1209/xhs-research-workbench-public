import { discoverCandidates } from "./dom/candidates.js";
import { detectBlockPage } from "./dom/challenge.js";
import { DetailProjectionError, isAuthorIdentityReason, projectDetail } from "./dom/detail.js";
import { detectPage } from "./dom/route.js";
import { scrollCandidates } from "./dom/scroll.js";
import { scanSearchCards } from "./dom/search-scan.js";
import { isSafeId } from "./security.js";

type InspectMessage = Readonly<{ kind: "inspect_page" }>;
type CandidateMessage = Readonly<{ kind: "discover_candidates"; maximum: number }>;
type ScrollMessage = Readonly<{ kind: "scroll_candidates" }>;
type SearchScanMessage = Readonly<{ kind: "scan_search" }>;
type DetailMessage = Readonly<{ kind: "project_detail"; noteId: string; sourcePosition: number }>;
type ContentMessage = InspectMessage | CandidateMessage | ScrollMessage | SearchScanMessage | DetailMessage;
const listenerSentinel = "__xhsResearchWorkbenchContentListenerV1";

type ContentGlobal = typeof globalThis & Record<string, unknown>;

function isContentMessage(value: unknown): value is ContentMessage {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const message = value as Record<string, unknown>;
  switch (message.kind) {
    case "inspect_page": return Object.keys(message).length === 1;
    case "discover_candidates": return Object.keys(message).length === 2 && Number.isInteger(message.maximum) && (message.maximum as number) >= 1 && (message.maximum as number) <= 100;
    case "scroll_candidates": return Object.keys(message).length === 1;
    case "scan_search": return Object.keys(message).length === 1;
    case "project_detail": return Object.keys(message).length === 3 && isSafeId(message.noteId) && Number.isInteger(message.sourcePosition) && (message.sourcePosition as number) >= 1 && (message.sourcePosition as number) <= 100;
    default: return false;
  }
}

/** Registers at most once in Chrome's persistent isolated-world global. */
export function installContentScript(globalRef: ContentGlobal = globalThis as ContentGlobal): void {
  if (globalRef[listenerSentinel] === true) return;
  chrome.runtime.onMessage.addListener((message: unknown, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id || !isContentMessage(message)) return;
    try {
      const context = detectPage(document, window.location);
      switch (message.kind) {
        case "inspect_page": sendResponse({ context, block: detectBlockPage(document) }); return;
        case "discover_candidates": sendResponse({ candidates: discoverCandidates(document, context, message.maximum) }); return;
        case "scroll_candidates": sendResponse(scrollCandidates(document, window.location, () => {
          window.scrollBy({ top: document.documentElement.scrollHeight, behavior: "auto" });
        })); return;
        case "scan_search": sendResponse(scanSearchCards(document, context)); return;
        case "project_detail": {
          const block = detectBlockPage(document);
          if (block !== undefined) {
            sendResponse({ error: block });
            return;
          }
          try {
            sendResponse(projectDetail(document, window.location, message.noteId, message.sourcePosition));
          } catch (error) {
            const stage = error instanceof DetailProjectionError ? error.stage : "unexpected";
            const reason = error instanceof DetailProjectionError ? error.detail_reason : undefined;
            sendResponse({ error: "detail_unavailable", stage, ...(stage === "author_identity" && isAuthorIdentityReason(reason) ? { detail_reason: reason } : {}) });
          }
          return;
        }
      }
    } catch {
      sendResponse({ error: "detail_unavailable" });
    }
  });
  globalRef[listenerSentinel] = true;
}

installContentScript();
