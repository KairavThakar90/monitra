"""
The backend's half of activity classification.

Three things are protected here.

**Ingest canonicalization.** The desktop already resolves an application's
canonical name before queueing, but these endpoints also serve desktops that
have not been updated, replayed requests, and anything else that can speak
HTTP. Without a server-side pass, an old client would keep writing ``chrome``
alongside a new client's ``Google Chrome`` -- one browser occupying two rows
of every report, each holding part of the same person's browsing, each small
enough to be ranked out of the top of the list. That split is what produced
the enormous unnamed remainder the Reports page used to draw.

**The aggregation's own total.** Every ranked page reports the seconds held
by every row the filters matched, so a part-to-whole chart divides by the
same measure its slices are drawn from.

**The migration's frozen alias table.** It was snapshotted from the live
catalogue and must still agree with it.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core.activity_identity import (
    ALIAS_INDEX,
    APPLICATION_CATALOGUE,
    ClassificationStatus,
    canonical_application_name,
    canonical_domain,
    classification_status,
)
from app.react_apis.reports_page.repository import ReportFilters, ReportsPageRepository
from app.react_apis.reports_page.service import ReportsPageService
from app.schemas.time_entry_app_usage import AppUsageCreate
from app.schemas.url_usage import URLUsageCreate
from app.services.url_usage_service import normalize_url

from datetime import date, datetime, timezone


def _filters(**overrides) -> ReportFilters:
    base = dict(
        organization_id=10,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 7),
        start_time=datetime(2026, 8, 31, 18, 30, tzinfo=timezone.utc),
        end_time=datetime(2026, 9, 7, 18, 30, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return ReportFilters(**base)


class IngestCanonicalizationTests(unittest.TestCase):
    def test_an_old_client_s_executable_name_is_stored_under_the_product_name(self):
        for sent, stored in (
            ("chrome", "Google Chrome"),
            ("chrome.exe", "Google Chrome"),
            ("msedge", "Microsoft Edge"),
            ("ms-teams", "Microsoft Teams"),
            ("Code", "Visual Studio Code"),
            ("explorer", "File Explorer"),
            ("python", "Monitra"),
        ):
            with self.subTest(sent=sent):
                payload = AppUsageCreate(
                    application_name=sent, duration_seconds=30, window_title="x"
                )
                self.assertEqual(payload.application_name, stored)

    def test_an_already_canonical_name_is_left_exactly_as_it_is(self):
        """The desktop normalises too. If the two passes disagreed, every
        record would be rewritten twice and could settle anywhere."""
        for canonical in APPLICATION_CATALOGUE:
            with self.subTest(name=canonical):
                payload = AppUsageCreate(application_name=canonical, duration_seconds=1)
                self.assertEqual(payload.application_name, canonical)

    def test_an_application_the_catalogue_does_not_know_keeps_its_own_name(self):
        """Acceptance criterion 7: genuinely unknown activity stays
        diagnosable. It is never renamed into a catch-all."""
        payload = AppUsageCreate(application_name="Antigravity IDE", duration_seconds=30)
        self.assertEqual(payload.application_name, "Antigravity IDE")
        self.assertEqual(
            classification_status(payload.application_name),
            ClassificationStatus.PARTIALLY_CLASSIFIED,
        )

    def test_a_name_with_no_identifier_in_it_is_refused_not_stored_blank(self):
        """Rejecting is right here: there is no application to record, and
        inventing one -- which is what the old "Others" fallback amounted to
        -- would put fiction in the database."""
        for empty in ("", "   ", "\\", "/"):
            with self.subTest(value=repr(empty)):
                with pytest.raises(ValidationError):
                    AppUsageCreate(application_name=empty, duration_seconds=30)

    def test_nothing_is_ever_stored_under_a_catch_all_name(self):
        forbidden = {"other", "others", "unknown", "unknown application",
                     "idle/system", "uncategorized", "unclassified"}
        for sent in ("chrome", "Antigravity IDE", "ShellExperienceHost", "python"):
            with self.subTest(sent=sent):
                payload = AppUsageCreate(application_name=sent, duration_seconds=1)
                self.assertNotIn(payload.application_name.lower(), forbidden)

    def test_a_window_title_is_still_captured_verbatim(self):
        """Titles are context, not identity, and are not content-checked: a
        real tab can be titled "<script> tutorial"."""
        payload = AppUsageCreate(
            application_name="chrome",
            window_title="<script> tutorial & more — 5 < 10",
            duration_seconds=30,
        )
        self.assertEqual(payload.window_title, "<script> tutorial & more — 5 < 10")


class UrlIngestTests(unittest.TestCase):
    def test_the_browser_is_named_the_same_way_application_usage_names_it(self):
        """A session recorded as "brave" in one table and "Google Chrome" in
        the other described the same window two different ways."""
        for sent, stored in (
            ("brave", "Brave"),
            ("chrome", "Google Chrome"),
            ("Google Chrome", "Google Chrome"),
            ("msedge", "Microsoft Edge"),
            ("firefox", "Mozilla Firefox"),
        ):
            with self.subTest(sent=sent):
                payload = URLUsageCreate(
                    time_entry_id=100, browser_name=sent,
                    domain="github.com", duration_seconds=10,
                )
                self.assertEqual(payload.browser_name, stored)
                self.assertEqual(canonical_application_name(sent), stored)

    def test_www_is_stripped_so_one_site_is_one_row(self):
        payload = URLUsageCreate(
            time_entry_id=100, browser_name="Google Chrome",
            domain="WWW.Bing.com", duration_seconds=10,
        )
        self.assertEqual(payload.domain, "bing.com")

    def test_a_real_subdomain_survives(self):
        payload = URLUsageCreate(
            time_entry_id=100, browser_name="Google Chrome",
            domain="docs.google.com", duration_seconds=10,
        )
        self.assertEqual(payload.domain, "docs.google.com")

    def test_the_url_itself_is_not_rewritten(self):
        """The domain is what a report groups on; the URL is the real address
        somebody visited, and is stored as it was."""
        payload = URLUsageCreate(
            time_entry_id=100, browser_name="Google Chrome",
            domain="www.bing.com", url="https://www.bing.com/search?q=x",
            duration_seconds=10,
        )
        self.assertEqual(payload.domain, "bing.com")
        self.assertEqual(str(payload.url), "https://www.bing.com/search?q=x")

    def test_the_service_does_not_undo_the_schema_s_canonicalization(self):
        """`normalize_url` re-derives the domain from the URL's own hostname,
        which is the better authority. It used to lowercase and stop there,
        quietly putting the record back under `www.github.com` after the
        schema had already resolved it -- so one site occupied two rows and
        its time was split between them. Both paths end in one function now.
        """
        domain, url = normalize_url("https://www.github.com/monitra", "www.github.com")
        self.assertEqual(domain, "github.com")
        self.assertEqual(url, "https://www.github.com/monitra")

    def test_the_service_agrees_with_the_schema_on_every_shape(self):
        for supplied_domain, supplied_url in (
            ("www.github.com", "https://www.github.com/x"),
            ("github.com", "https://www.github.com/x"),
            ("www.github.com", None),
            ("docs.google.com", "https://docs.google.com/document/d/1"),
            ("WWW.Bing.com", "https://WWW.Bing.com/search?q=x"),
        ):
            with self.subTest(domain=supplied_domain, url=supplied_url):
                from_service, _ = normalize_url(supplied_url, supplied_domain)
                from_schema = URLUsageCreate(
                    time_entry_id=1, browser_name="Google Chrome",
                    domain=supplied_domain, url=supplied_url, duration_seconds=1,
                ).domain
                self.assertEqual(from_service, from_schema)

    def test_a_record_with_no_url_is_still_accepted(self):
        """Acceptance criterion 4. Where no address bar can be read -- macOS,
        Linux, Firefox without accessibility -- the browser session is still
        real; only its URL is unknown."""
        payload = URLUsageCreate(
            time_entry_id=100, browser_name="Mozilla Firefox",
            domain="github.com", url=None, duration_seconds=10,
        )
        self.assertIsNone(payload.url)
        self.assertEqual(payload.browser_name, "Mozilla Firefox")


class PageTotalTests(unittest.TestCase):
    """The denominator the Reports page's distribution chart divides by.

    It used to divide the visible rows by the *summary* total instead. On the
    Apps and URLs tabs those are different measures -- session time versus
    separately-measured application time -- so the remainder absorbed every
    second the desktop had never claimed to attribute, and drew it as a single
    enormous unnamed slice.
    """

    @staticmethod
    def _row(id_, name, seconds):
        return SimpleNamespace(id=id_, name=name, total_seconds=seconds,
                               avg_activity=60.0, total_members=1, total_tasks=1)

    def test_a_page_carries_the_seconds_of_every_matching_row(self):
        rows = [self._row(1, "Google Chrome", 3600)]
        with patch.object(
            ReportsPageRepository, "usage", return_value=(rows, 42, 154800.0)
        ):
            page = ReportsPageService.apps(
                None, _filters(), None, "total_hours", "desc", 1, 20
            )
        self.assertEqual(page["total"], 42)
        self.assertEqual(page["total_seconds"], 154800)
        self.assertEqual(page["total_hours"], 43.0)
        # The page shows one row; the whole is 42 rows' worth.
        self.assertEqual(len(page["items"]), 1)

    def test_the_whole_is_never_smaller_than_the_visible_rows(self):
        """A remainder computed against this can only ever be >= 0, which is
        what stops an "everything else" slice being drawn from nothing."""
        rows = [self._row(1, "Google Chrome", 3600), self._row(2, "Slack", 1800)]
        with patch.object(
            ReportsPageRepository, "usage", return_value=(rows, 2, 5400.0)
        ):
            page = ReportsPageService.apps(
                None, _filters(), None, "total_hours", "desc", 1, 20
            )
        visible = sum(item["total_hours"] for item in page["items"])
        self.assertAlmostEqual(page["total_hours"], visible, places=2)

    def test_every_tab_reports_it(self):
        for name, repo_attr, kwargs in (
            ("projects", "projects", {}),
            ("tasks", "tasks", {}),
            ("apps", "usage", {}),
            ("urls", "usage", {}),
        ):
            with self.subTest(tab=name):
                with patch.object(
                    ReportsPageRepository, repo_attr,
                    return_value=([self._row(1, "x", 60)], 1, 60.0),
                ):
                    page = getattr(ReportsPageService, name)(
                        None, _filters(), None, "total_hours", "desc", 1, 20, **kwargs
                    )
                self.assertEqual(page["total_seconds"], 60)

    def test_the_count_and_the_sum_come_from_one_aggregate(self):
        """One statement, so exposing the denominator adds no query and
        cannot disagree with the row count it ships beside."""
        executed = []

        class _Session:
            def execute(self, statement):
                executed.append(statement)
                return SimpleNamespace(all=lambda: [], one=lambda: (0, 0.0))

            def scalar(self, statement):
                executed.append(statement)
                return 0

        ReportsPageRepository.usage(
            _Session(), _filters(), "app", None, "total_hours", "desc", 1, 20
        )
        # One aggregate statement, one page statement. Nothing else.
        self.assertEqual(len(executed), 2)
        aggregate = str(executed[0].compile(compile_kwargs={"literal_binds": False}))
        self.assertIn("count(", aggregate)
        self.assertIn("sum(report_rows.total_seconds)", aggregate)


class MigrationSnapshotTests(unittest.TestCase):
    """The data migration carries a frozen copy of the alias table so that
    re-running it cannot pick up a later, unreviewed catalogue change. Frozen
    is not the same as free to contradict the catalogue."""

    @staticmethod
    def _migration():
        path = (
            Path(__file__).resolve().parents[1]
            / "alembic" / "versions"
            / "c3f7a2d81b64_canonicalize_activity_identities.py"
        )
        spec = importlib.util.spec_from_file_location("_canonicalize_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_every_frozen_alias_still_agrees_with_the_catalogue(self):
        for alias, canonical in self._migration().ALIASES:
            with self.subTest(alias=alias):
                self.assertEqual(ALIAS_INDEX.get(alias), canonical)

    def test_the_snapshot_is_lowercase_so_the_sql_comparison_matches(self):
        """The UPDATE compares ``lower(btrim(column))`` against the alias, so
        an alias with a capital in it would silently never match."""
        for alias, _canonical in self._migration().ALIASES:
            self.assertEqual(alias, alias.lower())

    def test_the_downgrade_does_not_guess_at_the_original_spelling(self):
        """Several aliases map to one name, so reversing the rename would mean
        inventing history."""
        module = self._migration()
        module.downgrade()  # must not raise, and must not touch anything

    def test_the_domain_expression_matches_the_runtime_one(self):
        """Stored history and newly ingested rows have to group together."""
        module = self._migration()
        self.assertIn("www", module._CANONICAL_DOMAIN)
        self.assertIn("lower(", module._CANONICAL_DOMAIN)
        # And the runtime function it mirrors.
        self.assertEqual(canonical_domain("WWW.Bing.com."), "bing.com")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
