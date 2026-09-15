import { configureStore } from '@reduxjs/toolkit';
import { setupListeners } from '@reduxjs/toolkit/query';
import { baseApi, rehydrateApiCache } from './api/baseApi';
import { loadPersistedApiCache, startApiCachePersistence } from './persist';

// Importing the domain files registers their endpoints on `baseApi`.
import './api/membersApi';
import './api/projectsApi';
import './api/teamsApi';
import './api/timeTrackingApi';
import './api/feedbackApi';

export const store = configureStore({
  reducer: {
    [baseApi.reducerPath]: baseApi.reducer,
  },
  middleware: (getDefaultMiddleware) => getDefaultMiddleware().concat(baseApi.middleware),
});

// Seed the cache from the previous visit before the first render, then keep
// mirroring it. This is what makes a browser refresh paint data immediately
// while the background revalidation runs.
store.dispatch(rehydrateApiCache(loadPersistedApiCache()));
startApiCachePersistence(store);

// `refetchOnFocus` / `refetchOnReconnect` on the API slice do nothing until
// RTK Query is told to listen for the browser's focus and online events. This
// was never called, so a dashboard left open showed the numbers it had when
// it last mounted -- a timer stopped on the desktop only appeared after a
// manual reload. With the listeners installed, returning to the tab or
// regaining the network revalidates every stale query in the background.
setupListeners(store.dispatch);

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
