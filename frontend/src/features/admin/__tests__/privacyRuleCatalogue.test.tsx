// @vitest-environment jsdom
/**
 * The rules that have been added, shown on Screenshot Privacy until a member is
 * picked: an Applications tab and a Websites tab, newest first.
 *
 * Read-only by design -- it lists the catalogue. What matters: every added rule
 * is visible with the details that identify it, the tab that opens is one that
 * has something in it, search narrows within a tab, and an empty catalogue or an
 * empty tab says so instead of showing a bare table.
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { ScreenshotApplication, ScreenshotUrl } from '../../../api/screenshotPrivacy';
import { PrivacyRuleCatalogue } from '../PrivacyRuleCatalogue';

const app = (id: number, name: string, extra: Partial<ScreenshotApplication> = {}): ScreenshotApplication => ({
  id, name, process_name: `${name.toLowerCase().replace(/\s+/g, '')}.exe`, category: 'Communication', is_active: true,
  created_at: '2026-10-01T05:00:00+00:00', updated_at: '2026-10-01T05:00:00+00:00', ...extra,
});
const site = (id: number, name: string, extra: Partial<ScreenshotUrl> = {}): ScreenshotUrl => ({
  id, name, domain: `${name.toLowerCase().replace(/\s+/g, '')}.com`, url_pattern: `https://${name.toLowerCase()}.com/*`,
  category: 'Social', is_active: true,
  created_at: '2026-10-01T05:00:00+00:00', updated_at: '2026-10-01T05:00:00+00:00', ...extra,
});

describe('PrivacyRuleCatalogue', () => {
  let container: HTMLDivElement;
  let root: Root;

  const render = async (applications: ScreenshotApplication[], websites: ScreenshotUrl[]) => {
    await act(async () => { root.render(<PrivacyRuleCatalogue applications={applications} websites={websites} />); });
  };
  const tabs = () => Array.from(container.querySelectorAll('[role="tab"]')) as HTMLButtonElement[];
  const tab = (label: string) => tabs().find((t) => t.textContent?.startsWith(label)) as HTMLButtonElement;
  const click = async (el: Element) => { await act(async () => { (el as HTMLElement).click(); }); };
  const rows = () => Array.from(container.querySelectorAll('tbody tr')) as HTMLTableRowElement[];
  const names = () => rows().map((row) => row.querySelector('td span.truncate')?.textContent ?? '');
  const headers = () => Array.from(container.querySelectorAll('thead th')).map((th) => th.textContent);
  const search = () => container.querySelector('input[type="text"]') as HTMLInputElement;
  const type = async (value: string) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    await act(async () => { setter.call(search(), value); search().dispatchEvent(new Event('input', { bubbles: true })); });
  };
  const text = () => container.textContent ?? '';

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

  it('shows Applications and Websites tabs with how many of each there are', async () => {
    await render([app(1, 'Slack'), app(2, 'Teams')], [site(3, 'Reddit')]);

    expect(tabs().map((t) => t.textContent)).toEqual(['Applications2', 'Websites1']);
    expect(tab('Applications').getAttribute('aria-selected')).toBe('true');
    expect(container.querySelector('[role="tablist"]')?.getAttribute('aria-label')).toBe('Rule type');
  });

  it('lists each application with its process, category, status and the date it was added', async () => {
    await render([app(1, 'Slack', { process_name: 'slack.exe', category: 'Communication', created_at: '2026-10-05T10:00:00+00:00' })], []);

    expect(headers()).toEqual(['Application', 'Process', 'Category', 'Status', 'Added']);
    const row = rows()[0].textContent ?? '';
    expect(row).toContain('Slack');
    expect(row).toContain('slack.exe');
    expect(row).toContain('Communication');
    expect(row).toContain('Active');
    expect(row).toContain('05 Oct 2026');
  });

  it('lists each website with its domain and URL pattern on the Websites tab', async () => {
    await render([app(1, 'Slack')], [site(2, 'Reddit', { domain: 'reddit.com', url_pattern: 'https://www.reddit.com/*', category: 'Social' })]);
    await click(tab('Websites'));

    expect(tab('Websites').getAttribute('aria-selected')).toBe('true');
    expect(headers()).toEqual(['Website', 'Domain', 'URL pattern', 'Category', 'Status', 'Added']);
    const row = rows()[0].textContent ?? '';
    expect(row).toContain('Reddit');
    expect(row).toContain('reddit.com');
    expect(row).toContain('https://www.reddit.com/*');
    expect(row).toContain('Social');
    expect(text()).not.toContain('Slack');
  });

  it('marks a rule that is switched off as Inactive', async () => {
    await render([app(1, 'Slack'), app(2, 'Zoom', { is_active: false })], []);

    const zoom = rows().find((row) => row.textContent?.includes('Zoom'))!;
    expect(zoom.textContent).toContain('Inactive');
    expect(rows().find((row) => row.textContent?.includes('Slack'))!.textContent).toContain('Active');
  });

  it('puts the newest rule first, however the server ordered them', async () => {
    await render([
      app(1, 'Oldest', { created_at: '2026-09-01T00:00:00+00:00' }),
      app(3, 'Newest', { created_at: '2026-10-05T00:00:00+00:00' }),
      app(2, 'Middle', { created_at: '2026-09-20T00:00:00+00:00' }),
    ], []);

    expect(names()).toEqual(['Newest', 'Middle', 'Oldest']);
  });

  it('breaks a tie on the date by the later id', async () => {
    await render([app(4, 'Earlier id'), app(9, 'Later id')], []);

    expect(names()).toEqual(['Later id', 'Earlier id']);
  });

  it('opens on Websites when there are no applications to show', async () => {
    await render([], [site(1, 'Reddit')]);

    expect(tab('Websites').getAttribute('aria-selected')).toBe('true');
    expect(names()).toEqual(['Reddit']);
  });

  it('stays on the tab the reader chose', async () => {
    await render([app(1, 'Slack')], [site(2, 'Reddit')]);
    await click(tab('Websites'));
    await render([app(1, 'Slack'), app(3, 'Teams')], [site(2, 'Reddit')]);

    expect(tab('Websites').getAttribute('aria-selected')).toBe('true');
  });

  it('narrows by name, process, domain or category, and says when nothing matches', async () => {
    await render(
      [app(1, 'Slack', { category: 'Communication' }), app(2, 'Notepad', { category: 'Utility', process_name: 'notepad.exe' })],
      [],
    );

    await type('slack');
    expect(names()).toEqual(['Slack']);
    await type('NOTEPAD.EXE');
    expect(names()).toEqual(['Notepad']);
    await type('utility');
    expect(names()).toEqual(['Notepad']);
    await type('zzz');
    expect(rows()).toHaveLength(0);
    expect(text()).toContain('Nothing matches this search.');
  });

  it('searches websites by domain and URL pattern', async () => {
    await render([app(1, 'Slack')], [site(2, 'Reddit', { domain: 'reddit.com' }), site(3, 'News', { domain: 'bbc.co.uk', url_pattern: 'https://bbc.co.uk/news/*' })]);
    await click(tab('Websites'));

    await type('bbc');
    expect(names()).toEqual(['News']);
    await type('reddit.com');
    expect(names()).toEqual(['Reddit']);
  });

  it('clears the search when the tab changes', async () => {
    await render([app(1, 'Slack')], [site(2, 'Reddit')]);
    await type('slack');

    await click(tab('Websites'));

    expect(search().value).toBe('');
    expect(search().placeholder).toBe('Search websites…');
    expect(names()).toEqual(['Reddit']);
  });

  it('says so when one tab has no rules yet', async () => {
    await render([app(1, 'Slack')], []);
    await click(tab('Websites'));

    expect(rows()).toHaveLength(0);
    expect(text()).toContain('No website rules yet');
  });

  it('says so when no rule has been added at all, and still points at the member picker', async () => {
    await render([], []);

    expect(container.querySelector('table')).toBeNull();
    expect(text()).toContain('No privacy rules yet');
    expect(text()).toContain('No member selected');
  });

  it('tells the reader to pick a member to switch these on or off', async () => {
    await render([app(1, 'Slack')], []);

    expect(text()).toContain('No member selected');
    expect(text()).toMatch(/choose one above to switch these on or off/);
  });

  it('gives a long name its full text as a tooltip', async () => {
    const long = 'A'.repeat(120);
    await render([app(1, long)], []);

    expect(rows()[0].querySelector('td span.truncate')?.getAttribute('title')).toBe(long);
  });
});
