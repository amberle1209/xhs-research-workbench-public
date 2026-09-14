import { describe, expect, test } from "vitest";
import { publishedTimestamp } from "../src/dom/published-date.js";

const now = new Date("2026-09-14T00:30:00+08:00");
describe("visible publication date normalization", () => {
  test.each([
    ["05-26", "2026-05-26"], ["2025-12-31", "2025-12-31"],
    ["发布于 2026-08-01 10:00", "2026-08-01"],
    ["编辑于 08-12 广东", "2026-08-12"], ["昨天 23:45 北京", "2026-09-13"],
    ["今天 00:10", "2026-09-14"], ["2小时前", "2026-09-13"],
    ["40分钟前", "2026-09-13"], ["刚刚", "2026-09-14"],
  ])("normalizes %s", (raw, day) => {
    expect(publishedTimestamp(raw, now)?.slice(0, 10)).toBe(day);
  });
  test("handles previous year and China-local midnight independently of machine timezone", () => {
    expect(publishedTimestamp("12-31", new Date("2026-01-01T00:10:00+08:00"))).toBe("2025-12-31T00:00:00+08:00");
    expect(publishedTimestamp("今天", new Date("2026-09-13T17:00:00Z"))).toBe("2026-09-14T00:00:00+08:00");
  });
  test.each(["02-30", "2025-02-29", "2026-13-01", "时间未知", "", "2026-08-01 25:90"]) ("does not invent a date for %s", (raw) => {
    expect(publishedTimestamp(raw, now)).toBeUndefined();
  });
  test("preserves explicit clock and valid leap day", () => {
    expect(publishedTimestamp("发布于 2026-08-01 10:00", now)).toBe("2026-08-01T10:00:00+08:00");
    expect(publishedTimestamp("2024-02-29", now)).toBe("2024-02-29T00:00:00+08:00");
  });
});
