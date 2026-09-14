import policy from "../policy.json" with { type: "json" };

export const extensionPolicy = policy;

const safeId = /^[A-Za-z0-9_-]{1,128}$/;
const sensitiveText = /(?<![a-z0-9])(?:access[\s_-]*token|refresh[\s_-]*token|authorization|cookies?|sign(?:ature)?|token|web[\s_-]*session|xsec(?:[\s_-]*token)?|a1)(?![a-z0-9])\s*[:=]|\bauthorization\s+bearer\s+\S+/iu;
const queryBearingUrl = /https?:\/\/[^\s<>"']*[?#]/iu;
const urlLikeText = /(?:[a-z][a-z0-9+.-]*:\/\/|\/\/)/iu;
const xhsObjectPath = /^\/(?:explore|user\/profile)\/([A-Za-z0-9_-]{1,128})$/u;

function codePointLength(value: string): number {
  return Array.from(value).length;
}

export function isSafeId(value: unknown): value is string {
  return typeof value === "string" && safeId.test(value);
}

export function isSafeRetainedText(value: unknown, maximum = 20_000): value is string {
  return (
    typeof value === "string" &&
    codePointLength(value) <= maximum &&
    value.normalize("NFC") === value &&
    !sensitiveText.test(value) &&
    !queryBearingUrl.test(value) &&
    !urlLikeText.test(value)
  );
}

export function isSafeUrlValue(value: unknown, maximum = 20_000): value is string {
  return (
    typeof value === "string" &&
    codePointLength(value) <= maximum &&
    value.normalize("NFC") === value &&
    !sensitiveText.test(value) &&
    !queryBearingUrl.test(value)
  );
}

/** Mirrors the native VideoMetadata SRT boundary before optional text reaches finish_job. */
export function isValidBoundSrt(value: unknown): value is string {
  if (typeof value !== "string" || value.includes("\0") || value.includes("\uFEFF") ||
    new TextEncoder().encode(value).byteLength > 512 * 1024 ||
    sensitiveText.test(value) || queryBearingUrl.test(value)) return false;
  const blocks = value.trim().split(/\r?\n\r?\n/u);
  if (blocks.length === 0 || blocks.length > 10_000) return false;
  const timestamp = /^([0-9]{2}):([0-5][0-9]):([0-5][0-9]),([0-9]{3}) --> ([0-9]{2}):([0-5][0-9]):([0-5][0-9]),([0-9]{3})$/u;
  return blocks.every((block, index) => {
    const lines = block.split(/\r\n|[\n\r\v\f\u001c-\u001e\u0085\u2028\u2029]/u);
    if (lines.length < 3 || lines[0] !== String(index + 1) || lines[1] === undefined ||
      !lines.slice(2).some((line) => line.trim().length > 0)) return false;
    const match = timestamp.exec(lines[1]);
    if (match === null || match[0] !== lines[1]) return false;
    return match.slice(1, 5).join("") < match.slice(5, 9).join("");
  });
}

export function boundedText(value: unknown, maximum: number, field: string): string {
  if (!isSafeRetainedText(value, maximum)) throw new TypeError(`${field} is outside the retained-text boundary`);
  return value;
}

export function boundedStringList(value: unknown, maximumItems: number, maximumText: number, field: string): string[] {
  if (!Array.isArray(value) || value.length > maximumItems) throw new TypeError(`${field} is not a bounded list`);
  return value.map((item) => boundedText(item, maximumText, field));
}

export function canonicalizeXhsUrl(value: unknown): string {
  if (typeof value !== "string") throw new TypeError("XHS URL must be a string");
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new TypeError("XHS URL is invalid");
  }
  const authority = value.slice("https://".length).split(/[/?#]/u)[0];
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname !== "www.xiaohongshu.com" ||
    authority !== "www.xiaohongshu.com" ||
    parsed.port ||
    parsed.username ||
    parsed.password
  ) throw new TypeError("XHS URL is not approved");
  const match = xhsObjectPath.exec(parsed.pathname);
  if (!match) throw new TypeError("XHS object path is not approved");
  return `https://www.xiaohongshu.com${parsed.pathname}`;
}

export function assertCanonicalXhsUrl(value: unknown, expected: "note" | "profile"): string {
  if (typeof value !== "string" || value.includes("?") || value.includes("#")) throw new TypeError("canonical URL contains query or fragment");
  const canonical = canonicalizeXhsUrl(value);
  const expectedPrefix = expected === "note" ? "/explore/" : "/user/profile/";
  if (!canonical.startsWith(`https://www.xiaohongshu.com${expectedPrefix}`) || canonical !== value) {
    throw new TypeError("canonical URL does not bind the declared object");
  }
  return canonical;
}

export function assertAllowedMediaUrl(value: unknown): string {
  if (typeof value !== "string" || value.includes("?") || value.includes("#")) throw new TypeError("media URL contains query or fragment");
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new TypeError("media URL is invalid");
  }
  if (
    parsed.protocol !== "https:" ||
    !parsed.hostname.endsWith(".xhscdn.com") ||
    parsed.hostname === "xhscdn.com" ||
    parsed.port ||
    parsed.username ||
    parsed.password ||
    parsed.toString() !== value
  ) throw new TypeError("media URL is outside the HTTPS xhscdn allowlist");
  return value;
}

