import { describe, expect, it } from 'vitest';

import {
  ALLOWED_URL_SCHEMES,
  DESCRIPTION_MAX_LENGTH,
  EMAIL_MAX_LENGTH,
  IDENTIFIER_MIN,
  MAX_LIST_PARAM_ITEMS,
  NAME_MAX_LENGTH,
  NAME_MIN_LENGTH,
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_LENGTH,
  PLAIN_TEXT_MAX_LENGTH,
  SEARCH_MAX_LENGTH,
  URL_MAX_LENGTH,
  DOMAIN_MAX_LENGTH,
} from '../rules';
import {
  collapseWhitespace,
  normalizeEmail,
  normalizeNewlines,
  normalizeText,
  normalizeUnicode,
} from '../sanitizer';
import {
  findStructuredContent,
  looksLikeJsonDocument,
  validateCredential,
  validateDate,
  validateDecimal,
  validateDescription,
  validateDomain,
  validateEmail,
  validateEnum,
  validateIdentifier,
  validateInteger,
  validateName,
  validatePassword,
  validatePlainText,
  validateSearchTerm,
  validateTimeOfDay,
  validateUrl,
} from '../validators';
import { validateBySpec } from '../useFormValidation';

/** Assert a validator accepted, and hand back the normalised value. */
function accepted<T>(result: { ok: boolean; value?: T; error?: string }): T {
  expect(result.error ?? null).toBeNull();
  expect(result.ok).toBe(true);
  return result.value as T;
}

/** Assert a validator rejected, and hand back the user-facing message. */
function rejected(result: { ok: boolean; error?: string }): string {
  expect(result.ok).toBe(false);
  expect(typeof result.error).toBe('string');
  return result.error as string;
}

// ---------------------------------------------------------------------------

describe('the shared limits match the backend catalogue', () => {
  it('carries the same numbers backend/app/core/validation/rules.py declares', () => {
    expect(NAME_MAX_LENGTH).toBe(150);
    expect(NAME_MIN_LENGTH).toBe(1);
    expect(DESCRIPTION_MAX_LENGTH).toBe(5000);
    expect(PLAIN_TEXT_MAX_LENGTH).toBe(255);
    expect(EMAIL_MAX_LENGTH).toBe(254);
    expect(PASSWORD_MIN_LENGTH).toBe(8);
    expect(PASSWORD_MAX_LENGTH).toBe(128);
    expect(SEARCH_MAX_LENGTH).toBe(100);
    expect(URL_MAX_LENGTH).toBe(2048);
    expect(DOMAIN_MAX_LENGTH).toBe(255);
    expect(IDENTIFIER_MIN).toBe(1);
    expect(MAX_LIST_PARAM_ITEMS).toBe(200);
    expect([...ALLOWED_URL_SCHEMES]).toEqual(['http', 'https']);
  });
});

describe('sanitizer', () => {
  it('normalises Unicode to NFC so the two spellings of an accent compare equal', () => {
    const decomposed = 'Jose\u0301'; // 'e' + combining acute
    expect(decomposed.length).toBe(5);
    expect(normalizeUnicode(decomposed)).toBe('José');
    expect(normalizeUnicode(decomposed).length).toBe(4);
  });

  it('collapses CRLF and bare CR to a single newline form', () => {
    expect(normalizeNewlines('a\r\nb\rc')).toBe('a\nb\nc');
  });

  it('turns newlines into spaces for single-line fields, and keeps them otherwise', () => {
    expect(normalizeText('a\nb')).toBe('a b');
    expect(normalizeText('a\nb', { allowNewlines: true })).toBe('a\nb');
    expect(normalizeText('  padded  ')).toBe('padded');
  });

  it('lower-cases only the email domain, never the local part', () => {
    expect(normalizeEmail('  Alice.B@Example.COM ')).toBe('Alice.B@example.com');
  });

  it('squeezes runs of spaces without touching newlines', () => {
    expect(collapseWhitespace('Acme   Corp')).toBe('Acme Corp');
    expect(collapseWhitespace('one  two\nthree   four')).toBe('one two\nthree four');
  });
});

// ---------------------------------------------------------------------------

