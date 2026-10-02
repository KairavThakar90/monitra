/**
 * A project's billing type, as the API stores it in `projects.billing_type`.
 *
 * - `fixed`        billed against an hour budget (`fixed_hours`, required);
 * - `free`         flexible time: no budget, not billed;
 * - `non_billing`  not billed, no budget.
 *
 * Mirrors `BillingType` in `backend/app/schemas/project_management.py`. Only
 * `fixed` carries an hour budget and is billable; the other two are stored
 * non-billable.
 */
export type BillingType = 'fixed' | 'free' | 'non_billing';

/** The first filter choice: every project, billed ones, or not-billed ones. */
export type BillingScope = '' | 'billing' | 'non_billing';
/** The second choice, offered under Billing: how it is billed. '' means either. */
export type BillingKind = '' | 'fixed' | 'free';

/**
 * The billing types a two-step "Billing / Non Billing, then Fixed Hours /
 * Flexible Time" filter selects, in the form the API takes. `undefined` means
 * no filter at all. A kind only means something under Billing and is ignored
 * otherwise, so a stale Fixed Hours left behind by switching to Non Billing
 * can never narrow it.
 */
export const billingTypesFor = (scope: BillingScope, kind: BillingKind): BillingType[] | undefined => {
  if (scope === 'non_billing') return ['non_billing'];
  if (scope === 'billing') return kind === '' ? ['fixed', 'free'] : [kind];
  return undefined;
};

/** What a person calls each type, matching the Create Project form. */
export const billingTypeLabel = (type: string | null | undefined): string => {
  switch (type) {
    case 'fixed':
      return 'Fixed Hours';
    case 'free':
      return 'Flexible Time';
    case 'non_billing':
      return 'Non Billing';
    default:
      return '—';
  }
};
