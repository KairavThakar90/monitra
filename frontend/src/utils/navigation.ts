/**
 * Sidebar navigation modifiers.
 *
 * The sidebar entries are `<button>`s rather than links, so the browser's own
 * "Ctrl-click opens a new tab" behaviour never applies to them. These two
 * helpers give the shells that behaviour without each one re-deriving which
 * modifier means "new tab": Ctrl on Windows and Linux, ⌘ (Meta) on macOS, which
 * is where the same gesture lives there.
 */

/** The only part of a click event this decision depends on. */
export type NewTabModifiers = Pick<MouseEvent, "ctrlKey" | "metaKey">;

/** True when a click on a navigation entry should open a new tab instead. */
export const opensInNewTab = (event: NewTabModifiers): boolean =>
  event.ctrlKey || event.metaKey;

/**
 * Open an in-app route in a new tab.
 *
 * The path is resolved against the current document so a route is opened on
 * this origin whatever the app is served from, and `noopener` keeps the new
 * tab from holding a handle on this one.
 */
export const openPathInNewTab = (path: string): void => {
  window.open(new URL(path, window.location.href).toString(), "_blank", "noopener,noreferrer");
};
