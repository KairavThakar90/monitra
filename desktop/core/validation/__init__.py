"""Centralised input validation for the desktop client.

Every dialog that collects text from a person validates it through this
package. It exists so that the rules live in one place instead of being
re-typed as guard clauses in each dialog — which is how the app ended up with
four spellings of "this field is required" and no length limit anywhere except
the feedback form.

What this is, and what it is not
--------------------------------
This is a **user-experience** layer. It tells someone what is wrong while they
are still looking at the field, and it keeps requests the server will refuse
out of the sync queue. It is **not** a security control: the backend validates
everything again, because it also serves the web client and anything else that
can reach the API.

Consequently this layer must never be *stricter* than the backend. If it were,
a value the server would happily accept could not be submitted from the desktop
at all, and the bug would present as a mysteriously unusable form.

Position in the architecture
----------------------------
Pure functions, stdlib only. No Qt, no threads, no services, no state, no I/O.
It is imported directly by ``ui/`` — permitted, and unrelated to the
``BackgroundApi`` rule, which governs access to *runtime services*, not to
shared helpers under ``core/``. Validation is synchronous and instant; it must
never be handed to the ``TaskRunner``.

Using it
--------
The dialogs' existing guard-clause style is preserved deliberately::

    from core.validation import validate_description, validate_identifier

    def _on_save_clicked(self) -> None:
        self.error_label.hide()

        project = validate_identifier(
            self.project_combo.currentData(), field_label="Project"
        )
        if not project.ok:
            self._show_error(project.error)
            return

        description = validate_description(
            self.desc_input.toPlainText(), field_label="Description", required=True
        )
        if not description.ok:
            self._show_error(description.error)
            self.desc_input.setFocus()
            return

        self._description = description.value   # note: the *normalised* value
        self.accept()

Always submit ``result.value``, not the widget's text. That is what carries the
trimming and the line-ending normalisation; re-reading the widget throws both
away and sends the server the raw string.

See ``docs/VALIDATION.md`` at the repository root for the shared field
catalogue and which rule to pick for a new field.
"""
from .rules import (
    ALLOWED_URL_SCHEMES,
    DESCRIPTION_MAX_LENGTH,
    DOMAIN_MAX_LENGTH,
    EMAIL_MAX_LENGTH,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IDENTIFIER_MAX,
    IDENTIFIER_MIN,
    MAX_LIST_PARAM_ITEMS,
    NAME_MAX_LENGTH,
    NAME_MIN_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    PLAIN_TEXT_MAX_LENGTH,
    SEARCH_MAX_LENGTH,
    SHA256_LENGTH,
    URL_MAX_LENGTH,
    VERSION_MAX_LENGTH,
    Rule,
)
from .sanitizer import (
    collapse_whitespace,
    normalize_email,
    normalize_newlines,
    normalize_optional,
    normalize_text,
    normalize_unicode,
    strip_control_characters,
)
from .validators import (
    ValidationResult,
    find_structured_content,
    looks_like_json_document,
    validate_all,
    validate_credential,
    validate_date,
    validate_decimal,
    validate_description,
    validate_domain,
    validate_email,
    validate_enum,
    validate_identifier,
    validate_integer,
    validate_name,
    validate_password,
    validate_plain_text,
    validate_search_term,
    validate_sha256,
    validate_time_of_day,
    validate_url,
    validate_username,
    validate_uuid,
    validate_version,
)

__all__ = [
    "Rule",
    "ValidationResult",
    # limits
    "NAME_MAX_LENGTH",
    "NAME_MIN_LENGTH",
    "DESCRIPTION_MAX_LENGTH",
    "PLAIN_TEXT_MAX_LENGTH",
    "EMAIL_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_MAX_LENGTH",
    "SEARCH_MAX_LENGTH",
    "URL_MAX_LENGTH",
    "DOMAIN_MAX_LENGTH",
    "IDEMPOTENCY_KEY_MAX_LENGTH",
    "VERSION_MAX_LENGTH",
    "SHA256_LENGTH",
    "IDENTIFIER_MIN",
    "IDENTIFIER_MAX",
    "MAX_LIST_PARAM_ITEMS",
    "ALLOWED_URL_SCHEMES",
    # normalisation
    "normalize_text",
    "normalize_unicode",
    "normalize_newlines",
    "normalize_email",
    "normalize_optional",
    "collapse_whitespace",
    "strip_control_characters",
    # validators
    "validate_name",
    "validate_description",
    "validate_plain_text",
    "validate_search_term",
    "validate_email",
    "validate_password",
    "validate_credential",
    "validate_username",
    "validate_integer",
    "validate_identifier",
    "validate_decimal",
    "validate_uuid",
    "validate_date",
    "validate_enum",
    "validate_domain",
    "validate_url",
    "validate_version",
    "validate_time_of_day",
    "validate_sha256",
    "validate_all",
    "find_structured_content",
    "looks_like_json_document",
]
