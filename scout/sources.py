"""Generic 'scrape any list page' source.

Fetches each URL and pulls out every Greenhouse/Lever/Ashby/Workable/SmartRecruiters
board it references, in the HTML or in embedded JSON (e.g. Next.js __NEXT_DATA__).
Works for ClimateTechList and for any other directory page you add to config.yaml.
"""
from __future__ import annotations

from . import http
from .ats import find_all_ats


def extract_from_pages(urls: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    found: dict[tuple[str, str], tuple[str, str]] = {}
    errors: list[str] = []
    for url in urls:
        try:
            text = http.get_text(url)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
            continue
        for ats, slug in find_all_ats(text):
            found.setdefault((ats, slug.lower()), (ats, slug))
    return list(found.values()), errors
