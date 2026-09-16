/**
 * The Monitra desktop download page.
 *
 * The one thing this page must get right: **it never links to a versioned
 * file.** Every button points at its platform's stable distribution link
 * (`DOWNLOAD_LINKS`), so the build behind it can change without this file being
 * edited.
 *
 * Public and unauthenticated: a new member installing Monitra for the first
 * time has no account yet, and asking them to sign in before they can download
 * the thing they sign in with is a loop.
 *
 * **Downloading does not depend on the release service.** The installers are
 * hosted separately from the backend, so `GET /desktop/releases/downloads`
 * supplies the version, size, notes and checksum shown beside a button and
 * nothing more. When it fails or knows nothing, those rows are omitted — never
 * guessed — and the download stays exactly as clickable as it was. Hiding a
 * working download behind a metadata failure would be reporting a fault that
 * is not there, which is the mirror image of the fabricated-data pattern this
 * project has already removed once.
 *
 * The palette is the one the rest of the app uses — the #2563EB blue and the
 * slate hexes from LoginScreen — rather than Tailwind's stock indigo, so this
 * page reads as part of Monitra and not as a landing page bolted onto it.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  detectDownloadKey,
  downloadUrlFor,
  fetchDesktopDownloadsAPI,
  formatFileSize,
} from '../../api/desktopRelease';
import type { DesktopDownloadIndex, DesktopRelease, DownloadKey } from '../../api/desktopRelease';

/** Display order and copy for each build. */
const CARDS: Array<{
  key: DownloadKey;
  title: string;
  /** The short name a button says "Download for ..." with. */
  shortTitle: string;
  subtitle: string;
  note?: string;
}> = [
  {
    key: 'windows',
    title: 'Windows',
    shortTitle: 'Windows',
    subtitle: 'Windows 10 and 11, 64-bit',
    note: 'Installs without administrator rights.',
  },
  {
    key: 'macos-arm64',
    title: 'macOS — Apple Silicon',
    shortTitle: 'Apple Silicon',
    subtitle: 'M1, M2, M3 and later',
  },
  {
    key: 'macos-x86_64',
    title: 'macOS — Intel',
    shortTitle: 'Intel Mac',
    subtitle: 'Intel-based Macs',
  },
];

const WindowsIcon = () => (
  <svg className="h-7 w-7" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M3 5.5 10.2 4.5v7.1H3V5.5Zm0 13 7.2 1v-7H3v6ZM11.4 4.3 21 3v8.6h-9.6V4.3Zm0 8.5H21V21l-9.6-1.3v-6.9Z" />
  </svg>
);

const AppleIcon = () => (
  <svg className="h-7 w-7" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M16.4 12.8c0-2.3 1.9-3.4 2-3.5-1.1-1.6-2.8-1.8-3.4-1.8-1.4-.1-2.8.9-3.5.9-.7 0-1.9-.9-3-.8-1.6 0-3 .9-3.8 2.3-1.6 2.8-.4 7 1.2 9.3.8 1.1 1.7 2.4 2.9 2.3 1.2 0 1.6-.7 3-.7s1.8.7 3 .7c1.2 0 2-1.1 2.8-2.2.9-1.3 1.2-2.5 1.3-2.6-.1 0-2.5-1-2.5-3.9ZM14.2 5.9c.6-.8 1-1.9.9-3-.9 0-2 .6-2.7 1.4-.6.7-1.1 1.8-.9 2.9 1 .1 2-.5 2.7-1.3Z" />
  </svg>
);

function CardIcon({ platformKey }: { platformKey: DownloadKey }) {
  return platformKey === 'windows' ? <WindowsIcon /> : <AppleIcon />;
}

