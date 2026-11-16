import os
from contextlib import contextmanager
from time import monotonic

import httpx
from django.db import connection, transaction
from django.utils import timezone

from .linkedin import BASE, LinkedIn, SourceDenied, SourceError, SourceLimited, SourceUnavailable
from .models import Company, Job, Run


class StageBusy(Exception):
    pass


class CompanyAccessDenied(SourceDenied):
    pass


@contextmanager
def live_source():
    interval = max(0, float(os.environ.get("LINKEDIN_REQUEST_INTERVAL", "2")))
    with httpx.Client(
        base_url=BASE,
        headers={"Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
        timeout=httpx.Timeout(15, connect=5),
        follow_redirects=False,
    ) as client:
        yield LinkedIn(client, interval=interval)


@contextmanager
def track(stage, query=None, **parameters):
    lock = {"search": 1, "details": 2}[stage]
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [1624938790, lock])
        acquired = cursor.fetchone()[0]
    if not acquired:
        raise StageBusy(f"Another {stage} run is in progress.")

    try:
        run = Run.objects.create(
            stage=stage, query=query, parameters=parameters, reason="running",
            counts=dict.fromkeys(("received", "added", "updated", "skipped", "errors"), 0),
        )
        try:
            yield run
        except KeyboardInterrupt:
            run.reason = "interrupted"
            raise
        except Exception as exc:
            run.reason = "failed"
            run.error = type(exc).__name__
            raise
        finally:
            run.finished_at = timezone.now()
            run.save(update_fields=["reason", "error", "counts", "finished_at"])
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [1624938790, lock])


def run_search(query, source, *, max_pages=None, report=lambda _: None):
    with track("search", query, keywords=query.keywords, country=query.country, max_pages=max_pages) as run:
        start = 0
        pages = 0
        seen = set()
        while max_pages is None or pages < max_pages:
            try:
                page = source.search(query.keywords, query.country, start)
            except SourceError as exc:
                run.reason, run.error = exc.reason, str(exc)
                run.counts["errors"] += 1
                break
            if not page.size:
                run.reason = "exhausted"
                break

            run.counts["received"] += page.size
            signature = frozenset(card.source_id for card in page.cards)
            if signature in seen:
                run.reason = "repeated_page"
                run.counts["skipped"] += page.size
                break
            seen.add(signature)
            run.counts["skipped"] += page.size - len(page.cards)

            with transaction.atomic():
                for card in page.cards:
                    defaults = {"url": card.url, "title": card.title}
                    defaults.update((name, value) for name, value in {
                        "listed_company": card.company, "location": card.location,
                        "listed_company_url": card.company_url,
                    }.items() if value)
                    job, created = Job.objects.update_or_create(source_id=card.source_id, defaults=defaults)
                    job.searches.add(query)
                    run.counts["added" if created else "updated"] += 1
            report(f"Search offset={start} cards={page.size} total={run.counts['received']}")
            run.save(update_fields=["counts"])
            start += page.size
            pages += 1
        else:
            run.reason = "page_limit"
    return run


def store_attempt(job, details, status, error="", company=None):
    with transaction.atomic():
        fields = ["status", "error", "attempts", "attempted_at"]
        if details is not None:
            job.details = details
            fields.append("details")
        if company:
            profile, data = company
            job.company, _ = Company.objects.update_or_create(
                source_key=profile, defaults={
                    "name": data.get("name") or job.listed_company,
                    "profile_url": profile, "data": data,
                },
            )
            fields.append("company")
        job.status = status
        job.error = error
        job.attempts += 1
        job.attempted_at = timezone.now()
        job.save(update_fields=fields)


def process_job(job, source, company_denial=None):
    try:
        details = source.job(job.source_id)
    except SourceUnavailable as exc:
        store_attempt(job, None, Job.Status.UNAVAILABLE, f"Job: {exc}")
        return Job.Status.UNAVAILABLE, None
    except SourceError as exc:
        store_attempt(job, None, Job.Status.RETRY, f"Job: {exc}")
        return Job.Status.RETRY, exc

    profile = details.get("company_url") or job.listed_company_url
    company = None
    if profile:
        if company_denial:
            store_attempt(job, details, Job.Status.RETRY, f"Company skipped: {company_denial}")
            return Job.Status.RETRY, company_denial
        # A company lookup failure preserves the job data already retrieved.
        try:
            company = (profile, source.company(profile))
        except SourceDenied as exc:
            store_attempt(job, details, Job.Status.RETRY, f"Company: {exc}")
            return Job.Status.RETRY, CompanyAccessDenied(str(exc))
        except SourceUnavailable as exc:
            store_attempt(job, details, Job.Status.DONE, f"Company unavailable: {exc}")
            return Job.Status.DONE, exc
        except SourceError as exc:
            store_attempt(job, details, Job.Status.RETRY, f"Company: {exc}")
            return Job.Status.RETRY, exc

    store_attempt(job, details, Job.Status.DONE, company=company)
    return Job.Status.DONE, None


def run_details(source, *, limit=None, query=None, report=lambda _: None):
    with track("details", query, limit=limit) as run:
        pending = Job.objects.filter(status__in=(Job.Status.PENDING, Job.Status.RETRY))
        if query is not None:
            pending = pending.filter(searches=query)
        end = pending.order_by("-id").values_list("id", flat=True).first() or 0
        last = 0
        last_progress = monotonic()
        company_denial = None
        run.reason = "exhausted"
        while last < end:
            remaining = 100 if limit is None else min(100, limit - run.counts["received"])
            if remaining <= 0:
                run.reason = "job_limit"
                break
            jobs = list(pending.filter(id__gt=last, id__lte=end).order_by("id")[:remaining])
            if not jobs:
                break
            for job in jobs:
                last = job.pk
                status, error = process_job(job, source, company_denial)
                run.counts["received"] += 1
                if status == Job.Status.DONE:
                    run.counts["updated"] += 1
                elif status == Job.Status.UNAVAILABLE:
                    run.counts["skipped"] += 1
                if error:
                    run.counts["errors"] += 1
                if monotonic() - last_progress >= 4:
                    run.save(update_fields=["counts"])
                    last_progress = monotonic()
                if isinstance(error, CompanyAccessDenied):
                    company_denial = error
                elif isinstance(error, (SourceLimited, SourceDenied)):
                    run.reason, run.error = error.reason, str(error)
                    break
            report(f"Details processed={run.counts['received']} errors={run.counts['errors']}")
            run.save(update_fields=["counts"])
            if run.error:
                break
        if run.reason == "exhausted" and run.counts["errors"]:
            run.reason = "completed_with_errors"
    return run
