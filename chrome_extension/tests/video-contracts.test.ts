import { describe, expect, it } from "vitest";
import { assertResponseForRequest, parseNativeRequest, parseNativeResponse } from "../src/contracts.js";
import { videoProcessingCopy } from "../src/video-processing.js";
describe("additive video protocol", () => {
  it("binds status and stop to the original job", () => {
    for (const kind of ["video_status", "video_stop"]) {
      const request = parseNativeRequest({protocol_version:"1.0",kind,job_id:"job1"});
      const response = parseNativeResponse({protocol_version:"1.0",kind:"video_result",job_id:"job1",processing:{status:"running"}});
      expect(assertResponseForRequest(request,response)).toBe(response);
      expect(() => assertResponseForRequest(request,{...response,job_id:"other"})).toThrow();
    }
  });
  it("accepts supplementary text without leaking capabilities", () => {
    expect(parseNativeRequest({protocol_version:"1.0",kind:"finish_job",job_id:"job1",video_metadata:{note_id:"n1",duration_ms:300000,subtitle_status:"available",subtitle_srt:"1\n00:00:00,000 --> 00:00:01,000\n你好\n"}}).kind).toBe("finish_job");
    expect(() => parseNativeRequest({protocol_version:"1.0",kind:"finish_job",job_id:"job1",video_metadata:{note_id:"n1",sourceUrl:"https://sns-video.xhscdn.com/a.mp4?sign=private&t=1"}})).toThrow();
    expect(() => parseNativeRequest({protocol_version:"1.0",kind:"finish_job",job_id:"job1",video_metadata:{note_id:"n1",duration_ms:NaN}})).toThrow();
  });
  it.each([
    ["BOM after leading space", " \uFEFF1\n00:00:00,000 --> 00:00:01,000\n你好\n"],
    ["unnumbered cue", "00:00:00,000 --> 00:00:01,000\n你好\n"],
    ["starts at two", "2\n00:00:00,000 --> 00:00:01,000\n你好\n"],
    ["skips a cue number", "1\n00:00:00,000 --> 00:00:01,000\n你好\n\n3\n00:00:01,000 --> 00:00:02,000\n再见\n"],
    ["reversed time", "1\n00:00:01,000 --> 00:00:00,000\n你好\n"],
    ["invalid minute", "1\n00:60:00,000 --> 00:61:01,000\n你好\n"],
    ["empty cue", "1\n00:00:00,000 --> 00:00:01,000\n  \n"],
    ["sensitive text", "1\n00:00:00,000 --> 00:00:01,000\ntoken=secret\n"],
    ["signed URL text", "1\n00:00:00,000 --> 00:00:01,000\nhttps://ci.xhscdn.com/a?sign=secret\n"]
  ])("rejects Python-incompatible SRT: %s", (_name, subtitle_srt) => {
    expect(() => parseNativeRequest({ protocol_version: "1.0", kind: "finish_job", job_id: "job1", video_metadata: {
      note_id: "n1", subtitle_status: "available", subtitle_srt
    } })).toThrow();
  });
  it("keeps report availability separate from ASR and rejects invented states", () => {
    const base = {protocol_version:"1.0",kind:"job_result",job_id:"job1",status:"complete",retained_count:1,report_available:true,report_file:"index.html"};
    expect(parseNativeResponse({...base,video_processing:{status:"failed",reason:"processing_timeout"}}).status).toBe("complete");
    expect(() => parseNativeResponse({...base,video_processing:{status:"magic"}})).toThrow();
    expect(() => parseNativeResponse({...base,video_processing:{status:"complete",text:"private speech"}})).toThrow();
  });
  it("shows the fifteen-minute transcription limit for skipped videos", () => {
    expect(videoProcessingCopy({status:"skipped_too_long"})).toContain("15 分钟");
  });
});
