"""Does the page in the browser fall under an admin's website privacy rule?

A rule is entered in Settings > Screenshot Privacy as a domain ("upwork.com")
and a URL pattern ("https://www.upwork.com/*"). `ScreenshotService` asks this
module whether the address bar's URL is covered by a rule before it reads the
screen.

Why this is its own module
--------------------------
The check used to be ``pattern.replace("*", "") in url``: delete the wildcards
and look for what is left as a substring of the URL. That only works when the
pattern happens to be a literal prefix of the exact URL, and it quietly fails
for ordinary rules:

* ``https://*.upwork.com/*`` became ``https://.upwork.com/``, which is not a
  substring of any real address;
* ``https://upwork.com/*`` did not match ``https://www.upwork.com/...``, and
  the reverse also failed, although a browser shows one site for both;
* ``http://`` against the ``https://`` the address-bar reader restores.

A rule that fails to match is not an error anywhere: the screenshot is simply
taken. So these cases are decided here, in one place, with tests.

The rules, all case-insensitive:

* **Scheme is ignored**, on both sides.
* **``www.`` is ignored**, on both sides.
* **The host covers its subdomains.** ``upwork.com`` also covers
  ``community.upwork.com``; for privacy, covering too much is the safe error.
  A leading ``*.`` is the same thing spelled out. ``*`` elsewhere in the host
  matches any run of characters.
* **A path is a prefix.** ``example.com/jobs`` covers ``example.com/jobs/42``;
  no path, or just ``*``, covers the whole site. ``*`` inside a path matches
  any run of characters.
* **A blank pattern falls back to the rule's domain**; with neither there is
  nothing to match, so the rule matches nothing (never everything).
"""
from __future__ import annotations

import re
from typing import Mapping, Optional, Tuple
from urllib.parse import urlsplit

from tracking.app_identity import canonical_domain

_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_PORT = re.compile(r":\d*$")


def _glob(text: str) -> str:
    """A regular expression that matches `text` with `*` as "anything"."""
    return re.escape(text).replace(r"\*", ".*")


def _split_url(url: str) -> Tuple[Optional[str], str]:
    """(canonical host, path and query) of a real address; host is None if unreadable."""
    value = url.strip()
    try:
        parsed = urlsplit(value if _SCHEME.match(value) else f"https://{value}")
        host = canonical_domain(parsed.hostname)
    except ValueError:
        return None, ""
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return host, path


def _split_pattern(pattern: str) -> Tuple[str, str]:
    """(canonical host pattern, path pattern) of an admin-entered rule."""
    bare = _SCHEME.sub("", pattern.strip().lower())
    head, slash, rest = bare.partition("/")
    host = _PORT.sub("", head)
    if host.startswith("www."):
        host = host[4:]
    return host, (slash + rest) if slash else ""


def _host_matches(host: str, pattern_host: str) -> bool:
    if pattern_host.startswith("*."):
        pattern_host = pattern_host[2:]
    if "*" in pattern_host:
        return re.fullmatch(_glob(pattern_host), host) is not None
    return host == pattern_host or host.endswith(f".{pattern_host}")


def _path_matches(path: str, pattern_path: str) -> bool:
    prefix = pattern_path.rstrip("*")
    if prefix in ("", "/"):
        return True
    return re.match(_glob(prefix), path.lower()) is not None


def url_matches_rule(url: Optional[str], rule: Mapping[str, object]) -> bool:
    """True when `url` is covered by the website rule (a `screenshot_urls` row)."""
    if not url or not url.strip():
        return False
    host, path = _split_url(url)
    if not host:
        return False

    pattern = str(rule.get("url_pattern") or "").strip()
    if not pattern:
        pattern = str(rule.get("domain") or "").strip()
    if not pattern:
        return False

    pattern_host, pattern_path = _split_pattern(pattern)
    if not pattern_host:
        return False
    return _host_matches(host, pattern_host) and _path_matches(path, pattern_path)