describe('validateName — accepts real names', () => {
  const accept = [
    ['a plain label', 'Website Redesign'],
    ['an apostrophe', "O'Brien"],
    ['a hyphen', 'Smith-Jones'],
    ['accents', 'José Álvarez'],
    ['non-Latin script', '山田 太郎'],
    ['Cyrillic', 'Анна Петрова'],
    ['a numeric comparison', '5 < 10 target'],
    ['an arrow', 'a -> b handoff'],
    ['a percentage', '50% complete'],
    ['ordinary punctuation', 'Q3 (Phase 2): rollout, v1.2 — final!'],
    ['an ampersand', 'Design & Build'],
  ] as const;

  for (const [what, value] of accept) {
    it(`accepts ${what}`, () => {
      expect(accepted(validateName(value)).length).toBeGreaterThan(0);
    });
  }

  it('normalises surrounding and repeated whitespace', () => {
    expect(accepted(validateName('  Acme   Corp  '))).toBe('Acme Corp');
  });

  it('composes Unicode so the same name is stored one way', () => {
    expect(accepted(validateName('José'))).toBe('José');
  });
});

describe('validateName — rejects structured content', () => {
  const reject = [
    ['a script tag', '<script>alert(1)</script>'],
    ['a div', '<div>test</div>'],
    ['an event handler in a tag', '<img src=x onerror=alert(1)>'],
    ['a self-closing tag', 'line<br/>break'],
    ['an encoded tag', '&lt;script&gt;'],
    ['a template expression', '${7*7}'],
    ['a handlebars expression', '{{constructor}}'],
    ['an ERB expression', '<% system("rm") %>'],
    ['an XML prolog', '<?xml version="1.0"?>'],
    ['a doctype', '<!DOCTYPE html>'],
    ['a CDATA section', '<![CDATA[x]]>'],
    ['a javascript URI', 'javascript:alert(1)'],
    ['a data URI', 'data:text/html;base64,PHN2Zz4='],
  ] as const;

  for (const [what, value] of reject) {
    it(`rejects ${what}`, () => {
      const message = rejected(validateName(value));
      expect(message).not.toMatch(/[\\^$*+?]/);
    });
  }

  it('rejects a whole-value JSON object', () => {
    rejected(validateName('{"key":"value"}'));
  });

  it('rejects a whole-value JSON array', () => {
    rejected(validateName('["a","b"]'));
  });

  it('rejects an empty or whitespace-only name as required', () => {
    expect(rejected(validateName(''))).toBe('Name is required.');
    expect(rejected(validateName('   '))).toBe('Name is required.');
  });

  it('rejects a non-string', () => {
    rejected(validateName(42));
    rejected(validateName(null));
  });

  it('uses the field label the caller gave it', () => {
    expect(rejected(validateName('', { fieldLabel: 'Project name' }))).toBe(
      'Project name is required.',
    );
  });
});

describe('validateName — length boundaries', () => {
  it('accepts exactly the maximum', () => {
    expect(accepted(validateName('x'.repeat(NAME_MAX_LENGTH))).length).toBe(NAME_MAX_LENGTH);
  });

  it('rejects one character over the maximum with a plain message', () => {
    expect(rejected(validateName('x'.repeat(NAME_MAX_LENGTH + 1)))).toBe(
      'Name must be at most 150 characters.',
    );
  });

  it('accepts the shortest possible name', () => {
    expect(accepted(validateName('x'))).toBe('x');
  });

  it('honours a narrower caller-supplied maximum', () => {
    rejected(validateName('abcdefghijk', { maxLength: 10 }));
  });
});

// ---------------------------------------------------------------------------

