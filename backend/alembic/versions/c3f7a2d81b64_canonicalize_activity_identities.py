"""Reclassify historical activity rows onto canonical application identities.

Data only -- no table, column, index or constraint is touched, and no row is
deleted or created. Every change is a rename of a value the desktop already
wrote, from one spelling of an application to the spelling the whole system
now uses for it.

Why this is safe to run on production data
------------------------------------------
The mapping is deterministic and comes from metadata already in the row: the
desktop stored the operating system's own identifier for the application
(``chrome``, ``ms-teams``, ``msedge``), and this rewrites it to that
application's real name. Nothing is inferred, guessed or invented, and a
value the catalogue does not recognise is left exactly as it is -- an
unrecognised program keeps its executable name and stays diagnosable rather
than being swept into a bucket.

Without this pass the same application would exist twice in every report
covering a range that spans the release: ``chrome`` for the days before and
``Google Chrome`` for the days after, each holding part of one person's
browsing, each small enough to be ranked out of the top of the list. That
split is the very thing the release fixes.

The alias table below is a frozen snapshot of
``app/core/activity_identity.ALIAS_INDEX`` taken when this migration was
written, so re-running the migration cannot pick up a later, unreviewed
catalogue change. ``backend/tests/test_activity_classification.py`` asserts
that every pair here still agrees with the live catalogue.

Downgrade is deliberately a no-op: several aliases map to one canonical name
(``chrome``, ``chrome.exe`` and ``Google Chrome`` are all "Google Chrome"),
so reversing the rename would mean choosing one of the original spellings at
random. That would be fabricating history, which is worse than leaving the
rows correctly named.

Revision ID: c3f7a2d81b64
Revises: b8e4d13a7c92
Create Date: 2026-09-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c3f7a2d81b64"
down_revision: Union[str, None] = "b8e4d13a7c92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: (lowercase alias as stored, canonical name to store instead).
#: Frozen snapshot -- see the module docstring.
ALIASES: tuple[tuple[str, str], ...] = (
    ('acrobat', 'Adobe Acrobat'),
    ('acrord32', 'Adobe Acrobat'),
    ('adobe acrobat', 'Adobe Acrobat'),
    ('adobe illustrator', 'Adobe Illustrator'),
    ('adobe photoshop', 'Adobe Photoshop'),
    ('adobe xd', 'Adobe XD'),
    ('adobexd', 'Adobe XD'),
    ('android studio', 'Android Studio'),
    ('apple_terminal', 'Terminal'),
    ('arc', 'Arc'),
    ('asana', 'Asana'),
    ('brave', 'Brave'),
    ('brave browser', 'Brave'),
    ('canva', 'Canva'),
    ('chrome', 'Google Chrome'),
    ('cmd', 'Command Prompt'),
    ('code', 'Visual Studio Code'),
    ('code - insiders', 'Visual Studio Code'),
    ('command prompt', 'Command Prompt'),
    ('cpthost', 'Zoom'),
    ('cursor', 'Cursor'),
    ('dbeaver', 'DBeaver'),
    ('devenv', 'Visual Studio'),
    ('discord', 'Discord'),
    ('docker', 'Docker Desktop'),
    ('docker desktop', 'Docker Desktop'),
    ('eclipse', 'Eclipse'),
    ('edge', 'Microsoft Edge'),
    ('emacs', 'Emacs'),
    ('excel', 'Microsoft Excel'),
    ('explorer', 'File Explorer'),
    ('figma', 'Figma'),
    ('figma_agent', 'Figma'),
    ('file explorer', 'File Explorer'),
    ('finder', 'Finder'),
    ('firefox', 'Mozilla Firefox'),
    ('git bash', 'Git Bash'),
    ('github desktop', 'GitHub Desktop'),
    ('githubdesktop', 'GitHub Desktop'),
    ('goland', 'JetBrains GoLand'),
    ('goland64', 'JetBrains GoLand'),
    ('google chrome', 'Google Chrome'),
    ('google meet', 'Google Meet'),
    ('gvim', 'Vim'),
    ('idea', 'JetBrains IntelliJ IDEA'),
    ('idea64', 'JetBrains IntelliJ IDEA'),
    ('illustrator', 'Adobe Illustrator'),
    ('immersivecontrolpanel', 'Windows Settings'),
    ('insomnia', 'Insomnia'),
    ('intellij idea', 'JetBrains IntelliJ IDEA'),
    ('iterm', 'iTerm'),
    ('iterm2', 'iTerm'),
    ('jetbrains goland', 'JetBrains GoLand'),
    ('jetbrains intellij idea', 'JetBrains IntelliJ IDEA'),
    ('jetbrains phpstorm', 'JetBrains PhpStorm'),
    ('jetbrains pycharm', 'JetBrains PyCharm'),
    ('jetbrains rider', 'JetBrains Rider'),
    ('jetbrains webstorm', 'JetBrains WebStorm'),
    ('jira', 'Jira'),
    ('keynote', 'Keynote'),
    ('lockapp', 'Windows Lock Screen'),
    ('mail', 'Mail'),
    ('microsoft edge', 'Microsoft Edge'),
    ('microsoft excel', 'Microsoft Excel'),
    ('microsoft onenote', 'Microsoft OneNote'),
    ('microsoft outlook', 'Microsoft Outlook'),
    ('microsoft powerpoint', 'Microsoft PowerPoint'),
    ('microsoft teams', 'Microsoft Teams'),
    ('microsoft word', 'Microsoft Word'),
    ('mintty', 'Git Bash'),
    ('monitra', 'Monitra'),
    ('mozilla firefox', 'Mozilla Firefox'),
    ('ms-teams', 'Microsoft Teams'),
    ('msedge', 'Microsoft Edge'),
    ('mysql workbench', 'MySQL Workbench'),
    ('mysqlworkbench', 'MySQL Workbench'),
    ('neovim', 'Vim'),
    ('netbeans', 'NetBeans'),
    ('netbeans64', 'NetBeans'),
    ('notepad', 'Notepad'),
    ('notepad++', 'Notepad++'),
    ('notion', 'Notion'),
    ('numbers', 'Numbers'),
    ('nvim', 'Vim'),
    ('obsidian', 'Obsidian'),
    ('olk', 'Microsoft Outlook'),
    ('onenote', 'Microsoft OneNote'),
    ('opera', 'Opera'),
    ('opera gx', 'Opera'),
    ('opera_gx', 'Opera'),
    ('outlook', 'Microsoft Outlook'),
    ('pages', 'Pages'),
    ('pgadmin', 'pgAdmin'),
    ('pgadmin4', 'pgAdmin'),
    ('photoshop', 'Adobe Photoshop'),
    ('phpstorm', 'JetBrains PhpStorm'),
    ('phpstorm64', 'JetBrains PhpStorm'),
    ('postman', 'Postman'),
    ('powerpnt', 'Microsoft PowerPoint'),
    ('powershell', 'PowerShell'),
    ('preview', 'Preview'),
    ('pwsh', 'PowerShell'),
    ('pycharm', 'JetBrains PyCharm'),
    ('pycharm64', 'JetBrains PyCharm'),
    ('python', 'Monitra'),
    ('pythonw', 'Monitra'),
    ('quicktime player', 'QuickTime Player'),
    ('rider', 'JetBrains Rider'),
    ('rider64', 'JetBrains Rider'),
    ('safari', 'Safari'),
    ('searchapp', 'Windows Search'),
    ('searchhost', 'Windows Search'),
    ('securityhealthhost', 'Windows Security'),
    ('securityhealthsystray', 'Windows Security'),
    ('shellexperiencehost', 'Windows Shell'),
    ('sketch', 'Sketch'),
    ('skype', 'Skype'),
    ('slack', 'Slack'),
    ('sourcetree', 'Sourcetree'),
    ('spotify', 'Spotify'),
    ('startmenuexperiencehost', 'Windows Start Menu'),
    ('studio64', 'Android Studio'),
    ('sublime text', 'Sublime Text'),
    ('sublime_text', 'Sublime Text'),
    ('system preferences', 'System Settings'),
    ('system settings', 'System Settings'),
    ('systemsettings', 'Windows Settings'),
    ('tableplus', 'TablePlus'),
    ('taskmgr', 'Windows Task Manager'),
    ('teams', 'Microsoft Teams'),
    ('telegram', 'Telegram'),
    ('terminal', 'Terminal'),
    ('todoist', 'Todoist'),
    ('tor', 'Tor Browser'),
    ('tor browser', 'Tor Browser'),
    ('trello', 'Trello'),
    ('vim', 'Vim'),
    ('visual studio', 'Visual Studio'),
    ('visual studio code', 'Visual Studio Code'),
    ('vivaldi', 'Vivaldi'),
    ('vlc', 'VLC'),
    ('warp', 'Warp'),
    ('webstorm', 'JetBrains WebStorm'),
    ('webstorm64', 'JetBrains WebStorm'),
    ('whatsapp', 'WhatsApp'),
    ('windows explorer', 'File Explorer'),
    ('windows lock screen', 'Windows Lock Screen'),
    ('windows media player', 'Windows Media Player'),
    ('windows search', 'Windows Search'),
    ('windows security', 'Windows Security'),
    ('windows settings', 'Windows Settings'),
    ('windows shell', 'Windows Shell'),
    ('windows start menu', 'Windows Start Menu'),
    ('windows task manager', 'Windows Task Manager'),
    ('windows terminal', 'Windows Terminal'),
    ('windowsterminal', 'Windows Terminal'),
    ('winword', 'Microsoft Word'),
    ('wmplayer', 'Windows Media Player'),
    ('wt', 'Windows Terminal'),
    ('xcode', 'Xcode'),
    ('zoom', 'Zoom'),
    ('zoom.us', 'Zoom'),
)


def _canonicalize_names(table: str, column: str) -> None:
    """Rewrite every recognised alias in `table.column` to its canonical name.

    One statement for the whole table: the aliases arrive as a VALUES list
    that Postgres joins against, so this is a single indexed pass rather than
    one UPDATE per application. The ``<> canonical`` guard makes it
    idempotent -- a second run matches nothing and writes nothing.
    """
    values = sa.text(
        ", ".join(f"(:alias_{i}, :canonical_{i})" for i in range(len(ALIASES)))
    )
    params = {}
    for index, (alias, canonical) in enumerate(ALIASES):
        params[f"alias_{index}"] = alias
        params[f"canonical_{index}"] = canonical

    op.get_bind().execute(
        sa.text(
            f"""
            UPDATE {table} AS t
               SET {column} = m.canonical
              FROM (VALUES {values}) AS m(alias, canonical)
             WHERE lower(btrim(t.{column})) = m.alias
               AND t.{column} <> m.canonical
            """
        ),
        params,
    )


#: Lowercase the host, drop the trailing root dot and a leading "www.", and
#: nothing else -- exactly what ``activity_identity.canonical_domain`` does,
#: so stored history and newly ingested rows group together.
_CANONICAL_DOMAIN = r"regexp_replace(lower(rtrim(btrim(domain), '.')), '^www\.', '')"


def upgrade() -> None:
    _canonicalize_names("time_entry_app_usage", "application_name")
    # The browser is named from the same catalogue as the application, so a
    # session recorded as "brave" in one table and "Google Chrome" in the
    # other becomes "Brave" in both.
    _canonicalize_names("time_entry_url_usage", "browser_name")
    op.get_bind().execute(
        sa.text(
            f"""
            UPDATE time_entry_url_usage
               SET domain = {_CANONICAL_DOMAIN}
             WHERE domain IS NOT NULL
               AND domain <> {_CANONICAL_DOMAIN}
            """
        )
    )


def downgrade() -> None:
    """Intentionally does nothing. See the module docstring."""
