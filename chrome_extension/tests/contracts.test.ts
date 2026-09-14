import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "vitest";

import {
  REQUEST_TO_ALLOWED_RESPONSE_KINDS,
  assertResponseForRequest,
  parseNativeRequest,
  parseNativeResponse
} from "../src/contracts.js";
import {
  assertAllowedMediaUrl,
  canonicalizeXhsUrl,
  isSafeId,
  isSafeRetainedText
} from "../src/security.js";

const fixtureDir = resolve(import.meta.dirname, "../../tests/fixtures/native_protocol");
const acceptanceFixtureDir = resolve(import.meta.dirname, "../../tests/fixtures/extension_acceptance");
const fixture = (name: string): unknown => JSON.parse(readFileSync(resolve(fixtureDir, name), "utf8"));
const names = (prefix: string): string[] => readdirSync(fixtureDir).filter((name) => name.startsWith(prefix));
const requestFixtures = ["request_health.json", "request_begin_job.json", "request_candidate_snapshot.json", "request_candidate_unavailable.json", "request_finish_scan.json", "request_media_begin.json", "request_media_begin_rejected.json", "request_media_chunk.json", "request_media_end.json", "request_media_missing.json", "request_finish_job.json", "request_stop_job.json", "request_open_report.json"];
const responseFixtures = ["response_health.json", "response_begin_job.json", "response_candidate_snapshot.json", "response_candidate_unavailable.json", "response_finish_scan.json", "response_media_begin.json", "response_media_begin_rejected.json", "response_media_chunk.json", "response_media_end.json", "response_media_missing.json", "response_finish_job.json", "response_stop_job.json", "response_open_report.json", "response_error_health.json", "response_error_begin_job.json", "response_error_candidate_snapshot.json", "response_error_candidate_unavailable.json", "response_error_finish_scan.json", "response_error_media_begin.json", "response_error_media_begin_rejected.json", "response_error_media_chunk.json", "response_error_media_end.json", "response_error_media_missing.json", "response_error_finish_job.json", "response_error_stop_job.json", "response_error_open_report.json", "response_error_job.json"];

