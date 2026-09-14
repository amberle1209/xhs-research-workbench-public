import { isVisible } from "./visibility.js";

export type BlockPageReason = "login_required" | "challenge_detected" | undefined;

const challengeUi = ".captcha, [data-captcha], .challenge-container, [data-challenge], .security-check";
const loginUi = ".login-container, [data-login], .login-modal, [data-auth='login'], #login-btn";

function hasVisibleUi(document: Document, selector: string): boolean {
  return Array.from(document.querySelectorAll(selector)).some(isVisible);
}

export function detectBlockPage(document: Document): BlockPageReason {
  if (hasVisibleUi(document, challengeUi) ||
    Array.from(document.querySelectorAll("iframe[src*='captcha']")).some(isVisible)) {
    return "challenge_detected";
  }
  if (hasVisibleUi(document, loginUi)) return "login_required";
  return undefined;
}
