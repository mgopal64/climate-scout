"""ATS detection + fetchers.

Detection turns URLs / page text into (ats, slug) pairs.
Fetchers turn each ATS's public JSON into a common Job shape.

Public endpoints used (no auth):
  greenhouse       boards-api.greenhouse.io/v1/boards/{slug}/jobs
  lever / lever_eu api(.eu).lever.co/v0/postings/{slug}?mode=json
  ashby            api.ashbyhq.com/posting-api/job-board/{slug}
  workable         apply.workable.com/api/v1/widget/accounts/{slug}?details=true
  smartrecruiters  api.smartrecruiters.com/v1/companies/{slug}/postings
Workday is detected (so we can count it) but not polled.
"""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from . import http

POLLABLE = {"greenhouse", "lever", "lever_eu", "ashby", "workable", "smartrecruiters"}


@dataclass
class Job:
    ats: str
    slug: str
    company: str
    job_id: str
    title: str
    location: str
    url: str
    posted_at: str = ""
    description: str = ""

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.slug.lower()}:{self.job_id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("description")
        return d


# --------------------------------------------------------------------------- detection

_BLOCKED = {"embed", "v1", "api", "jobs", "job_board", "js", "posting-api",
            "static", "assets", "careers", "search", "images", "j"}

_PATTERNS = [
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)")),
    ("lever_eu", re.compile(r"jobs\.eu\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#\"'\s<>\\]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workday", re.compile(
        r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
]


def _unescape_urls(text: str) -> str:
    return (text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/"))


def _slug(ats: str, m: re.Match) -> str | None:
    if ats == "workday":
        return f"{m.group(1)}|{m.group(2)}"
    s = unquote(m.group(1)).strip().rstrip(".")
    if not s or s.lower() in _BLOCKED:
        return None
    return s


def detect_ats(url: str | None) -> tuple[str, str] | None:
    """First ATS match in a single URL, or None."""
    if not url:
        return None
    url = _unescape_urls(url)
    for ats, pat in _PATTERNS:
        m = pat.search(url)
        if m:
            slug = _slug(ats, m)
            if slug:
                return ats, slug
    return None


def find_all_ats(text: str) -> set[tuple[str, str]]:
    """Every ATS board referenced anywhere in a page (HTML or embedded JSON)."""
    text = _unescape_urls(text)
    found: dict[tuple[str, str], tuple[str, str]] = {}
    for ats, pat in _PATTERNS:
        for m in pat.finditer(text):
            slug = _slug(ats, m)
            if slug:
                found.setdefault((ats, slug.lower()), (ats, slug))
    return set(found.values())


# --------------------------------------------------------------------------- text utils

_BLOCK_TAGS = re.compile(r"<\s*(br|/p|/li|/h\d|/div|/tr)[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)            # Greenhouse double-escapes its HTML
    s = _BLOCK_TAGS.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    lines = (" ".join(line.split()) for line in s.splitlines())
    return "\n".join(line for line in lines if line)


def _iso_ms(ms) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


# --------------------------------------------------------------------------- fetchers

def _greenhouse(slug: str, company: str) -> list[Job]:
    data = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{quote(slug)}/jobs")
    return [
        Job("greenhouse", slug, company, str(j["id"]), j.get("title", ""),
            (j.get("location") or {}).get("name", ""), j.get("absolute_url", ""),
            j.get("first_published") or j.get("updated_at", ""))
        for j in data.get("jobs", [])
    ]


def _greenhouse_detail(job: Job) -> str:
    d = http.get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{quote(job.slug)}/jobs/{job.job_id}")
    return strip_html(d.get("content", ""))


def _lever(slug: str, company: str, eu: bool = False) -> list[Job]:
    host = "api.eu.lever.co" if eu else "api.lever.co"
    data = http.get_json(f"https://{host}/v0/postings/{quote(slug)}", params={"mode": "json"})
    out = []
    for p in data if isinstance(data, list) else []:
        cats = p.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        if p.get("workplaceType") == "remote" and "remote" not in loc.lower():
            loc = f"{loc} / Remote".strip(" /")
        parts = [p.get("descriptionPlain", "")]
        for lst in p.get("lists") or []:
            parts.append(lst.get("text", ""))
            parts.append(strip_html(lst.get("content", "")))
        parts.append(p.get("additionalPlain", ""))
        out.append(Job("lever_eu" if eu else "lever", slug, company, p["id"], p.get("text", ""),
                       loc, p.get("hostedUrl", ""), _iso_ms(p.get("createdAt")),
                       "\n".join(x for x in parts if x)))
    return out


def _ashby(slug: str, company: str) -> list[Job]:
    data = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{quote(slug)}")
    out = []
    for j in data.get("jobs", []) if isinstance(data, dict) else []:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""]
        for s in j.get("secondaryLocations") or []:
            if isinstance(s, dict):
                locs.append(s.get("location", ""))
        if j.get("isRemote") or j.get("workplaceType") == "Remote":
            locs.append("Remote")
        out.append(Job("ashby", slug, company, j["id"], j.get("title", ""),
                       " / ".join(x for x in locs if x),
                       j.get("jobUrl") or j.get("applyUrl", ""), j.get("publishedAt", ""),
                       j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", ""))))
    return out


def _workable(slug: str, company: str) -> list[Job]:
    data = http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{quote(slug)}",
                         params={"details": "true"})
    out = []
    for j in data.get("jobs", []) if isinstance(data, dict) else []:
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        if j.get("telecommuting"):
            loc = f"{loc} / Remote".strip(" /")
        jid = j.get("shortcode") or j.get("id") or j.get("url", "")
        out.append(Job("workable", slug, company, str(jid), j.get("title", ""), loc,
                       j.get("url") or j.get("application_url", ""),
                       j.get("published_on") or j.get("created_at", ""),
                       strip_html(j.get("description", ""))))
    return out


def _smartrecruiters(slug: str, company: str) -> list[Job]:
    data = http.get_json(f"https://api.smartrecruiters.com/v1/companies/{quote(slug)}/postings",
                         params={"limit": 100})
    out = []
    for p in data.get("content", []) if isinstance(data, dict) else []:
        loc = p.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        if loc.get("remote"):
            where = f"{where} / Remote".strip(" /")
        out.append(Job("smartrecruiters", slug, company, str(p["id"]), p.get("name", ""), where,
                       f"https://jobs.smartrecruiters.com/{slug}/{p['id']}",
                       p.get("releasedDate", "")))
    return out


def _smartrecruiters_detail(job: Job) -> str:
    d = http.get_json(
        f"https://api.smartrecruiters.com/v1/companies/{quote(job.slug)}/postings/{job.job_id}")
    sections = ((d.get("jobAd") or {}).get("sections") or {}).values()
    return "\n".join(strip_html(s.get("text", "")) for s in sections if isinstance(s, dict))


FETCHERS = {
    "greenhouse": _greenhouse,
    "lever": lambda s, c: _lever(s, c, eu=False),
    "lever_eu": lambda s, c: _lever(s, c, eu=True),
    "ashby": _ashby,
    "workable": _workable,
    "smartrecruiters": _smartrecruiters,
}
DETAIL = {"greenhouse": _greenhouse_detail, "smartrecruiters": _smartrecruiters_detail}


def fetch_jobs(ats: str, slug: str, company: str) -> list[Job]:
    return FETCHERS[ats](slug, company)


def ensure_description(job: Job) -> Job:
    """Fill job.description for ATSs whose list endpoint omits it. Best-effort."""
    if not job.description and job.ats in DETAIL:
        try:
            job.description = DETAIL[job.ats](job)
        except Exception:  # noqa: BLE001 - a missing description must not kill a run
            pass
    return job


# --------------------------------------------------------------------------- probing (discovery)

def probe_greenhouse(slug: str) -> str | None:
    """Board display name if the Greenhouse board exists, else None."""
    try:
        d = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{quote(slug)}", attempts=1)
        return d.get("name") or ""
    except Exception:  # noqa: BLE001
        return None


def has_jobs(ats: str, slug: str) -> bool:
    try:
        return bool(fetch_jobs(ats, slug, slug))
    except Exception:  # noqa: BLE001
        return False


def board_gone(ats: str, slug: str) -> bool:
    """True only if the board definitively 404s. Timeouts/5xx never count as gone."""
    try:
        fetch_jobs(ats, slug, slug)
        return False
    except http.NotFound:
        return True
    except Exception:  # noqa: BLE001
        return False