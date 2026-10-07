import React, { useCallback, useEffect, useRef, useState } from "react";

/**
 * An `<img>` for a backend route that requires the bearer token.
 *
 * Screenshot bytes are proxied by `/time-entry-screenshots/{id}/view`, which is
 * authorised per request — a plain `<img src>` sends no `Authorization` header,
 * so the browser would render a broken tile for every capture. The bytes are
 * fetched here instead and handed to the tag as an object URL, which is revoked
 * when the tile unmounts so a long scroll does not leak one blob per image.
 *
 * A failure is never reported as one thing. This used to print "Image
 * unavailable" for *every* rejected fetch and throw the status away, so a card
 * whose image was permanently gone from storage, one whose request merely timed
 * out under the load of a whole day's thumbnails, and one the viewer was not
 * signed in to see were the same sentence — and nobody could tell which were
 * worth a retry and which were data loss. Now:
 *
 *   transient  (network, timeout, 408/425/429/5xx)  retried with backoff, then
 *                                                   offered a Retry button
 *   missing    (404/410)                            the record exists, the file
 *                                                   does not: an integrity failure
 *   signed_out (401)                                sign in again
 *   forbidden  (403)                                not permitted
 *   corrupt    (bytes arrive, the browser cannot decode them)
 *
 * The state and the HTTP status are on the tile (`data-image-state`,
 * `data-http-status`, `title`), so a screenshot of the page says what happened.
 */

export type ImageFailureKind = "transient" | "missing" | "signed_out" | "forbidden" | "corrupt";

export interface ImageFailure {
  kind: ImageFailureKind;
  /** The HTTP status, when the server answered at all. */
  status?: number;
}

/** What each failure says on the tile. Exported so the wording is tested once. */
export const IMAGE_FAILURE_TEXT: Record<ImageFailureKind, { label: string; detail: string }> = {
  transient: { label: "Couldn’t load image", detail: "Storage did not answer in time" },
  missing: { label: "Image missing from storage", detail: "The capture is recorded but its image was not found" },
  signed_out: { label: "Sign in again to view", detail: "Your session may have expired" },
  forbidden: { label: "Not permitted to view", detail: "You do not have access to this image" },
  corrupt: { label: "Image is damaged", detail: "The file arrived but could not be read" },
};

/** Classify an HTTP status. Pure. */
export const classifyStatus = (status: number): ImageFailureKind => {
  if (status === 401) return "signed_out";
  if (status === 403) return "forbidden";
  if (status === 404 || status === 410) return "missing";
  // 408/425/429 and every 5xx are the server or storage having a bad moment;
  // anything else unexpected is treated the same, because retrying it is cheap
  // and calling it "missing" would be a claim the status does not support.
  return "transient";
};

/** Pauses before each automatic retry of a transient failure. */
export const RETRY_DELAYS_MS = [1000, 3000, 8000];

/** How long one request may take before it is abandoned as transient. */
export const REQUEST_TIMEOUT_MS = 20000;

/**
 * How far outside the viewport a tile starts loading. A day's grid is a hundred
 * and forty-four tiles; only the ones the viewer can reach need a request, and
 * fetching all of them on render is what turned a page load into a storm against
 * a proxied Drive download per thumbnail.
 */
export const LOAD_MARGIN_PX = 600;

/**
 * Most image requests in flight at once, across the whole page.
 *
 * A day's grid asks for every thumbnail the moment it renders. Fired together
 * they are dozens of simultaneous proxied Drive downloads against a backend with
 * a small connection pool and a Drive quota, and the ones that lose are the
 * "unavailable" cards among healthy neighbours.
 */
export const MAX_CONCURRENT_IMAGE_REQUESTS = 6;

let inFlight = 0;
const waiting: Array<() => void> = [];

const acquire = (): Promise<void> =>
  new Promise((resolve) => {
    if (inFlight < MAX_CONCURRENT_IMAGE_REQUESTS) {
      inFlight += 1;
      resolve();
    } else {
      waiting.push(() => {
        inFlight += 1;
        resolve();
      });
    }
  });

const release = () => {
  inFlight -= 1;
  const next = waiting.shift();
  if (next) next();
};

class FetchFailure extends Error {
  failure: ImageFailure;
  constructor(failure: ImageFailure) {
    super(failure.kind);
    this.failure = failure;
  }
}

const currentToken = (): string | null => localStorage.getItem("accessToken");

