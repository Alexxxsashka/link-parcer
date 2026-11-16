from django.core.management.base import BaseCommand

from jobs.export import export_jobs


class Command(BaseCommand):
    help = "Export PostgreSQL vacancies as an Excel-compatible UTF-8 CSV."

    def add_arguments(self, parser):
        parser.add_argument("destination")

    def handle(self, **options):
        destination = options["destination"]
        count = export_jobs(destination)
        self.stdout.write(f"Exported {count} vacancies to {destination}")
