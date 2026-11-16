import csv
from secrets import token_urlsafe
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
import psycopg
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from jobs.export import export_jobs
from jobs.linkedin import BASE, DETAIL, SEARCH, LinkedIn, SourceLimited
from jobs.models import Company, Job, SearchQuery
from jobs.pipeline import StageBusy, run_details, run_search
from .fixtures import fixture_response


class SourceTests(SimpleTestCase):
    @patch("jobs.linkedin.time.sleep")
    def test_rate_limit_delay_and_country_alias(self, sleep):
        calls = []

        def respond(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return fixture_response(request, 1)

        with httpx.Client(base_url=BASE, transport=httpx.MockTransport(respond)) as client:
            page = LinkedIn(client, 0).search("Python", "USA", 0)
        self.assertEqual(page.size, 1)
        self.assertEqual(calls[0].url.params["location"], "United States")
        sleep.assert_any_call(7)

    def test_large_retry_after_stops(self):
        with httpx.Client(base_url=BASE, transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "600"}),
        )) as client:
            with self.assertRaises(SourceLimited):
                LinkedIn(client, 0).search("Python", "USA", 0)


class PipelineTests(TestCase):
    def setUp(self):
        self.query = SearchQuery.objects.create(keywords="Python", country="USA")
        self.other = SearchQuery.objects.create(keywords="Python", country="Croatia")

    def test_search_details_export_and_repeat(self):
        with httpx.Client(base_url=BASE, transport=httpx.MockTransport(
            lambda request: fixture_response(request, 27),
        )) as client:
            source = LinkedIn(client, 0)
            first = run_search(self.query, source)
            details = run_details(source)
            repeat = run_search(self.other, source)
            second_details = run_details(source)

        self.assertEqual((first.reason, first.counts["added"]), ("exhausted", 27))
        self.assertEqual(details.counts["updated"], 27)
        self.assertEqual(repeat.counts["updated"], 27)
        self.assertEqual(second_details.counts["received"], 0)
        self.assertEqual(Job.objects.count(), 27)
        self.assertEqual(Company.objects.count(), 27)
        job = Job.objects.get(source_id="1")
        self.assertEqual(job.searches.count(), 2)
        self.assertEqual(job.details["employment_type"], "FULL_TIME")
        self.assertEqual(job.company.data["industry"], "Synthetic industry")

        with TemporaryDirectory() as folder:
            path = Path(folder) / "vacancies.csv"
            self.assertEqual(export_jobs(path), 27)
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream, delimiter=";"))
        self.assertEqual(len(rows), 27)
        self.assertIn("Croatia", rows[0]["searches"])
        self.assertEqual(rows[0]["company_website"], "https://example.org/1")

    @patch("jobs.linkedin.time.sleep")
    def test_failures_and_resume(self, sleep):
        def fail_search(request):
            if request.url.path == SEARCH and request.url.params["start"] == "25":
                return httpx.Response(429)
            return fixture_response(request, 26)

        def fail_details(request):
            if request.url.path == f"{DETAIL}1":
                return httpx.Response(503)
            if request.url.path == f"{DETAIL}2":
                return httpx.Response(404)
            if request.url.path == "/company/fixture-3":
                return httpx.Response(503)
            return fixture_response(request, 26)

        with httpx.Client(base_url=BASE, transport=httpx.MockTransport(fail_search)) as client:
            first = run_search(self.query, LinkedIn(client, 0))
        self.assertEqual((first.reason, first.counts["added"]), ("rate_limited", 25))
        with httpx.Client(base_url=BASE, transport=httpx.MockTransport(fail_details)) as client:
            details = run_details(LinkedIn(client, 0))
        self.assertEqual(details.reason, "completed_with_errors")
        self.assertEqual(Job.objects.get(source_id="1").status, Job.Status.RETRY)
        self.assertEqual(Job.objects.get(source_id="2").status, Job.Status.UNAVAILABLE)
        self.assertTrue(Job.objects.get(source_id="3").details["description"])
        self.assertEqual(Job.objects.get(source_id="4").status, Job.Status.DONE)

        with httpx.Client(base_url=BASE, transport=httpx.MockTransport(
            lambda request: fixture_response(request, 26),
        )) as client:
            source = LinkedIn(client, 0)
            self.assertEqual(run_search(self.query, source).counts["added"], 1)
            self.assertEqual(run_details(source).counts["updated"], 3)
        self.assertEqual(Job.objects.filter(status=Job.Status.DONE).count(), 25)

    def test_overlapping_search_is_blocked_without_blocking_details(self):
        params = connection.get_connection_params()
        with psycopg.connect(**params, autocommit=True) as other:
            other.execute("SELECT pg_advisory_lock(1624938790, 1)")
            with self.assertRaises(StageBusy):
                run_search(self.query, None)
            self.assertEqual(run_details(None).reason, "exhausted")

    def test_company_denial_does_not_stop_vacancy_details(self):
        company_requests = []

        def respond(request):
            if request.url.path.startswith("/company/"):
                company_requests.append(request.url.path)
                return httpx.Response(403)
            return fixture_response(request, 2)

        with httpx.Client(base_url=BASE, transport=httpx.MockTransport(respond)) as client:
            source = LinkedIn(client, 0)
            run_search(self.query, source)
            run = run_details(source)
        self.assertEqual(run.reason, "completed_with_errors")
        self.assertEqual(len(company_requests), 1)
        self.assertEqual(Job.objects.filter(status=Job.Status.RETRY)
                         .exclude(details={}).count(), 2)

    def test_repeated_page_preserves_existing_results(self):
        with httpx.Client(base_url=BASE, transport=httpx.MockTransport(
            lambda request: fixture_response(
                httpx.Request("GET", f"{BASE}{SEARCH}", params={"start": 0}), 3,
            ),
        )) as client:
            run = run_search(self.query, LinkedIn(client, 0))
        self.assertEqual((run.reason, run.counts["added"], run.counts["skipped"]),
                         ("repeated_page", 3, 3))

    def test_admin_login_list_filters_and_read_only_details(self):
        password = token_urlsafe(24)
        admin = get_user_model().objects.create_superuser(username="fixture-admin", password=password)
        self.assertNotEqual(admin.password, password)
        self.assertTrue(self.client.login(username=admin.username, password=password))
        job = Job.objects.create(source_id="1", title="Developer", url=f"{BASE}/jobs/view/1/")
        job.searches.add(self.query)
        changelist = reverse("admin:jobs_job_changelist")
        response = self.client.get(changelist, {"q": "Developer", "searches__country": "USA"})
        self.assertContains(response, 'scope="col"')
        self.assertContains(response, 'for="searchbar"')
        self.assertContains(response, job.get_status_display())
        self.assertContains(self.client.get(reverse("admin:jobs_job_change", args=[job.pk])), self.query.keywords)
        self.assertContains(self.client.get(reverse("admin:jobs_searchquery_add")), 'for="id_country"')

    @patch("jobs.admin.subprocess.Popen")
    def test_admin_launches_each_stage(self, spawn):
        user = get_user_model().objects.create_superuser(username="stage-admin")
        self.client.force_login(user)
        search_list = reverse("admin:jobs_searchquery_changelist")
        runs = reverse("admin:jobs_run_changelist")
        for action, command in (("start_search", "search_jobs"), ("start_collection", "collect_jobs")):
            response = self.client.post(search_list, {
                "action": action, "_selected_action": [str(self.query.pk)], "index": 0,
            })
            self.assertRedirects(response, runs)
            self.assertEqual(spawn.call_args.args[0][2:], [command, str(self.query.pk)])

        details = reverse("admin:jobs_job_run_details")
        self.assertEqual(self.client.get(details).status_code, 405)
        self.assertRedirects(self.client.post(details), runs)
        self.assertEqual(spawn.call_args.args[0][2:], ["detail_jobs"])
        self.assertContains(self.client.get(runs), 'aria-pressed="false"')

    @patch("jobs.management.commands.collect_jobs.live_source")
    def test_combined_collection_processes_only_selected_query(self, live_source):
        unrelated = Job.objects.create(source_id="100", title="Unrelated", url=f"{BASE}/jobs/view/100/")
        unrelated.searches.add(self.other)
        with httpx.Client(base_url=BASE, transport=httpx.MockTransport(
            lambda request: fixture_response(request, 1),
        )) as client:
            live_source.return_value.__enter__.return_value = LinkedIn(client, 0)
            call_command("collect_jobs", self.query.pk)
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.status, Job.Status.PENDING)
        self.assertEqual(Job.objects.get(source_id="1").status, Job.Status.DONE)
