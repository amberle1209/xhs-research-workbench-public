import { JSDOM } from "jsdom";
import { describe, expect, test } from "vitest";

import { readBoundVideoState } from "../src/dom/video-state.js";
import { fetchBoundMedia, fetchBoundSubtitle, observeBoundMedia } from "../src/dom/media.js";
import { assertEphemeralSubtitleUrl, assertEphemeralVideoUrl, assertAllowedMediaUrl } from "../src/security.js";

const noteId = "note_123";
const authorId = "author_123";
const streamUrl = "http://sns-video-v6.xhscdn.com/stream/a/movie.mp4?sign=abc123&t=123456";
const subtitleUrl = "https://sns-subtitle-s2.xhscdn.com/subtitle/a/caption.srt?sign=abc123&t=123456";

function state(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const subtitles = { source: [{ url: subtitleUrl, language: "zh-CN", format: 0, type: 0 }] };
  const video = { media: { stream: { h264: [{ videoCodec: "h264", audioCodec: "aac", format: "mp4", width: 720, height: 960, size: 12_000_000, duration: 135867, masterUrl: streamUrl, backupUrls: [] }] } }, mediaV2: JSON.stringify({ video: { subtitles } }) };
  return { note: { noteDetailMap: { [noteId]: { note: { noteId, user: { userId: authorId }, type: "video", video, ...overrides } } } } };
}

function documentFor(value: string, poster = ""): Document {
  const dom = new JSDOM(`<section id="detail"><div class="media-container"><video ${poster ? `poster="${poster}"` : ""}><source src="blob:https://www.xiaohongshu.com/opaque"></video></div></section><script>${value}</script>`, { url: "https://www.xiaohongshu.com/explore/note_123" });
  Object.defineProperty(dom.window.HTMLElement.prototype, "getClientRects", { configurable: true, value: () => [{ width: 1, height: 1 }] });
  return dom.window.document;
}

function initial(value: unknown): string { return `window.__INITIAL_STATE__=${JSON.stringify(value)};`; }

