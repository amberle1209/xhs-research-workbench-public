export type SearchRiskPolicy = Readonly<{
  allowedRequestedCounts: readonly number[];
  maxScrollRounds: number;
  maxDetailWorkers: number;
  maxDetailVisits: number;
  minimumDetailIntervalMs: number;
  batchCooldownMs: number;
}>;

export const DEFAULT_SEARCH_RISK_POLICY: SearchRiskPolicy = Object.freeze({
  allowedRequestedCounts: Object.freeze([5, 10]),
  maxScrollRounds: 2,
  maxDetailWorkers: 1,
  maxDetailVisits: 10,
  minimumDetailIntervalMs: 6_000,
  batchCooldownMs: 60_000
});