function DownloadCard({
  card,
  release,
  recommended,
}: {
  card: (typeof CARDS)[number];
  release: DesktopRelease | undefined;
  recommended: boolean;
}) {
  /**
   * Whether the release service told us *about* this build — not whether it
   * can be downloaded. The installer is served from its own host, so the
   * button works whatever this call returned; all this decides is which
   * details are printed beside it.
   */
  const described = Boolean(release?.available);
  const size = formatFileSize(release?.file_size ?? null);

  return (
    <div
      className={`relative flex flex-col rounded-xl border bg-white p-6 shadow-sm transition ${
        recommended ? 'border-[#2563EB] ring-1 ring-[#2563EB]/20' : 'border-[#E2E8F0]'
      }`}
    >
      {recommended && (
        <span className="absolute -top-3 left-6 rounded-full bg-[#2563EB] px-3 py-1 text-xs font-semibold text-white">
          Recommended for your device
        </span>
      )}

      <div className="flex items-center gap-3 text-[#0F172A]">
        <CardIcon platformKey={card.key} />
        <div>
          <h3 className="text-base font-semibold text-[#0F172A]">{card.title}</h3>
          <p className="text-sm text-[#64748B]">{card.subtitle}</p>
        </div>
      </div>

      <dl className="mt-5 space-y-1 text-sm">
        <div className="flex justify-between">
          <dt className="text-[#64748B]">Version</dt>
          <dd className="font-medium text-[#0F172A]">{described ? release?.version : '—'}</dd>
        </div>
        {size && (
          <div className="flex justify-between">
            <dt className="text-[#64748B]">Size</dt>
            <dd className="font-medium text-[#0F172A]">{size}</dd>
          </div>
        )}
      </dl>

      {card.note && <p className="mt-3 text-xs text-[#94A3B8]">{card.note}</p>}

      {/* Always a real link. The installer is hosted independently of the
          release service, so a visitor can download Monitra even when that
          service has published nothing or cannot be reached — the two used to
          be the same fact and are not any more. */}
      <a
        href={downloadUrlFor(card.key)}
        rel="noreferrer noopener"
        className="mt-5 inline-flex items-center justify-center rounded-md bg-[#2563EB] px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition duration-150 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:ring-offset-2"
      >
        Download for {card.shortTitle}
      </a>

      {described && (release?.release_notes || release?.release_notes_url) && (
        <details className="mt-4">
          <summary className="cursor-pointer text-xs font-medium text-[#2563EB] hover:text-blue-700">
            What&rsquo;s new in {release.version}
          </summary>
          {release.release_notes && (
            /* The hand-written CHANGELOG entry, rendered as the plain text it
               is. Deliberately not parsed as Markdown: this string comes from
               the release record, and running it through a renderer here would
               be the one place on a public page where release content could
               inject markup. */
            <p className="mt-2 whitespace-pre-line text-xs leading-relaxed text-[#64748B]">
              {release.release_notes}
            </p>
          )}
          {release.release_notes_url && (
            <a
              href={release.release_notes_url}
              target="_blank"
              rel="noreferrer noopener"
              className="mt-2 inline-block text-xs font-medium text-[#2563EB] hover:text-blue-700"
            >
              Full release notes
            </a>
          )}
        </details>
      )}

      {described && release?.sha256 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs text-[#94A3B8] hover:text-[#64748B]">
            Verify this download
          </summary>
          <p className="mt-2 text-xs text-[#64748B]">
            SHA-256 checksum. Compare it against the file you downloaded with{' '}
            <code className="rounded bg-[#F1F5F9] px-1">
              {card.key === 'windows' ? 'Get-FileHash <file>' : 'shasum -a 256 <file>'}
            </code>
            .
          </p>
          <code className="mt-2 block break-all rounded bg-[#F8FAFC] p-2 text-[11px] text-[#64748B]">
            {release.sha256}
          </code>
        </details>
      )}
    </div>
  );
}

