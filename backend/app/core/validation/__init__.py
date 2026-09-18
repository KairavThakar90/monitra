"""Centralised input validation — the backend's final security authority.

The application validates user input at three layers. The frontend and the
desktop client validate early, so a person is told what is wrong while they are
still looking at the field. This package validates last, and it is the only one
of the three that is a security control.

That distinction is the whole design. A request can reach the API from a
browser, from the desktop app, from ``curl``, or from a script replaying a
captured request with the body edited. Only two of those ran our client code.
So **the backend never assumes a value has already been checked** — every
user-controlled field is validated here regardless of where it came from, and
the rules are identical to the ones the clients apply.

Layout
------
``rules.py``       The shared catalogue: the ``Rule`` kinds, the length limits,
                   the patterns. Mirrored by the desktop and frontend copies.
``sanitizer.py``   Safe, meaning-preserving normalisation, plus the SQL
                   ``LIKE`` wildcard escape.
``validators.py``  The enforcement functions. Reject rather than scrub.
``types.py``       ``Annotated`` Pydantic types — how schemas should consume it.

Using it
--------
In a schema, prefer the ready-made types::

    from app.core.validation import Name, OptionalDescription, Identifier

    class TaskCreate(BaseModel):
        task_name: Name
        description: OptionalDescription = None
        project_id: Identifier

In a route's query parameters, validate with the functions, because FastAPI's
``Query`` carries the bounds and the validator carries the content rules::

    from app.core.validation import validate_search_term

    search = validate_search_term(search)   # escaped, ready for ILIKE

Anything a user typed that reaches a ``LIKE`` must go through
``validate_search_term`` or ``escape_like_wildcards`` and be paired with
``escape="\\\\"`` at the query site, or the ``%`` they typed will match
everything.

See ``docs/VALIDATION.md`` for the full field catalogue and the rule to pick
when adding a new one.
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
    LIKE_ESCAPE_CHARACTER,
    collapse_whitespace,
    escape_like_wildcards,
    like_pattern,
    normalize_email,
    normalize_newlines,
    normalize_optional,
    normalize_text,
    normalize_unicode,
    strip_control_characters,
)
from .types import (
    Credential,
    DateValue,
    Description,
    Domain,
    Email,
    IdempotencyKey,
    Identifier,
    IdentifierList,
    Name,
    OptionalDescription,
    OptionalIdempotencyKey,
    OptionalIdentifier,
    OptionalName,
    OptionalPlainText,
    OptionalSearch,
    OptionalUrl,
    OptionalVersion,
    Password,
    PlainText,
    Sha256,
    Uuid,
    Version,
    decimal_field,
    description_field,
    id_list_field,
    identifier_field,
    integer_field,
    optional_integer_field,
    name_field,
    optional_name_field,
    plain_text_field,
    search_field,
    url_field,
)
from .validators import (
    InputValidationError,
    contains_letter_or_digit,
    ensure_plain_text,
    find_structured_content,
    looks_like_json_document,
    reject_control_characters,
    require_meaningful_content,
    validate_credential,
    validate_date,
    validate_decimal,
    validate_description,
    validate_domain,
    validate_email,
    validate_enum,
    validate_id_list,
    validate_idempotency_key,
    validate_identifier,
    validate_integer,
    validate_name,
    validate_password,
    validate_plain_text,
    validate_search_term,
    validate_sha256,
    validate_url,
    validate_uuid,
    validate_version,
)

__all__ = [
    "Rule",
    "InputValidationError",
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
    "escape_like_wildcards",
    "like_pattern",
    "LIKE_ESCAPE_CHARACTER",
    # validators
    "validate_name",
    "validate_description",
    "validate_plain_text",
    "validate_search_term",
    "validate_email",
    "validate_password",
    "validate_credential",
    "validate_integer",
    "validate_identifier",
    "validate_decimal",
    "validate_uuid",
    "validate_date",
    "validate_enum",
    "validate_domain",
    "validate_url",
    "validate_idempotency_key",
    "validate_version",
    "validate_sha256",
    "validate_id_list",
    "ensure_plain_text",
    "reject_control_characters",
    "require_meaningful_content",
    "contains_letter_or_digit",
    "find_structured_content",
    "looks_like_json_document",
    # pydantic types
    "Name",
    "OptionalName",
    "Description",
    "OptionalDescription",
    "PlainText",
    "OptionalPlainText",
    "Email",
    "Password",
    "Credential",
    "Identifier",
    "OptionalIdentifier",
    "Uuid",
    "DateValue",
    "Domain",
    "OptionalUrl",
    "OptionalIdempotencyKey",
    "IdempotencyKey",
    "Version",
    "OptionalVersion",
    "Sha256",
    "OptionalSearch",
    "IdentifierList",
    # factories
    "name_field",
    "optional_name_field",
    "description_field",
    "plain_text_field",
    "identifier_field",
    "integer_field",
    "optional_integer_field",
    "decimal_field",
    "search_field",
    "url_field",
    "id_list_field",
]