const fetchImage = async (url: string, cancel: AbortSignal): Promise<Blob> => {
  const token = currentToken();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  // The tile going away abandons the request in flight, so a viewer scrolling
  // past a day's tiles does not leave their downloads running behind them.
  const onCancel = () => controller.abort();
  cancel.addEventListener("abort", onCancel);
  // Settle on abort whether or not the platform's fetch honours the signal, so
  // a request that never answers can never hold one of the page's request slots
  // (`release` runs when this settles).
  const aborted = new Promise<never>((_resolve, reject) => {
    controller.signal.addEventListener("abort", () =>
      reject(new DOMException("aborted", "AbortError")),
    );
  });
  aborted.catch(() => undefined);
  try {
    const response = await Promise.race([
      fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        signal: controller.signal,
      }),
      aborted,
    ]);
    if (!response.ok) {
      throw new FetchFailure({ kind: classifyStatus(response.status), status: response.status });
    }
    const blob = await Promise.race([response.blob(), aborted]);
    if (!blob.size) throw new FetchFailure({ kind: "corrupt", status: response.status });
    return blob;
  } catch (error) {
    if (error instanceof FetchFailure) throw error;
    // A network error or our own timeout: the server never answered.
    throw new FetchFailure({ kind: "transient" });
  } finally {
    clearTimeout(timer);
    cancel.removeEventListener("abort", onCancel);
  }
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export const AuthedImage: React.FC<{
  url: string;
  alt: string;
  className?: string;
  /** Rendered in place of the image while loading and on failure. */
  frameClassName?: string;
}> = ({ url, alt, className = "", frameClassName = "" }) => {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [failure, setFailure] = useState<ImageFailure | null>(null);
  // Bumped by the Retry button to run the whole load again.
  const [attempt, setAttempt] = useState(0);
  // Whether the tile is close enough to the viewer to be worth a request. A
  // browser without IntersectionObserver (and the test DOM) loads at once.
  const [near, setNear] = useState(typeof IntersectionObserver === "undefined");
  const placeholder = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (near) return;
    const node = placeholder.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setNear(true);
      },
      { rootMargin: `${LOAD_MARGIN_PX}px` },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [near]);

  useEffect(() => {
    if (!near) return;
    let cancelled = false;
    const cancel = new AbortController();
    let created: string | null = null;
    setObjectUrl(null);
    setFailure(null);

    (async () => {
      let last: ImageFailure = { kind: "transient" };
      // A 401 can mean only that the access token this request carried was
      // replaced while it was in flight (the rest of the app refreshes it; this
      // raw fetch does not). Try once more with whatever is stored now before
      // telling the viewer their session is over.
      let reauthenticated = false;
      for (let tries = 0; tries <= RETRY_DELAYS_MS.length; tries += 1) {
        if (cancelled) return;
        await acquire();
        if (cancelled) {
          // Gone while it waited its turn: never make the request at all.
          release();
          return;
        }
        const tokenSent = currentToken();
        try {
          const blob = await fetchImage(url, cancel.signal);
          if (cancelled) return;
          created = URL.createObjectURL(blob);
          setObjectUrl(created);
          return;
        } catch (error) {
          last = error instanceof FetchFailure ? error.failure : { kind: "transient" };
        } finally {
          release();
        }
        if (last.kind === "signed_out" && !reauthenticated && currentToken() !== tokenSent) {
          reauthenticated = true;
          tries -= 1;                      // this was not one of the transient retries
          continue;
        }
        // Only a transient failure is worth another try. A 404 will still be a
        // 404 in eight seconds, and retrying a 401 only repeats it.
        if (last.kind !== "transient" || tries === RETRY_DELAYS_MS.length) break;
        await sleep(RETRY_DELAYS_MS[tries]);
      }
      if (!cancelled) setFailure(last);
    })();

    return () => {
      cancelled = true;
      cancel.abort();
      if (created) URL.revokeObjectURL(created);
    };
  }, [url, attempt, near]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  if (failure) {
    const text = IMAGE_FAILURE_TEXT[failure.kind];
    return (
      <div
        role="img"
        aria-label={`${alt}: ${text.label}`}
        data-image-state={failure.kind}
        data-http-status={failure.status ?? ""}
        title={`${text.label}. ${text.detail}${failure.status ? ` (HTTP ${failure.status})` : ""}`}
        className={`flex flex-col items-center justify-center gap-1 bg-[#F1F5F9] px-3 text-center ${frameClassName}`}
      >
        <span className="text-[10px] font-semibold text-[#94A3B8]">{text.label}</span>
        <span className="text-[9.5px] leading-snug text-[#94A3B8]">{text.detail}</span>
        {(failure.kind === "transient" || failure.kind === "signed_out") && (
          <button
            type="button"
            onClick={retry}
            className="mt-0.5 rounded-full border border-[#CBD5E1] bg-white px-2.5 py-0.5 text-[10px] font-semibold text-[#2563EB] hover:bg-[#EFF6FF]"
          >
            Retry
          </button>
        )}
      </div>
    );
  }

  if (!objectUrl) {
    return (
      <div
        ref={placeholder}
        className={`animate-pulse bg-[#E2E8F0] ${frameClassName}`}
        data-image-state="loading"
      />
    );
  }

  return (
    <img
      src={objectUrl}
      alt={alt}
      loading="lazy"
      className={className}
      // The bytes arrived but the browser cannot draw them (a truncated or
      // non-image body). That is its own failure, not "unavailable".
      onError={() => setFailure({ kind: "corrupt" })}
    />
  );
};
