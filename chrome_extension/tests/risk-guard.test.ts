import { describe, expect, test } from "vitest";

import {
  DEFAULT_SEARCH_RISK_POLICY,
  canScroll,
  canStart,
  canVisitDetail,
  completeVisit,
  createGuardState,
  halt,
  recordScroll,
  recordVisit
} from "../src/risk/guard.js";

describe("search risk guard", () => {
  test("starts Simple Search only for five or ten requested notes", () => {
    expect(canStart(createGuardState(DEFAULT_SEARCH_RISK_POLICY), 4, 0)).toEqual({ allowed: false, reason: "invalid_requested_count" });
    expect(canStart(createGuardState(DEFAULT_SEARCH_RISK_POLICY), 5, 0)).toEqual({ allowed: true });
    expect(canStart(createGuardState(DEFAULT_SEARCH_RISK_POLICY), 10, 0)).toEqual({ allowed: true });
    expect(canStart(createGuardState(DEFAULT_SEARCH_RISK_POLICY), 11, 0)).toEqual({ allowed: false, reason: "invalid_requested_count" });
  });

  test("allows exactly two source scrolls before refusing another", () => {
    const initial = createGuardState(DEFAULT_SEARCH_RISK_POLICY);
    const once = recordScroll(initial);
    const twice = recordScroll(once);

    expect(canScroll(initial)).toEqual({ allowed: true });
    expect(canScroll(once)).toEqual({ allowed: true });
    expect(canScroll(twice)).toEqual({ allowed: false, reason: "scroll_limit" });
  });

  test("uses one detail worker and caps detail visits at ten", () => {
    let state = createGuardState(DEFAULT_SEARCH_RISK_POLICY);
    for (let visit = 0; visit < 10; visit += 1) {
      expect(canVisitDetail(state, visit * 6_000)).toEqual({ allowed: true });
      state = recordVisit(state, visit * 6_000);
      expect(canVisitDetail(state, visit * 6_000)).toEqual({ allowed: false, reason: "worker_limit" });
      state = completeVisit(state);
    }

    expect(state.activeDetailWorkers).toBe(0);
    expect(canVisitDetail(state, 60_000)).toEqual({ allowed: false, reason: "visit_limit" });
  });

  test("does not allow a second detail visit until the six-second interval has elapsed", () => {
    const state = completeVisit(recordVisit(createGuardState(DEFAULT_SEARCH_RISK_POLICY), 1_000));

    expect(canVisitDetail(state, 6_999)).toEqual({ allowed: false, reason: "pacing" });
    expect(canVisitDetail(state, 7_000)).toEqual({ allowed: true });
  });

  test("refuses a new batch until the sixty-second cooldown has elapsed", () => {
    const state = createGuardState(DEFAULT_SEARCH_RISK_POLICY, { lastBatchStartedAt: 5_000 });

    expect(canStart(state, 5, 64_999)).toEqual({ allowed: false, reason: "cooldown" });
    expect(canStart(state, 5, 65_000)).toEqual({ allowed: true });
  });

  test.each(["stopped", "login_required", "challenge_detected"] as const)("%s is terminal for scroll and detail navigation", (reason) => {
    const stopped = halt(createGuardState(DEFAULT_SEARCH_RISK_POLICY), reason);

    expect(canScroll(stopped)).toEqual({ allowed: false, reason });
    expect(canVisitDetail(stopped, 100_000)).toEqual({ allowed: false, reason });
  });
});
