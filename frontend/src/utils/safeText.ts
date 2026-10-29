import { looksLikeJsonDocument, validateSearchTerm } from '../validation/validators';

/**
 * The text to show for a stored label -- a member's name or designation, a project or task name --
 * and never anything that is not one.
 *
 * A record can hold a value the input rules would refuse today: one saved before they existed, or
 * written by an identity provider that sent an object where text was expected and had it turned into
 * `{"first":"Ravi"}`. React escapes whatever it is given, so none of that can run -- but the Members
 * page then showed the raw JSON as the person's name. This returns the fallback instead, for
 *
 *  - anything that is not a string (an object is never `String()`-ed into a label), and
 *  - a string that is, in its entirety, a JSON object or array.
 *
 * Ordinary text -- including text that merely mentions braces mid-sentence -- is returned as it is,
 * and display never alters what is stored: editing the record still shows the real value.
 */
export function safeLabel(value: unknown, fallback = '-'): string {
  if (typeof value !== 'string') return fallback;
  const text = value.trim();
  if (!text) return fallback;
  return looksLikeJsonDocument(text) ? fallback : value;
}

/**
 * A text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is read by
 * Excel and Sheets as a formula, so a member name, task name or window title of
 * `=HYPERLINK(...)` would run when somebody opens the export (CSV injection). Such text is
 * prefixed with an apostrophe, which spreadsheets show as nothing and treat as "this is text".
 * Real numbers (including negative ones), the bare `-` used for "no value" and numeric-looking
 * strings are left exactly as they were.
 */
export const neutraliseSpreadsheetFormula = (cell: string | number): string => {
  const text = String(cell);
  if (typeof cell === 'number') return text;
  if (!/^[=+\-@\t\r]/.test(text)) return text;
  if (text === '-' || /^[+-]?\d+(?:[.,]\d+)*$/.test(text)) return text;
  return `'${text}`;
};

/**
 * A search box's text, ready to send as a query parameter: the validated term, or nothing at all when it
 * is refused (markup, control characters, too long). A refused term is never sent -- the list simply is not
 * narrowed -- and what the person typed stays in the box so they can see and correct it.
 */
export const searchQuery = (term: string): string => {
  const checked = validateSearchTerm(term);
  return checked.ok ? checked.value : '';
};