/** Download-only CDN capability. Never place its signed query in retained records. */
function approvedEphemeralQuery(search: string, video: boolean): boolean {
  const limits = new Map([
    ["b", 64], ["csig", 64], ["oi", 128], ["ou", 128],
    ["sign", 256], ["t", 32], ["trid", 128]
  ]);
  const parts = search.slice(1).split("&");
  if (parts.length !== 2 && !(video && parts.length === 7)) return false;
  const seen = new Set<string>();
  for (const part of parts) {
    const match = /^([a-z]+)=([A-Za-z0-9_-]+)$/u.exec(part);
    const key = match?.[1];
    const content = match?.[2];
    if (key === undefined || content === undefined || seen.has(key)) return false;
    const maximum = limits.get(key);
    if (maximum === undefined || content.length > maximum) return false;
    seen.add(key);
  }
  if (parts.length === 2) return seen.has("sign") && seen.has("t");
  return ["b", "csig", "oi", "ou", "sign", "t", "trid"].every((key) => seen.has(key));
}

function ephemeralCdnUrl(value: unknown, path: RegExp, allowHttpUpgrade: boolean, video = false): string {
  if (typeof value !== "string" || value.length > 2048 || value.includes("#")) throw new TypeError("ephemeral media URL is invalid");
  let parsed: URL;
  try { parsed = new URL(value); } catch { throw new TypeError("ephemeral media URL is invalid"); }
  if (
    (parsed.protocol !== "https:" && !(allowHttpUpgrade && parsed.protocol === "http:")) ||
    !parsed.hostname.endsWith(".xhscdn.com") || parsed.hostname === "xhscdn.com" ||
    parsed.port || parsed.username || parsed.password || !path.test(parsed.pathname) ||
    parsed.toString() !== value
  ) throw new TypeError("ephemeral media URL is outside the CDN boundary");
  if (value.includes("?") && (!parsed.search || !approvedEphemeralQuery(parsed.search, video))) throw new TypeError("ephemeral media URL has an unapproved query");
  if (parsed.protocol === "http:") parsed.protocol = "https:";
  return parsed.toString();
}

export function assertEphemeralVideoUrl(value: unknown): string {
  return ephemeralCdnUrl(value, /^\/stream\/[A-Za-z0-9_!./-]+\.mp4$/u, true, true);
}

export function assertEphemeralSubtitleUrl(value: unknown): string {
  return ephemeralCdnUrl(value, /^\/subtitle\/[A-Za-z0-9_!./-]+\.srt$/u, false);
}
