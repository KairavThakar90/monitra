"""
activity_identity — the backend's copy of the application-identity catalogue.

The desktop resolves an application's canonical name before it queues an
activity record (`desktop/tracking/app_identity.py`), and this module
resolves it again on ingest. The second pass is not redundant:

* A desktop that has not been updated yet keeps sending the spelling it
  always sent (``chrome``, ``ms-teams``, ``python``). Without a server-side
  pass those installs would write a second row for every application,
  forever, and every report would show one product under two names.
* ``/time-entries/{id}/app-usage`` and ``/url-usage`` are ordinary HTTP
  endpoints. They also serve ``curl``, replayed requests and any future
  client, and the backend validates and canonicalizes everything it stores
  regardless of what a client claims to have done already — the same
  principle ``docs/VALIDATION.md`` states for every other field.

This is canonicalization of machine-captured telemetry, not scrubbing of
something a person typed: nobody wrote ``chrome.exe``, the operating system
reported it, and ``Google Chrome`` is the same fact under the name the
product actually has. User-entered text is still rejected rather than
quietly rewritten.

**This catalogue and the desktop's must stay identical.**
``desktop/tests/test_activity_classification.py`` reads both files and fails
if they diverge, the same way the two validation rule sets are kept in step.
Change one, change the other.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

#: Upper bound on a stored application name, matching
#: ``time_entry_app_usage.application_name`` (VARCHAR(255)).
MAX_APPLICATION_NAME_LENGTH = 255


class ClassificationStatus:
    """How confidently an activity record's application was identified.

    Derived from the name, never stored: adding a column would create a
    second source of truth for something the name already answers. See the
    desktop module's docstring.
    """

    CLASSIFIED = "classified"
    PARTIALLY_CLASSIFIED = "partially_classified"
    UNKNOWN = "unknown"


#: Canonical product name -> every spelling that means it.
#: Kept byte-for-byte in step with ``desktop/tracking/app_identity.py``.
APPLICATION_CATALOGUE: Dict[str, Tuple[str, ...]] = {
    # ── Browsers ─────────────────────────────────────────────────────────
    "Google Chrome": ("chrome", "google chrome", "chrome.exe"),
    "Microsoft Edge": ("msedge", "microsoft edge", "msedge.exe", "edge"),
    "Mozilla Firefox": ("firefox", "mozilla firefox", "firefox.exe"),
    "Brave": ("brave", "brave browser", "brave.exe"),
    "Vivaldi": ("vivaldi", "vivaldi.exe"),
    "Opera": ("opera", "opera.exe", "opera gx", "opera_gx"),
    "Safari": ("safari",),
    "Arc": ("arc",),
    "Tor Browser": ("tor browser", "tor"),

    # ── Editors and IDEs ─────────────────────────────────────────────────
    "Visual Studio Code": ("code", "code.exe", "visual studio code", "code - insiders"),
    "Visual Studio": ("devenv", "devenv.exe", "visual studio"),
    "Cursor": ("cursor", "cursor.exe"),
    "Sublime Text": ("sublime_text", "sublime_text.exe", "sublime text"),
    "JetBrains PyCharm": ("pycharm64", "pycharm", "pycharm.exe", "pycharm64.exe"),
    "JetBrains IntelliJ IDEA": ("idea64", "idea", "idea64.exe", "intellij idea"),
    "JetBrains WebStorm": ("webstorm64", "webstorm", "webstorm64.exe", "webstorm.exe"),
    "JetBrains Rider": ("rider64", "rider", "rider64.exe"),
    "JetBrains GoLand": ("goland64", "goland", "goland64.exe"),
    "JetBrains PhpStorm": ("phpstorm64", "phpstorm", "phpstorm64.exe"),
    "Android Studio": ("studio64", "studio64.exe", "android studio"),
    "Xcode": ("xcode",),
    "Notepad++": ("notepad++", "notepad++.exe"),
    "Notepad": ("notepad", "notepad.exe"),
    "Vim": ("vim", "gvim", "nvim", "neovim"),
    "Emacs": ("emacs", "emacs.exe"),
    "Eclipse": ("eclipse", "eclipse.exe"),
    "NetBeans": ("netbeans", "netbeans64", "netbeans64.exe"),

    # ── Terminals and shells ─────────────────────────────────────────────
    "Windows Terminal": ("windowsterminal", "windowsterminal.exe", "wt"),
    "Command Prompt": ("cmd", "cmd.exe"),
    "PowerShell": ("powershell", "powershell.exe", "pwsh", "pwsh.exe"),
    "Git Bash": ("mintty", "mintty.exe", "git bash"),
    "Terminal": ("terminal", "apple_terminal"),
    "iTerm": ("iterm", "iterm2"),
    "Warp": ("warp", "warp.exe"),

    # ── Communication ────────────────────────────────────────────────────
    "Microsoft Teams": ("ms-teams", "ms-teams.exe", "teams", "teams.exe", "microsoft teams"),
    "Slack": ("slack", "slack.exe"),
    "Zoom": ("zoom", "zoom.exe", "zoom.us", "cpthost"),
    "Discord": ("discord", "discord.exe"),
    "Skype": ("skype", "skype.exe"),
    "Google Meet": ("google meet",),
    "Microsoft Outlook": ("outlook", "outlook.exe", "microsoft outlook", "olk"),
    "Mail": ("mail",),
    "Telegram": ("telegram", "telegram.exe"),
    "WhatsApp": ("whatsapp", "whatsapp.exe"),

    # ── Office and documents ─────────────────────────────────────────────
    "Microsoft Word": ("winword", "winword.exe", "microsoft word"),
    "Microsoft Excel": ("excel", "excel.exe", "microsoft excel"),
    "Microsoft PowerPoint": ("powerpnt", "powerpnt.exe", "microsoft powerpoint"),
    "Microsoft OneNote": ("onenote", "onenote.exe", "microsoft onenote"),
    "Adobe Acrobat": ("acrobat", "acrord32", "acrobat.exe", "acrord32.exe", "adobe acrobat"),
    "Preview": ("preview",),
    "Pages": ("pages",),
    "Numbers": ("numbers",),
    "Keynote": ("keynote",),

    # ── Design ───────────────────────────────────────────────────────────
    "Figma": ("figma", "figma.exe", "figma_agent"),
    "Adobe Photoshop": ("photoshop", "photoshop.exe", "adobe photoshop"),
    "Adobe Illustrator": ("illustrator", "illustrator.exe", "adobe illustrator"),
    "Adobe XD": ("adobe xd", "adobexd"),
    "Canva": ("canva", "canva.exe"),
    "Sketch": ("sketch",),

    # ── Developer tools ──────────────────────────────────────────────────
    "Docker Desktop": ("docker desktop", "docker", "docker desktop.exe"),
    "Postman": ("postman", "postman.exe"),
    "Insomnia": ("insomnia", "insomnia.exe"),
    "GitHub Desktop": ("github desktop", "githubdesktop", "githubdesktop.exe"),
    "Sourcetree": ("sourcetree", "sourcetree.exe"),
    "TablePlus": ("tableplus", "tableplus.exe"),
    "DBeaver": ("dbeaver", "dbeaver.exe"),
    "pgAdmin": ("pgadmin4", "pgadmin", "pgadmin4.exe"),
    "MySQL Workbench": ("mysqlworkbench", "mysqlworkbench.exe", "mysql workbench"),

    # ── Productivity ─────────────────────────────────────────────────────
    "Notion": ("notion", "notion.exe"),
    "Obsidian": ("obsidian", "obsidian.exe"),
    "Trello": ("trello", "trello.exe"),
    "Jira": ("jira",),
    "Asana": ("asana",),
    "Todoist": ("todoist", "todoist.exe"),

    # ── The OS shell ─────────────────────────────────────────────────────
    "File Explorer": ("explorer", "explorer.exe", "windows explorer"),
    "Finder": ("finder",),
    "Windows Settings": ("systemsettings", "systemsettings.exe", "immersivecontrolpanel"),
    "System Settings": ("system settings", "system preferences"),
    "Windows Search": ("searchapp", "searchapp.exe", "searchhost", "searchhost.exe"),
    "Windows Start Menu": ("startmenuexperiencehost", "startmenuexperiencehost.exe"),
    "Windows Shell": ("shellexperiencehost", "shellexperiencehost.exe"),
    "Windows Task Manager": ("taskmgr", "taskmgr.exe"),
    "Windows Lock Screen": ("lockapp", "lockapp.exe"),
    "Windows Security": ("securityhealthsystray", "securityhealthhost.exe"),

    # ── Media ────────────────────────────────────────────────────────────
    "Spotify": ("spotify", "spotify.exe"),
    "VLC": ("vlc", "vlc.exe"),
    "Windows Media Player": ("wmplayer", "wmplayer.exe"),
    "QuickTime Player": ("quicktime player",),

    # ── This application ─────────────────────────────────────────────────
    "Monitra": ("monitra", "monitra.exe", "python", "python.exe", "pythonw", "pythonw.exe"),
}


def _build_index() -> Dict[str, str]:
    """Flatten the catalogue into alias -> canonical name.

    Raises on a duplicate alias: two products claiming the same executable
    is a mistake in the table above, and a silent last-one-wins would make
    resolution depend on dict insertion order.
    """
    index: Dict[str, str] = {}
    for canonical, aliases in APPLICATION_CATALOGUE.items():
        for alias in (canonical,) + tuple(aliases):
            key = _normalize_key(alias)
            if not key:
                continue
            existing = index.get(key)
            if existing is not None and existing != canonical:
                raise ValueError(
                    f"alias {alias!r} is claimed by both {existing!r} and {canonical!r}"
                )
            index[key] = canonical
    return index


def _normalize_key(value: Optional[str]) -> Optional[str]:
    """Reduce an OS-reported identifier to its comparable form."""
    if not value:
        return None
    text = str(value).strip().strip('"').rstrip("\\/")
    if not text:
        return None
    text = os.path.basename(text.replace("\\", "/"))
    lowered = text.lower()
    for suffix in (".exe", ".app", ".bat", ".cmd"):
        if lowered.endswith(suffix):
            text = text[: -len(suffix)]
            break
    text = text.strip()
    return text.lower() or None


def _raw_identifier(value: Optional[str]) -> Optional[str]:
    """The same reduction, keeping the OS's own casing.

    This is what an application outside the catalogue is stored under, so it
    stays recognisable and the row stays diagnosable.
    """
    if not value:
        return None
    text = str(value).strip().strip('"').rstrip("\\/")
    if not text:
        return None
    text = os.path.basename(text.replace("\\", "/"))
    lowered = text.lower()
    for suffix in (".exe", ".app", ".bat", ".cmd"):
        if lowered.endswith(suffix):
            text = text[: -len(suffix)]
            break
    text = text.strip()
    return text[:MAX_APPLICATION_NAME_LENGTH] or None


#: alias -> canonical, built once at import.
ALIAS_INDEX: Dict[str, str] = _build_index()


def canonical_application_name(value: Optional[str]) -> Optional[str]:
    """The catalogue's name for `value`, or `value`'s own identifier.

    Idempotent: a name that is already canonical resolves to itself, so the
    desktop and the backend cannot disagree by both normalising. Returns
    ``None`` for input with no identifier in it at all — never a
    placeholder, and never a catch-all bucket.
    """
    key = _normalize_key(value)
    if key is None:
        return None
    canonical = ALIAS_INDEX.get(key)
    if canonical is not None:
        return canonical
    return _raw_identifier(value)


def classification_status(value: Optional[str]) -> str:
    """How `value` was identified. Derived, for diagnostics and tests."""
    key = _normalize_key(value)
    if key is None:
        return ClassificationStatus.UNKNOWN
    if key in ALIAS_INDEX:
        return ClassificationStatus.CLASSIFIED
    return ClassificationStatus.PARTIALLY_CLASSIFIED


def canonical_domain(value: Optional[str]) -> Optional[str]:
    """Reduce a hostname to the form a report groups on.

    Lowercased, with the trailing root dot and a leading ``www.`` removed,
    so ``www.github.com`` and ``github.com`` are one row rather than two.
    Nothing else is stripped: ``docs.google.com`` and ``mail.google.com``
    are genuinely different places to spend an afternoon.
    """
    if not value:
        return None
    host = str(value).strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None