describe('validateDescription', () => {
  it('keeps line breaks', () => {
    expect(accepted(validateDescription('first\nsecond'))).toBe('first\nsecond');
  });

  it('accepts an inline mention of JSON mid-sentence', () => {
    const text = 'The API returned {"a":1} which is wrong.';
    expect(accepted(validateDescription(text))).toBe(text);
  });

  it('accepts prose with comparisons, arrows and percentages', () => {
    const text = 'If 5 < 10 then a -> b, and we are 50% done.';
    expect(accepted(validateDescription(text))).toBe(text);
  });

  it('rejects a value that is entirely a JSON document', () => {
    expect(rejected(validateDescription('{"key":"value"}'))).toBe(
      'Description: This field expects plain text, not JSON.',
    );
  });

  it('rejects markup with a message naming the field and no regex', () => {
    expect(rejected(validateDescription('<div>test</div>'))).toBe(
      'Description: HTML or XML tags are not allowed here.',
    );
  });

  it('treats blank as absent when optional, and as an error when required', () => {
    expect(accepted(validateDescription(''))).toBe('');
    expect(accepted(validateDescription(null))).toBe('');
    expect(rejected(validateDescription('  ', { required: true, fieldLabel: 'Reason' }))).toBe(
      'Reason is required.',
    );
  });

  it('enforces the 5000-character boundary', () => {
    accepted(validateDescription('x'.repeat(DESCRIPTION_MAX_LENGTH)));
    expect(rejected(validateDescription('x'.repeat(DESCRIPTION_MAX_LENGTH + 1)))).toBe(
      'Description must be at most 5000 characters.',
    );
  });
});

describe('validatePlainText', () => {
  it('folds a newline into a space so a second line cannot be smuggled in', () => {
    expect(accepted(validatePlainText('one\ntwo'))).toBe('one two');
  });

  it('enforces the 255-character boundary', () => {
    accepted(validatePlainText('x'.repeat(PLAIN_TEXT_MAX_LENGTH)));
    rejected(validatePlainText('x'.repeat(PLAIN_TEXT_MAX_LENGTH + 1)));
  });
});

// ---------------------------------------------------------------------------

describe('control characters are rejected on the raw input, not stripped', () => {
  const payloads: Array<[string, string]> = [
    ['a NUL byte', 'bad\u0000name'],
    ['a backspace', 'bad\u0008name'],
    ['a vertical tab', 'bad\u000bname'],
    ['a form feed', 'bad\u000cname'],
    ['an ANSI escape', 'bad\u001bname'],
    ['a DEL', 'bad\u007fname'],
  ];

  for (const [what, payload] of payloads) {
    it(`rejects ${what} rather than scrubbing it`, () => {
      const message = rejected(validateName(payload));
      expect(message).toBe('Name contains unsupported characters.');
    });
  }

  it('rejects a control character in a description too', () => {
    rejected(validateDescription('a\u0000b'));
  });

  it('rejects a control character in an email, a URL and a search box', () => {
    rejected(validateEmail('a\u0000b@example.com'));
    rejected(validateUrl('https://example.com/\u0000'));
    rejected(validateSearchTerm('a\u0000b'));
  });

  it('still allows tab and newline, which are ordinary typing', () => {
    accepted(validateDescription('a\tb\nc'));
    accepted(validateName('a\tb'));
  });
});

// ---------------------------------------------------------------------------

describe('validateEmail', () => {
  it('accepts an ordinary address and lower-cases only the domain', () => {
    expect(accepted(validateEmail(' Alice.B@Example.COM '))).toBe('Alice.B@example.com');
  });

  it('accepts a plus-addressed and a subdomain address', () => {
    accepted(validateEmail('alice+tag@mail.example.co.uk'));
  });

  const bad = ['', '   ', 'not-an-email', 'missing@domain', 'two@@at.com', 'spaced out@x.com', '@x.com', 'a@.com'];
  for (const value of bad) {
    it(`rejects ${JSON.stringify(value)}`, () => {
      rejected(validateEmail(value));
    });
  }

  it('says exactly "Please enter a valid email address."', () => {
    expect(rejected(validateEmail('nope'))).toBe('Please enter a valid email address.');
  });

  it('enforces the 254-character boundary', () => {
    const local = 'a'.repeat(EMAIL_MAX_LENGTH - '@example.com'.length);
    accepted(validateEmail(`${local}@example.com`));
    rejected(validateEmail(`${local}x@example.com`));
  });
});

// ---------------------------------------------------------------------------

