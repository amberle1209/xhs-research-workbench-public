import { DEFAULT_SEARCH_RISK_POLICY, type SearchRiskPolicy } from "./policy.js";

export { DEFAULT_SEARCH_RISK_POLICY, type SearchRiskPolicy } from "./policy.js";

export type GuardHaltReason = "stopped" | "login_required" | "challenge_detected";
export type GuardRejectionReason = GuardHaltReason | "invalid_requested_count" | "cooldown" | "scroll_limit" | "worker_limit" | "visit_limit" | "pacing";
export type GuardDecision = Readonly<{ allowed: true }> | Readonly<{ allowed: false; reason: GuardRejectionReason }>;

export type GuardState = Readonly<{
  policy: SearchRiskPolicy;
  scrollRounds: number;
  detailVisits: number;
  activeDetailWorkers: number;
  lastVisitAt: number | undefined;
  lastBatchStartedAt: number | undefined;
  haltReason: GuardHaltReason | undefined;
}>;

export function createGuardState(policy: SearchRiskPolicy = DEFAULT_SEARCH_RISK_POLICY, previous: Readonly<{ lastBatchStartedAt?: number }> = {}): GuardState {
  return {
    policy,
    scrollRounds: 0,
    detailVisits: 0,
    activeDetailWorkers: 0,
    lastVisitAt: undefined,
    lastBatchStartedAt: previous.lastBatchStartedAt,
    haltReason: undefined
  };
}

function terminal(state: GuardState): GuardDecision | undefined {
  return state.haltReason === undefined ? undefined : { allowed: false, reason: state.haltReason };
}

export function canStart(state: GuardState, requestedCount: number, nowMs: number): GuardDecision {
  const stopped = terminal(state);
  if (stopped !== undefined) return stopped;
  if (!state.policy.allowedRequestedCounts.includes(requestedCount)) return { allowed: false, reason: "invalid_requested_count" };
  if (state.lastBatchStartedAt !== undefined && nowMs - state.lastBatchStartedAt < state.policy.batchCooldownMs) return { allowed: false, reason: "cooldown" };
  return { allowed: true };
}

export function canScroll(state: GuardState): GuardDecision {
  const stopped = terminal(state);
  if (stopped !== undefined) return stopped;
  if (state.scrollRounds >= state.policy.maxScrollRounds) return { allowed: false, reason: "scroll_limit" };
  return { allowed: true };
}

export function canVisitDetail(state: GuardState, nowMs: number): GuardDecision {
  const stopped = terminal(state);
  if (stopped !== undefined) return stopped;
  if (state.activeDetailWorkers >= state.policy.maxDetailWorkers) return { allowed: false, reason: "worker_limit" };
  if (state.detailVisits >= state.policy.maxDetailVisits) return { allowed: false, reason: "visit_limit" };
  if (state.lastVisitAt !== undefined && nowMs - state.lastVisitAt < state.policy.minimumDetailIntervalMs) return { allowed: false, reason: "pacing" };
  return { allowed: true };
}

export function recordScroll(state: GuardState): GuardState {
  return { ...state, scrollRounds: state.scrollRounds + 1 };
}

export function recordVisit(state: GuardState, nowMs: number): GuardState {
  return { ...state, detailVisits: state.detailVisits + 1, activeDetailWorkers: state.activeDetailWorkers + 1, lastVisitAt: nowMs };
}

export function completeVisit(state: GuardState): GuardState {
  return { ...state, activeDetailWorkers: Math.max(0, state.activeDetailWorkers - 1) };
}

export function halt(state: GuardState, reason: GuardHaltReason): GuardState {
  return { ...state, haltReason: reason };
}
