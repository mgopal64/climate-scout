"""Client for Getro-hosted VC/community job boards (MCJ, Climate Draft, BEV, ...).

Known traps (documented by github.com/rajatkarnwaldigital-hash/gtm-signal-monitor):
  * The API returns 406 unless the request sends `Accept: application/json`.
  * hitsPerPage is capped at 20 server-side, so pagination is mandatory.
  * Several filter keys are accepted and silently ignored, so we filter locally.

Network ids are resolved from the board's own HTML so adding a board is one config line.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from . import http
from .ats import Job

API = "https://api.getro.com/api/v2/collections/{net}/search/jobs"
PAGE_SIZE = 20
MAX_PAGES = 1000
PAUSE = 0.2

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_BADGE = re.compile(r"(?:New|Hybrid|On-?Site|Remote|Urgent|Featured)+$")


def _headers(board: str) -> dict:
    return {"Accept": "application/json", "Content-Type": "application/json",
            "Origin": board, "Referer": board + "/"}


_KEY_OBJ = {"network", "collection", "currentnetwork", "currentcollection"}
_KEY_ID = {"networkid", "network_id", "collectionid", "collection_id"}
_RX = [
    re.compile(r'"(?:network|collection)"\s*:\s*\{[^{}]{0,400}?"id"\s*:\s*"?(\d+)'),
    re.compile(r'"(?:networkId|network_id|collectionId|collection_id)"\s*:\s*"?(\d+)'),
    re.compile(r"api/v2/collections/(\d+)"),
]


def _add(v, out: list[int]) -> None:
    if isinstance(v, bool):
        return
    if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
        n = int(v)
        if n not in out:
            out.append(n)


def _walk(obj, out: list[int], depth: int = 0) -> None:
    if depth > 16:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in _KEY_OBJ and isinstance(v, dict):
                _add(v.get("id"), out)
            elif kl in _KEY_ID:
                _add(v, out)
            _walk(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:100]:
            _walk(v, out, depth + 1)


def extract_candidates(page_html: str) -> list[int]:
    """Possible network ids in a board page, most trustworthy first."""
    out: list[int] = []
    m = _NEXT_DATA.search(page_html)
    if m:
        try:
            _walk(json.loads(m.group(1)), out)
        except ValueError:
            pass
    for rx in _RX:
        for mm in rx.finditer(page_html):
            _add(mm.group(1), out)
    return out


def parse_network_id(page_html: str) -> int | None:
    c = extract_candidates(page_html)
    return c[0] if c else None


def probe(net: int, board: str) -> tuple[int | None, str]:
    """(job count, status) for a candidate network id. count None = unusable."""
    try:
        body = http.post_json(API.format(net=net), {"hitsPerPage": 1, "page": 0, "filters": {}},
                              headers=_headers(board.rstrip("/")), attempts=1)
    except http.NotFound:
        return None, "404"
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:80]
    res = (body or {}).get("results") or {}
    if not isinstance(res.get("jobs"), list):
        return None, f"unexpected shape {list(res)[:5]}"
    return res.get("count", 0), "ok"


def resolve_network_id(board: str) -> int:
    board = board.rstrip("/")
    tried: dict[int, str] = {}
    page_errors = []
    for path in ("/jobs", "/companies", ""):
        try:
            cands = extract_candidates(http.get_text(board + path))
        except Exception as e:  # noqa: BLE001
            page_errors.append(f"{path or '/'}: {e}")
            continue
        for net in cands:
            if net in tried:
                continue
            count, status = probe(net, board)
            tried[net] = status if count is None else f"count={count}"
            if count:
                return net
    detail = ", ".join(f"{k}:{v}" for k, v in tried.items()) or "no candidates in page"
    if page_errors:
        detail += f"; page errors: {page_errors}"
    raise RuntimeError(f"no working Getro network id for {board} (tried {detail}). "
                       f"Run `python -m scout.getro {board}` or pin network_id in config.yaml")


def walk_jobs(net: int, board: str, max_pages: int = MAX_PAGES) -> list[dict]:
    board = board.rstrip("/")
    jobs: list[dict] = []
    for page in range(max_pages):
        body = http.post_json(API.format(net=net),
                              {"hitsPerPage": PAGE_SIZE, "page": page, "filters": {}},
                              headers=_headers(board))
        res = (body or {}).get("results") or {}
        batch = res.get("jobs")
        if not isinstance(batch, list):
            raise RuntimeError(f"Getro response shape changed on {board}: keys={list(res)}")
        jobs.extend(batch)
        if len(batch) < PAGE_SIZE or len(jobs) >= res.get("count", 0):
            break
        time.sleep(PAUSE)
    return jobs


def clean_title(t: str | None) -> str:
    t = " ".join((t or "").split())
    prev = None
    while prev != t:
        prev, t = t, _BADGE.sub("", t).strip()
    return t


def to_job(item: dict, board: str) -> Job:
    """A Getro listing as a Job (used for the daily catch-all sweep)."""
    board = board.rstrip("/")
    org = item.get("organization") or {}
    org_slug = org.get("slug") or ""
    job_slug = item.get("slug") or str(item.get("id") or "")
    locs = item.get("locations") or item.get("searchable_locations") or []
    created = item.get("created_at")
    posted = (datetime.fromtimestamp(created, timezone.utc).isoformat()
              if isinstance(created, (int, float)) else "")
    link = (f"{board}/companies/{org_slug}/jobs/{job_slug}"
            if org_slug and job_slug else item.get("url", ""))
    return Job("getro", "all", " ".join((org.get("name") or "").split()),
               str(item.get("id") or ""), clean_title(item.get("title")),
               " / ".join(str(x) for x in locs[:3]), link, posted)


def debug(board: str) -> None:
    """python -m scout.getro <board_url> : show what the page exposes and what works."""
    board = board.rstrip("/")
    for path in ("/jobs", "/companies", ""):
        print(f"\n== {board + path}")
        try:
            text = http.get_text(board + path)
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed: {e}")
            continue
        print(f"  {len(text)} chars, __NEXT_DATA__: {bool(_NEXT_DATA.search(text))}")
        for kw in ('"network"', '"collection"', "collections/", "getro.com"):
            i = text.find(kw)
            if i >= 0:
                print(f"  {kw} ...{text[max(0, i - 60): i + 160]!r}")
        cands = extract_candidates(text)
        print(f"  candidates: {cands}")
        for net in cands[:8]:
            print(f"    {net}: {probe(net, board)}")


if __name__ == "__main__":
    import sys
    for b in sys.argv[1:] or ["https://jobs.climatedraft.org"]:
        debug(b)