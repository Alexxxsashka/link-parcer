import json

import httpx

from jobs.linkedin import BASE, DETAIL, SEARCH


def card_html(number):
    employer = number % 50
    return f"""
<li><div class="job-search-card" data-entity-urn="urn:li:jobPosting:{number}">
<a class="base-card__full-link" href="{BASE}/jobs/view/position-{number}/"></a>
<h3 class="base-search-card__title">Synthetic job {number}</h3>
<h4 class="base-search-card__subtitle"><a href="{BASE}/company/fixture-{employer}/?trk=test">
Synthetic company {employer}</a></h4><span class="job-search-card__location">Fixture city</span>
</div></li>"""


def structured_html(data):
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


def fixture_response(request, count):
    if request.url.path == SEARCH:
        offset = int(request.url.params["start"])
        cards = "".join(card_html(i) for i in range(offset + 1, min(offset + 25, count) + 1))
        return httpx.Response(200, text=f"<ul>{cards}</ul>")
    if request.url.path.startswith(DETAIL):
        job = int(request.url.path.removeprefix(DETAIL))
        return httpx.Response(200, text=structured_html({
            "@type": "JobPosting", "title": f"Synthetic job {job}", "employmentType": "FULL_TIME",
            "description": "<p>Synthetic description for testing.</p>",
            "datePosted": "2026-01-01", "jobLocationType": "TELECOMMUTE",
            "hiringOrganization": {"name": f"Synthetic company {job % 50}"},
        }))
    if request.url.path.startswith("/company/fixture-"):
        employer = request.url.path.split("-")[-1]
        return httpx.Response(200, text=structured_html({
            "@type": "Organization", "name": f"Synthetic company {employer}",
            "url": f"https://example.org/{employer}", "industry": "Synthetic industry",
            "numberOfEmployees": 50, "address": "Fixture city",
        }))
    return httpx.Response(404)
