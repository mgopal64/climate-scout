"""Shared HTTP session. Every network call in scout goes through here.

- 404 raises NotFound immediately (no retry): it means "no such board".
- 429 / 5xx / transport errors retry with exponential backoff.
- Other 4xx raise immediately.
"""
from __future__ import annotations

import random
import time

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 climate-scout/0.1")

_session = requests.Session()
_session.headers.update({"User-Agent": UA})


class NotFound(Exception):
    pass


def request(method: str, url: str, *, attempts: int = 3, timeout: int = 20, **kw):
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = _session.request(method, url, timeout=timeout, **kw)
        except requests.RequestException as e:
            last = e
        else:
            if r.status_code == 404:
                raise NotFound(url)
            if r.status_code == 429 or r.status_code >= 500:
                last = requests.HTTPError(f"HTTP {r.status_code} for {url}")
            else:
                r.raise_for_status()
                return r
        if i < attempts - 1:
            time.sleep(2 ** i + random.random())
    raise last  # type: ignore[misc]


def get_json(url: str, **kw):
    return request("GET", url, **kw).json()


def get_text(url: str, **kw) -> str:
    return request("GET", url, **kw).text


def post_json(url: str, payload: dict, **kw):
    return request("POST", url, json=payload, **kw).json()
