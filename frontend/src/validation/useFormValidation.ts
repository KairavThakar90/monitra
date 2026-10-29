/**
 * `useFormValidation` — the one place a React form talks to the validators.
 *
 * A form declares what each of its fields *is*, once, and then hands the hook
 * the current values when it is submitted. The hook picks the right validator
 * for each rule, collects every failure instead of stopping at the first, and
 * hands back the normalised values so the caller can send those rather than the
 * raw ones.
 *
 * ```ts
 * const form = useFormValidation({
 *   name:  { rule: 'name',  label: 'Project name', required: true },
 *   hours: { rule: 'decimal', label: 'Fixed hours', minimum: 0, allowNegative: false },
 * });
 *
 * const result = form.validateAll({ name: formName, hours: formHours });
 * if (!result.ok) return;            // form.errors is now populated
 * save({ name: result.values.name, fixed_hours: result.values.hours });
 * ```
 *
 * Rendering a field:
 *
 * ```tsx
 * <input {...form.fieldProps('name')} value={formName} onChange={...} />
 * <FieldError id={form.errorId('name')} message={form.errors.name} />
 * ```
 *
 * `fieldProps` supplies `maxLength` from the rule's own limit plus the
 * `aria-invalid`/`aria-describedby` pair that points a screen reader at the
 * message, so the two cannot drift apart.
 *
 * Behaviour notes:
 * - `validateAll` marks every field touched, so a submit reveals all errors.
 * - `validateField` checks one field and is what a blur handler should call.
 * - Errors are only *shown* for touched fields; `errors` reflects that, while
 *   `validateAll`'s returned `errors` is the complete unfiltered set.
 * - Nothing here throws.
 */

import { useCallback, useMemo, useRef, useState } from 'react';
import type { Rule } from './rules';
import { RULE_MAX_LENGTH } from './rules';
import type { ValidationResult } from './validators';
import {
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
  validateSha256,
  validateUrl,
  validateVersion,
} from './validators';

/** How one field is described to the hook. */
export type FieldSpec = {
  /** Which validator to run. */
  rule: Rule;
  /** How the field is named in its error messages, e.g. "Project name". */
  label: string;
  /** Reject an empty value. Defaults to false. */
  required?: boolean;
  /** Override the rule's own character limit (a narrower column, usually). */
  maxLength?: number;
  minLength?: number;
  /** Numeric bounds, for the `integer`/`decimal`/`identifier` rules. */
  minimum?: number;
  maximum?: number;
  allowNegative?: boolean;
  /**
   * Also refuse code that carries no markup tag (jQuery, JavaScript, CSS).
   * Only meaningful for `rule: 'description'`; set it on a project's or a
   * task's description and nowhere else.
   */
  rejectCode?: boolean;
  /** The permitted values, for the `enum` rule. */
  allowed?: readonly unknown[];
  /**
   * Treat a login password as a credential rather than a newly chosen one:
   * length upper bound only, no minimum. Only meaningful for `rule: 'password'`.
   */
  credential?: boolean;
};

export type FormSpec = Record<string, FieldSpec>;

/** The outcome of validating a whole form. */
export type FormResult<S extends FormSpec> = {
  ok: boolean;
  /** Every failure found, keyed by field. Empty when `ok`. */
  errors: Partial<Record<keyof S, string>>;
  /** The normalised values, keyed by field. Only complete when `ok`. */
  values: Record<keyof S, unknown>;
};

const isBlank = (value: unknown): boolean =>
  value === null || value === undefined || (typeof value === 'string' && value.trim().length === 0);

/**
 * Run the validator a single field's spec asks for.
 *
 * Exported so a non-React caller (a thunk, a test) can reuse exactly the same
 * rule-to-validator mapping the hook uses.
 */
export function validateBySpec(value: unknown, spec: FieldSpec): ValidationResult<unknown> {
  const label = spec.label;
  const required = spec.required === true;

  // Passwords are excluded from the blank short-circuit below on purpose: they
  // are never trimmed, and their own validators own the empty case.
  if (spec.rule !== 'password' && isBlank(value)) {
    if (required) return { ok: false, error: `${label} is required.` };
    if (spec.rule === 'integer' || spec.rule === 'decimal' || spec.rule === 'identifier') {
      return { ok: true, value: null };
    }
    return { ok: true, value: '' };
  }

  switch (spec.rule) {
    case 'name':
      return validateName(value, {
        fieldLabel: label,
        maxLength: spec.maxLength,
        minLength: spec.minLength,
      });
    case 'description':
      return validateDescription(value, {
        fieldLabel: label,
        maxLength: spec.maxLength,
        required,
        rejectCode: spec.rejectCode,
      });
    case 'plain_text':
    case 'idempotency_key':
      return validatePlainText(value, {
        fieldLabel: label,
        maxLength: spec.maxLength,
        required,
      });
    case 'search':
      return validateSearchTerm(value, { fieldLabel: label, maxLength: spec.maxLength });
    case 'email':
      return validateEmail(value, { fieldLabel: label });
    case 'password':
      return spec.credential === true
        ? validateCredential(value, { fieldLabel: label })
        : validatePassword(value, { fieldLabel: label });
    case 'integer':
      return validateInteger(value, {
        fieldLabel: label,
        minimum: spec.minimum,
        maximum: spec.maximum,
      });
    case 'decimal':
      return validateDecimal(value, {
        fieldLabel: label,
        minimum: spec.minimum,
        maximum: spec.maximum,
        allowNegative: spec.allowNegative,
      });
    case 'identifier':
      return validateIdentifier(value, { fieldLabel: label });
    case 'date':
    case 'datetime':
      return validateDate(value, { fieldLabel: label, required });
    case 'enum':
      return validateEnum(value, spec.allowed ?? [], { fieldLabel: label });
    case 'url':
      return validateUrl(value, { fieldLabel: label, required });
    case 'domain':
      return validateDomain(value, { fieldLabel: label });
    case 'version':
      return validateVersion(value, { fieldLabel: label, required });
    case 'sha256':
      return validateSha256(value, { fieldLabel: label, required });
    case 'uuid':
    default:
      return validatePlainText(value, {
        fieldLabel: label,
        maxLength: spec.maxLength,
        required,
      });
  }
}

