from django.core.management.base import BaseCommand, CommandError

from jobs.models import SearchQuery
from jobs.pipeline import StageBusy, live_source, run_details, run_search


class Command(BaseCommand):
    help = "Search for vacancies and process the saved results for one query."

    def add_arguments(self, parser):
        parser.add_argument("query_id", type=int)

    def handle(self, **options):
        try:
            query = SearchQuery.objects.get(pk=options["query_id"], enabled=True)
        except SearchQuery.DoesNotExist as exc:
            raise CommandError("Active search query not found.") from exc
        try:
            with live_source() as source:
                search = run_search(query, source, report=self.stdout.write)
                if search.error:
                    raise CommandError(f"Search {search.pk}: {search.reason} {search.error}")
                details = run_details(source, query=query, report=self.stdout.write)
                if details.error:
                    raise CommandError(f"Details {details.pk}: {details.reason} {details.error}")
        except StageBusy as exc:
            raise CommandError(str(exc)) from exc