describe("identity-bound page video state", () => {
  test("extracts a complete matching video and independent SRT despite a blob player and absent poster", () => {
    const document = documentFor(initial(state()));
    expect(readBoundVideoState(document, { noteId, authorId })).toEqual({
      sourceUrl: "https://sns-video-v6.xhscdn.com/stream/a/movie.mp4?sign=abc123&t=123456",
      durationMs: 135867,
      expectedSizeBytes: 12000000,
      subtitle: { sourceUrl: subtitleUrl, sourceKind: "independent_srt" }
    });
    const observed = observeBoundMedia(document.querySelector("#detail")!, { noteId, authorId });
    expect(observed.sources).toEqual([{ note_id: noteId, role: "video", position: 1, expectedSizeBytes:12000000, sourceUrl: "https://sns-video-v6.xhscdn.com/stream/a/movie.mp4?sign=abc123&t=123456" }]);
    expect(observed.durationMs).toBe(135867);
    expect(observed.subtitle).toEqual({ sourceUrl: subtitleUrl, sourceKind: "independent_srt" });
  });

  test("keeps a safe poster optional and does not mistake a blob poster for a source", () => {
    const safe = documentFor(initial(state()), "https://ci.xhscdn.com/cover.webp");
    const blob = documentFor(initial(state()), "blob:https://www.xiaohongshu.com/cover");
    expect(observeBoundMedia(safe.querySelector("#detail")!, { noteId, authorId }).sources.map(x => x.role)).toEqual(["video_cover", "video"]);
    expect(observeBoundMedia(blob.querySelector("#detail")!, { noteId, authorId }).sources.map(x => x.role)).toEqual(["video"]);
  });

  test("reads exact-note declared EF5 MP4 from a hydrated blob player despite an unrelated empty Map literal", () => {
    const live = state() as any;
    live.unrelated = null;
    live.note.noteDetailMap[noteId].note.video.media.stream = {
      EF5: [
        { videoCodec: "EF5", format: "mp4", size: 10_451_341, width: 1138, height: 720, duration: 204499, masterUrl: streamUrl },
        { videoCodec: "EF5", format: "mp4", size: 16_641_333, width: 1706, height: 1080, duration: 204499, masterUrl: "https://sns-video-v4.xhscdn.com/stream/a/larger.mp4?sign=abc123&t=123456" }
      ],
      EF7: [], EF4: [{ videoCodec: "EF4", format: "mp4", size: 7_083_130, width: 720, height: 480, duration: 204499, masterUrl: streamUrl }], EF6: []
    };
    const script = initial(live).replace('"unrelated":null', '"unrelated":new Map([])');
    const document = documentFor(script, "https://ci.xhscdn.com/cover.webp");
    const observed = observeBoundMedia(document.querySelector("#detail")!, { noteId, authorId });
    expect(observed.sources).toEqual([
      { note_id: noteId, role: "video_cover", position: 1, sourceUrl: "https://ci.xhscdn.com/cover.webp" },
      { note_id: noteId, role: "video", position: 1, expectedSizeBytes: 16_641_333, sourceUrl: "https://sns-video-v4.xhscdn.com/stream/a/larger.mp4?sign=abc123&t=123456" }
    ]);
    expect(observed.durationMs).toBe(204499);
  });

  test("keeps a bound cover when malformed page state cannot authorize a blob video", () => {
    const document = documentFor('window.__INITIAL_STATE__={"unrelated":new Map([["x",1]])};', "https://ci.xhscdn.com/cover.webp");
    const observed = observeBoundMedia(document.querySelector("#detail")!, { noteId, authorId });
    expect(observed.slots.map(slot => slot.role)).toEqual(["video_cover", "video"]);
    expect(observed.sources).toEqual([{ note_id: noteId, role: "video_cover", position: 1, sourceUrl: "https://ci.xhscdn.com/cover.webp" }]);
  });

  test("does not execute or accept nonempty Maps, quoted code, or trailing statements", () => {
    const live = state() as any;
    live.unrelated = null;
    const serialized = initial(live).replace('"unrelated":null', '"unrelated":new Map([["x",1]])');
    expect(() => readBoundVideoState(documentFor(serialized), { noteId, authorId })).toThrow();
    const quoted = initial({ ...state(), unrelated: "new Map([])" });
    expect(readBoundVideoState(documentFor(quoted), { noteId, authorId })?.sourceUrl).toBe("https://sns-video-v6.xhscdn.com/stream/a/movie.mp4?sign=abc123&t=123456");
    const trailing = initial(live).replace('"unrelated":null', '"unrelated":new Map([]),"run":alert(1)');
    expect(() => readBoundVideoState(documentFor(trailing), { noteId, authorId })).toThrow();
  });

  test("keeps h264 preference and rejects unknown or mismatched opaque stream labels", () => {
    const mixed = state() as any;
    mixed.note.noteDetailMap[noteId].note.video.media.stream.EF5 = [{
      videoCodec: "EF5", format: "mp4", size: 20_000_000, width: 1706, height: 1080,
      masterUrl: "https://sns-video-v4.xhscdn.com/stream/a/opaque.mp4?sign=abc123&t=123456"
    }];
    expect(readBoundVideoState(documentFor(initial(mixed)), { noteId, authorId })?.sourceUrl).toBe("https://sns-video-v6.xhscdn.com/stream/a/movie.mp4?sign=abc123&t=123456");

    const opaqueOnly = state() as any;
    opaqueOnly.note.noteDetailMap[noteId].note.video.media.stream = {
      EF5: [{ videoCodec: "hevc", format: "mp4", size: 20_000_000, width: 1706, height: 1080, masterUrl: streamUrl }],
      EF8: [{ videoCodec: "EF8", format: "mp4", size: 20_000_000, width: 1706, height: 1080, masterUrl: streamUrl }],
      h265: [{ videoCodec: "hevc", format: "mp4", size: 20_000_000, width: 1706, height: 1080, masterUrl: streamUrl }]
    };
    expect(readBoundVideoState(documentFor(initial(opaqueOnly)), { noteId, authorId })?.sourceUrl).toBeUndefined();
  });

  test("does not use an opaque stream to bypass an oversized legacy h264 declaration", () => {
    const mixed = state() as any;
    const stream = mixed.note.noteDetailMap[noteId].note.video.media.stream;
    stream.h264 = Array.from({ length: 17 }, () => stream.h264[0]);
    stream.EF5 = [{ videoCodec: "EF5", format: "mp4", size: 20_000_000, width: 1706, height: 1080, masterUrl: streamUrl }];
    expect(readBoundVideoState(documentFor(initial(mixed)), { noteId, authorId })).toBeUndefined();
  });

  test("refuses a mismatched map entry, note identity, or visible author", () => {
    const mismatchedMap = state();
    (mismatchedMap.note as any).noteDetailMap = { other_note: (mismatchedMap.note as any).noteDetailMap[noteId] };
    expect(() => readBoundVideoState(documentFor(initial(mismatchedMap)), { noteId, authorId })).toThrow();
    expect(() => readBoundVideoState(documentFor(initial(state({ noteId: "other_note" }))), { noteId, authorId })).toThrow();
    expect(() => readBoundVideoState(documentFor(initial(state())), { noteId, authorId: "other_author" })).toThrow();
  });

  test("rejects duplicate, malformed, deep, and oversized state rather than evaluating it", () => {
    const duplicate = documentFor(initial(state()) + `</script><script>${initial(state())}`);
    expect(() => readBoundVideoState(duplicate, { noteId, authorId })).toThrow();
    expect(() => readBoundVideoState(documentFor("window.__INITIAL_STATE__={bad:1};"), { noteId, authorId })).toThrow();
    let deep: unknown = state(); for (let i = 0; i < 80; i++) deep = { next: deep };
    expect(() => readBoundVideoState(documentFor(initial(deep)), { noteId, authorId })).toThrow();
    expect(() => readBoundVideoState(documentFor(`window.__INITIAL_STATE__={"x":"${"a".repeat(600_000)}"};`), { noteId, authorId })).toThrow();
  });

  test("replaces only unquoted undefined values and rejects malicious statements", () => {
    const value = initial(state()).replace('"type":"video"', '"type":"video", "unused_text":"undefined", "unused":undefined');
    expect(readBoundVideoState(documentFor(value), { noteId, authorId })?.durationMs).toBe(135867);
    expect(() => readBoundVideoState(documentFor(`${initial(state())}alert(1)`), { noteId, authorId })).toThrow();
  });

  test("selects a fitting complete stream deterministically without crossing identity", () => {
    const s = state(); const streams = (s.note as any).noteDetailMap[noteId].note.video.media.stream.h264;
    streams.unshift({ ...streams[0], size: 101 * 1024 * 1024, masterUrl: "https://sns-video-v6.xhscdn.com/stream/a/too-large.mp4" });
    streams.push({ ...streams[1], size: 10_000_000, masterUrl: "https://sns-video-v6.xhscdn.com/stream/a/small.mp4" });
    expect(readBoundVideoState(documentFor(initial(s)), { noteId, authorId })?.sourceUrl).toContain("movie.mp4");
  });

  test("preserves declared duration when the exact stream is over cap, and downloads a source without declared duration or audio", () => {
    const tooLarge = state();
    const item = (tooLarge.note as any).noteDetailMap[noteId].note.video.media.stream.h264[0];
    item.size = 101 * 1024 * 1024;
    expect(readBoundVideoState(documentFor(initial(tooLarge)), { noteId, authorId })).toMatchObject({ durationMs: 135867 });
    const observed = observeBoundMedia(documentFor(initial(tooLarge)).querySelector("#detail")!, { noteId, authorId });
    expect(observed.durationMs).toBe(135867);
    expect(observed.sources).toEqual([]);
    expect(observed.unavailableReason).toBe("size_limit");

    const unknown = state();
    const unknownItem = (unknown.note as any).noteDetailMap[noteId].note.video.media.stream.h264[0];
    delete unknownItem.duration;
    delete unknownItem.audioCodec;
    const result = readBoundVideoState(documentFor(initial(unknown)), { noteId, authorId });
    expect(result?.sourceUrl).toContain("movie.mp4");
    expect(result?.durationMs).toBeUndefined();
  });
});

