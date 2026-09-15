"""
app_identity — the one place that turns what the OS reports about a
foreground window into the identity an activity record is stored under.

Why this module exists
----------------------
`tracking/active_window.py` reports what Windows and macOS actually hand
back: on Windows the executable's base name (``chrome``, ``msedge``,
``ms-teams``, ``Code``), on macOS ``NSWorkspace.localizedName()``
(``Google Chrome``, ``Visual Studio Code``). Those are two different
spellings of the same product, and neither is what a person recognises in a
report. Storing them verbatim meant one application arrived in the database
under several names, each holding a slice of the time — which is how a
report ends up with a long tail of small rows and a large "everything else"
remainder.

So every activity record now resolves its identity here first, and the
backend resolves it again on ingest from the same catalogue
(`backend/app/core/activity_identity.py`) so that an old desktop client
cannot keep writing a second spelling forever. The two catalogues are kept
in step mechanically by `desktop/tests/test_activity_classification.py`,
the same way the two validation rule sets are.

What this module will not do
----------------------------
* **It never invents an identity.** If the OS gave no usable process
  identifier there is no application to record, and `resolve_application`
  says so (`ClassificationStatus.UNKNOWN`, name ``None``) instead of
  returning a placeholder. Callers record nothing in that case.
* **It never falls back to a catch-all bucket.** An application that is not
  in the catalogue keeps its real executable identifier, verbatim, so the
  row stays diagnosable — someone can look at it and see exactly which
  program it was.
* **It never identifies an application by its window title.** Titles change
  with every file, tab and notification; an identity that changes every few
  seconds is not an identity. The title is still captured alongside, as
  context.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

#: Upper bound on a stored application name. Matches
#: ``time_entry_app_usage.application_name`` (VARCHAR(255)) and the
#: ``AppUsageCreate.application_name`` schema bound, so a long identifier is
#: shortened here rather than having the whole upload batch rejected.
MAX_APPLICATION_NAME_LENGTH = 255


class ClassificationStatus:
    """How confidently an activity record's application was identified.

    Derived, never stored: the name itself already carries the answer, and
    an extra column would be a second source of truth to keep in step. It is
    returned so that callers can log it, tests can assert on it, and the
    reason a record could not be classified is never guesswork.
    """

    #: The process matched the catalogue; `name` is the product's real name.
    CLASSIFIED = "classified"
    #: A genuine, stable process identifier that the catalogue does not know.
    #: `name` is that identifier, verbatim — diagnosable, not a bucket.
    PARTIALLY_CLASSIFIED = "partially_classified"
    #: No process identity at all. There is nothing to record.
    UNKNOWN = "unknown"


class UnclassifiedReason:
    """Why an identity could not be established. Exposed so an operator
    reading a log can tell "this machine has no supported API" apart from
    "this one application is not in the catalogue"."""

    #: The catalogue has no entry for an otherwise valid process identifier.
    NOT_IN_CATALOGUE = "not_in_catalogue"
    #: The OS reported no foreground window, or refused the process query.
    NO_PROCESS_IDENTIFIER = "no_process_identifier"


@dataclass(frozen=True)
class ApplicationIdentity:
    """The canonical identity of one observed application.

    :param name: What the record is stored and reported under. ``None``
        exactly when `status` is ``UNKNOWN`` — the signal to record nothing.
    :param key: A stable, lowercase matching key. Two observations of the
        same product share it even when the OS spelled them differently, so
        it is what de-duplication and tests compare on.
    :param process_name: The raw identifier the OS reported, kept whatever
        the outcome so a row can always be traced back to a real program.
    :param status: See `ClassificationStatus`.
    :param reason: Set only when `status` is not ``CLASSIFIED``.
    """

    name: Optional[str]
    key: Optional[str]
    process_name: Optional[str]
    status: str
    reason: Optional[str] = None

    @property
    def is_recordable(self) -> bool:
        """Whether there is an application here worth storing at all."""
        return bool(self.name)


#: Canonical product name -> every spelling that means it.
#:
#: Keys on the left are exactly what gets stored. Entries on the right are
#: lowercase and cover both platforms: a Windows executable base name (with
#: or without ``.exe``) and, where it differs, the macOS
#: ``NSWorkspace.localizedName()``. The canonical name is always an alias of
#: itself, which is what makes resolution idempotent — re-resolving an
#: already-canonical name must not change it, or the backend's defensive
#: second pass would rewrite what the desktop already got right.
#:
#: To add an application: add one line. Do not add a second lookup
#: elsewhere, and do not special-case a name at a call site.
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
    # These are real, nameable programs a person spends time in. Reporting
    # "explorer" tells a user nothing; "File Explorer" is the same thing
    # under the name the OS itself uses for it.
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
    # Monitra runs as a bare Python process in development and as
    # ``Monitra.exe`` once packaged. Both are the same product, and reporting
    # the interpreter's name instead of the product's was actively
    # misleading — a developer's day showed hours of "python".
    "Monitra": ("monitra", "monitra.exe", "python", "python.exe", "pythonw", "pythonw.exe"),
}


def _build_index() -> Dict[str, str]:
    """Flatten the catalogue into alias -> canonical name.

    Raises on a duplicate alias: two products claiming the same executable
    is a mistake in the table above, and a silent last-one-wins would make
    the resolution order depend on dict insertion order.
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
    """Reduce an OS-reported identifier to its comparable form.

    Handles all three shapes the two platforms produce: a full executable
    path (``C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe``), a
    bare executable name (``chrome.exe``), and a macOS bundle path or
    display name (``/Applications/Google Chrome.app``, ``Google Chrome``).
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
    return text.lower() or None


def _raw_identifier(value: Optional[str]) -> Optional[str]:
    """The same reduction as `_normalize_key`, but keeping the OS's casing.

    This is what an unclassified record is stored under, so it has to stay
    recognisable: ``ShellExperienceHost``, not ``shellexperiencehost``.
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


