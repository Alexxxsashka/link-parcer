import csv
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .models import Job

DETAIL_FIELDS = (
    "description", "job_location", "published_at", "valid_through", "employment_type",
    "workplace_type", "seniority", "job_function", "industries", "salary", "apply_url",
)
COMPANY_FIELDS = (
    "website", "description", "industry", "size", "location", "company_type",
    "founded", "specialties",
)
COLUMNS = (
    "linkedin_id", "url", "title", "location", "company_name", "company_id", "company_url",
    *DETAIL_FIELDS, *(f"company_{field}" for field in COMPANY_FIELDS),
    "status", "attempts", "attempted_at", "error", "searches",
)


def csv_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    if isinstance(value, str) and value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def write_jobs_csv(output):
    jobs = Job.objects.select_related("company").prefetch_related("searches").order_by("id")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(COLUMNS)
    count = 0
    for job in jobs.iterator(chunk_size=500):
        details = job.details
        company = job.company
        data = company.data if company else {}
        row = (
            job.source_id, job.url, details.get("title") or job.title,
            details.get("location") or job.location,
            (company.name if company else None) or details.get("company_name") or job.listed_company,
            company.source_key if company else "",
            (company.profile_url if company else None) or details.get("company_url") or job.listed_company_url,
            *(details.get(field) for field in DETAIL_FIELDS),
            *(data.get(field) for field in COMPANY_FIELDS),
            job.status, job.attempts, job.attempted_at.isoformat() if job.attempted_at else "",
            job.error, [{"query": q.keywords, "country": q.country} for q in job.searches.all()],
        )
        writer.writerow(map(csv_value, row))
        count += 1
    return count


def export_jobs(destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False,
    ) as output:
        temporary = Path(output.name)
        try:
            count = write_jobs_csv(output)
            output.close()
            os.replace(temporary, path)
        finally:
            output.close()
            temporary.unlink(missing_ok=True)
    return count