describe("Native Messaging wire contracts", () => {
  test("accepts every golden request and response without serialization drift", () => {
    expect(names("request_").sort()).toEqual([...requestFixtures].sort());
    expect(names("response_").sort()).toEqual([...responseFixtures].sort());
    for (const name of requestFixtures) {
      const raw = fixture(name);
      expect(parseNativeRequest(raw)).toEqual(raw);
    }
    for (const name of responseFixtures) {
      const raw = fixture(name);
      expect(parseNativeResponse(raw)).toEqual(raw);
    }
  });

  test("maps every successful fixture to its exact allowed response kind and matching job scope", () => {
    for (const name of names("request_")) {
      const request = parseNativeRequest(fixture(name));
      const response = parseNativeResponse(fixture(name.replace("request_", "response_")));
      expect(REQUEST_TO_ALLOWED_RESPONSE_KINDS[request.kind]).toContain(response.kind);
      expect(assertResponseForRequest(request, response)).toEqual(response);
    }
  });

  test("cross-checks every offline acceptance message with the TypeScript protocol", () => {
    for (const name of ["current_note_image_messages.json", "batch_ten_candidates_messages.json"]) {
      const messages: unknown = JSON.parse(readFileSync(resolve(acceptanceFixtureDir, name), "utf8"));
      expect(Array.isArray(messages)).toBe(true);
      for (const message of messages as unknown[]) {
        expect(JSON.parse(JSON.stringify(parseNativeRequest(message)))).toEqual(message);
      }
    }
  });

  test("keeps legacy exact-likes messages compatible and validates page-order begin jobs", () => {
    const pageOrder = fixture("page_order_begin_job.json") as Record<string, unknown>;
    const invalidCutoff = fixture("invalid_page_order_begin_job_cutoff.json");
    const legacy = { ...fixture("request_begin_job.json") as Record<string, unknown> };
    delete legacy.selection_order;

    for (const requested_count of [5, 10]) {
      const accepted = { ...pageOrder, requested_count, candidate_scan_limit: requested_count };
      expect(parseNativeRequest(accepted)).toEqual(accepted);
    }
    expect(parseNativeRequest(legacy)).toEqual(legacy);
    for (const invalid of [
      invalidCutoff,
      { ...pageOrder, selection_order: "unknown_order" },
      { ...pageOrder, requested_count: 1 },
      { ...pageOrder, collection_surface: "extension_account" },
      { ...pageOrder, source_page_url: "https://www.xiaohongshu.com/search_result?sort=latest" },
      { ...pageOrder, source_page_url: "https://www.xiaohongshu.com/search_result#latest" },
      { ...pageOrder, sort_label: "token=secret" },
      { ...pageOrder, media_policy: "full_media" }
    ]) expect(() => parseNativeRequest(invalid)).toThrow();
  });

  test("accepts every finite candidate-unavailable reason from the shared contract", () => {
    const candidate = fixture("request_candidate_unavailable.json") as Record<string, unknown>;
    for (const reason of ["detail_unavailable", "sponsored", "invalid_card"]) {
      const message = { ...candidate, reason };
      expect(parseNativeRequest(message)).toEqual(message);
    }
  });

  test("rejects unknown wire fields and broken conditional contracts", () => {
    expect(() => parseNativeRequest({ ...fixture("request_health.json") as object, extra: true })).toThrow();
    expect(() => parseNativeRequest({ ...fixture("request_begin_job.json") as object, requested_count: 2, candidate_scan_limit: 1 })).toThrow();
    expect(() => parseNativeRequest({ ...fixture("request_candidate_snapshot.json") as object, media_slots: [{ note_id: "other", role: "image", position: 1 }] })).toThrow();
    expect(() => parseNativeResponse({ ...fixture("response_finish_scan.json") as object, selected_count: 2 })).toThrow();
    expect(() => parseNativeResponse({ ...fixture("response_finish_job.json") as object, report_file: "/tmp/report.html" })).toThrow();
    expect(() => parseNativeResponse({ ...fixture("response_error_health.json") as object, message: "sensitive" })).toThrow();
  });

  test("strictly validates additive terminal and page-order scan evidence", () => {
    const finish = { protocol_version: "1.0", kind: "finish_scan", job_id: "job_123", scroll_rounds: 2, sort_label: "综合" };
    const stop = { protocol_version: "1.0", kind: "stop_job", job_id: "job_123", terminal_cause: "identity_mismatch" };
    expect(parseNativeRequest(finish)).toEqual(finish);
    expect(parseNativeRequest(stop)).toEqual(stop);
    for (const invalid of [
      { ...finish, scroll_rounds: 3 }, { ...finish, extra: true },
      { ...stop, terminal_cause: "unknown" }, { ...stop, extra: true }
    ]) expect(() => parseNativeRequest(invalid)).toThrow();
    expect(parseNativeResponse({ protocol_version: "1.0", kind: "job_result", job_id: "job_123", status: "failed", retained_count: 0, report_available: false })).toMatchObject({ status: "failed" });
  });

  test("requires health responses to omit job_id and job responses to retain the matching job_id", () => {
    const health = parseNativeRequest(fixture("request_health.json"));
    const healthError = parseNativeResponse({ ...fixture("response_error_health.json") as object, job_id: "job_123" });
    const job = parseNativeRequest(fixture("request_begin_job.json"));
    const missingJob = parseNativeResponse(fixture("response_error_health.json"));
    const wrongJob = parseNativeResponse({ ...fixture("response_error_begin_job.json") as object, job_id: "other" });

    expect(() => assertResponseForRequest(health, healthError)).toThrow();
    expect(() => assertResponseForRequest(job, missingJob)).toThrow();
    expect(() => assertResponseForRequest(job, wrongJob)).toThrow();
  });

  test("rejects candidate and media responses that do not echo the exact request identity", () => {
    const candidate = parseNativeRequest(fixture("request_candidate_snapshot.json"));
    const wrongCandidate = parseNativeResponse({
      ...fixture("response_candidate_snapshot.json") as object,
      note_id: "note_other"
    });
    const wrongCandidatePosition = parseNativeResponse({
      ...fixture("response_candidate_snapshot.json") as object,
      source_position: 2
    });
    const unavailableCandidate = parseNativeRequest(fixture("request_candidate_unavailable.json"));
    const wrongSnapshotOutcome = parseNativeResponse({ ...fixture("response_candidate_snapshot.json") as object, outcome: "unavailable" });
    const wrongUnavailableOutcome = parseNativeResponse({ ...fixture("response_candidate_unavailable.json") as object, outcome: "recorded" });
    const media = parseNativeRequest(fixture("request_media_end.json"));
    const wrongMedia = parseNativeResponse({
      ...fixture("response_media_end.json") as object,
      position: 2
    });
    const wrongMediaNote = parseNativeResponse({
      ...fixture("response_media_end.json") as object,
      note_id: "note_other"
    });
    const wrongMediaRole = parseNativeResponse({
      ...fixture("response_media_end.json") as object,
      role: "video_cover"
    });

    expect(() => assertResponseForRequest(candidate, wrongCandidate)).toThrow();
    expect(() => assertResponseForRequest(candidate, wrongCandidatePosition)).toThrow();
    expect(() => assertResponseForRequest(candidate, wrongSnapshotOutcome)).toThrow();
    expect(() => assertResponseForRequest(unavailableCandidate, wrongUnavailableOutcome)).toThrow();
    expect(() => assertResponseForRequest(media, wrongMedia)).toThrow();
    expect(() => assertResponseForRequest(media, wrongMediaNote)).toThrow();
    expect(() => assertResponseForRequest(media, wrongMediaRole)).toThrow();
  });

  test("accepts an exact 256 KiB media chunk and rejects one byte over", () => {
    const base = fixture("request_media_chunk.json") as Record<string, unknown>;
    const exact = Buffer.alloc(256 * 1024, 0x61).toString("base64");
    const over = Buffer.alloc(256 * 1024 + 1, 0x61).toString("base64");

    expect(parseNativeRequest({ ...base, data_base64: exact })).toMatchObject({ data_base64: exact });
    expect(() => parseNativeRequest({ ...base, data_base64: over })).toThrow();
  });

  test("accepts a globally monotonic media sequence after the per-note slot boundary", () => {
    const begin = fixture("request_media_begin.json") as Record<string, unknown>;
    const chunk = fixture("request_media_chunk.json") as Record<string, unknown>;
    const end = fixture("request_media_end.json") as Record<string, unknown>;

    expect(parseNativeRequest({ ...begin, sequence: 23 })).toMatchObject({ sequence: 23 });
    expect(parseNativeRequest({ ...chunk, sequence: 23 })).toMatchObject({ sequence: 23 });
    expect(parseNativeRequest({ ...end, sequence: 23 })).toMatchObject({ sequence: 23 });
  });

  test("rejects an explicit default port before URL normalization", () => {
    const begin = fixture("request_begin_job.json") as Record<string, unknown>;

    expect(() => parseNativeRequest({
      ...begin,
      source_page_url: "https://www.xiaohongshu.com:443/explore/source_123"
    })).toThrow();
  });
});