describe("ephemeral media capability and bounded SRT", () => {
  test("accepts the observed seven-key signed video profile without exposing broader query forms", () => {
    const base = "https://sns-video-v4.xhscdn.com/stream/a/movie.mp4";
    const values = {
      b: "b".repeat(18), csig: "c".repeat(16), oi: "o".repeat(48), ou: "u".repeat(27),
      sign: "s".repeat(32), t: "12345678", trid: "r".repeat(32)
    };
    const query = Object.entries(values).map(([key, value]) => `${key}=${value}`).join("&");
    expect(assertEphemeralVideoUrl(`${base}?${query}`)).toBe(`${base}?${query}`);
    for (const invalid of [
      `${base}?`,
      `${base}?${query}&token=secret`,
      `${base}?${query}&sign=duplicate`,
      `${base}?${query.replace(`oi=${values.oi}`, `oi=${"o".repeat(129)}`)}`,
      `${base}?${query.replace(`sign=${values.sign}`, "sign=%73ecret")}`,
      `${base}?${query.replace(`b=${values.b}`, "b=hello+world")}`,
      `${base}?${query.replace(`sign=${values.sign}&t=${values.t}&`, "")}`
    ]) expect(() => assertEphemeralVideoUrl(invalid)).toThrow();
    expect(() => assertEphemeralSubtitleUrl(`https://sns-subtitle-s2.xhscdn.com/subtitle/a/caption.srt?${query}`)).toThrow();
  });

  test("keeps retained URLs strict while accepting only narrow signed CDN capabilities", () => {
    expect(() => assertAllowedMediaUrl(streamUrl)).toThrow();
    expect(assertEphemeralVideoUrl(streamUrl)).toBe("https://sns-video-v6.xhscdn.com/stream/a/movie.mp4?sign=abc123&t=123456");
    expect(assertEphemeralSubtitleUrl(subtitleUrl)).toBe(subtitleUrl);
    for (const url of [
      "https://user:pass@sns-video-v6.xhscdn.com/stream/a/movie.mp4?sign=x&t=1",
      "https://sns-video-v6.xhscdn.com/stream/a/movie.mp4?sign=x&t=1&token=secret",
      "https://sns-video-v6.xhscdn.com/other/a/movie.mp4?sign=x&t=1",
      "https://evil.xhscdn.com.evil.test/stream/a/movie.mp4?sign=x&t=1"
    ]) expect(() => assertEphemeralVideoUrl(url)).toThrow();
  });

  test("fetches signed video as binary and validates HTML responses without decoding full MP4", async () => {
    const bytes = new Uint8Array([0, 0, 0, 24, 102, 116, 121, 112, 0, 99, 97, 112, 116, 99, 104, 97]);
    const result = await fetchBoundMedia(streamUrl, "video", async () => new Response(bytes, { headers: { "content-type": "video/mp4" } }));
    expect(result.sizeBytes).toBe(bytes.length);
    await expect(fetchBoundMedia(streamUrl, "video", async () => new Response("请完成安全验证", { headers: { "content-type": "text/html" } }))).rejects.toMatchObject({ code: "challenge_detected" });
  });

  test("normalizes generic binary video MIME only from a matching container header", async () => {
    const mp4 = new Uint8Array([0,0,0,28,102,116,121,112,105,115,111,109,0,0,2,0,105,115,111,109,105,115,111,50,109,112,52,49,0,0,0,8]);
    const webm = new Uint8Array([0x1a,0x45,0xdf,0xa3,0x9f,0x42,0x86,0x81]);
    for (const generic of ["application/octet-stream", "binary/octet-stream"]) {
      const result = await fetchBoundMedia(streamUrl, "video", async () => new Response(mp4, { headers: { "content-type": generic } }));
      expect(result.mimeType).toBe("video/mp4");
      expect(result.sizeBytes).toBe(mp4.length);
    }
    expect((await fetchBoundMedia(streamUrl, "video", async () => new Response(webm, { headers: { "content-type": "application/octet-stream" } }))).mimeType).toBe("video/webm");
    await expect(fetchBoundMedia(streamUrl, "video", async () => new Response(new Uint8Array([0,0,0,8,102,116,121,112,0,0,0,0,0,0,0,0]), { headers: { "content-type": "application/octet-stream" } }))).rejects.toMatchObject({ code: "mime_mismatch" });
    await expect(fetchBoundMedia(streamUrl, "video", async () => new Response(mp4, { headers: { "content-type": "text/html" } }))).rejects.toMatchObject({ code: "mime_mismatch" });
    await expect(fetchBoundMedia("https://ci.xhscdn.com/image.webp", "image", async () => new Response(mp4, { headers: { "content-type": "application/octet-stream" } }))).rejects.toMatchObject({ code: "mime_mismatch" });
  });

  test("rejects truncated bytes even when the MP4 header and MIME appear valid", async () => {
    const bytes = new Uint8Array([0,0,0,24,102,116,121,112]);
    await expect(fetchBoundMedia(streamUrl,"video",async () => new Response(bytes,{headers:{"content-type":"video/mp4","content-length":"24"}}))).rejects.toMatchObject({code:"download_failed"});
  });

  test("downloads valid UTF-8 SRT under 512 KiB and rejects redirects, invalid UTF-8 and oversized input", async () => {
    const srt = "1\n00:00:00,000 --> 00:00:01,000\n你好\n";
    expect(await fetchBoundSubtitle(subtitleUrl, async () => new Response(srt, { headers: { "content-type": "application/x-subrip" } }))).toEqual({ text: srt });
    await expect(fetchBoundSubtitle(subtitleUrl, async () => new Response(new Uint8Array([0xff]), { headers: { "content-type": "application/x-subrip" } }))).rejects.toThrow();
    await expect(fetchBoundSubtitle(subtitleUrl, async () => new Response("x".repeat(512 * 1024 + 1), { headers: { "content-type": "application/x-subrip" } }))).rejects.toThrow();
    await expect(fetchBoundSubtitle(subtitleUrl, async () => new Response("moved", { status: 302 }))).rejects.toThrow();
    for (const invalid of [
      "00:00:00,000 --> 00:00:01,000\n你好\n",
      "2\n00:00:00,000 --> 00:00:01,000\n你好\n"
    ]) await expect(fetchBoundSubtitle(subtitleUrl, async () => new Response(invalid, { headers: { "content-type": "text/plain" } }))).rejects.toThrow();
  });
});
