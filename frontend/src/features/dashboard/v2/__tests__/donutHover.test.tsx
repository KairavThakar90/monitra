// @vitest-environment jsdom
/**
 * The donut and its list are one control: hovering an arc highlights the arc
 * and its row and opens a tooltip; hovering a row does the same from the other
 * side. A row that is only part of an arc (an app folded into "Other apps")
 * highlights that arc and keeps its own name and time in the tooltip.
 */
import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { Donut, Legend, SliceRow, SliceTooltip } from '../charts';

const SLICES = [
  { label: 'Chrome', value: 6, color: '#2563EB' },
  { label: 'Code', value: 2, color: '#0D9488' },
];

const Harness: React.FC = () => {
  const [active, setActive] = useState<string | null>(null);
  return (
    <div>
      <Donut
        size={180}
        slices={SLICES}
        centerLabel="Total App Time"
        centerValue="08:00:00"
        activeLabel={active}
        onActiveChange={setActive}
      />
      <Legend
        activeLabel={active}
        onActiveChange={setActive}
        items={SLICES.map((s) => ({
          label: s.label,
          color: s.color,
          value: `${s.value}h`,
          rawValue: s.value,
        }))}
      />
    </div>
  );
};

/** Two list rows for one arc, as the member dashboard draws apps beyond the named five. */
const FoldedRows: React.FC = () => {
  const [active, setActive] = useState<string | null>(null);
  return (
    <div>
      <Donut
        size={180}
        slices={[...SLICES, { label: 'Other apps', value: 1, color: '#94A3B8' }]}
        centerLabel="Total App Time"
        centerValue="09:00:00"
        activeLabel={active}
        onActiveChange={setActive}
      />
      <SliceRow
        sliceLabel="Other apps"
        activeLabel={active}
        onActiveChange={setActive}
        tooltip={<SliceTooltip label="Widgets" color="#94A3B8" value="00:22:48" share={0.04} note="Counted in Other apps" />}
      >
        <span data-testid="widgets-row">Widgets</span>
      </SliceRow>
    </div>
  );
};

describe('donut and list hover', () => {
  let container: HTMLDivElement;
  let root: Root;

  const arcs = () => Array.from(container.querySelectorAll<SVGCircleElement>('svg g circle'));
  const enter = (el: Element) => el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, clientX: 40, clientY: 300 }));
  const leave = (el: Element) =>
    el.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: document.body }));

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    // jsdom has no IntersectionObserver; the donut uses one to animate in.
    (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it('highlights the hovered arc, dims the rest, and shows its name, time and share', async () => {
    await act(async () => root.render(<Harness />));
    expect(container.querySelector('[role="tooltip"]')).toBeNull();

    await act(async () => { enter(arcs()[0]); });

    expect(arcs()[0].getAttribute('stroke-width')).toBe('22');
    expect(arcs()[1].getAttribute('stroke-width')).toBe('18');
    expect(arcs()[1].getAttribute('opacity')).toBe('0.35');
    const tip = container.querySelector('[role="tooltip"]')!;
    expect(tip.textContent).toContain('Chrome');
    expect(tip.textContent).toContain('06:00:00'); // 6 hours, through formatHoursAsHMS
    expect(tip.textContent).toContain('75.0% of total');
    expect(tip.parentElement!.className).toContain('fixed');

    await act(async () => { leave(arcs()[0]); });
    expect(container.querySelector('[role="tooltip"]')).toBeNull();
    expect(arcs()[1].getAttribute('opacity')).toBe('1');
  });

  it('highlights the matching list row while an arc is hovered', async () => {
    await act(async () => root.render(<Harness />));
    const rows = Array.from(container.querySelectorAll('li'));
    expect(rows[1].className).not.toContain('bg-slate-100');

    await act(async () => { enter(arcs()[1]); });
    expect(rows[1].className).toContain('bg-slate-100');
    expect(rows[0].className).not.toContain('bg-slate-100');

    await act(async () => { leave(arcs()[1]); });
    expect(rows[1].className).not.toContain('bg-slate-100');
  });

  it('highlights the arc and opens the same tooltip when a list row is hovered', async () => {
    await act(async () => root.render(<Harness />));
    const rows = Array.from(container.querySelectorAll('li'));

    await act(async () => { enter(rows[1]); });
    expect(arcs()[1].getAttribute('stroke-width')).toBe('22');
    expect(arcs()[0].getAttribute('opacity')).toBe('0.35');
    const tip = container.querySelector('[role="tooltip"]')!;
    expect(tip.textContent).toContain('Code');
    expect(tip.textContent).toContain('25.0% of total');

    await act(async () => { leave(rows[1]); });
    expect(container.querySelector('[role="tooltip"]')).toBeNull();
    expect(arcs()[0].getAttribute('opacity')).toBe('1');
  });

  it('lets a row that is only part of an arc highlight that arc with its own tooltip', async () => {
    await act(async () => root.render(<FoldedRows />));
    const row = container.querySelector('[data-testid="widgets-row"]')!.parentElement!;

    await act(async () => { enter(row); });
    expect(arcs()[2].getAttribute('stroke-width')).toBe('22'); // the "Other apps" arc
    const tip = container.querySelector('[role="tooltip"]')!;
    expect(tip.textContent).toContain('Widgets');
    expect(tip.textContent).toContain('Counted in Other apps');
  });

  it('closes the arc tooltip when the page scrolls, because the pointer anchor has moved', async () => {
    await act(async () => root.render(<Harness />));
    await act(async () => { enter(arcs()[0]); });
    expect(container.querySelector('[role="tooltip"]')).not.toBeNull();

    await act(async () => { window.dispatchEvent(new Event('scroll')); });
    expect(container.querySelector('[role="tooltip"]')).toBeNull();
    expect(arcs()[1].getAttribute('opacity')).toBe('1');
  });

  it('leaves a legend without onActiveChange static', async () => {
    await act(async () =>
      root.render(<Legend items={[{ label: 'Alpha', color: '#2563EB', value: '1h', rawValue: 1 }]} />),
    );
    await act(async () => { enter(container.querySelector('li')!); });
    expect(container.querySelector('[role="tooltip"]')).toBeNull();
  });
});
