import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.linkedin.com"
SEARCH = "/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL = "/jobs-guest/jobs/api/jobPosting/"
COUNTRY_ALIASES = {"us": "United States", "usa": "United States", "uk": "United Kingdom"}


class SourceError(Exception):
    reason = "source_error"


class SourceLimited(SourceError):
    reason = "rate_limited"


class SourceDenied(SourceError):
    reason = "access_denied"


class SourceUnavailable(SourceError):
    reason = "unavailable"


@dataclass(frozen=True)
class Card:
    source_id: str
    url: str
    title: str
    company: str
    location: str
    company_url: str = ""


@dataclass(frozen=True)
class Page:
    cards: list[Card]
    size: int


def clean_link(value):
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parts = urlsplit(urljoin(BASE, value.strip()))
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username:
            return ""
    except ValueError:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:1000]


def company_link(value):
    url = clean_link(value)
    parts = urlsplit(url)
    if parts.hostname not in {"linkedin.com", "www.linkedin.com"}:
        return ""
    match = re.match(r"^/company/([A-Za-z0-9._-]{1,200})(?:/|$)", parts.path)
    return f"{BASE}/company/{match[1].lower()}" if match else ""


def selected_text(root, selector):
    node = root.select_one(selector)
    return node.get_text(" ", strip=True) if node else ""


def selected_attr(root, selector, name):
    node = root.select_one(selector)
    return node.get(name, "") if node else ""


def schema(root, wanted):
    for script in root.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        pending = value if isinstance(value, list) else [value]
        for item in pending:
            if isinstance(item, dict):
                types = item.get("@type", [])
                if wanted == types or isinstance(types, list) and wanted in types:
                    return item
                if isinstance(item.get("@graph"), list):
                    pending.extend(item["@graph"])
    return {}


def criteria(root):
    result = {}
    for item in root.select(".description__job-criteria-list li"):
        label = selected_text(item, ".description__job-criteria-subheader").casefold()
        value = selected_text(item, ".description__job-criteria-text")
        if label and value:
            result[label] = value
    return result


def string_value(value):
    return value if isinstance(value, str) else ""


def parse_search(html):
    root = BeautifulSoup(html, "html.parser")
    if root.select_one('.no-search-results, .jobs-search__no-results, .jobs-search-no-results'):
        return Page([], 0)
    items = root.select('[data-entity-urn^="urn:li:jobPosting:"], .job-search-card')
    if not items and root.get_text(strip=True):
        raise SourceError("Unrecognized search response.")
    cards = []
    for item in items:
        title = selected_text(item, ".base-search-card__title")
        raw_url = selected_attr(item, "a.base-card__full-link", "href")
        identifier = re.search(r"(?:^urn:li:jobPosting:|[-/])(\d{1,32})(?:$|[/?#])",
                               item.get("data-entity-urn") or clean_link(raw_url))
        if not identifier or not title:
            continue
        sid = identifier[1]
        cards.append(Card(
            sid, f"{BASE}/jobs/view/{sid}/", title,
            selected_text(item, ".base-search-card__subtitle"),
            selected_text(item, ".job-search-card__location"),
            company_link(selected_attr(item, ".base-search-card__subtitle a", "href")),
        ))
    if items and not cards:
        raise SourceError("Search cards found, but no stable IDs and titles could be parsed.")
    return Page(cards, len(items))


