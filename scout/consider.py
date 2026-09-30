"""Client for Consider-hosted VC job boards ("Powered by Consider", e.g. MCJ at jobs.mcj.vc).

Response shape (verified from a live MCJ response, Sep 2026):
  {"jobs": [...], "meta": {"size": 30, "sequence": "<cursor>"}, "total": 1128}
Each job has companyName, title, url/applyUrl (often a Greenhouse/Lever/Ashby link),
locations, remote, timeStamp, jobId, and structured minYearsExp / jobSeniorityIds.

Request shape is Consider's standard board search; check it with
  python -m scout.consider https://jobs.mcj.vc mcj
"""
from __future__ import annotations

import sys
import time

from . import http
from .ats import Job

API_PATH = "/api-boards/search-jobs"
PAGE_SIZE = 30
MAX_PAGES = 200
PAUSE = 0.2


def _payload(board_id: str, sequence: str | None = None) -> dict:
    meta = {"size": PAGE_SIZE}
    if sequence:
        meta["sequence"] = sequence
    # Exactly what the browser sends (captured from jobs.mcj.vc, Sep 2026). Extra keys -> HTTP 412.
    return {"meta": meta, "board": {"id": board_id, "isParent": True},
            "query": {"promoteFeatured": True}}


BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def _headers(board: str) -> dict:
    return {"Accept": "application/json", "Content-Type": "application/json",
            "Accept-Language": "en-US,en;q=0.9", "User-Agent": BROWSER_UA,
            "Origin": board, "Referer": board + "/jobs"}


def fetch_page(board: str, board_id: str, sequence: str | None = None) -> dict:
    board = board.rstrip("/")
    body = http.post_json(board + API_PATH, _payload(board_id, sequence), headers=_headers(board))
    if not isinstance((body or {}).get("jobs"), list):
        raise RuntimeError(f"Consider response shape changed on {board}: keys={list(body or {})[:8]}")
    return body


def walk_jobs(board: str, board_id: str, max_pages: int = MAX_PAGES) -> list[dict]:
    jobs: list[dict] = []
    seq = None
    for _ in range(max_pages):
        body = fetch_page(board, board_id, seq)
        batch = body["jobs"]
        jobs.extend(batch)
        seq = (body.get("meta") or {}).get("sequence")
        if not batch or not seq or len(jobs) >= body.get("total", 0):
            break
        time.sleep(PAUSE)
    return jobs


def to_job(item: dict) -> Job:
    """A Consider listing as a Job. Structured seniority/years go into the description
    so the existing years filter and the Claude check both see them."""
    locs = [str(x) for x in item.get("locations") or [] if x]
    if item.get("remote"):
        locs.append("Remote")
    notes = []
    if isinstance(item.get("minYearsExp"), (int, float)):
        notes.append(f"Requires {int(item['minYearsExp'])}+ years of experience.")
    if item.get("jobSeniorityIds"):
        notes.append("Seniority: " + ", ".join(item["jobSeniorityIds"]))
    if item.get("manager"):
        notes.append("People-manager role.")
    return Job("consider", "all", " ".join((item.get("companyName") or "").split()),
               str(item.get("jobId") or ""), " ".join((item.get("title") or "").split()),
               " / ".join(locs[:3]), item.get("url") or item.get("applyUrl") or "",
               item.get("timeStamp") or "", "\n".join(notes))


def debug(board: str, board_ids: list[str]) -> None:
    for bid in board_ids:
        try:
            body = fetch_page(board, bid)
            titles = [f"{j.get('companyName')}: {j.get('title')}" for j in body["jobs"][:3]]
            print(f"  board_id={bid!r}: OK total={body.get('total')} e.g. {titles}")
            return
        except Exception as e:  # noqa: BLE001
            body = getattr(getattr(e, "response", None), "text", "") or ""
            print(f"  board_id={bid!r}: FAILED {e}" + (f"\n    server said: {body[:300]!r}" if body else ""))
    print("\nNone worked. Paste me the 'server said' line above, plus the remaining Request "
          "Headers from DevTools (origin, referer, user-agent, and any x-... headers).")


if __name__ == "__main__":
    b = sys.argv[1] if len(sys.argv) > 1 else "https://jobs.mcj.vc"
    ids = sys.argv[2:] or ["mcj"]
    print(f"== {b}{API_PATH}")
    debug(b, ids)