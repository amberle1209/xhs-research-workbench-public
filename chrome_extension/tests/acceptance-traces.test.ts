import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "vitest";

import { type NativeResponse } from "../src/contracts.js";
import {
  JobController,
  type JobControllerDependencies,
  type NativeTransport
} from "../src/job-controller.js";

const acceptanceFixtureDir = resolve(import.meta.dirname, "../../tests/fixtures/extension_acceptance");
const JPEG_BYTES = new Uint8Array([0xff, 0xd8, 0xff, 0x66, 0x69, 0x78, 0x74, 0x75, 0x72, 0x65, 0x2d, 0x69, 0x6d, 0x61, 0x67, 0x65]);
const JPEG_SHA256 = "f26b33c2b0294d41989d6653fd81181b689b549e4c7473ca31f65db738e72c1e";

type CandidateInput = Readonly<{
  sourcePosition: number;
  noteId: string;
  snapshot: Record<string, unknown>;
  hasImage?: boolean;
}>;

type Scenario = Readonly<{
  jobId: string;
  sourceKind: "current" | "search";
  sourceUrl: string;
  candidates: readonly CandidateInput[];
  selectedIds: readonly string[];
  requestedCount: number;
  candidateScanLimit: number;
  publicationCutoff?: string;
}>;

function candidate(
  sourcePosition: number,
  noteId: string,
  snapshot: Record<string, unknown>,
  hasImage = false
): CandidateInput {
  return { sourcePosition, noteId, snapshot, hasImage };
}

function noteUrl(noteId: string): string {
  return `https://www.xiaohongshu.com/explore/${noteId}`;
}

const currentScenario: Scenario = {
  jobId: "acceptance_current",
  sourceKind: "current",
  sourceUrl: noteUrl("current_note"),
  requestedCount: 1,
  candidateScanLimit: 1,
  selectedIds: ["current_note"],
  candidates: [candidate(1, "current_note", {
    source_position: 1,
    note_id: "current_note",
    canonical_url: noteUrl("current_note"),
    title: "当前帖子",
    author_id: "author_current",
    author_name: "当前作者",
    author_profile_url: "https://www.xiaohongshu.com/user/profile/author_current",
    metrics: { likes: { raw_value: "7", normalized_value: 7, precision: "exact" } },
    metric_provenance: { likes: "detail_visible_count" },
    media_slots: [{ note_id: "current_note", role: "image", position: 1 }]
  }, true)]
};

