// @vitest-environment jsdom
/**
 * The shared list footer -- the Members page's pagination, now also Assign
 * Tasks'. "Showing a to b of N <noun>", a page-size picker, numbered pages with
 * ellipses, and the previous/next arrows.
 */
import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { Pagination } from '../Pagination';

const Harness: React.FC<{ total: number; noun?: string; pageSizes?: number[] }> = ({ total, noun = 'members', pageSizes }) => {
  const [page, setPage] = useState(1);
  const [limit, setLimit] = useState(pageSizes?.[0] ?? 20);
  const totalPages = Math.max(1, Math.ceil(total / limit));
  return (
    <Pagination
      page={Math.min(page, totalPages)}
      totalPages={totalPages}
      totalItems={total}
      limit={limit}
      setPage={setPage}
      setLimit={setLimit}
      noun={noun}
      pageSizes={pageSizes}
    />
  );
};

describe('Pagination', () => {
  let container: HTMLDivElement;
  let root: Root;

  const render = async (ui: React.ReactElement) => {
    await act(async () => { root.render(ui); });
  };
  const click = async (el: Element | undefined) => {
    expect(el).toBeTruthy();
    await act(async () => { el!.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  };
  const pageButtons = () =>
    Array.from(container.querySelectorAll('button')).filter((b) => /^\d+$/.test(b.textContent ?? ''));
  const text = () => container.textContent ?? '';
  const arrow = (label: string) => container.querySelector(`button[aria-label="${label}"]`) as HTMLButtonElement;

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

  it('says which rows are showing, in the noun it is given', async () => {
    await render(<Harness total={66} />);
    expect(text()).toContain('Showing 1 to 20 of 66 members');

    await render(<Harness key="projects" total={25} noun="projects" pageSizes={[10, 20, 50]} />);
    expect(text()).toContain('Showing 1 to 10 of 25 projects');
  });

  it('offers the page sizes it is given and defaults to the Members sizes', async () => {
    await render(<Harness total={66} />);
    const sizes = (container.querySelector('select[aria-label="Rows per page"]') as HTMLSelectElement).options;
    expect(Array.from(sizes).map((o) => o.value)).toEqual(['12', '20', '50', '100']);

    await render(<Harness key="own-sizes" total={66} pageSizes={[10, 20, 50]} />);
    const own = (container.querySelector('select[aria-label="Rows per page"]') as HTMLSelectElement).options;
    expect(Array.from(own).map((o) => o.value)).toEqual(['10', '20', '50']);
  });

  it('moves with the numbers and the arrows, and disables an arrow at either end', async () => {
    await render(<Harness total={66} />); // 4 pages of 20
    expect(arrow('Previous page').disabled).toBe(true);
    expect(arrow('Next page').disabled).toBe(false);

    await click(arrow('Next page'));
    expect(text()).toContain('Showing 21 to 40 of 66');
    await click(pageButtons().find((b) => b.textContent === '4'));
    expect(text()).toContain('Showing 61 to 66 of 66');
    expect(arrow('Next page').disabled).toBe(true);
  });

  it('shows an ellipsis rather than every page when there are many', async () => {
    await render(<Harness total={400} />); // 20 pages
    expect(container.querySelectorAll('span').length).toBeGreaterThan(0);
    expect(text()).toContain('...');
    expect(pageButtons().map((b) => b.textContent)).toEqual(['1', '2', '3', '4', '5', '20']);
  });

  it('goes back to the first page when the page size changes', async () => {
    await render(<Harness total={66} />);
    await click(arrow('Next page'));
    expect(text()).toContain('Showing 21 to 40');

    const select = container.querySelector('select[aria-label="Rows per page"]') as HTMLSelectElement;
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(select, '50');
    await act(async () => { select.dispatchEvent(new Event('change', { bubbles: true })); });

    expect(text()).toContain('Showing 1 to 50 of 66');
  });
});
