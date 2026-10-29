// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it } from 'vitest';
import { useFormValidation } from '../useFormValidation';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CUT_JSON = '{ "id": 42, "name": "RandomUser", "active": true, "score": 87, "tags": ["alpha", "beta", "gamma"], "meta": { "createdAt": "2026-09-22T';

type Form = ReturnType<typeof useFormValidation<{ name: { rule: 'name'; label: string; required: true } }>>;

function mount() {
  const holder: { form?: Form } = {};
  function Probe() {
    holder.form = useFormValidation({ name: { rule: 'name', label: 'Project name', required: true } });
    return null;
  }
  const root = createRoot(document.createElement('div'));
  act(() => root.render(createElement(Probe)));
  return holder as { form: Form };
}

describe('useFormValidation.validateField', () => {
  it('shows the error as soon as the field is left, not only after submit', () => {
    const h = mount();
    act(() => {
      h.form.validateField('name', CUT_JSON);
    });
    expect(h.form.errors.name).toBeTruthy();
  });

  it('shows nothing for a valid value', () => {
    const h = mount();
    act(() => {
      h.form.validateField('name', 'Website Redesign');
    });
    expect(h.form.errors.name).toBeUndefined();
  });
});
