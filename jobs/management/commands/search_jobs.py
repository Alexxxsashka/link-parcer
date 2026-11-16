from django.core.management.base import BaseCommand, CommandError

from jobs.models import SearchQuery
from jobs.pipeline import StageBusy, live_source, run_search


class Command(BaseCommand):
    help = "Collect pages for a saved search query."

    def add_arguments(self, parser):
        parser.add_argument("query_id", type=int)
        parser.add_argument("--max-pages", type=int)

    def handle(self, **options):
        limit = options["max_pages"]
        if limit is not None and limit < 1:
            raise CommandError("--max-pages must be positive.")
        try:
            query = SearchQuery.objects.get(pk=options["query_id"], enabled=True)
        except SearchQuery.DoesNotExist as exc:
            raise CommandError("Active search query not found.") from exc
        try:
            with live_source() as source:
                run = run_search(query, source, max_pages=limit, report=self.stdout.write)
        except StageBusy as exc:
            raise CommandError(str(exc)) from exc
        summary = f"Run {run.pk}: {run.reason} {run.counts}"
        if run.error:
            raise CommandError(f"{summary} {run.error}")
        self.stdout.write(summary)