const batchScenario: Scenario = {
  jobId: "acceptance_batch",
  sourceKind: "search",
  sourceUrl: "https://www.xiaohongshu.com/search_result",
  requestedCount: 5,
  candidateScanLimit: 10,
  publicationCutoff: "2026-03-01T12:00:00+08:00",
  selectedIds: ["note_06", "note_08", "note_07"],
  candidates: [
    candidate(1, "note_01", { source_position: 1, note_id: "note_01", canonical_url: noteUrl("note_01"), published_at: "2026-03-02T12:00:00+08:00", time_evidence: { kind: "published", raw_text: "发布于 2026-03-02 12:00" }, metrics: { likes: { raw_value: "1万+", normalized_value: 10000, precision: "display_rounded" } } }),
    candidate(2, "note_02", { source_position: 2, note_id: "note_02", canonical_url: noteUrl("note_02"), published_at: "2026-03-02T12:00:00+08:00", time_evidence: { kind: "published", raw_text: "发布于 2026-03-02 12:00" }, metrics: {} }),
    candidate(3, "note_03", { source_position: 3, note_id: "note_03", canonical_url: noteUrl("note_03"), published_at: null, time_evidence: { kind: "unknown", raw_text: "3天前" }, metrics: { likes: { raw_value: "100", normalized_value: 100, precision: "exact" } } }),
    candidate(4, "note_04", { source_position: 4, note_id: "note_04", canonical_url: noteUrl("note_04"), published_at: "2026-03-02T12:00:00+08:00", time_evidence: { kind: "edited", raw_text: "编辑于 2026-03-02 12:00" }, metrics: { likes: { raw_value: "90", normalized_value: 90, precision: "exact" } } }),
    candidate(5, "note_05", { source_position: 5, note_id: "note_05", canonical_url: noteUrl("note_05"), published_at: "2026-03-02T12:00:00+08:00", time_evidence: { kind: "published", raw_text: "发布于 2026-03-02 12:00" }, metrics: { likes: { raw_value: "9万", normalized_value: 90000, precision: "display_rounded" } } }),
    candidate(6, "note_06", { source_position: 6, note_id: "note_06", canonical_url: noteUrl("note_06"), published_at: "2026-03-02T12:00:00+08:00", time_evidence: { kind: "published", raw_text: "发布于 2026-03-02 12:00" }, metrics: { likes: { raw_value: "50", normalized_value: 50, precision: "exact" } }, media_slots: [{ note_id: "note_06", role: "image", position: 1 }] }, true),
    candidate(7, "note_07", { source_position: 7, note_id: "note_07", canonical_url: noteUrl("note_07"), published_at: "2026-03-02T12:00:00+08:00", time_evidence: { kind: "published", raw_text: "发布于 2026-03-02 12:00" }, metrics: { likes: { raw_value: "20", normalized_value: 20, precision: "exact" } }, media_slots: [] }),
    candidate(8, "note_08", { source_position: 8, note_id: "note_08", canonical_url: noteUrl("note_08"), published_at: "2026-03-02T12:00:00+08:00", time_evidence: { kind: "published", raw_text: "发布于 2026-03-02 12:00" }, metrics: { likes: { raw_value: "30", normalized_value: 30, precision: "exact" } }, media_slots: [] }),
    candidate(9, "note_09", { source_position: 9, note_id: "note_09", canonical_url: noteUrl("note_09"), published_at: "2026-03-02T12:00:00+08:00", time_evidence: { kind: "published", raw_text: "发布于 2026-03-02 12:00" }, metrics: {} }),
    candidate(10, "note_10", { source_position: 10, note_id: "note_10", canonical_url: noteUrl("note_10"), published_at: null, time_evidence: { kind: "unknown", raw_text: "刚刚" }, metrics: { likes: { raw_value: "60", normalized_value: 60, precision: "exact" } } })
  ]
};

class RecordingNativeTransport implements NativeTransport {
  readonly requests: Record<string, unknown>[] = [];

  constructor(private readonly scenario: Scenario) {}

  async connect(): Promise<Readonly<{ hostVersion: string }>> {
    return { hostVersion: "0.1.0" };
  }

  interrupt(): void {}

  completeTerminal(): void {}

  async request(value: unknown): Promise<NativeResponse> {
    const request = value as Record<string, unknown>;
    this.requests.push(request);
    const jobId = request.job_id as string;
    const scanned = this.scenario.candidates.length;
    const selected = this.scenario.selectedIds.map((note_id, index) => ({ note_id, selection_rank: index + 1 }));
    const partial = selected.length !== this.scenario.requestedCount;
    switch (request.kind) {
      case "begin_job": return { protocol_version: "1.0", kind: "job_started", job_id: jobId, status: "started" } as NativeResponse;
      case "candidate_snapshot": return { protocol_version: "1.0", kind: "candidate_result", job_id: jobId, note_id: request.note_id as string, source_position: request.source_position as number, outcome: "recorded" } as NativeResponse;
      case "finish_scan": return { protocol_version: "1.0", kind: "selection_result", job_id: jobId, scanned_count: scanned, eligible_count: selected.length, selected_count: selected.length, status: partial ? "partial" : "complete", selected } as NativeResponse;
      case "media_begin": case "media_chunk": return { protocol_version: "1.0", kind: "progress", job_id: jobId, phase: "downloading", discovered: scanned, inspected: scanned, eligible: selected.length, selected: selected.length, saved: 0, current_source_position: scanned } as NativeResponse;
      case "media_end": return { protocol_version: "1.0", kind: "media_result", job_id: jobId, note_id: request.note_id as string, role: request.role as "image", position: request.position as number, outcome: "downloaded" } as NativeResponse;
      case "finish_job": return { protocol_version: "1.0", kind: "job_result", job_id: jobId, status: partial ? "partial" : "complete", retained_count: selected.length, report_available: true, report_file: "index.html" } as NativeResponse;
      default: throw new Error(`unexpected production request: ${String(request.kind)}`);
    }
  }
}

