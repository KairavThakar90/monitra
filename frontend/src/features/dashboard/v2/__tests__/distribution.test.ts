/**
 * The distribution ring's arithmetic, tested as the defect it fixes.
 *
 * The Reports page once drew a grey "Others" arc holding 94.5% of a month's
 * tracked time. Nothing was wrong with the data behind the five named arcs —
 * the fault was that the ring took its slices from the Apps tab's rows and its
 * *whole* from the summary strip, which counts session time. Every second the
 * desktop had never claimed to attribute to an application therefore ended up
 * in the leftover arc, drawn as though it were a program somebody had used.
 *
 * The first suite below reproduces exactly that shape and asserts it can no
 * longer happen. The rest pin down the properties that keep it from coming
 * back: a remainder that is only ever the rows that did not fit, and a
 * coverage gap that is stated rather than absorbed.
 */
import { describe, expect, it } from "vitest";

import { buildDistribution, describeCoverage } from "../distribution";

const COLORS = ["#F59E0B", "#3B82F6", "#8B5CF6", "#10B981", "#EF4444"];

const apps = (...pairs: [string, number][]) =>
  pairs.map(([name, seconds]) => ({ name, seconds }));

describe("buildDistribution", () => {
  it("does not turn unmeasured session time into an application", () => {
    // The real shape of the bug: a long-tracked range where URL capture saw
    // only a few minutes of it. 45.7 hours tracked, 2.5 hours of browsing.
    const rows = apps(
      ["github.com", 3600],
      ["docs.google.com", 2700],
      ["mail.google.com", 1200],
      ["chatgpt.com", 900],
      ["bing.com", 600],
      ["localhost", 43],
    );
    const measured = 9043;

    const slices = buildDistribution({
      rows,
      totalSeconds: measured,
      rowCount: rows.length,
      dimensionLabel: "url",
      colors: COLORS,
    });

    const remainder = slices.find((slice) => slice.isRemainder);
    // The leftover is the one row that did not fit — 43 seconds — and not the
    // 155,376 seconds of session time the old arithmetic produced.
    expect(remainder?.seconds).toBe(43);
    expect(slices.reduce((sum, slice) => sum + slice.seconds, 0)).toBe(measured);
  });

  it("sums to exactly the whole it was given", () => {
    const rows = apps(["A", 500], ["B", 300], ["C", 100], ["D", 60], ["E", 30], ["F", 10]);
    const slices = buildDistribution({
      rows,
      totalSeconds: 1000,
      rowCount: 6,
      dimensionLabel: "application",
      colors: COLORS,
    });
    expect(slices.reduce((sum, slice) => sum + slice.seconds, 0)).toBe(1000);
  });

  it("draws no leftover arc when every row already has one", () => {
    const rows = apps(["A", 500], ["B", 300]);
    const slices = buildDistribution({
      rows,
      totalSeconds: 800,
      rowCount: 2,
      dimensionLabel: "application",
      colors: COLORS,
    });
    expect(slices).toHaveLength(2);
    expect(slices.some((slice) => slice.isRemainder)).toBe(false);
  });

  it("names the leftover arc for the rows it covers, not as a category", () => {
    const rows = apps(["A", 500], ["B", 300], ["C", 100], ["D", 60], ["E", 30], ["F", 10]);
    const slices = buildDistribution({
      rows,
      totalSeconds: 1000,
      rowCount: 12,
      dimensionLabel: "application",
      colors: COLORS,
    });
    const remainder = slices.at(-1)!;
    expect(remainder.isRemainder).toBe(true);
    expect(remainder.label).toBe("Other applications (7)");
  });

  it("counts rows that were never fetched, not just the ones on the page", () => {
    // The list endpoint caps the page; `rowCount` is the scope-wide count, so
    // the leftover has to speak for every row below the top five.
    const rows = apps(["A", 500], ["B", 300], ["C", 100], ["D", 60], ["E", 30]);
    const slices = buildDistribution({
      rows,
      totalSeconds: 5000,
      rowCount: 250,
      dimensionLabel: "application",
      colors: COLORS,
    });
    expect(slices.at(-1)!.label).toBe("Other applications (245)");
    expect(slices.at(-1)!.seconds).toBe(5000 - 990);
  });

  it("says application rather than applications when one row is left over", () => {
    const rows = apps(["A", 500], ["B", 300], ["C", 100], ["D", 60], ["E", 30], ["F", 10]);
    const slices = buildDistribution({
      rows,
      totalSeconds: 1000,
      rowCount: 6,
      dimensionLabel: "application",
      colors: COLORS,
    });
    expect(slices.at(-1)!.label).toBe("Other application (1)");
  });

  it("never labels a slice with a catch-all category", () => {
    const forbidden = ["others", "other", "unknown", "uncategorized", "unclassified"];
    const rows = apps(["A", 500], ["B", 300], ["C", 100], ["D", 60], ["E", 30], ["F", 10]);
    const slices = buildDistribution({
      rows,
      totalSeconds: 1000,
      rowCount: 20,
      dimensionLabel: "application",
      colors: COLORS,
    });
    for (const slice of slices) {
      expect(forbidden).not.toContain(slice.label.trim().toLowerCase());
    }
  });

  it("draws nothing from nothing", () => {
    expect(
      buildDistribution({
        rows: [],
        totalSeconds: 0,
        rowCount: 0,
        dimensionLabel: "application",
        colors: COLORS,
      })
    ).toEqual([]);
  });

  it("does not invent a leftover from rounding noise", () => {
    const rows = apps(["A", 500], ["B", 300], ["C", 100], ["D", 60], ["E", 30], ["F", 0]);
    const slices = buildDistribution({
      rows,
      totalSeconds: 990,
      rowCount: 6,
      dimensionLabel: "application",
      colors: COLORS,
    });
    expect(slices.some((slice) => slice.isRemainder)).toBe(false);
  });

  it("keeps each named slice's own identity", () => {
    const rows = apps(["Google Chrome", 500], ["Visual Studio Code", 300]);
    const slices = buildDistribution({
      rows,
      totalSeconds: 800,
      rowCount: 2,
      dimensionLabel: "application",
      colors: COLORS,
    });
    expect(slices.map((slice) => slice.label)).toEqual([
      "Google Chrome",
      "Visual Studio Code",
    ]);
    expect(slices.every((slice) => slice.isRemainder === false)).toBe(true);
  });

  it("reports hours alongside seconds for the chart's scale", () => {
    const slices = buildDistribution({
      rows: apps(["A", 5400]),
      totalSeconds: 5400,
      rowCount: 1,
      dimensionLabel: "application",
      colors: COLORS,
    });
    expect(slices[0].value).toBe(1.5);
  });
});

describe("describeCoverage", () => {
  it("reports the gap between what was measured and what was tracked", () => {
    const coverage = describeCoverage(9043, 164419);
    expect(coverage.unmeasuredSeconds).toBe(155376);
    expect(coverage.measuredShare).toBeCloseTo(5.5, 1);
    expect(coverage.hasGap).toBe(true);
  });

  it("has nothing to report when the two agree", () => {
    const coverage = describeCoverage(3600, 3600);
    expect(coverage.unmeasuredSeconds).toBe(0);
    expect(coverage.hasGap).toBe(false);
  });

  it("never reports a negative gap", () => {
    // Application time can exceed session time in a range where a segment
    // straddles the boundary. That is not "negative unmeasured time".
    const coverage = describeCoverage(4000, 3600);
    expect(coverage.unmeasuredSeconds).toBe(0);
    expect(coverage.hasGap).toBe(false);
  });

  it("has no share to report when nothing was tracked at all", () => {
    expect(describeCoverage(0, 0).measuredShare).toBeNull();
  });

  it("says nothing when there is no measured usage to reconcile", () => {
    // An honest empty state, not a claim that 100% of the day is unattributed.
    expect(describeCoverage(0, 3600).hasGap).toBe(false);
  });
});
