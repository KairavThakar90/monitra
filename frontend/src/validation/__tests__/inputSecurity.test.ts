/**
 * Hostile and legitimate input against the browser's own defences.
 *
 * These are the client's half of the contract (immediate, friendly feedback). The backend's half --
 * the one that is actually a control -- is `backend/tests/test_input_security_api.py`, which posts the
 * same payloads straight to the API with no client involved. Both lists are kept in step on purpose.
 */
import { describe, expect, it } from 'vitest';

import { formatApiError } from '../../api/utils';
import { neutraliseSpreadsheetFormula, safeLabel } from '../../utils/safeText';
import {
  validateDescription, validateEmail, validateName, validatePlainText, validateSearchTerm, validateUrl,
} from '..';

const HOSTILE = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  'javascript:alert(document.cookie)',
  'Ravi" onmouseover="alert(1)',
  '{"role": "administrator", "permissions": {"all": true}}',
  '["a", "b"]',
  "<?xml version='1.0'?><a/>",
  '{{7*7}}',
  '&lt;script&gt;alert(1)&lt;/script&gt;',
  'Ravi\u0000Acharya',
  'Ravi\u0007Acharya',
  '!!!',
];

const LEGITIMATE = [
  'Ravi Acharya', "Sinéad O'Connor", 'Zoë Müller-Smith', 'रवि आचार्य', 'હાર્દિક રાવલ', 'محمد علي', '王小明',
  'Иван Петров', 'A. B. Rao Jr.', 'R&D / Prototype #4 (50% done)', 'Fix the login page - Non billable',
];

describe('names', () => {
  it.each(HOSTILE)('refuses %j', (value) => {
    expect(validateName(value).ok).toBe(false);
  });
  it.each(LEGITIMATE)('accepts %s', (value) => {
    const result = validateName(value);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value).toBe(value.trim());
  });
  it('refuses values that are not text instead of turning them into text', () => {
    for (const value of [{ a: 1 }, ['a'], 5, true]) expect(validateName(value).ok).toBe(false);
  });
  it('never quotes what was typed back in its message', () => {
    const result = validateName('<script>alert(1)</script>');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).not.toContain('<script>');
      expect(result.error).not.toMatch(/regex|pattern|\^|\$/i);
    }
  });
});

describe('descriptions and reasons', () => {
  it.each([
    '<script>alert(1)</script>', 'bad\u0000text', '{"a": 1}', '<?xml version="1.0"?>', '&#60;script&#62;',
  ])('refuses %j', (value) => {
    expect(validateDescription(value).ok).toBe(false);
  });
  it('accepts ordinary prose with punctuation, numbers and several scripts', () => {
    const prose = "Fixed the login timeout (ticket #4521). Cost is $5, 5 < 10, a -> b, 100% of users: O'Brien & Sons.\nદિવસ સારો રહ્યો.";
    expect(validateDescription(prose).ok).toBe(true);
  });
  it('refuses code with no markup when the field must be prose', () => {
    for (const value of ['$(".x").hide()', "'; DROP TABLE tasks; --", "x' OR '1'='1", 'alert(document.cookie)']) {
      expect(validateDescription(value, { rejectCode: true }).ok).toBe(false);
    }
  });
});

describe('other fields', () => {
  it('refuses a malformed or control-character email', () => {
    for (const value of ['a@b.com\u0000', 'no-at-sign', 'two@@example.com', 'a b@example.com', 'a@b']) {
      expect(validateEmail(value).ok).toBe(false);
    }
    expect(validateEmail('Ravi@Example.COM').ok).toBe(true);
  });
  it('refuses script URLs and accepts http(s) ones', () => {
    for (const value of ['javascript:alert(1)', 'data:text/html,<b>x</b>', 'file:///etc/passwd', '//evil.example', 'ftp://x.y']) {
      expect(validateUrl(value).ok).toBe(false);
    }
    expect(validateUrl('https://example.com/notes?a=1&b=2').ok).toBe(true);
  });
  it('lets a search term carry SQL-looking text (it is data) but not markup or control characters', () => {
    for (const value of ["' OR 1=1 --", '100%', "'; DROP TABLE users; --"]) expect(validateSearchTerm(value).ok).toBe(true);
    expect(validateSearchTerm('<script>').ok).toBe(false);
    expect(validateSearchTerm('x'.repeat(101)).ok).toBe(false);
  });
  it('keeps single-line text on one line', () => {
    const result = validatePlainText('one\ntwo');
    expect(result.ok && result.value).toBe('one two');
  });
});

describe('error messages are shown as sentences, never as raw JSON', () => {
  it('uses a string detail as it is', () => {
    expect(formatApiError({ detail: 'Email already in use' })).toBe('Email already in use');
  });
  it('joins a list of field errors', () => {
    expect(formatApiError({ detail: [{ loc: ['body', 'name'], msg: 'Name is required.', type: 'value_error' }] }))
      .toBe('name: Name is required.');
  });
  it('uses the message of an object detail', () => {
    expect(formatApiError({ detail: { message: 'Not allowed', code: 'X' } })).toBe('Not allowed');
  });
  it('falls back to the default, not JSON, for an object with no sentence in it', () => {
    const shown = formatApiError({ detail: { code: 'E42', trace: ['a', 'b'] } }, 'Could not save.');
    expect(shown).toBe('Could not save.');
    expect(shown).not.toContain('{');
  });
  it('falls back for a list with nothing readable', () => {
    expect(formatApiError({ detail: [{ nothing: 1 }, null, 3] }, 'Could not save.')).toBe('Could not save.');
  });
});

describe('a stored label is never raw JSON on screen', () => {
  it('shows ordinary text as it is', () => {
    expect(safeLabel('Ravi Acharya')).toBe('Ravi Acharya');
    expect(safeLabel('Uses {braces} mid-sentence')).toBe('Uses {braces} mid-sentence');
  });
  it('replaces a whole JSON document, an object, or nothing with the fallback', () => {
    expect(safeLabel('{"first": "Ravi", "last": "Acharya"}')).toBe('-');
    expect(safeLabel('["a"]', 'Unnamed')).toBe('Unnamed');
    expect(safeLabel({ first: 'Ravi' })).toBe('-');
    expect(safeLabel(null)).toBe('-');
    expect(safeLabel('   ')).toBe('-');
  });
});

describe('spreadsheet exports cannot run a formula', () => {
  it.each(['=HYPERLINK("http://evil","x")', '+cmd|calc', '-2+3', '@SUM(A1)', '\tinjected', '\rinjected'])(
    'prefixes %j', (value) => {
      expect(neutraliseSpreadsheetFormula(value).startsWith("'")).toBe(true);
    });
  it('leaves numbers, the "no value" dash and normal text alone', () => {
    expect(neutraliseSpreadsheetFormula(-5)).toBe('-5');
    expect(neutraliseSpreadsheetFormula('-')).toBe('-');
    expect(neutraliseSpreadsheetFormula('-5')).toBe('-5');
    expect(neutraliseSpreadsheetFormula('+91')).toBe('+91');
    expect(neutraliseSpreadsheetFormula('Ravi Acharya')).toBe('Ravi Acharya');
  });
});
