import { describe, expect, it } from 'vitest';

import { billingTypeLabel, billingTypesFor } from '../billing';

describe('billingTypesFor — the Billing / Non Billing, then Fixed Hours / Flexible Time filter', () => {
  it('selects nothing (no filter) when no project type is chosen', () => {
    expect(billingTypesFor('', '')).toBeUndefined();
  });

  it('selects both billed kinds under Billing when neither is chosen', () => {
    expect(billingTypesFor('billing', '')).toEqual(['fixed', 'free']);
  });

  it('narrows Billing to the one kind chosen', () => {
    expect(billingTypesFor('billing', 'fixed')).toEqual(['fixed']);
    expect(billingTypesFor('billing', 'free')).toEqual(['free']);
  });

  it('selects non_billing alone for Non Billing', () => {
    expect(billingTypesFor('non_billing', '')).toEqual(['non_billing']);
  });

  it('ignores a stale kind once Billing is left', () => {
    expect(billingTypesFor('non_billing', 'fixed')).toEqual(['non_billing']);
    expect(billingTypesFor('', 'free')).toBeUndefined();
  });
});

describe('billingTypeLabel', () => {
  it('names each type the way the Create Project form does', () => {
    expect(billingTypeLabel('fixed')).toBe('Fixed Hours');
    expect(billingTypeLabel('free')).toBe('Flexible Time');
    expect(billingTypeLabel('non_billing')).toBe('Non Billing');
  });

  it('shows a dash for a missing or unknown type rather than a raw value', () => {
    expect(billingTypeLabel(null)).toBe('—');
    expect(billingTypeLabel(undefined)).toBe('—');
    expect(billingTypeLabel('something_new')).toBe('—');
  });
});
