// @vitest-environment jsdom
/**
 * A card with no picture says why — and only retries what a retry can fix.
 *
 * "Image unavailable" was printed for every rejected fetch with the status
 * thrown away. These drive the real component with a scripted `fetch` and pin
 * the cases the investigation named: a stored image, a file gone from storage,
 * a transient storage failure (retried, then offered a Retry), a signed-out or
 * forbidden viewer, an empty or undecodable body, and a network error.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  AuthedImage,
  IMAGE_FAILURE_TEXT,
  MAX_CONCURRENT_IMAGE_REQUESTS,
  REQUEST_TIMEOUT_MS,
  RETRY_DELAYS_MS,
  classifyStatus,
} from '../AuthedImage';

const ok = (bytes = 'RIFFxxxxWEBP') =>
  new Response(new Blob([bytes], { type: 'image/webp' }), { status: 200 });
const status = (code: number) => new Response('x', { status: code });

describe('classifyStatus', () => {
  it.each([
    [401, 'signed_out'],
    [403, 'forbidden'],
    [404, 'missing'],
    [410, 'missing'],
    [408, 'transient'],
    [429, 'transient'],
    [500, 'transient'],
    [502, 'transient'],
    [503, 'transient'],
    [504, 'transient'],
    [418, 'transient'],
  ] as const)('%i is %s', (code, kind) => {
    expect(classifyStatus(code)).toBe(kind);
  });

  it('never calls a server error "missing": that would claim data loss the status does not show', () => {
    for (const code of [500, 502, 503, 504]) expect(classifyStatus(code)).not.toBe('missing');
  });
});

describe('AuthedImage', () => {
  let container: HTMLDivElement;
  let root: Root;
  let fetchMock: ReturnType<typeof vi.fn>;

  const mount = async () => {
    await act(async () => {
      root.render(<AuthedImage url="/time-entry-screenshots/5/view" alt="Screen of Alex" />);
    });
  };
  /** Let promises and any pending timers settle. */
  const settle = async (ms = 0) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  };
  const tile = () => container.querySelector('[data-image-state]');
  const state = () => tile()?.getAttribute('data-image-state');

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('localStorage', { getItem: () => 'token-123' });
    URL.createObjectURL = vi.fn(() => 'blob:mock');
    URL.revokeObjectURL = vi.fn();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('shows the image when the backend serves one, and sends the bearer token', async () => {
    fetchMock.mockResolvedValue(ok());
    await mount();
    await settle();
    expect(container.querySelector('img')?.getAttribute('src')).toBe('blob:mock');
    expect(fetchMock.mock.calls[0][1].headers).toEqual({ Authorization: 'Bearer token-123' });
    expect(container.textContent).not.toContain('unavailable');
  });

  it('says the image is missing from storage for a 404, and does not retry', async () => {
    fetchMock.mockResolvedValue(status(404));
    await mount();
    await settle(30_000);
    expect(state()).toBe('missing');
    expect(tile()?.getAttribute('data-http-status')).toBe('404');
    expect(container.textContent).toContain(IMAGE_FAILURE_TEXT.missing.label);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(container.querySelector('button')).toBeNull();
  });

  it('treats 410 (the file is gone from Drive) the same as missing', async () => {
    fetchMock.mockResolvedValue(status(410));
    await mount();
    await settle();
    expect(state()).toBe('missing');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('retries a transient storage failure with backoff and then shows the image', async () => {
    fetchMock
      .mockResolvedValueOnce(status(502))
      .mockResolvedValueOnce(status(503))
      .mockResolvedValueOnce(ok());
    await mount();
    await settle(0);
    expect(state()).toBe('loading');                      // still trying, not failed
    await settle(RETRY_DELAYS_MS[0]);
    await settle(RETRY_DELAYS_MS[1]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(container.querySelector('img')).not.toBeNull();
    expect(tile()).toBeNull();
  });

  it('gives up on a persistent transient failure and offers Retry, with the status shown', async () => {
    fetchMock.mockResolvedValue(status(502));
    await mount();
    for (const delay of RETRY_DELAYS_MS) await settle(delay);
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(RETRY_DELAYS_MS.length + 1);
    expect(state()).toBe('transient');
    expect(tile()?.getAttribute('data-http-status')).toBe('502');
    expect(container.textContent).toContain(IMAGE_FAILURE_TEXT.transient.label);

    // Retry runs the load again, and a recovered backend now serves it.
    fetchMock.mockResolvedValue(ok());
    const button = container.querySelector('button') as HTMLButtonElement;
    await act(async () => { button.click(); });
    await settle();
    expect(container.querySelector('img')).not.toBeNull();
  });

  it('treats a network error as transient, not as a missing image', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    await mount();
    for (const delay of RETRY_DELAYS_MS) await settle(delay);
    await settle();
    expect(state()).toBe('transient');
    expect(tile()?.getAttribute('data-http-status')).toBe('');
  });

  it('abandons a request that never answers instead of hanging the tile for ever', async () => {
    fetchMock.mockImplementation((_url: string, init: { signal: AbortSignal }) =>
      new Promise((_resolve, reject) => {
        init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      }),
    );
    await mount();
    await settle(REQUEST_TIMEOUT_MS + 1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await settle(RETRY_DELAYS_MS[0]);
    expect(fetchMock).toHaveBeenCalledTimes(2);          // it tried again
  });

  it('asks a signed-out viewer to sign in and does not retry', async () => {
    fetchMock.mockResolvedValue(status(401));
    await mount();
    await settle(30_000);
    expect(state()).toBe('signed_out');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('says a forbidden viewer is not permitted', async () => {
    fetchMock.mockResolvedValue(status(403));
    await mount();
    await settle();
    expect(state()).toBe('forbidden');
  });

  it('reports an empty body as a damaged image, not a missing one', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 200 }));
    await mount();
    await settle();
    expect(state()).toBe('corrupt');
  });

  it('reports bytes the browser cannot decode as a damaged image', async () => {
    fetchMock.mockResolvedValue(ok('this is not a picture'));
    await mount();
    await settle();
    const img = container.querySelector('img') as HTMLImageElement;
    await act(async () => { img.dispatchEvent(new Event('error')); });
    expect(state()).toBe('corrupt');
    expect(container.textContent).toContain(IMAGE_FAILURE_TEXT.corrupt.label);
  });

  it('never prints the old catch-all wording', async () => {
    for (const code of [404, 410, 401, 403]) {
      fetchMock.mockResolvedValue(status(code));
      await mount();
      await settle();
      expect(container.textContent).not.toContain('Image unavailable');
    }
  });
});

