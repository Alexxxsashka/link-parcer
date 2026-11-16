import os
import subprocess
import sys

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import path, reverse
from django.views.decorators.http import require_GET, require_POST

from .export import write_jobs_csv
from .models import Company, Job, Run, SearchQuery

admin.site.site_header = "Парсер вакансий LinkedIn"


def start_parser(model_admin, request, command, *arguments):
    folder = settings.BASE_DIR / ".local" / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{command}.log").open("ab") as log:
            subprocess.Popen(
                [sys.executable, "manage.py", command, *map(str, arguments)],
                cwd=settings.BASE_DIR,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
    except OSError:
        model_admin.message_user(request, "Не удалось запустить процесс.", level=messages.ERROR)
        return None
    model_admin.message_user(
        request, "Команда передана фоновому процессу. Статус появится в списке прогонов.",
    )
    return redirect("admin:jobs_run_changelist")


@admin.register(SearchQuery)
class SearchQueryAdmin(admin.ModelAdmin):
    list_display = ("keywords", "country", "enabled", "id")
    search_fields = ("keywords", "country")
    list_filter = ("enabled", "country")
    actions = ("start_search", "start_collection")

    def has_run_permission(self, request):
        return request.user.is_superuser

    def start_query(self, request, queryset, command):
        selected = list(queryset[:2])
        if len(selected) != 1 or not selected[0].enabled:
            self.message_user(request, "Выберите один активный запрос.", level=messages.WARNING)
            return None
        return start_parser(self, request, command, selected[0].pk)

    @admin.action(permissions=["run"], description="Запустить поиск Search")
    def start_search(self, request, queryset):
        return self.start_query(request, queryset, "search_jobs")

    @admin.action(permissions=["run"], description="Запустить поиск и обработку (Search + Details)")
    def start_collection(self, request, queryset):
        return self.start_query(request, queryset, "collect_jobs")


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Job)
class JobAdmin(ReadOnlyAdmin):
    list_display = ("title", "listed_company", "location", "status", "attempted_at")
    search_fields = ("title", "details__title", "listed_company", "company__name", "=source_id")
    list_filter = ("status", "searches__country", "searches")
    list_per_page = 50
    show_full_result_count = False
    fields = (
        "source_id", "url", "title", "listed_company", "location", "company", "search_queries",
        "details", "company_details", "status", "attempts", "attempted_at", "error",
    )
    readonly_fields = ("search_queries", "company_details")

    def get_urls(self):
        return [
            path("run-details/", self.admin_site.admin_view(require_POST(self.start_details)),
                 name="jobs_job_run_details"),
            path("export.csv", self.admin_site.admin_view(require_GET(self.download_csv)),
                 name="jobs_job_export_csv"),
            *super().get_urls(),
        ]

    def download_csv(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        response = HttpResponse("\ufeff", content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="linkedin-jobs.csv"'
        write_jobs_csv(response)
        return response

    def start_details(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied
        return start_parser(self, request, "detail_jobs") or redirect("admin:jobs_job_changelist")

    @admin.display(description="Поисковые запросы")
    def search_queries(self, job):
        return ", ".join(str(query) for query in job.searches.all())

    @admin.display(description="Сведения о компании")
    def company_details(self, job):
        if not job.company:
            return None
        fields = {"Профиль LinkedIn": job.company.profile_url, **job.company.data}
        return "\n".join(f"{key}: {value}" for key, value in fields.items() if value)


@admin.register(Company)
class CompanyAdmin(ReadOnlyAdmin):
    list_display = ("name", "profile_url")
    search_fields = ("name", "source_key")


@admin.register(Run)
class RunAdmin(ReadOnlyAdmin):
    list_display = (
        "id", "stage", "query", "run_status", "received", "updated", "errors", "started_at", "finished_at",
    )
    list_filter = ("stage", "reason")

    @admin.display(description="Статус")
    def run_status(self, run):
        return {
            "running": "Выполняется",
            "exhausted": "Завершён",
            "rate_limited": "Ограничение LinkedIn (429)",
            "access_denied": "Доступ ограничен",
            "completed_with_errors": "Завершён с ошибками",
            "page_limit": "Достигнут лимит страниц",
            "job_limit": "Достигнут лимит вакансий",
            "repeated_page": "Повтор страницы выдачи",
            "interrupted": "Прерван",
            "failed": "Ошибка",
            "source_error": "Ошибка источника",
        }.get(run.reason, run.reason)

    @admin.display(description="Получено")
    def received(self, run):
        return run.counts.get("received", 0)

    @admin.display(description="Сохранено")
    def updated(self, run):
        return run.counts.get("added", 0) + run.counts.get("updated", 0)

    @admin.display(description="Ошибки")
    def errors(self, run):
        return run.counts.get("errors", 0)