export function DownloadPage() {
  const [index, setIndex] = useState<DesktopDownloadIndex | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /** Bumped by "Try again", which is the only thing that refetches. */
  const [attempt, setAttempt] = useState(0);

  const recommended = useMemo(() => detectDownloadKey(), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchDesktopDownloadsAPI()
      .then((result) => {
        if (!cancelled) setIndex(result);
      })
      .catch((err: Error) => {
        // A failed request is reported as a failure, not rendered as "nothing
        // is available" — those are different facts and the user can act on
        // one of them.
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  return (
    <div className="min-h-screen bg-[#F8FAFC] py-16 font-sans">
      <div className="mx-auto max-w-5xl px-6">
        <header className="text-center">
          <img
            src="/logo.png"
            alt="Monitra"
            className="mx-auto mb-6 h-12 w-auto object-contain drop-shadow-sm"
          />
          <h1 className="text-3xl font-extrabold tracking-tight text-[#0F172A]">
            Download Monitra
          </h1>
          <p className="mx-auto mt-3 max-w-2xl text-[#64748B]">
            Monitra is the desktop application for Monitra time and activity
            tracking. It runs in the background, records the time you spend on
            your projects and tasks, and keeps working when you go offline.
          </p>
          <p className="mt-3 text-sm text-[#64748B]">
            Install it once. It keeps itself up to date from then on — you will
            be told when a new version is ready and can install it without
            coming back here.
          </p>
          {/*
            Windows shows SmartScreen on any installer it has not seen signed by
            a known publisher, and Monitra's is not signed yet. Saying so here,
            with the exact wording and the exact clicks, is the honest thing:
            somebody who meets an unexplained security warning either abandons a
            legitimate install or learns to click past warnings without reading
            them, and the second habit is worse than the first.
          */}
          <div className="mx-auto mt-5 max-w-2xl rounded-lg border border-[#E2E8F0] bg-[#F8FAFC] p-4 text-left">
            <p className="text-sm font-semibold text-[#0F172A]">
              Windows security warning — “Windows protected your PC”
            </p>
            <p className="mt-1 text-sm text-[#64748B]">
              Windows may show this message the first time you run the
              installer. It is expected. To continue:
            </p>
            <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-[#64748B]">
              <li>
                You may see the “Windows protected your PC” message.
              </li>
              <li>
                Click <span className="font-semibold text-[#0F172A]">More info</span>.
              </li>
              <li>
                Then click{' '}
                <span className="font-semibold text-[#0F172A]">Run anyway</span>{' '}
                to proceed with the application installation or execution.
              </li>
            </ol>
          </div>
          {index?.latest_version && (
            <p className="mt-4 text-sm text-[#64748B]">
              Latest version:{' '}
              <span className="font-semibold text-[#0F172A]">{index.latest_version}</span>
            </p>
          )}
        </header>

        {error && !loading && (
          /* The release service is what supplies version, size and checksum —
             it is not what serves the file. So this is a note beside working
             downloads, not a wall in front of them: the buttons below still
             do exactly what they say. */
          <div className="mx-auto mt-10 max-w-2xl rounded-md border border-amber-200 bg-amber-50 p-4 text-center">
            <p className="text-sm font-semibold text-amber-900">
              Version details could not be loaded.
            </p>
            <p className="mt-1 text-sm text-amber-800">
              {error} The downloads below still work — this only affects the
              version and size shown on each card.
            </p>
            <button
              type="button"
              onClick={retry}
              className="mt-4 inline-flex items-center justify-center rounded-md bg-[#2563EB] px-4 py-2 text-sm font-semibold text-white shadow-sm transition duration-150 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:ring-offset-2"
            >
              Try again
            </button>
          </div>
        )}

        {/* Linux, or anything else we do not build for. Every download stays
            listed and usable — the message explains the situation rather than
            hiding the page. */}
        {recommended === null && (
          <div className="mx-auto mt-12 max-w-2xl rounded-md border border-[#E2E8F0] bg-white p-4 text-center">
            <p className="text-sm font-semibold text-[#0F172A]">
              We could not identify your operating system.
            </p>
            <p className="mt-1 text-sm text-[#64748B]">
              Monitra desktop is built for Windows and macOS. All available
              downloads are listed below — choose the one that matches your
              machine.
            </p>
          </div>
        )}

        {/* The cards do not wait for the release call. Downloading is the one
            thing this page exists for, and it does not depend on that call
            having returned — only the version and size rows do. */}
        <div className="mt-12 grid gap-6 md:grid-cols-3">
          {CARDS.map((card) => (
            <DownloadCard
              key={card.key}
              card={card}
              release={index?.downloads?.[card.key]}
              recommended={recommended === card.key}
            />
          ))}
        </div>

        {loading && (
          <p className="mt-6 text-center text-sm text-[#64748B]">
            Loading version details…
          </p>
        )}

        <p className="mt-8 text-center text-xs text-[#94A3B8]">
          macOS ships one build per processor. Apple Silicon and Intel are
          different downloads — if you are unsure, choose Apple Silicon for any
          Mac bought in 2020 or later.
        </p>

        <p className="mt-10 text-center text-sm text-[#64748B]">
          Already installed?{' '}
          <Link to="/login" className="font-semibold text-[#2563EB] hover:text-blue-700">
            Sign in to your workspace
          </Link>
        </p>
      </div>
    </div>
  );
}

export default DownloadPage;