describe('validatePassword — length policy only, value untouched', () => {
  it('accepts every special character a password may legitimately contain', () => {
    const secret = '<>{}[]!@#$%^&*()_+-=|\\/~`"\'';
    expect(accepted(validatePassword(secret))).toBe(secret);
  });

  it('accepts a password that looks exactly like a script tag', () => {
    const secret = '<script>alert(1)</script>';
    expect(accepted(validatePassword(secret))).toBe(secret);
  });

  it('accepts a password that is a whole JSON document', () => {
    const secret = '{"key":"value"}';
    expect(accepted(validatePassword(secret))).toBe(secret);
  });

  it('accepts a template expression as a password', () => {
    expect(accepted(validatePassword('${7*7}xxxxx'))).toBe('${7*7}xxxxx');
  });

  it('NEVER trims a password — leading and trailing spaces survive', () => {
    const secret = '  spaced secret  ';
    expect(accepted(validatePassword(secret))).toBe(secret);
    expect(accepted(validatePassword(secret))).not.toBe(secret.trim());
  });

  it('NEVER normalises Unicode in a password', () => {
    const decomposed = 'passwo\u0301rd'; // 'o' + combining acute
    const result = accepted(validatePassword(decomposed));
    expect(result).toBe(decomposed);
    expect(result).not.toBe(decomposed.normalize('NFC'));
  });

  it('does not collapse internal whitespace', () => {
    expect(accepted(validatePassword('a   b   c'))).toBe('a   b   c');
  });

  it('rejects one character below the minimum', () => {
    expect(rejected(validatePassword('x'.repeat(PASSWORD_MIN_LENGTH - 1)))).toBe(
      'Password must be at least 8 characters.',
    );
  });

  it('accepts exactly the minimum and exactly the maximum', () => {
    accepted(validatePassword('x'.repeat(PASSWORD_MIN_LENGTH)));
    accepted(validatePassword('x'.repeat(PASSWORD_MAX_LENGTH)));
  });

  it('rejects one character above the maximum', () => {
    expect(rejected(validatePassword('x'.repeat(PASSWORD_MAX_LENGTH + 1)))).toBe(
      'Password must be at most 128 characters.',
    );
  });
});

describe('validateCredential — login, not password choice', () => {
  it('accepts a short legacy secret that the chooser policy would refuse', () => {
    expect(accepted(validateCredential('abc'))).toBe('abc');
    rejected(validatePassword('abc'));
  });

  it('rejects an empty credential', () => {
    expect(rejected(validateCredential(''))).toBe('Password is required.');
  });

  it('does not trim', () => {
    expect(accepted(validateCredential(' x '))).toBe(' x ');
  });

  it('still caps the length handed to the hasher', () => {
    rejected(validateCredential('x'.repeat(PASSWORD_MAX_LENGTH + 1)));
  });
});

// ---------------------------------------------------------------------------

describe('validateInteger and validateIdentifier', () => {
  it('accepts whole numbers as numbers and as strings', () => {
    expect(accepted(validateInteger(7))).toBe(7);
    expect(accepted(validateInteger(' 7 '))).toBe(7);
    expect(accepted(validateInteger('-3'))).toBe(-3);
  });

  const bad = ['', '   ', 'abc', '1.5', '1e3', 'NaN', '0x10', '7abc'];
  for (const value of bad) {
    it(`rejects ${JSON.stringify(value)} rather than coercing it`, () => {
      rejected(validateInteger(value));
    });
  }

  it('rejects a boolean, which would otherwise pass as 1', () => {
    rejected(validateInteger(true));
  });

  it('honours minimum and maximum', () => {
    expect(rejected(validateInteger(3, { minimum: 5 }))).toBe('Value must be at least 5.');
    expect(rejected(validateInteger(9, { maximum: 5 }))).toBe('Value must be at most 5.');
  });

  it('rejects zero, negatives and non-numbers as identifiers', () => {
    rejected(validateIdentifier(0));
    rejected(validateIdentifier(-1));
    rejected(validateIdentifier('abc'));
    rejected(validateIdentifier(''));
    expect(accepted(validateIdentifier('42'))).toBe(42);
  });

  it('rejects an id past the point where a JS number can still be trusted', () => {
    // The ceiling is deliberately narrower than the backend's 64-bit maximum,
    // which JavaScript cannot represent. Narrower is the safe direction.
    accepted(validateIdentifier(Number.MAX_SAFE_INTEGER));
    rejected(validateIdentifier(Number.MAX_SAFE_INTEGER + 2));
  });

  it('gives identifiers a message that does not leak the numeric range', () => {
    expect(rejected(validateIdentifier(0, { fieldLabel: 'Team' }))).toBe(
      'Team is not a valid selection.',
    );
  });
});

