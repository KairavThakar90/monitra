// @vitest-environment jsdom
/**
 * The dashboard's scrolling project lists draw their hover cards `fixed`, so a
 * scroll container cannot clip them: the card shows on hover, is not a child
 * of the row's own box model (position: fixed), and closes when anything scrolls.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { RankedBars, FloatingCard, useHoverAnchor } from '../charts';

const Row: React.FC = () => {
  const { rect, bind } = useHoverAnchor();
  return (
    <div data-testid="row" {...bind}>
      row
      <FloatingCard rect={rect}>
        <span data-testid="card">details</span>
      </FloatingCard>
    </div>
  );
};

describe('hover cards in a scrolling list', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it('opens fixed on hover and closes on leave', async () => {
    await act(async () => root.render(<Row />));
    expect(container.querySelector('[data-testid="card"]')).toBeNull();

    const row = container.querySelector('[data-testid="row"]')!;
    await act(async () => { row.dispatchEvent(new MouseEvent('mouseover', { bubbles: true })); });
    // React's onMouseEnter is driven by mouseover from outside the element.
    const card = container.querySelector('[data-testid="card"]');
    expect(card).not.toBeNull();
    expect((card!.parentElement as HTMLElement).className).toContain('fixed');

    await act(async () => { row.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: document.body })); });
    expect(container.querySelector('[data-testid="card"]')).toBeNull();
  });

  it('closes when the list scrolls, because the row has moved', async () => {
    await act(async () => root.render(<Row />));
    const row = container.querySelector('[data-testid="row"]')!;
    await act(async () => { row.dispatchEvent(new MouseEvent('mouseover', { bubbles: true })); });
    expect(container.querySelector('[data-testid="card"]')).not.toBeNull();
    await act(async () => { window.dispatchEvent(new Event('scroll')); });
    expect(container.querySelector('[data-testid="card"]')).toBeNull();
  });

  it('gives every ranked row a tooltip that is not clipped by the list', async () => {
    await act(async () =>
      root.render(
        <div style={{ maxHeight: 40, overflowY: 'auto' }}>
          <RankedBars
            items={[{ id: '1', name: 'Apollo', value: 3, meta: '', secondary: 72 }]}
            color="#2563EB"
            formatValue={(n) => `${n}h`}
          />
        </div>,
      ),
    );
    const li = container.querySelector('li')!;
    await act(async () => { li.dispatchEvent(new MouseEvent('mouseover', { bubbles: true })); });
    expect(container.textContent).toContain('Activity 72%');
    expect(container.querySelector('.fixed')).not.toBeNull();
  });
});