def parse_job(html):
    root = BeautifulSoup(html, "html.parser")
    data = schema(root, "JobPosting")
    title = string_value(data.get("title")) or selected_text(root, ".top-card-layout__title, .topcard__title")
    description = root.select_one(".show-more-less-html__markup")
    body = description.get_text("\n", strip=True) if description else BeautifulSoup(
        string_value(data.get("description")), "html.parser",
    ).get_text("\n", strip=True)
    if not title or not (body or data):
        raise SourceError("Unrecognized job details response.")
    organization = data.get("hiringOrganization")
    if not isinstance(organization, dict):
        organization = {}
    employer = organization.get("name") or selected_text(root, ".topcard__org-name-link")
    profile = company_link(organization.get("sameAs")) or company_link(
        selected_attr(root, ".topcard__org-name-link", "href"),
    )
    fields = criteria(root)
    details = {
        "title": title,
        "description": body,
        "location": selected_text(root, ".topcard__flavor--bullet"),
        "job_location": data.get("jobLocation"),
        "published_at": data.get("datePosted") or selected_attr(root, "time[datetime]", "datetime"),
        "valid_through": data.get("validThrough"),
        "employment_type": data.get("employmentType") or fields.get("employment type"),
        "workplace_type": data.get("jobLocationType") or fields.get("workplace type"),
        "seniority": fields.get("seniority level"),
        "job_function": fields.get("job function"),
        "industries": fields.get("industries") or data.get("industry"),
        "salary": data.get("baseSalary") or selected_text(root, ".salary, .compensation__salary"),
        "apply_url": (
            string_value(data.get("url")) if data.get("directApply") else ""
        ) or selected_attr(root, "a.apply-button[href], a.apply-button__link[href]", "href"),
        "company_name": employer,
        "company_url": profile,
    }
    if details["apply_url"]:
        try:
            apply_url = urljoin(BASE, details["apply_url"])
            parts = urlsplit(apply_url)
            details["apply_url"] = apply_url if (
                parts.scheme in {"https", "http"} and parts.hostname and not parts.username
            ) else ""
        except ValueError:
            details["apply_url"] = ""
    return details


def parse_company(html):
    root = BeautifulSoup(html, "html.parser")
    data = schema(root, "Organization")
    name = string_value(data.get("name")) or selected_text(root, "main h1, .top-card-layout__title")
    if not name or not (data or root.select_one(".about-us, .top-card-layout")):
        raise SourceError("Unrecognized company response.")
    values = {}
    for label in root.select(".about-us dt"):
        value = label.find_next_sibling("dd")
        if value:
            values[label.get_text(" ", strip=True).casefold()] = value.get_text(" ", strip=True)
    return {
        "name": name,
        "website": clean_link(data.get("url") or values.get("website")),
        "description": data.get("description") or selected_text(root, ".about-us p"),
        "industry": data.get("industry") or values.get("industry"),
        "size": data.get("numberOfEmployees") or values.get("company size"),
        "location": data.get("address") or values.get("headquarters"),
        "company_type": values.get("type"),
        "founded": data.get("foundingDate") or values.get("founded"),
        "specialties": values.get("specialties"),
    }


def retry_after(value):
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class LinkedIn:
    def __init__(self, client, interval=2):
        self.client = client
        self.interval = interval
        self.next_request = 0
        self.companies = {}

    def get(self, path, params=None, *, missing=False):
        for attempt in range(3):
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.interval
            wait = 2 ** attempt
            try:
                response = self.client.get(path, params=params)
            except httpx.RequestError as exc:
                error = SourceError(f"Network error ({type(exc).__name__}).")
            else:
                code = response.status_code
                if 200 <= code < 300:
                    return response.text
                if missing and code in {404, 410}:
                    raise SourceUnavailable(f"HTTP {code}.")
                if code in {401, 403, 999} or 300 <= code < 400:
                    raise SourceDenied(f"HTTP {code}.")
                if code == 429:
                    error = SourceLimited("HTTP 429.")
                    wait = retry_after(response.headers.get("Retry-After"))
                    if wait is None or wait > 30:
                        raise error
                elif code in {408, 500, 502, 503, 504}:
                    error = SourceError(f"HTTP {code}.")
                    advised = retry_after(response.headers.get("Retry-After"))
                    if advised is not None:
                        if advised > 30:
                            raise error
                        wait = max(wait, advised)
                else:
                    raise SourceError(f"Unexpected HTTP {code}.")
            if attempt < 2:
                time.sleep(wait)
        raise error

    def search(self, keywords, country, start):
        location = COUNTRY_ALIASES.get(country.casefold(), country)
        return parse_search(self.get(SEARCH, {
            "keywords": keywords, "location": location, "start": start,
        }))

    def job(self, source_id):
        return parse_job(self.get(f"{DETAIL}{source_id}", missing=True))

    def company(self, profile):
        if profile not in self.companies:
            self.companies[profile] = parse_company(self.get(profile, missing=True))
        return self.companies[profile]