describe('validateDecimal', () => {
  it('accepts fractional input', () => {
    expect(accepted(validateDecimal('7.5'))).toBe(7.5);
    expect(accepted(validateDecimal('.5'))).toBe(0.5);
    expect(accepted(validateDecimal(3))).toBe(3);
  });

  it('rejects the empty string instead of turning it into zero', () => {
    // This is the bug bare Number() causes: Number('') === 0.
    rejected(validateDecimal(''));
    rejected(validateDecimal('   '));
  });

  it('rejects NaN, Infinity and garbage', () => {
    rejected(validateDecimal('abc'));
    rejected(validateDecimal(NaN));
    rejected(validateDecimal(Infinity));
    rejected(validateDecimal('1.2.3'));
  });

  it('can refuse negatives with a message that says so', () => {
    expect(
      rejected(validateDecimal('-1', { allowNegative: false, fieldLabel: 'Fixed hours' })),
    ).toBe('Fixed hours cannot be negative.');
  });

  it('honours an upper bound', () => {
    rejected(validateDecimal('100001', { maximum: 100000 }));
  });
});

// ---------------------------------------------------------------------------

describe('validateDate', () => {
  it('accepts a real ISO date and returns it unchanged', () => {
    expect(accepted(validateDate('2026-02-28'))).toBe('2026-02-28');
  });

  it('accepts a leap day in a leap year', () => {
    accepted(validateDate('2024-02-29'));
  });

  it('rejects a leap day in a non-leap year', () => {
    expect(rejected(validateDate('2025-02-29'))).toBe('Date is not a real date.');
  });

  it('rejects a day and a month that do not exist', () => {
    rejected(validateDate('2025-02-30'));
    rejected(validateDate('2025-13-01'));
    rejected(validateDate('2025-00-10'));
  });

  it('rejects the wrong format with a message that shows the right one', () => {
    expect(rejected(validateDate('28/02/2026'))).toBe('Date must be in YYYY-MM-DD format.');
    rejected(validateDate('2026-2-8'));
  });

  it('treats blank as absent when optional and as an error when required', () => {
    expect(accepted(validateDate(''))).toBe('');
    expect(rejected(validateDate('', { required: true, fieldLabel: 'Start date' }))).toBe(
      'Start date is required.',
    );
  });
});

describe('validateEnum', () => {
  it('accepts a member of the set', () => {
    expect(accepted(validateEnum('admin', ['admin', 'member'] as const))).toBe('admin');
  });

  it('rejects anything else and names the permitted values', () => {
    expect(rejected(validateEnum('root', ['admin', 'member'], { fieldLabel: 'Role' }))).toBe(
      'Role must be one of: admin, member.',
    );
  });

  it('rejects an empty selection', () => {
    rejected(validateEnum('', ['admin', 'member']));
    rejected(validateEnum(undefined, ['admin', 'member']));
  });
});

// ---------------------------------------------------------------------------

describe('validateUrl — URL rules, never plain-text rules', () => {
  it('accepts a URL full of characters the prose checks would reject', () => {
    const url = 'https://example.com/path?a=1&b=2#frag%20ment';
    expect(accepted(validateUrl(url))).toBe(url);
  });

  it('accepts http and https', () => {
    accepted(validateUrl('http://example.com'));
    accepted(validateUrl('https://example.com'));
  });

  it('rejects a javascript: URL — the scheme allowlist is the control here', () => {
    expect(rejected(validateUrl('javascript:alert(1)'))).toBe(
      'URL must start with http:// or https://.',
    );
  });

  it('rejects data:, file: and ftp:', () => {
    rejected(validateUrl('data:text/html;base64,PHN2Zz4='));
    rejected(validateUrl('file:///etc/passwd'));
    rejected(validateUrl('ftp://example.com'));
  });

  it('rejects a relative or unparseable URL', () => {
    expect(rejected(validateUrl('/just/a/path'))).toBe('Please enter a valid URL.');
    rejected(validateUrl('example.com'));
  });

  it('treats blank as absent when optional and as an error when required', () => {
    expect(accepted(validateUrl(''))).toBe('');
    expect(rejected(validateUrl('', { required: true }))).toBe('URL is required.');
  });

  it('enforces the 2048-character boundary', () => {
    const base = 'https://example.com/';
    accepted(validateUrl(base + 'x'.repeat(URL_MAX_LENGTH - base.length)));
    rejected(validateUrl(base + 'x'.repeat(URL_MAX_LENGTH - base.length + 1)));
  });
});

