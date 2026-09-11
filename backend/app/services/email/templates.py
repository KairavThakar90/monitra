"""Rendering an email template, with escaping that cannot be forgotten.

The renderer is about thirty lines, and that is deliberate. Email templates are
the one place in this system where user-written text — a feedback message
somebody typed into the desktop client — is interpolated into markup that is
then sent to other people. The property that matters more than any feature is
that **a value is escaped unless someone explicitly said it is markup**, and
that is easier to guarantee in a renderer small enough to read in one sitting
than in a general-purpose template engine configured to autoescape.

So:

* ``{{ name }}`` substitutes ``context["name"]``, HTML-escaped. Always.
* A value that genuinely is markup — a table of rows this module built — must
  be a ``markupsafe.Markup`` instance, which escapes to itself. There is no
  "raw" syntax to reach for by accident, and no way to opt a value out of
  escaping except by constructing it as markup in Python.
* A placeholder with no matching key raises rather than rendering an empty
  string, so a renamed context key is a failing test and not a blank section in
  a message that has already been sent.

It also keeps the dependency list where it is: ``markupsafe`` is already
installed (Alembic's Mako pulls it in), so none of this adds a package to a
deployment that has a 50 MB serverless bundle limit.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from markupsafe import Markup, escape

#: Where the .html templates live, next to the rest of the backend package.
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "emails"

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class TemplateError(RuntimeError):
    """A template referred to something the caller did not supply."""


@lru_cache(maxsize=16)
def load_template(name: str) -> str:
    """One template's source. Cached — templates do not change at runtime."""
    path = TEMPLATE_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:  # pragma: no cover - a packaging mistake
        raise TemplateError(f"Email template {name!r} is not installed.") from exc


def render(source: str, context: dict[str, Any]) -> str:
    """Substitute ``{{ name }}`` placeholders, escaping every value."""

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise TemplateError(f"Email template referenced unknown value {key!r}.")
        return str(escape(context[key]))

    return _PLACEHOLDER.sub(substitute, source)


def render_template(name: str, context: dict[str, Any]) -> str:
    """Render one template file by name."""
    return render(load_template(name), context)


def render_page(body_template: str, context: dict[str, Any]) -> str:
    """Render a body template and wrap it in the shared frame.

    Every email is one body inside ``base.html``, so the header, the footer and
    the responsive rules exist exactly once. `context` supplies both the body's
    values and the frame's (``subject``, ``preheader``, the two brand blocks).
    """
    body = Markup(render_template(body_template, context))
    return render_template("base.html", {**context, "content": body})


def brand_html(
    logo: Optional[Any],
    *,
    fallback_text: str,
    fallback_color: str,
    align: str = "left",
) -> Markup:
    """A brand rendered as its logo, or as its name when the logo is absent.

    The fallback is text saying the company's name — not a substitute image and
    not an approximation of the mark. If the artwork is not installed, the email
    says who it is from in words and looks deliberate doing it.

    `align` is applied as an auto margin rather than left to the cell's
    ``text-align``: the image is ``display:block``, which is what avoids the
    stray few pixels of descender space Outlook adds under an inline image, and
    a block element ignores its parent's text alignment entirely.
    """
    if logo is None:
        return Markup(
            '<span style="font-family:Helvetica,Arial,sans-serif;font-size:15px;'
            'font-weight:700;letter-spacing:0.06em;text-transform:uppercase;'
            'color:{color};">{text}</span>'
        ).format(color=fallback_color, text=fallback_text)

    margin = "margin:0 0 0 auto;" if align == "right" else "margin:0 auto 0 0;"
    return Markup(
        '<img src="{src}" alt="{alt}" width="{width}" '
        'style="display:block;border:0;outline:none;text-decoration:none;'
        '{margin}width:{width}px;max-width:100%;height:auto;" />'
    ).format(src=logo.src, alt=logo.alt, width=logo.width, margin=Markup(margin))


def detail_rows(rows: list[tuple[str, Any]]) -> Markup:
    """A label/value table body, with every value escaped.

    `rows` carries plain Python values; each one goes through the same escaping
    as any other interpolation, so a username of ``<script>`` renders as that
    text and never as an element. A value of ``None`` or ``""`` is skipped
    entirely rather than printed as "None" — an absent optional field should
    leave no row behind, not an empty one.
    """
    cells: list[Markup] = []
    for label, value in rows:
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        cells.append(
            Markup(
                '<tr>'
                '<td style="padding:10px 16px;border-bottom:1px solid #EDF0F5;'
                'font-family:Helvetica,Arial,sans-serif;font-size:13px;'
                'color:#6B7280;white-space:nowrap;vertical-align:top;width:38%;">{label}</td>'
                '<td style="padding:10px 16px;border-bottom:1px solid #EDF0F5;'
                'font-family:Helvetica,Arial,sans-serif;font-size:14px;'
                'color:#111827;font-weight:600;vertical-align:top;">{value}</td>'
                '</tr>'
            ).format(label=label, value=value)
        )
    return Markup("").join(cells)


def paragraphs(text: str) -> Markup:
    """User-written text as escaped HTML, with its line breaks preserved.

    Blank lines separate paragraphs and single newlines become ``<br>``, which
    is what keeps a bug report's numbered steps readable. The text is escaped
    first and the tags added after, so nothing the user typed can become markup.
    """
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text or "") if block.strip()]
    if not blocks:
        return Markup("")
    rendered = [
        Markup('<p style="margin:0 0 12px 0;">{}</p>').format(
            Markup("<br />").join(line.strip() for line in block.splitlines())
        )
        for block in blocks
    ]
    return Markup("").join(rendered)
