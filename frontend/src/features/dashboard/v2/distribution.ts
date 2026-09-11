/**
 * The arithmetic behind the Reports page's "Hours Distribution" ring.
 *
 * This lives on its own, away from the two pages that draw it, because it is
 * where a real defect was: the ring used to take its slices from one dataset
 * and its whole from another.
 *
 * The slices come from a tab's own ranked rows. The whole used to come from
 * the summary strip. On the Projects and Tasks tabs those agree — both count
 * session time — so the leftover arc was honestly "the projects below the top
 * five". On the Apps and URLs tabs they do not. Application and browser usage
 * is measured separately by the desktop client, and legitimately covers less
 * of the day than the timer does: no URL can be read on macOS or Linux at all,
 * nor on Firefox without accessibility enabled, and nothing is captured while
 * the client is not running. Subtracting five applications from a whole day of
 * session time therefore swept every one of those unmeasured seconds into the
 * leftover arc and drew it as though it were an application. On real data that
 * arc reached 94.5% of the chart.
 *
 * Two rules follow, and they are what this module exists to hold:
 *
 * 1. **A part-to-whole chart divides by the population its parts came from.**
 *    `totalSeconds` here is the list endpoint's own scope-wide total, not the
 *    summary's.
 * 2. **The gap between the two measures is reported, not absorbed.**
 *    `describeCoverage` states it in seconds, so it stays visible and
 *    diagnosable instead of hiding inside an unnamed slice.
 */

export interface DistributionRow {
  /** The entity's own name, exactly as the backend reported it. */
  name: string;
  seconds: number;
}

export interface DistributionSlice {
  label: string;
  /** Hours, for the chart component's value scale. */
  value: number;
  seconds: number;
  color: string;
  /**
   * True for the leftover arc. It aggregates several entities, so callers
   * must not draw a single entity's icon beside it — and must not treat it as
   * a name they can look up.
   */
  isRemainder: boolean;
}

export interface DistributionInput {
  /** This tab's ranked rows, highest first. */
  rows: DistributionRow[];
  /**
   * Seconds held by *every* row the filters matched, from the list response's
   * `total_seconds`. Never the summary strip's total — see the module note.
   */
  totalSeconds: number;
  /** How many rows matched in total, from the list response's `total`. */
  rowCount: number;
  /** Singular noun for this tab's entity, e.g. "application", "project". */
  dimensionLabel: string;
  colors: string[];
  /** How many entities get their own arc. */
  topN?: number;
}

/**
 * Build the ring's slices: the largest `topN` entities, plus one arc for
 * everything ranked below them.
 *
 * The leftover arc is named for what it actually is — the rest of this tab's
 * own rows, and how many of them — rather than a category. It is never a home
 * for time that no row accounts for: because the whole and the parts now come
 * from the same query, the remainder can only ever be the rows that did not
 * fit on the chart.
 */
export function buildDistribution(input: DistributionInput): DistributionSlice[] {
  const { rows, totalSeconds, rowCount, dimensionLabel, colors, topN = 5 } = input;

  const named: DistributionSlice[] = rows.slice(0, topN).map((row, index) => ({
    label: row.name,
    value: row.seconds / 3600,
    seconds: row.seconds,
    color: colors[index % colors.length],
    isRemainder: false,
  }));

  const shown = named.reduce((sum, slice) => sum + slice.seconds, 0);
  const remainder = Math.round(totalSeconds - shown);
  // Sub-second leftovers are rounding noise in the server's 2dp hours, not an
  // entity anybody spent time in.
  if (rowCount > topN && remainder > 0) {
    const hidden = rowCount - topN;
    named.push({
      label: `Other ${dimensionLabel}${hidden === 1 ? "" : "s"} (${hidden})`,
      value: remainder / 3600,
      seconds: remainder,
      color: "#94A3B8",
      isRemainder: true,
    });
  }
  return named;
}

export interface Coverage {
  /** Seconds this tab's rows actually account for. */
  measuredSeconds: number;
  /** Seconds the timer recorded over the same filters. */
  trackedSeconds: number;
  /** Tracked time that no row attributes. Never negative. */
  unmeasuredSeconds: number;
  /** `measuredSeconds` as a percentage of tracked, or null if nothing tracked. */
  measuredShare: number | null;
  /** Whether there is a gap worth telling the reader about. */
  hasGap: boolean;
}

/**
 * Reconcile what a usage tab measured against what the timer recorded.
 *
 * Only meaningful for the Apps and URLs tabs. On Projects and Tasks the two
 * numbers are the same measure, so there is nothing to reconcile and callers
 * should not render this at all.
 */
export function describeCoverage(
  measuredSeconds: number,
  trackedSeconds: number
): Coverage {
  const unmeasuredSeconds = Math.max(0, Math.round(trackedSeconds - measuredSeconds));
  return {
    measuredSeconds,
    trackedSeconds,
    unmeasuredSeconds,
    measuredShare: trackedSeconds > 0 ? (measuredSeconds / trackedSeconds) * 100 : null,
    hasGap: measuredSeconds > 0 && unmeasuredSeconds > 0,
  };
}
