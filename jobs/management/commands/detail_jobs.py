from django.core.management.base import BaseCommand, CommandError

from jobs.pipeline import StageBusy, live_source, run_details


class Command(BaseCommand):
    help = "Fetch details for pending and retryable vacancies."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int)

    def handle(self, **options):
        limit = options["limit"]
        if limit is not None and limit < 1:
            raise CommandError("--limit must be positive.")
        try:
            with live_source() as source:
                run = run_details(source, limit=limit, report=self.stdout.write)
        except StageBusy as exc:
            raise CommandError(str(exc)) from exc
        summary = f"Run {run.pk}: {run.reason} {run.counts}"
        if run.error:
            raise CommandError(f"{summary} {run.error}")
        self.stdout.write(summary)