describe('validateDomain', () => {
  it('accepts a hostname and lower-cases it', () => {
    expect(accepted(validateDomain('Docs.Example.COM'))).toBe('docs.example.com');
  });

  it('rejects a scheme, a path, a space and a leading hyphen', () => {
    rejected(validateDomain('https://example.com'));
    rejected(validateDomain('example.com/path'));
    rejected(validateDomain('exa mple.com'));
    rejected(validateDomain('-example.com'));
    rejected(validateDomain(''));
  });
});

// ---------------------------------------------------------------------------

describe('validateSearchTerm', () => {
  it('accepts an ordinary term and a blank box', () => {
    expect(accepted(validateSearchTerm('alice'))).toBe('alice');
    expect(accepted(validateSearchTerm(''))).toBe('');
    expect(accepted(validateSearchTerm(null))).toBe('');
  });

  it('leaves LIKE wildcards alone — the browser sends a term, not a pattern', () => {
    expect(accepted(validateSearchTerm('100%'))).toBe('100%');
    expect(accepted(validateSearchTerm('a_b'))).toBe('a_b');
  });

  it('enforces the 100-character boundary', () => {
    accepted(validateSearchTerm('x'.repeat(SEARCH_MAX_LENGTH)));
    expect(rejected(validateSearchTerm('x'.repeat(SEARCH_MAX_LENGTH + 1)))).toBe(
      'Search must be at most 100 characters.',
    );
  });

  it('rejects markup typed into a search box', () => {
    rejected(validateSearchTerm('<script>alert(1)</script>'));
  });
});

// ---------------------------------------------------------------------------

