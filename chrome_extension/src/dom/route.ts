import { assertCanonicalXhsUrl, isSafeId } from "../security.js";

export type PageContext =
  | Readonly<{ kind: "current"; noteId: string; canonicalUrl: string }>
  | Readonly<{ kind: "search"; canonicalUrl: string }>
  | Readonly<{ kind: "account"; profileId: string; canonicalUrl: string }>
  | Readonly<{ kind: "unsupported" }>;

type LocationLike = Pick<Location, "protocol" | "hostname" | "port" | "pathname">;

const notePath = /^\/explore\/([A-Za-z0-9_-]{1,128})$/u;
const profilePath = /^\/user\/profile\/([A-Za-z0-9_-]{1,128})$/u;

function trustedLocation(location: LocationLike): boolean {
  return location.protocol === "https:" && location.hostname === "www.xiaohongshu.com" && location.port === "";
}

export function detectPage(_document: Document, location: LocationLike): PageContext {
  if (!trustedLocation(location)) return { kind: "unsupported" };
  const note = notePath.exec(location.pathname);
  if (note?.[1] !== undefined && isSafeId(note[1])) {
    const canonicalUrl = assertCanonicalXhsUrl(`https://www.xiaohongshu.com${location.pathname}`, "note");
    return { kind: "current", noteId: note[1], canonicalUrl };
  }
  const profile = profilePath.exec(location.pathname);
  if (profile?.[1] !== undefined && isSafeId(profile[1])) {
    const canonicalUrl = assertCanonicalXhsUrl(`https://www.xiaohongshu.com${location.pathname}`, "profile");
    return { kind: "account", profileId: profile[1], canonicalUrl };
  }
  if (location.pathname === "/search_result") {
    return { kind: "search", canonicalUrl: "https://www.xiaohongshu.com/search_result" };
  }
  return { kind: "unsupported" };
}
