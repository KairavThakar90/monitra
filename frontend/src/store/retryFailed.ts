import { baseApi } from "./api/baseApi";

type QueryEntry = { status?: string; endpointName?: string; originalArgs?: unknown };
type Endpoint = { initiate: (arg: unknown, options: Record<string, unknown>) => unknown };

/**
 * Ask again, once, for every query that is currently in the failed state.
 *
 * A screen's "Retry" does not need to know which hook it is showing: whatever
 * failed is re-sent, whatever succeeded is left alone. It is a one-shot
 * (`subscribe: false`, forced), never a loop, so pressing it cannot produce a
 * request storm.
 */
export const retryFailedQueries = (
  dispatch: (action: unknown) => unknown,
  getState: () => {
    api?: {
      queries?: Record<string, QueryEntry | undefined>;
      subscriptions?: Record<string, Record<string, unknown> | undefined>;
    };
  },
): number => {
  const state = getState().api;
  const queries = state?.queries ?? {};
  const subscriptions = state?.subscriptions ?? {};
  let retried = 0;
  for (const [cacheKey, entry] of Object.entries(queries)) {
    // Only what a mounted screen is showing; a failure the user has navigated
    // away from is not worth a request.
    if (Object.keys(subscriptions[cacheKey] ?? {}).length === 0) continue;
    if (!entry || entry.status !== "rejected" || !entry.endpointName) continue;
    const endpoint = (baseApi.endpoints as unknown as Record<string, Endpoint | undefined>)[entry.endpointName];
    if (!endpoint) continue;
    dispatch(endpoint.initiate(entry.originalArgs, { forceRefetch: true, subscribe: false }));
    retried += 1;
  }
  return retried;
};