describe('structured-content helpers', () => {
  it('only calls a value JSON when the whole value parses as one', () => {
    expect(looksLikeJsonDocument('{"a":1}')).toBe(true);
    expect(looksLikeJsonDocument('  ["a","b"] ')).toBe(true);
    expect(looksLikeJsonDocument('see {"a":1} here')).toBe(false);
    expect(looksLikeJsonDocument('{not json}')).toBe(false);
    expect(looksLikeJsonDocument('plain text')).toBe(false);
  });

  it('never returns a message containing a pattern or an internal name', () => {
    const samples = ['<b>x</b>', '<?xml ?>', 'javascript:x', 'onerror=x', '${x}', '&lt;', '{"a":1}'];
    for (const sample of samples) {
      const reason = findStructuredContent(sample);
      expect(reason).not.toBeNull();
      expect(reason as string).toMatch(/\.$/);
      expect(reason as string).not.toMatch(/\\[dswb]|\[\^|Pattern|regex/i);
    }
  });

  it('finds nothing to complain about in ordinary prose', () => {
    expect(findStructuredContent('If 5 < 10 then a -> b, 50% done.')).toBeNull();
    expect(findStructuredContent("O'Brien — José, 山田")).toBeNull();
  });
});

// ---------------------------------------------------------------------------

describe('validateBySpec — the rule-to-validator mapping the hook uses', () => {
  it('routes a login password to the credential rule when asked', () => {
    accepted(validateBySpec('abc', { rule: 'password', label: 'Password', credential: true }));
    rejected(validateBySpec('abc', { rule: 'password', label: 'Password' }));
  });

  it('never short-circuits a password on blankness, so its own rules apply', () => {
    // A whitespace-only value is blank for every other rule, but a password is
    // never trimmed, so these three spaces are a three-character secret.
    expect(accepted(validateBySpec('   ', { rule: 'password', label: 'Password', credential: true })))
      .toBe('   ');
    expect(accepted(validateBySpec('        ', { rule: 'password', label: 'Password' }))).toBe(
      '        ',
    );
    // ...and eight spaces still fail the *chooser* policy at seven.
    rejected(validateBySpec('       ', { rule: 'password', label: 'Password' }));
  });

  it('reports a blank required field with the label the form gave it', () => {
    expect(rejected(validateBySpec('', { rule: 'name', label: 'Project name', required: true }))).toBe(
      'Project name is required.',
    );
    expect(rejected(validateBySpec('', { rule: 'identifier', label: 'Team', required: true }))).toBe(
      'Team is required.',
    );
  });

  it('leaves a blank optional numeric field as null, not zero', () => {
    expect(accepted(validateBySpec('', { rule: 'decimal', label: 'Fixed hours' }))).toBeNull();
  });

  it('applies enum membership from the spec', () => {
    accepted(validateBySpec('admin', { rule: 'enum', label: 'Role', allowed: ['admin', 'member'] }));
    rejected(validateBySpec('root', { rule: 'enum', label: 'Role', allowed: ['admin', 'member'] }));
  });

  it('applies a URL spec with URL rules, not plain-text ones', () => {
    accepted(validateBySpec('https://a.example.com/x?y=1&z=2', { rule: 'url', label: 'Link' }));
    rejected(validateBySpec('javascript:alert(1)', { rule: 'url', label: 'Link' }));
  });
});

// ---------------------------------------------------------------------------
// Regression: a field of nothing but punctuation used to save
// ---------------------------------------------------------------------------

describe('a value of only punctuation is refused', () => {
  const PUNCTUATION_ONLY = [
    '!!!', '@@@', '...', '---', '???', '$$$', '***', '&&&', '##', '()',
    '/', '\\', '.', '-', '_', '+++', '~~~', ';;', '!@#$%^&*()', '   ---   ',
  ];

  // Length and structure alone passed these — "!!!" has a length, is not
  // markup and is not JSON — so a required field could be satisfied without
  // being answered, and the value was stored.
  it.each(PUNCTUATION_ONLY)('rejects %j as a name', (payload) => {
    rejected(validateName(payload));
  });

  it.each(PUNCTUATION_ONLY)('rejects %j as a description', (payload) => {
    rejected(validateDescription(payload, { required: true }));
  });

  it.each(PUNCTUATION_ONLY)('rejects %j as plain text', (payload) => {
    rejected(validatePlainText(payload, { required: true }));
  });

  it('says what the user should do', () => {
    expect(rejected(validateName('!!!', { fieldLabel: 'Project name' }))).toBe(
      'Project name must contain at least one letter or number.',
    );
  });
});

describe('punctuation alongside real content is still accepted', () => {
  // The check asks for *some* content, not for the absence of symbols.
  const WITH_CONTENT = [
    'Fixed!!!', '...and then it crashed', 'C++', 'v2.0!',
    'R&D / Prototype #4', '50% !!!', 'A',
  ];

  it.each(WITH_CONTENT)('accepts %j', (value) => {
    accepted(validateName(value));
  });

  // A letter is a letter in any script: this must never become an ASCII test.
  it.each(['プロジェクト', 'Проект', 'مشروع', '项目', '프로젝트'])(
    'accepts the non-Latin name %j',
    (value) => {
      accepted(validateName(value));
    },
  );
});

describe('search boxes are exempt from the content requirement', () => {
  // The backend does not require a search term to contain a letter or digit.
  // Requiring it here would make the browser stricter than the server, which
  // is the one direction a client must never be.
  it('still allows a punctuation-only search term', () => {
    expect(accepted(validateSearchTerm('???'))).toBe('???');
  });
});

describe('validateTimeOfDay', () => {
  // The same rule the backend applies (TIME_OF_DAY): HH:MM on a 24-hour clock,
  // one spelling per time, rejected rather than repaired.
  it.each(['00:00', '09:30', '13:45', '23:59'])('accepts %s', (value) => {
    expect(accepted(validateTimeOfDay(value))).toBe(value);
  });

  it.each(['9:30', '24:00', '09:60', '09:30:00', '0930', '09.30', 'ab:cd', ''])('rejects %j', (value) => {
    rejected(validateTimeOfDay(value));
  });

  it('trims surrounding space but does not repair a single-digit hour', () => {
    expect(accepted(validateTimeOfDay(' 09:30 '))).toBe('09:30');
    rejected(validateTimeOfDay('9:30'));
  });

  it('refuses a value that is not text', () => {
    rejected(validateTimeOfDay(930));
    rejected(validateTimeOfDay(null));
  });
});
