"""Reusable Pydantic field types — the way schemas are meant to consume this.

This is the ergonomic front door. A schema author picks the type that matches
the field's meaning and gets normalisation, length limits, format checks and
structured-content rejection for free::

    from app.core.validation import Name, OptionalDescription, Email, Identifier

    class ProjectCreate(BaseModel):
        project_name: Name
        description: OptionalDescription = None
        owner_id: Identifier
        contact_email: Email

Nothing else needs writing. That is the whole point: the cost of doing the
right thing has to be lower than the cost of hand-rolling a ``field_validator``,
or future schemas will keep hand-rolling them.

When a field needs a different limit — a column narrower than the shared
maximum — use the factory instead of inventing a new type::

    short_code: Annotated[str, name_field(max_length=32, label="Short code")]

Every type here is an ``Annotated[...]`` alias wrapping a
``BeforeValidator``. "Before" matters: the function runs on the raw input, so
it normalises the value before Pydantic's own coercion sees it, and a rejection
surfaces as FastAPI's standard 422 rather than a 500.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import BeforeValidator

from . import validators as v
from .rules import (
    DESCRIPTION_MAX_LENGTH,
    IDENTIFIER_MAX,
    IDENTIFIER_MIN,
    NAME_MAX_LENGTH,
    PLAIN_TEXT_MAX_LENGTH,
    SEARCH_MAX_LENGTH,
)

# ---------------------------------------------------------------------------
# Factories — for fields that need a label or a limit of their own
# ---------------------------------------------------------------------------


def name_field(
    *, label: str = "Name", max_length: int = NAME_MAX_LENGTH, min_length: int = 1
) -> BeforeValidator:
    """A required short label with a custom limit or error label."""
    return BeforeValidator(
        lambda value: v.validate_name(
            value, field_label=label, max_length=max_length, min_length=min_length
        )
    )


def optional_name_field(
    *, label: str = "Name", max_length: int = NAME_MAX_LENGTH
) -> BeforeValidator:
    """A short label that may be omitted. Blank becomes ``None``."""

    def _validate(value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return v.validate_name(value, field_label=label, max_length=max_length)

    return BeforeValidator(_validate)


def description_field(
    *,
    label: str = "Description",
    max_length: int = DESCRIPTION_MAX_LENGTH,
    required: bool = False,
) -> BeforeValidator:
    """Multi-line prose with a custom limit or error label."""
    return BeforeValidator(
        lambda value: v.validate_description(
            value, field_label=label, max_length=max_length, required=required
        )
    )


def plain_text_field(
    *,
    label: str = "Value",
    max_length: int = PLAIN_TEXT_MAX_LENGTH,
    required: bool = False,
) -> BeforeValidator:
    """Single-line free text with a custom limit or error label."""
    return BeforeValidator(
        lambda value: v.validate_plain_text(
            value, field_label=label, max_length=max_length, required=required
        )
    )


def identifier_field(*, label: str = "Identifier") -> BeforeValidator:
    """A positive database key with a field-specific error label."""
    return BeforeValidator(lambda value: v.validate_identifier(value, field_label=label))


def integer_field(
    *,
    label: str = "Value",
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> BeforeValidator:
    """A whole number within an explicit range."""
    return BeforeValidator(
        lambda value: v.validate_integer(
            value, field_label=label, minimum=minimum, maximum=maximum
        )
    )


def optional_integer_field(
    *,
    label: str = "Value",
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> BeforeValidator:
    """The same range check as :func:`integer_field`, but the field may be omitted."""

    def _validate(value):
        if value is None:
            return None
        return v.validate_integer(value, field_label=label, minimum=minimum, maximum=maximum)

    return BeforeValidator(_validate)


def decimal_field(
    *,
    label: str = "Value",
    minimum: Optional[Decimal] = None,
    maximum: Optional[Decimal] = None,
    allow_negative: bool = True,
) -> BeforeValidator:
    """A fractional number within an explicit range."""
    return BeforeValidator(
        lambda value: v.validate_decimal(
            value,
            field_label=label,
            minimum=minimum,
            maximum=maximum,
            allow_negative=allow_negative,
        )
    )


def search_field(
    *, label: str = "Search", max_length: int = SEARCH_MAX_LENGTH
) -> BeforeValidator:
    """A search term, returned with its LIKE wildcards escaped."""
    return BeforeValidator(
        lambda value: v.validate_search_term(
            value, field_label=label, max_length=max_length
        )
    )


def url_field(*, label: str = "URL", required: bool = False) -> BeforeValidator:
    """An absolute http(s) URL."""
    return BeforeValidator(
        lambda value: v.validate_url(value, field_label=label, required=required)
    )


def id_list_field(
    *, label: str = "Selection", max_items: Optional[int] = None, unique: bool = True
) -> BeforeValidator:
    """A bounded list of positive identifiers."""
    return BeforeValidator(
        lambda value: v.validate_id_list(
            value, field_label=label, max_items=max_items, unique=unique
        )
    )


# ---------------------------------------------------------------------------
# Ready-made aliases — the common cases, named after the rule they apply
# ---------------------------------------------------------------------------

#: A required short human-readable label, at most 150 characters.
Name = Annotated[str, name_field()]

#: The same, optional. Blank input normalises to ``None``.
OptionalName = Annotated[Optional[str], optional_name_field()]

#: Required multi-line prose, at most 5000 characters.
Description = Annotated[str, description_field(required=True)]

#: Optional multi-line prose. Blank input normalises to ``None``.
OptionalDescription = Annotated[Optional[str], description_field()]

#: Required single-line free text, at most 255 characters.
PlainText = Annotated[str, plain_text_field(required=True)]

#: Optional single-line free text.
OptionalPlainText = Annotated[Optional[str], plain_text_field()]

#: An email address, trimmed with a lower-cased domain.
Email = Annotated[str, BeforeValidator(v.validate_email)]

#: A password being *chosen*. Length policy only; the value is never altered.
Password = Annotated[str, BeforeValidator(v.validate_password)]

#: A password being *presented at login*. Upper bound only — see
#: :func:`~app.core.validation.validators.validate_credential` for why an
#: existing account's shorter secret must still be accepted.
Credential = Annotated[str, BeforeValidator(v.validate_credential)]

#: A positive database key.
Identifier = Annotated[int, identifier_field()]

#: An optional positive database key.
OptionalIdentifier = Annotated[
    Optional[int],
    BeforeValidator(lambda value: None if value is None else v.validate_identifier(value)),
]

#: A canonical hyphenated UUID, lower-cased.
Uuid = Annotated[str, BeforeValidator(v.validate_uuid)]

#: A calendar date, accepting ``date`` or ``YYYY-MM-DD``.
DateValue = Annotated[date, BeforeValidator(v.validate_date)]

#: A hostname, lower-cased.
Domain = Annotated[str, BeforeValidator(v.validate_domain)]

#: An absolute http(s) URL. Optional, because the desktop legitimately reports
#: "no URL could be read" and must never substitute a placeholder.
OptionalUrl = Annotated[Optional[str], url_field()]

#: An opaque client-generated de-duplication key.
OptionalIdempotencyKey = Annotated[
    Optional[str], BeforeValidator(v.validate_idempotency_key)
]

#: A required client-generated de-duplication key.
IdempotencyKey = Annotated[
    str, BeforeValidator(lambda value: v.validate_idempotency_key(value, required=True))
]

#: A ``major.minor.patch`` release version.
Version = Annotated[str, BeforeValidator(v.validate_version)]

#: The same, optional — a policy field that may simply not be set.
OptionalVersion = Annotated[
    Optional[str], BeforeValidator(lambda value: v.validate_version(value, required=False))
]

#: A SHA-256 digest, lower-cased.
Sha256 = Annotated[str, BeforeValidator(v.validate_sha256)]

#: An escaped search term, ready for a ``LIKE`` pattern.
OptionalSearch = Annotated[Optional[str], search_field()]

#: A bounded, de-duplicated list of positive identifiers.
IdentifierList = Annotated[Optional[list], id_list_field()]


__all__ = [
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
    "IDENTIFIER_MIN",
    "IDENTIFIER_MAX",
]