describe("extension retention boundary", () => {
  test("canonicalizes only query-free approved note and profile URLs", () => {
    expect(canonicalizeXhsUrl("https://www.xiaohongshu.com/explore/note_123?x=1")).toBe("https://www.xiaohongshu.com/explore/note_123");
    expect(canonicalizeXhsUrl("https://www.xiaohongshu.com/user/profile/author_123#section")).toBe("https://www.xiaohongshu.com/user/profile/author_123");
    expect(() => canonicalizeXhsUrl("http://www.xiaohongshu.com/explore/note_123")).toThrow();
    expect(() => canonicalizeXhsUrl("https://www.xiaohongshu.com:443/explore/note_123")).toThrow();
    expect(() => canonicalizeXhsUrl("https://user:pass@www.xiaohongshu.com/explore/note_123")).toThrow();
  });

  test("accepts only bounded opaque identifiers and safe retained visible text", () => {
    expect(isSafeId("note_123-A")).toBe(true);
    expect(isSafeId("bad/id")).toBe(false);
    expect(isSafeRetainedText("可见正文")).toBe(true);
    expect(isSafeRetainedText("https://www.xiaohongshu.com/explore/note_123")).toBe(false);
    expect(isSafeRetainedText("token=secret")).toBe(false);
    expect(isSafeRetainedText("https://example.test/?token=secret")).toBe(false);
  });

  test("counts retained text by Unicode code point rather than UTF-16 code unit", () => {
    expect(isSafeRetainedText("😀".repeat(200), 200)).toBe(true);
    expect(isSafeRetainedText("😀".repeat(201), 200)).toBe(false);
  });

  test("accepts only HTTPS xhscdn media URLs without credentials, query, fragment, or unsafe host", () => {
    expect(assertAllowedMediaUrl("https://ci.xhscdn.com/media/image.webp")).toBe("https://ci.xhscdn.com/media/image.webp");
    for (const unsafe of [
      "http://ci.xhscdn.com/media/image.webp",
      "https://xhscdn.com/media/image.webp",
      "https://evil.xhscdn.com.evil.test/media/image.webp",
      "https://user:pass@ci.xhscdn.com/media/image.webp",
      "https://ci.xhscdn.com/media/image.webp?token=secret",
      "https://ci.xhscdn.com/media/image.webp#fragment"
    ]) expect(() => assertAllowedMediaUrl(unsafe)).toThrow();
  });
});