async function runScenario(scenario: Scenario): Promise<unknown[]> {
  let queueNoteId: string | undefined;
  const originalChrome = (globalThis as { chrome?: typeof chrome }).chrome;
  const candidateByNoteId = new Map(scenario.candidates.map((item) => [item.noteId, item]));
  const sourceTabId = 1;
  const queueTabId = 2;
  const contextFor = (noteId: string) => ({ context: { kind: "current", noteId, canonicalUrl: noteUrl(noteId) } });
  (globalThis as { chrome: unknown }).chrome = {
    permissions: { contains: async () => true },
    scripting: { executeScript: async () => [] },
    tabs: {
      create: async () => ({ id: queueTabId, url: "about:blank" }),
      update: async (_tabId: number, changes: { url?: string }) => {
        queueNoteId = changes.url?.split("/").at(-1);
        return { id: queueTabId, url: changes.url };
      },
      remove: async () => undefined,
      sendMessage: async (tabId: number, message: { kind: string; noteId?: string }) => {
        if (message.kind === "inspect_page") {
          if (tabId === sourceTabId) {
            return scenario.sourceKind === "current"
              ? contextFor(scenario.candidates[0]?.noteId ?? "")
              : { context: { kind: "search", canonicalUrl: scenario.sourceUrl } };
          }
          return contextFor(queueNoteId ?? "");
        }
        if (message.kind === "discover_candidates") return {
          candidates: scenario.candidates.map(({ sourcePosition, noteId }) => ({ sourcePosition, noteId, canonicalUrl: noteUrl(noteId) }))
        };
        if (message.kind === "project_detail") {
          const item = candidateByNoteId.get(message.noteId ?? "");
          if (item === undefined) return null;
          return {
            snapshot: item.snapshot,
            media: item.hasImage ? [{ note_id: item.noteId, role: "image", position: 1, sourceUrl: "https://ci.xhscdn.com/fixture.jpg" }] : []
          };
        }
        throw new Error(`unexpected content message: ${message.kind}`);
      },
      onRemoved: { addListener: () => undefined, removeListener: () => undefined }
    },
    storage: { local: { set: async () => undefined } }
  } as unknown as typeof chrome;
  const native = new RecordingNativeTransport(scenario);
  const dependencies: JobControllerDependencies = {
    native,
    sleep: async () => undefined,
    delay: (minimum) => minimum,
    newJobId: () => scenario.jobId,
    fetchMedia: async () => ({ bytes: JPEG_BYTES, sizeBytes: JPEG_BYTES.byteLength, mimeType: "image/jpeg", sha256: JPEG_SHA256 })
  };
  try {
    const controller = new JobController(dependencies);
    if (scenario.sourceKind === "current") await controller.collectCurrent({ id: sourceTabId } as chrome.tabs.Tab);
    else await controller.collectBatch({ id: sourceTabId } as chrome.tabs.Tab, {
      requestedCount: scenario.requestedCount,
      candidateScanLimit: scenario.candidateScanLimit,
      publicationCutoff: scenario.publicationCutoff
    });
    return native.requests;
  } finally {
    if (originalChrome === undefined) delete (globalThis as { chrome?: typeof chrome }).chrome;
    else (globalThis as { chrome: typeof chrome }).chrome = originalChrome;
  }
}

export async function generateAcceptanceTraces(): Promise<Record<string, unknown[]>> {
  return {
    "current_note_image_messages.json": await runScenario(currentScenario),
    "batch_ten_candidates_messages.json": await runScenario(batchScenario)
  };
}

describe("production-controller acceptance traces", () => {
  test("verifies committed native traces are produced by the controller", async () => {
    const generated = await generateAcceptanceTraces();
    for (const name of ["current_note_image_messages.json", "batch_ten_candidates_messages.json"]) {
      expect(generated[name]).toEqual(JSON.parse(readFileSync(resolve(acceptanceFixtureDir, name), "utf8")));
    }
  });
});