describe('the page-wide request cap', () => {
  it('never has more than the cap in flight at once', async () => {
    vi.useFakeTimers();
    let active = 0;
    let peak = 0;
    const resolvers: Array<() => void> = [];
    vi.stubGlobal('localStorage', { getItem: () => null });
    vi.stubGlobal('fetch', vi.fn(() => {
      active += 1;
      peak = Math.max(peak, active);
      return new Promise<Response>((resolve) => {
        resolvers.push(() => { active -= 1; resolve(ok()); });
      });
    }));
    URL.createObjectURL = vi.fn(() => 'blob:mock');
    URL.revokeObjectURL = vi.fn();
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <>
          {Array.from({ length: 20 }, (_, i) => (
            <AuthedImage key={i} url={`/time-entry-screenshots/${i}/view`} alt={`s${i}`} />
          ))}
        </>,
      );
    });
    // Release them in waves until all 20 have been served.
    for (let guard = 0; guard < 40 && resolvers.length; guard += 1) {
      await act(async () => { resolvers.splice(0).forEach((r) => r()); await vi.advanceTimersByTimeAsync(0); });
    }
    expect(peak).toBeLessThanOrEqual(MAX_CONCURRENT_IMAGE_REQUESTS);
    expect(container.querySelectorAll('img').length).toBe(20);
    await act(async () => { root.unmount(); });
    container.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
});
