import json
from pathlib import Path
from time import perf_counter

import httpx
from django.core.management.base import BaseCommand, CommandError

from jobs.export import export_jobs
from jobs.linkedin import BASE, LinkedIn
from jobs.models import Company, Job, Run, SearchQuery
from jobs.pipeline import run_details, run_search
from jobs.tests.fixtures import fixture_response


class Command(BaseCommand):
    help = "Run the synthetic benchmark in a dedicated empty PostgreSQL database."

    def add_arguments(self, parser):
        parser.add_argument("destination")
        parser.add_argument("--count", type=int, default=5000, choices=range(5000, 10001),
                            metavar="[5000-10000]")

    def handle(self, **options):
        if any(model.objects.exists() for model in (Job, SearchQuery, Company, Run)):
            raise CommandError("Select a fresh benchmark database using PGDATABASE.")
        path = Path(options["destination"])
        count = options["count"]
        query = SearchQuery.objects.create(keywords="Synthetic benchmark", country="USA")
        with httpx.Client(base_url=BASE, transport=httpx.MockTransport(
            lambda request: fixture_response(request, count),
        )) as client:
            source = LinkedIn(client, interval=0)
            started = perf_counter()
            search = run_search(query, source)
            searched = perf_counter()
            details = run_details(source)
            processed = perf_counter()
        exported = export_jobs(path)
        finished = perf_counter()
        result = {
            "source": "synthetic",
            "query": query.keywords,
            "country": query.country,
            "unique_jobs": Job.objects.count(),
            "companies": Company.objects.count(),
            "search": search.counts,
            "search_reason": search.reason,
            "details": details.counts,
            "details_reason": details.reason,
            "search_seconds": round(searched - started, 2),
            "details_seconds": round(processed - searched, 2),
            "export_seconds": round(finished - processed, 2),
            "csv_rows": exported,
            "csv_bytes": path.stat().st_size,
        }
        self.stdout.write(json.dumps(result, indent=2))
        if search.counts["added"] != count or details.counts["updated"] != count or exported != count:
            raise CommandError("Benchmark yielded an unexpected result.")