#: alias -> canonical, built once at import. Lookups are dict hits, so
#: resolution costs nothing measurable even at the two-second sample rate.
ALIAS_INDEX: Dict[str, str] = _build_index()


def canonical_application_name(value: Optional[str]) -> Optional[str]:
    """The catalogue's name for `value`, or `value`'s own identifier.

    Never returns a placeholder: ``None`` in, ``None`` out. This is the
    narrow form used where only a name is available (the backend's ingest
    validator, an already-stored row); `resolve_application` is the richer
    entry point that also reports how the answer was reached.
    """
    key = _normalize_key(value)
    if key is None:
        return None
    canonical = ALIAS_INDEX.get(key)
    if canonical is not None:
        return canonical
    return _raw_identifier(value)


def resolve_application(
    process_name: Optional[str] = None,
    executable_path: Optional[str] = None,
) -> ApplicationIdentity:
    """Identify the application behind one foreground-window observation.

    The executable path is consulted first because it is the most stable
    thing the OS offers: a process can be renamed in a task list, and a
    macOS display name is localized, but the binary on disk is the same
    binary. The process/display name is the fallback, and is all macOS
    gives for some applications.

    A window title is deliberately not accepted here. See the module
    docstring.
    """
    for candidate in (executable_path, process_name):
        key = _normalize_key(candidate)
        if key is None:
            continue
        canonical = ALIAS_INDEX.get(key)
        if canonical is not None:
            return ApplicationIdentity(
                name=canonical,
                key=key,
                process_name=_raw_identifier(candidate) or _raw_identifier(process_name),
                status=ClassificationStatus.CLASSIFIED,
            )

    # Not in the catalogue. Keep whatever real identifier we were given,
    # spelled the way the OS spelled it, rather than discarding it into a
    # bucket: the row stays traceable to an actual program on an actual
    # machine, and adding it to the catalogue later is a one-line change.
    raw = _raw_identifier(executable_path) or _raw_identifier(process_name)
    if raw:
        return ApplicationIdentity(
            name=raw,
            key=_normalize_key(raw),
            process_name=raw,
            status=ClassificationStatus.PARTIALLY_CLASSIFIED,
            reason=UnclassifiedReason.NOT_IN_CATALOGUE,
        )

    # Nothing identified the process: no foreground window, a failed or
    # refused OS query, or a platform with no active-window API at all.
    # There is no application here, so there is nothing to record.
    return ApplicationIdentity(
        name=None,
        key=None,
        process_name=None,
        status=ClassificationStatus.UNKNOWN,
        reason=UnclassifiedReason.NO_PROCESS_IDENTIFIER,
    )


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
