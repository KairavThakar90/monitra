import React, { useCallback, useLayoutEffect, useEffect, useRef, useState } from "react";
import { AppIcon } from "../../../components/AppIcon";
import { brand } from "./theme";

/** Measures a block element so SVG charts can use real pixel coordinates. */

export const useInView = (options = { threshold: 0.1, triggerOnce: true }) => {
  const [inView, setInView] = useState(false);
  const ref = useRef<any>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) {
        setInView(true);
        if (options.triggerOnce) observer.unobserve(el);
      } else {
        if (!options.triggerOnce) setInView(false);
      }
    }, options);

    observer.observe(el);
    return () => {
      if (el) observer.unobserve(el);
    };
  }, [options.threshold, options.triggerOnce]);

  return { ref, inView };
};

export const useMeasure = <T extends HTMLElement>() => {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(0);

  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const update = () => setWidth(node.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return { ref, width };
};

/* ------------------------------------------------------------------ */
/* Sparkline — the small trend inside a stat tile. One series, no axes. */
/* ------------------------------------------------------------------ */

export const Sparkline: React.FC<{ values: number[]; color: string; height?: number }> = ({
  values,
  color,
  height = 40,
}) => {
  const w = 120;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 4;
  const x = (i: number) => (i / (values.length - 1)) * w;
  const y = (v: number) => pad + (1 - (v - min) / span) * (height - pad * 2);

  const line = values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${w},${height} L0,${height} Z`;
  const gid = `spark-${color.replace("#", "")}`;

  return (
    <svg width={w} height={height} viewBox={`0 0 ${w} ${height}`} aria-hidden="true" className="overflow-visible">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gid})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={x(values.length - 1)} cy={y(values[values.length - 1])} r="3.5" fill={color} stroke="#FFFFFF" strokeWidth="2" />
    </svg>
  );
};

/* ------------------------------------------------------------------ */
/* Trend area chart — two series on ONE axis, crosshair + tooltip.     */
/* ------------------------------------------------------------------ */

interface TrendSeries {
  label: string;
  values: number[];
  color: string;
}

/**
 * The next "round" number at or above `max`, on a 1 / 2 / 5 ladder.
 *
 * The axis used to round up to the next multiple of 100, which is fine for a
 * 190-hour peak but squashes an ordinary week — a 9-hour peak drew a line
 * pinned to the bottom 9% of the plot and read as no data at all.
 */
const niceCeil = (max: number) => {
  if (!Number.isFinite(max) || max <= 0) return 1;
  const magnitude = Math.pow(10, Math.floor(Math.log10(max)));
  const scaled = max / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * magnitude;
};

export const TrendAreaChart: React.FC<{
  labels: string[];
  seriesList: TrendSeries[];
  height?: number;
  unit?: string;
}> = ({ labels, seriesList, height = 260, unit = "h" }) => {
  const { ref, width } = useMeasure<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);

  const padL = 44;
  const padR = 16;
  const padT = 16;
  const padB = 28;
  const w = Math.max(width, 320);
  const innerW = w - padL - padR;
  const innerH = height - padT - padB;

  const max = Math.max(...seriesList.flatMap((s) => s.values));
  const niceMax = niceCeil(max);
  // A one-day range is a single point: dividing by (length - 1) would make
  // every coordinate NaN and blank the chart, so centre it instead.
  const x = (i: number) =>
    labels.length > 1 ? padL + (i / (labels.length - 1)) * innerW : padL + innerW / 2;
  const y = (v: number) => padT + (1 - v / niceMax) * innerH;

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => {
    const v = niceMax * t;
    // Under 10 the quarter-steps are fractional; above it they are whole hours.
    return niceMax >= 10 ? Math.round(v) : Math.round(v * 100) / 100;
  });
  const labelStride = Math.max(1, Math.ceil(labels.length / 8));
  // Always show the final label; drop the stride label that would sit on top of it.
  const showLabel = (i: number) => {
    const last = labels.length - 1;
    if (i === last) return true;
    return i % labelStride === 0 && last - i >= labelStride * 0.6;
  };

  const handleMove = useCallback(
    (event: React.MouseEvent<SVGSVGElement>) => {
      const box = event.currentTarget.getBoundingClientRect();
      const rel = event.clientX - box.left - padL;
      const step = labels.length > 1 ? innerW / (labels.length - 1) : innerW;
      const idx = Math.min(labels.length - 1, Math.max(0, Math.round(rel / step)));
      setHover(idx);
    },
    [innerW, labels.length]
  );

  const { ref: viewRef, inView } = useInView({ threshold: 0.1, triggerOnce: false });

  return (
    <div ref={(el) => {
      ref.current = el;
      viewRef.current = el;
    }} className="relative w-full">
      <svg
        width="100%"
        height={height}
        viewBox={`0 0 ${w} ${height}`}
        onMouseMove={handleMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label="Tracked hours and activity over the selected date range"
      >
        <defs>
          <clipPath id="chart-reveal-clip">
            <rect x={padL} y="0" height={height} className="transition-all duration-1000 ease-out" style={{ width: inView ? innerW + padR + 10 : 0 }} />
          </clipPath>
          {seriesList.map((s) => (
            <linearGradient id={`area-${s.label.replace(/\s/g, "")}`} key={s.label} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={s.color} stopOpacity="0.2" />
              <stop offset="100%" stopColor={s.color} stopOpacity="0" />
            </linearGradient>
          ))}
        </defs>

        {/* Recessive solid hairline grid */}
        {ticks.map((t) => (
          <g key={t}>
            <line x1={padL} x2={w - padR} y1={y(t)} y2={y(t)} stroke={brand.line} strokeWidth="1" />
            <text x={padL - 10} y={y(t) + 4} textAnchor="end" fontSize="11" fill={brand.subtle}>
              {t}
            </text>
          </g>
        ))}

        {seriesList.map((s) => {
          const line = s.values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
          return (
            <g key={s.label} clipPath="url(#chart-reveal-clip)">
              <path d={`${line} L${x(s.values.length - 1)},${y(0)} L${x(0)},${y(0)} Z`} fill={`url(#area-${s.label.replace(/\s/g, "")})`} />
              <path d={line} fill="none" stroke={s.color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </g>
          );
        })}

        {/* X labels — thinned to ~8 so they never collide */}
        {labels.map((l, i) =>
          showLabel(i) ? (
            <text
              key={l}
              x={x(i)}
              y={height - 8}
              textAnchor={
                labels.length === 1
                  ? "middle"
                  : i === 0
                    ? "start"
                    : i === labels.length - 1
                      ? "end"
                      : "middle"
              }
              fontSize="11"
              fill={brand.subtle}
            >
              {l}
            </text>
          ) : null
        )}

        {hover !== null && (
          <g pointerEvents="none">
            <line x1={x(hover)} x2={x(hover)} y1={padT} y2={padT + innerH} stroke={brand.subtle} strokeWidth="1" />
            {seriesList.map((s) => (
              <circle key={s.label} cx={x(hover)} cy={y(s.values[hover])} r="4.5" fill={s.color} stroke="#FFFFFF" strokeWidth="2" />
            ))}
          </g>
        )}
      </svg>

      {hover !== null && (
        <div
          className="pointer-events-none absolute z-10 rounded-xl border border-[#E2E8F0] bg-white px-3 py-2 shadow-lg"
          style={{
            left: Math.min(Math.max(x(hover) - 70, 0), w - 150),
            top: 8,
            width: 150,
          }}
        >
          <div className="text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8]">{labels[hover]}</div>
          {seriesList.map((s) => (
            <div key={s.label} className="mt-1.5 flex items-center gap-2">
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: s.color }} />
              <span className="flex-1 text-[11px] text-[#64748B]">{s.label}</span>
              <span className="text-[11px] font-bold text-[#0F172A]">
                {s.values[hover]}
                {unit}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* Ranked horizontal bars — one series, one color, direct labels.      */
/* ------------------------------------------------------------------ */

export interface RankedItem {
  id: string;
  name: string;
  value: number;
  meta: string;
  secondary?: number; // shown in the tooltip, e.g. activity %
}

/**
 * Hover cards that survive a scrolling list.
 *
 * A card drawn `absolute` inside a row is clipped by any scrolling ancestor,
 * so a list with a scroller cannot use one. This anchors the card to the row's
 * on-screen rectangle instead and draws it `fixed`, which no ancestor clips.
 * It closes when anything scrolls, since the rectangle it was measured from
 * has moved.
 */
export const useHoverAnchor = () => {
  const [rect, setRect] = useState<DOMRect | null>(null);
  useEffect(() => {
    if (!rect) return;
    const close = () => setRect(null);
    window.addEventListener("scroll", close, true);
    return () => window.removeEventListener("scroll", close, true);
  }, [rect]);
  return {
    rect,
    bind: {
      onMouseEnter: (event: React.MouseEvent<HTMLElement>) => setRect(event.currentTarget.getBoundingClientRect()),
      onMouseLeave: () => setRect(null),
    },
  };
};

/** A card centred above the anchor row, or below it when there is no room above. */
export const FloatingCard: React.FC<{
  rect: DOMRect | null;
  className?: string;
  children: React.ReactNode;
}> = ({ rect, className = "", children }) => {
  if (!rect) return null;
  const above = rect.top > 150;
  return (
    <div
      className={`pointer-events-none fixed z-50 ${className}`}
      style={{
        left: rect.left + rect.width / 2,
        top: above ? rect.top - 6 : rect.bottom + 6,
        transform: above ? "translate(-50%, -100%)" : "translateX(-50%)",
      }}
    >
      {children}
    </div>
  );
};

/** One ranked row; its tooltip is a `FloatingCard`, so a scroller cannot clip it. */
const RankedRow: React.FC<{ className: string; tooltip: React.ReactNode; children: React.ReactNode }> = ({
  className,
  tooltip,
  children,
}) => {
  const { rect, bind } = useHoverAnchor();
  return (
    <li className={className} {...(tooltip ? bind : {})}>
      {children}
      {tooltip && <FloatingCard rect={rect}>{tooltip}</FloatingCard>}
    </li>
  );
};

export const RankedBars: React.FC<{
  items: RankedItem[];
  color: string;
  formatValue: (n: number) => string;
  secondaryLabel?: string;
  showRank?: boolean;
  avatars?: boolean;
  /**
   * Draw each row's application mark after the rank. Only the apps dimension
   * has one -- a project or a task is not an application and has no icon to
   * show, so this stays off unless the caller says otherwise.
   */
  appIcons?: boolean;
}> = ({ items, color, formatValue, secondaryLabel = "Activity", showRank = true, avatars = false, appIcons = false }) => {
  const max = Math.max(...items.map((i) => i.value)) || 1;

  const initials = (name: string) =>
    name
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map((p) => p[0])
      .join("")
      .toUpperCase();

  return (
    <ul className="flex flex-col gap-1.5">
      {items.map((item, index) => {
        const percent = Math.max((item.value / max) * 100, 1);
        return (
          <RankedRow key={item.id} className="group relative flex items-center gap-3 rounded-lg px-3 py-2 transition-all hover:bg-slate-50/50" tooltip={
            item.secondary !== undefined ? (
              <div className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs shadow-sm">
                <span className="font-medium text-slate-800">{item.name}</span>
                <span className="ml-2 text-slate-500">
                  {secondaryLabel} {item.secondary}%
                </span>
              </div>
            ) : null
          }>
            {/* Background Bar Fill */}
            <div
              className="absolute inset-y-0 left-0 rounded-lg opacity-[0.08] transition-all duration-500 ease-out"
              style={{ width: `${percent}%`, backgroundColor: color }}
            />
            
            {showRank && !avatars && (
              <span className="w-5 shrink-0 text-center text-[11px] font-semibold text-slate-400">
                {index + 1}
              </span>
            )}
            
            {avatars && (
              <span
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[10px] font-bold text-white shadow-sm ring-2 ring-white"
                style={{ backgroundColor: color }}
              >
                {initials(item.name)}
              </span>
            )}
            
            {appIcons && <AppIcon name={item.name} size={22} className="relative z-10" />}

            <div className="relative z-10 flex flex-1 items-center justify-between min-w-0">
              <div className="flex flex-col truncate pr-4">
                <span className="truncate text-[13px] font-medium text-slate-700 transition-colors group-hover:text-slate-900">
                  {item.name}
                </span>
                {item.meta && (
                  <span className="truncate text-[11px] text-slate-500">
                    {item.meta}
                  </span>
                )}
              </div>
              <div className="shrink-0 text-right">
                <span className="text-[13px] font-semibold text-slate-800">
                  {formatValue(item.value)}
                </span>
              </div>
            </div>

          </RankedRow>
        );
      })}
    </ul>
  );
};

/* ------------------------------------------------------------------ */
/* Donut — part-to-whole at a glance, <= 6 segments, 2px surface gaps. */
/* ------------------------------------------------------------------ */

export interface DonutSlice {
  label: string;
  value: number;
  color: string;
}

export const Donut: React.FC<{ slices: DonutSlice[]; size?: number; centerLabel: string; centerValue: string }> = ({
  slices,
  size = 168,
  centerLabel,
  centerValue,
}) => {
  const { ref: viewRef, inView } = useInView({ threshold: 0.1, triggerOnce: false });
  const total = slices.reduce((sum, s) => sum + s.value, 0) || 1;
  const stroke = 18;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const gap = 3; // surface gap in px of circumference
  const centerValueFontSize = centerValue.length > 8 ? 16 : centerValue.length > 6 ? 20 : 24;

  let offset = 0;

  return (
    <svg ref={viewRef} width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={centerLabel}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#F1F5F9" strokeWidth={stroke} />
      <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
        {slices.map((s, index) => {
          const len = (s.value / total) * c;
          const dash = `${Math.max(len - gap, 0)} ${c - Math.max(len - gap, 0)}`;
          const initialDash = `0 ${c}`;
          const el = (
            <circle
              key={s.label}
              cx={size / 2}
              cy={size / 2}
              r={r}
              fill="none"
              stroke={s.color}
              strokeWidth={stroke}
              strokeDasharray={inView ? dash : initialDash}
              strokeDashoffset={-offset}
              strokeLinecap="butt"
              className="transition-all duration-1000 ease-out"
              style={{ transitionDelay: `${index * 100}ms` }}
            />
          );
          offset += len;
          return el;
        })}
      </g>
      <text
        x={size / 2}
        y={size / 2 - 2}
        textAnchor="middle"
        fontSize={centerValueFontSize}
        fontWeight="800"
        fill={brand.ink}
        textLength={Math.min(size - stroke - 8, centerValue.length * centerValueFontSize * 0.65)}
        lengthAdjust="spacingAndGlyphs"
      >
        {centerValue}
      </text>
      <text x={size / 2} y={size / 2 + 18} textAnchor="middle" fontSize="11" fill={brand.subtle}>
        {centerLabel}
      </text>
    </svg>
  );
};

/* ------------------------------------------------------------------ */
/* Legend — always present for >= 2 series.                            */
/* ------------------------------------------------------------------ */

/**
 * `icon` is optional and sits *after* the colour swatch -- the apps legend
 * carries each application's own mark, where a project or member legend has
 * nothing to show but its slice colour. The swatch stays either way: it is
 * what maps a legend row back to its arc in the donut.
 */
export const Legend: React.FC<{
  items: { label: string; color: string; value?: string; icon?: React.ReactNode }[];
}> = ({ items }) => (
  <ul className="flex flex-wrap items-center gap-x-5 gap-y-2">
    {items.map((i) => (
      <li key={i.label} className="flex items-center gap-2">
        <span className="h-2.5 w-2.5 rounded-sm" style={{ background: i.color }} />
        {i.icon}
        <span className="text-[12px] text-[#64748B]">{i.label}</span>
        {i.value && <span className="text-[12px] font-bold text-[#0F172A]">{i.value}</span>}
      </li>
    ))}
  </ul>
);