/** Attributes to spread onto the input a field renders. */
export type FieldProps = {
  maxLength?: number;
  'aria-invalid'?: boolean;
  'aria-describedby'?: string;
};

export function useFormValidation<S extends FormSpec>(spec: S) {
  type Key = keyof S & string;

  const [errors, setErrors] = useState<Partial<Record<Key, string>>>({});
  const [touched, setTouchedState] = useState<Partial<Record<Key, boolean>>>({});

  // The spec is written inline at the call site, so it is a fresh object every
  // render. Holding it in a ref keeps the callbacks below stable instead of
  // invalidating every memo in the form on each keystroke.
  const specRef = useRef(spec);
  specRef.current = spec;

  const idPrefix = useMemo(
    () => `fv-${Math.random().toString(36).slice(2, 9)}`,
    [],
  );

  const errorId = useCallback((field: Key) => `${idPrefix}-${field}-error`, [idPrefix]);

  const setTouched = useCallback((field: Key, value = true) => {
    setTouchedState((current) => ({ ...current, [field]: value }));
  }, []);

  const setError = useCallback((field: Key, message: string | null) => {
    setErrors((current) => {
      const next = { ...current };
      if (message === null || message === undefined || message === '') delete next[field];
      else next[field] = message;
      return next;
    });
    if (message) setTouchedState((current) => ({ ...current, [field]: true }));
  }, []);

  const clear = useCallback(() => {
    setErrors({});
    setTouchedState({});
  }, []);

  const clearField = useCallback((field: Key) => {
    setErrors((current) => {
      const next = { ...current };
      delete next[field];
      return next;
    });
  }, []);

  /** Validate one field, record its error, and return the result. */
  const validateField = useCallback(
    (field: Key, value: unknown): ValidationResult<unknown> => {
      const fieldSpec = specRef.current[field];
      if (!fieldSpec) return { ok: true, value };
      const result = validateBySpec(value, fieldSpec);
      // Errors are only shown for touched fields; a field the user has just left has been touched, so a blur
      // check must reveal its error now rather than only after the next submit.
      setTouchedState((current) => (current[field] ? current : { ...current, [field]: true }));
      setErrors((current) => {
        const next = { ...current };
        if (result.ok) delete next[field];
        else next[field] = result.error;
        return next;
      });
      return result;
    },
    [],
  );

  /**
   * Validate every declared field against `values`.
   *
   * Every field is checked — the first failure does not stop the rest — so the
   * user sees everything wrong at once instead of fixing one error per submit.
   */
  const validateAll = useCallback(
    (values: Partial<Record<Key, unknown>>): FormResult<S> => {
      const found: Partial<Record<Key, string>> = {};
      const normalized: Record<string, unknown> = {};
      const allTouched: Partial<Record<Key, boolean>> = {};
      const currentSpec = specRef.current;

      for (const key of Object.keys(currentSpec) as Key[]) {
        allTouched[key] = true;
        const result = validateBySpec(values[key], currentSpec[key]);
        if (result.ok) normalized[key] = result.value;
        else found[key] = result.error;
      }

      setErrors(found);
      setTouchedState(allTouched);
      return {
        ok: Object.keys(found).length === 0,
        errors: found as Partial<Record<keyof S, string>>,
        values: normalized as Record<keyof S, unknown>,
      };
    },
    [],
  );

  /** The visible errors: a field's message appears once it has been touched. */
  const visibleErrors = useMemo(() => {
    const shown: Partial<Record<Key, string>> = {};
    for (const key of Object.keys(errors) as Key[]) {
      if (touched[key]) shown[key] = errors[key];
    }
    return shown;
  }, [errors, touched]);

  /** `maxLength` plus the aria wiring for a field's input. */
  const fieldProps = useCallback(
    (field: Key): FieldProps => {
      const fieldSpec = specRef.current[field];
      if (!fieldSpec) return {};
      const limit = fieldSpec.maxLength ?? RULE_MAX_LENGTH[fieldSpec.rule];
      const invalid = Boolean(errors[field] && touched[field]);
      const props: FieldProps = {};
      if (limit !== undefined) props.maxLength = limit;
      if (invalid) {
        props['aria-invalid'] = true;
        props['aria-describedby'] = errorId(field);
      }
      return props;
    },
    [errors, touched, errorId],
  );

  return {
    /** Errors for touched fields — what the UI should render. */
    errors: visibleErrors as Partial<Record<Key, string>>,
    /** Every recorded error, touched or not. */
    allErrors: errors,
    touched,
    setTouched,
    setError,
    clear,
    clearField,
    validateField,
    validateAll,
    fieldProps,
    errorId,
  };
}
