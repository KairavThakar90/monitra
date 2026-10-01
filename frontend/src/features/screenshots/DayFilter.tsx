import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { IST_TIME_ZONE } from '../../utils/duration';
import { CalendarPane } from '../dashboard/v2/filters';

/**
 * The screenshots page's date control: one day at a time.
 *
 * The rest of the app filters by a *span*, through the dashboard's
 * `DateRangeFilter`, and this screen used that too. It reads badly here for
 * two reasons. The grid is organised as hours of a single tracked day, so a
 * week of days is a page nobody scrolls to the bottom of; and the range picker
 * commits only on a *second* click — picking one day and getting no change is
 * what "the date filter doesn't work" turns out to mean. A day picker commits
 * on the first click and cannot leave the control in a half-chosen state.
 *
 * It *looks* like that range picker, though: the same presets rail and the same
 * two-month `CalendarPane`, so a date control reads the same on every page.
 *
 * Every date here is an IST calendar date, matching what the API means by
 * `from`/`to`, so a viewer in another timezone still asks for the day the
 * employee worked.
 */

/** Today's IST calendar date as `YYYY-MM-DD`. */
export const istTodayIso = (): string =>
  new Intl.DateTimeFormat('en-CA', {
    timeZone: IST_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date());

const parseIso = (iso: string) => {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
};

const isoOf = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

const addDays = (iso: string, days: number) => {
  const d = parseIso(iso);
  d.setDate(d.getDate() + days);
  return isoOf(d);
};

/** How the chosen day reads on the button, e.g. "Tue, 08 Sep 2026". */
const longDate = (iso: string) =>
  parseIso(iso).toLocaleDateString('en-GB', {
    weekday: 'short',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });

/** One-day presets, each a number of days before today. */
const DAY_PRESETS: { label: string; daysAgo: number }[] = [
  { label: 'Today', daysAgo: 0 },
  { label: 'Yesterday', daysAgo: 1 },
  { label: '2 days ago', daysAgo: 2 },
  { label: '3 days ago', daysAgo: 3 },
  { label: 'A week ago', daysAgo: 7 },
];

export const DayFilter: React.FC<{
  /** The selected IST day, `YYYY-MM-DD`. */
  value: string;
  onChange: (iso: string) => void;
}> = ({ value, onChange }) => {
  const today = useMemo(istTodayIso, []);
  const [open, setOpen] = useState(false);
  /**
   * The month the left pane opens on. The right pane is always the month after
   * it and nothing past today is selectable, so the left pane is clamped to one
   * month before the current one -- otherwise a day in this month would put a
   * wholly-unselectable future month on the right.
   */
  const viewFor = (iso: string) => {
    const d = parseIso(iso);
    const t = parseIso(today);
    const latest = new Date(t.getFullYear(), t.getMonth() - 1, 1);
    const wanted = new Date(d.getFullYear(), d.getMonth(), 1);
    const shown = wanted > latest ? latest : wanted;
    return { year: shown.getFullYear(), month: shown.getMonth() };
  };
  const [view, setView] = useState(() => viewFor(value));
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  // Hang the panel from the right edge when hanging from the left would push
  // it off-screen (it is ~700px wide).
  const [alignRight, setAlignRight] = useState(false);

  useEffect(() => {
    if (!open) return;
    const handler = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const atToday = value >= today;
  const pick = (iso: string) => {
    if (iso > today) return;
    onChange(iso);
    setOpen(false);
  };

  const step = (delta: number) =>
    setView((v) => {
      const d = new Date(v.year, v.month + delta, 1);
      return { year: d.getFullYear(), month: d.getMonth() };
    });

  const right = new Date(view.year, view.month + 1, 1);
  // The right pane may reach the current month but never go past it.
  const todayDate = parseIso(today);
  const atLastMonth =
    right.getFullYear() > todayDate.getFullYear() ||
    (right.getFullYear() === todayDate.getFullYear() && right.getMonth() >= todayDate.getMonth());

  useLayoutEffect(() => {
    if (!open || !panelRef.current || !wrapRef.current) return;
    const panel = panelRef.current.getBoundingClientRect();
    const anchorBox = wrapRef.current.getBoundingClientRect();
    const margin = 12;
    const flippedLeft = anchorBox.right - panel.width;
    setAlignRight(panel.right > window.innerWidth - margin && flippedLeft >= margin);
  }, [open, view.year, view.month]);

  return (
    <div className="relative flex items-center gap-1" ref={wrapRef}>
      <button
        type="button"
        onClick={() => onChange(addDays(value, -1))}
        aria-label="Previous day"
        className="flex h-9 w-9 items-center justify-center rounded-lg border border-[#E2E8F0] bg-white text-[#64748B] transition hover:border-[#CBD5E1] hover:text-[#0F172A]"
      >
        <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15 19l-7-7 7-7" />
        </svg>
      </button>

      <button
        type="button"
        onClick={() => {
          setView(viewFor(value));
          setAlignRight(false);
          setOpen((o) => !o);
        }}
        aria-expanded={open}
        className={
          'flex h-9 items-center gap-2 rounded-lg border bg-white px-3.5 text-[13px] font-semibold text-[#0F172A] transition ' +
          (open ? 'border-[#38BDF8] ring-2 ring-[#38BDF8]/20' : 'border-[#E2E8F0] hover:border-[#CBD5E1]')
        }
      >
        <svg className="h-4 w-4 text-[#38BDF8]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="2"
            d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
          />
        </svg>
        <span>{longDate(value)}</span>
        {value === today && (
          <span className="rounded-full bg-[#38BDF8]/10 px-2 py-0.5 text-[10px] font-bold text-[#0284C7]">
            Today
          </span>
        )}
      </button>

      <button
        type="button"
        onClick={() => !atToday && onChange(addDays(value, 1))}
        disabled={atToday}
        aria-label="Next day"
        className={
          'flex h-9 w-9 items-center justify-center rounded-lg border border-[#E2E8F0] bg-white transition ' +
          (atToday
            ? 'cursor-not-allowed text-[#E2E8F0]'
            : 'text-[#64748B] hover:border-[#CBD5E1] hover:text-[#0F172A]')
        }
      >
        <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 5l7 7-7 7" />
        </svg>
      </button>

      {!atToday && (
        <button
          type="button"
          onClick={() => onChange(today)}
          className="ml-1 h-9 rounded-lg border border-[#E2E8F0] bg-white px-3 text-[12px] font-bold text-[#2563EB] transition hover:border-[#CBD5E1]"
        >
          Today
        </button>
      )}

      {open && (
        <div
          ref={panelRef}
          className={
            'absolute top-full z-40 mt-2 flex max-w-[calc(100vw-2rem)] gap-5 overflow-x-auto rounded-xl border border-[#E2E8F0] bg-white p-4 shadow-2xl ' +
            (alignRight ? 'right-0' : 'left-0')
          }
        >
          <div className="flex w-[132px] flex-col gap-2">
            {DAY_PRESETS.map((preset) => {
              const iso = addDays(today, -preset.daysAgo);
              return (
                <button
                  key={preset.label}
                  type="button"
                  onClick={() => pick(iso)}
                  className={
                    'rounded-md border px-3 py-1.5 text-[13px] font-medium transition ' +
                    (value === iso
                      ? 'border-[#38BDF8] bg-[#38BDF8]/10 text-[#0284C7]'
                      : 'border-[#E2E8F0] text-[#0F172A] hover:border-[#CBD5E1] hover:bg-[#F8FAFC]')
                  }
                >
                  {preset.label}
                </button>
              );
            })}
          </div>

          <div className="flex gap-6">
            <CalendarPane
              year={view.year}
              month={view.month}
              from={value}
              to={value}
              onPick={pick}
              onHover={() => undefined}
              onPrev={() => step(-1)}
            />
            <CalendarPane
              year={right.getFullYear()}
              month={right.getMonth()}
              from={value}
              to={value}
              onPick={pick}
              onHover={() => undefined}
              onNext={atLastMonth ? undefined : () => step(1)}
            />
          </div>
        </div>
      )}
    </div>
  );
};
