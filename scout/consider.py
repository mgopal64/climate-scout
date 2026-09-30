"""Client for Consider-hosted VC job boards ("Powered by Consider", e.g. MCJ at jobs.mcj.vc).

Response shape (verified from a live MCJ response, Sep 2026):
  {"jobs": [...], "meta": {"size": 30, "sequence": "<cursor>"}, "total": 1128}
Each job has companyName, title, url/applyUrl (often a Greenhouse/Lever/Ashby link),
locations, remote, timeStamp, jobId, and structured minYearsExp / jobSeniorityIds.

Request shape is Consider's standard board search; check it with
  python -m scout.consider https://jobs.mcj.vc mcj
"""
from __future__ import annotations

import re
import sys
import time

import requests

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


_TOKEN_RX = [
    re.compile(r'<meta[^>]+name=["\']csrf[-_]?token["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'"(?:csrfToken|csrf_token|csrf|xsrfToken)"\s*:\s*"([^"]+)"'),
]


class Session:
    """Consider boards reject API calls without the CSRF token the page hands out
    (HTTP 412 {"error":"INVALID_CSRF"}). Load the page like a browser, then send the
    token back in the common header names."""

    def __init__(self, board: str):
        self.board = board.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
        self.token = None
        self.cookie_names: list[str] = []

    def prime(self) -> None:
        r = self.s.get(self.board + "/jobs", timeout=20,
                       headers={"Accept": "text/html,application/xhtml+xml"})
        r.raise_for_status()
        self.cookie_names = sorted(c.name for c in self.s.cookies)
        for c in self.s.cookies:
            if "csrf" in c.name.lower() or "xsrf" in c.name.lower():
                self.token = c.value
        if not self.token:
            for rx in _TOKEN_RX:
                m = rx.search(r.text)
                if m:
                    self.token = m.group(1)
                    break

    def headers(self) -> dict:
        h = _headers(self.board)
        if self.token:
            h.update({"X-CSRF-Token": self.token, "X-XSRF-TOKEN": self.token,
                      "csrf-token": self.token, "x-csrftoken": self.token})
        return h

    def post(self, payload: dict) -> dict:
        if self.token is None and not self.cookie_names:
            self.prime()
        for attempt in range(3):
            r = self.s.post(self.board + API_PATH, json=payload, headers=self.headers(), timeout=30)
            if r.status_code == 412 and attempt == 0:      # token expired/rotated: re-prime once
                self.prime()
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            body = r.json()
            if not isinstance((body or {}).get("jobs"), list):
                raise RuntimeError(f"Consider response shape changed on {self.board}: "
                                   f"keys={list(body or {})[:8]}")
            return body
        r.raise_for_status()
        raise RuntimeError(f"Consider request kept failing on {self.board}")


def fetch_page(board: str, board_id: str, sequence: str | None = None,
               session: Session | None = None) -> dict:
    session = session or Session(board)
    return session.post(_payload(board_id, sequence))


def walk_jobs(board: str, board_id: str, max_pages: int = MAX_PAGES) -> list[dict]:
    jobs: list[dict] = []
    seq = None
    session = Session(board)
    for _ in range(max_pages):
        body = fetch_page(board, board_id, seq, session)
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
    sess = Session(board)
    try:
        sess.prime()
        print(f"  page cookies: {sess.cookie_names or 'none'}; "
              f"csrf token found: {'yes' if sess.token else 'NO'}")
    except Exception as e:  # noqa: BLE001
        print(f"  loading {board}/jobs failed: {e}")
    for bid in board_ids:
        try:
            body = fetch_page(board, bid, session=sess)
            titles = [f"{j.get('companyName')}: {j.get('title')}" for j in body["jobs"][:3]]
            print(f"  board_id={bid!r}: OK total={body.get('total')} e.g. {titles}")
            return
        except Exception as e:  # noqa: BLE001
            body = getattr(getattr(e, "response", None), "text", "") or ""
            print(f"  board_id={bid!r}: FAILED {e}" + (f"\n    server said: {body[:300]!r}" if body else ""))
    print("\nStill failing. In DevTools > Network > the search-jobs request > Headers, send me the"
          " NAMES (not values) of any request header containing 'csrf' or 'xsrf', and the cookie"
          " names under Application > Cookies > jobs.mcj.vc.")


if __name__ == "__main__":
    b = sys.argv[1] if len(sys.argv) > 1 else "https://jobs.mcj.vc"
    ids = sys.argv[2:] or ["mcj"]
    print(f"== {b}{API_PATH}")
    debug(b, ids)