/**
 * The budget-usage colour bands, one definition for every screen that colours a
 * fixed-hours project: blue under 80%, yellow 80-99%, green exactly on budget,
 * red over. Banded on the rounded value a row prints, so a label and its colour
 * always agree.
 */
import { describe, expect, it } from 'vitest';

import { usageColor } from '../theme';
import dashboardSource from '../DashboardV2.tsx?raw';
import taskListingSource from '../../../admin/AdminTaskListing.tsx?raw';

const BLUE = '#3B82F6';
const YELLOW = '#EAB308';
const GREEN = '#10B981';
const RED = '#EF4444';

describe('usageColor', () => {
  it('is blue while a project is in progress (under 80%)', () => {
    expect(usageColor(0)).toBe(BLUE);
    expect(usageColor(50)).toBe(BLUE);
    expect(usageColor(79.4)).toBe(BLUE);
  });

  it('is yellow when closing in (80% up to, not including, 100%)', () => {
    expect(usageColor(79.5)).toBe(YELLOW); // prints as 80%
    expect(usageColor(80)).toBe(YELLOW);
    expect(usageColor(99)).toBe(YELLOW);
    expect(usageColor(99.4)).toBe(YELLOW);
  });

  it('is green exactly on budget', () => {
    expect(usageColor(99.5)).toBe(GREEN); // prints as 100%
    expect(usageColor(100)).toBe(GREEN);
    expect(usageColor(100.4)).toBe(GREEN);
  });

  it('is red once over budget', () => {
    expect(usageColor(100.5)).toBe(RED); // prints as 101%
    expect(usageColor(110)).toBe(RED);
    expect(usageColor(250)).toBe(RED);
  });
});

describe('one colour rule for the dashboard and the Task Listing', () => {
  it('both screens import it from the theme and neither keeps a copy', () => {
    for (const source of [dashboardSource, taskListingSource]) {
      expect(source).toMatch(/usageColor[^;]*from\s+["'][./]+(?:dashboard\/v2\/)?theme["']/);
      expect(source).not.toMatch(/const usageColor\s*=/);
      expect(source).not.toMatch(/#10B981["']\)?;?\s*\/\/ emerald/);
    }
  });
});
