import React, { useEffect, useState } from 'react';

/**
 * One screenshot, fetched as an authenticated blob and shown as an image.
 *
 * A plain `<img src>` can't carry the `Authorization` header the view
 * endpoint requires (screenshots are private, proxied through the backend,
 * never a public Drive link), so this fetches the bytes itself and hands the
 * browser an object URL instead.
 */
export const ClientScreenshotThumbnail: React.FC<{ url: string; capturedAt: string }> = ({ url, capturedAt }) => {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;

    (async () => {
      try {
        const token = localStorage.getItem('accessToken');
        const response = await fetch(url, {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        if (!response.ok) throw new Error('Screenshot could not be loaded');
        const blob = await response.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url]);

  return (
    <div className="overflow-hidden rounded-lg border border-[#E2E8F0] bg-[#F8FAFC]">
      <div className="flex aspect-video items-center justify-center bg-[#0F172A]/5">
        {failed ? (
          <span className="text-xs text-[#94A3B8]">Unavailable</span>
        ) : src ? (
          <img src={src} alt="Screenshot" className="h-full w-full object-cover" />
        ) : (
          <span className="text-xs text-[#94A3B8]">Loading…</span>
        )}
      </div>
      <div className="px-2.5 py-1.5 text-[11px] font-semibold text-[#64748B]">
        {new Date(capturedAt).toLocaleString()}
      </div>
    </div>
  );
};
