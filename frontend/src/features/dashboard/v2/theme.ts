/**
 * Monitra V2 design tokens.
 *
 * Categorical series colors are validated (lightness band, chroma floor, CVD
 * separation, normal-vision floor, contrast vs. white surface). Assign them by
 * slot order and never cycle past slot 6 — fold the tail into "Other" instead.
 */

export const brand = {
  cyan: "#22D3EE",
  blue: "#2563EB",
  violet: "#7C3AED",
  teal: "#0D9488",
  navy: "#0B1220",
  ink: "#0F172A",
  muted: "#64748B",
  subtle: "#94A3B8",
  line: "#E2E8F0",
  canvas: "#F8FAFC",
  surface: "#FFFFFF",
} as const;

/** Fixed-order categorical slots. Color follows the entity, never its rank. */
export const series = [
  "#2563EB", // 1 blue
  "#0D9488", // 2 teal
  "#7C3AED", // 3 violet
  "#D97706", // 4 amber
  "#DB2777", // 5 pink
  "#4D7C0F", // 6 olive
] as const;

/** Logo gradient, reused for the brand mark and hero accents only. */
export const brandGradient = "linear-gradient(135deg, #22D3EE 0%, #3B82F6 52%, #7C3AED 100%)";

/**
 * Budget-usage color for a fixed-hours project, against the project's own
 * `fixed_hours` -- not a generic 0-100 gauge. Under 80% is in progress, 80-99%
 * is closing in, exactly 100% landed on budget, and above 100% is over budget.
 * Bands, not a gradient: a project is either in one state or another, never
 * "a bit of both". Banded on the same rounded value a row prints, so the label
 * and its color always agree.
 *
 * One definition for every screen that colors a project by its budget (the
 * dashboard's Billable tab and the Task Listing), so a project is the same
 * color wherever it appears.
 */
export const usageColor = (pct: number): string => {
  const shown = Math.round(pct);
  if (shown > 100) return "#EF4444"; // red-500 -- over budget
  if (shown === 100) return "#10B981"; // emerald-500 -- on budget
  if (shown >= 80) return "#EAB308"; // yellow-500 -- closing in
  return "#3B82F6"; // blue-500 -- in progress
};

export const fmtHours = (h: number) => `${Math.floor(h)}h ${Math.round((h % 1) * 60)}m`;
export const fmtNumber = (n: number) => n.toLocaleString("en-US");
