"""LLM fit + seniority check for jobs that already passed the rule filters.

Providers (config.yaml -> llm.provider):
  gemini     free tier via Google AI Studio key (GEMINI_API_KEY); Flash / Flash-Lite models
  anthropic  paid API (ANTHROPIC_API_KEY)
With no key, or llm.enabled: false, scout runs rules-only at $0.

Free tiers are rate-limited, so calls are throttled to llm.rpm, and after 3 consecutive
429s the rest of the run continues rules-only instead of failing.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import requests

LEVELS = {"new_grad", "early", "mid", "senior", "unclear"}
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}

SYSTEM = """You screen job postings for ONE candidate, described in the candidate profile.
Your job is to protect their time: be skeptical, never inflate scores.

Return ONLY a JSON object, no prose, no code fences:
{"fit": <int 0-10>, "level": "<new_grad|early|mid|senior|unclear>",
 "knockouts": [<short strings>], "reason": "<one sentence, <=25 words>"}

level = the experience the ROLE targets:
  new_grad: explicitly new grad / entry level / 0-1 yrs
  early: 0-2 yrs, or an unspecified individual-contributor role a strong new grad could land
  mid: asks for ~3-5 yrs or frames the role as mid-level
  senior: 5+ yrs, senior/staff/lead scope
  unclear: not enough information

fit rubric (relative to the candidate's actual background):
  9-10: core overlap - ML, modeling, simulation, optimization or data work in energy/grid,
        water, weather/climate, climate risk, geospatial or environmental systems
  6-8: solid software/data role at a climate company where their skills clearly transfer
  3-5: technical but a weak match (e.g. heavy product SWE in a systems language, hardware,
       mechanical/electrical engineering, production-reliability heavy)
  0-2: non-technical, wrong discipline, or clearly not for them

knockouts: things likely to trigger an automatic rejection for this candidate, e.g.
"requires 3+ yrs", "PhD required", "security clearance", "onsite outside US/UK",
"needs PE license", "UK right-to-work required". Empty list if none."""

_lock = threading.Lock()
_state = {"next": 0.0, "fails": 0}
MAX_429 = 3
RETRY_SLEEP = 3


def provider(cfg: dict) -> tuple[str, str | None]:
    p = cfg.get("provider", "anthropic")
    return p, os.environ.get(KEY_ENV.get(p, ""), None)


def available(cfg: dict) -> bool:
    return bool(cfg.get("enabled", True) and provider(cfg)[1])


def _throttle(rpm) -> None:
    if not rpm:
        return
    with _lock:
        now = time.monotonic()
        wait = _state["next"] - now
        _state["next"] = max(now, _state["next"]) + 60.0 / float(rpm)
    if wait > 0:
        time.sleep(wait)


def parse_score(text: str) -> dict:
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError(f"no JSON in model output: {text[:200]!r}")
    d = json.loads(m.group(0))
    fit = max(0, min(10, int(d.get("fit", 0))))
    level = str(d.get("level", "unclear")).lower().strip()
    if level not in LEVELS:
        level = "unclear"
    kos = [str(k) for k in (d.get("knockouts") or []) if str(k).strip()][:5]
    return {"fit": fit, "level": level, "knockouts": kos,
            "reason": str(d.get("reason", ""))[:240]}


def _anthropic(key: str, model: str, user: str) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 400, "system": SYSTEM,
              "messages": [{"role": "user", "content": user}]},
        timeout=60)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


def _gemini(key: str, model: str, user: str) -> str:
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "content-type": "application/json"},
        json={"systemInstruction": {"parts": [{"text": SYSTEM}]},
              "contents": [{"role": "user", "parts": [{"text": user}]}],
              "generationConfig": {"responseMimeType": "application/json",
                                   "maxOutputTokens": 1024, "temperature": 0}},
        timeout=60)
    r.raise_for_status()
    cands = r.json().get("candidates") or []
    parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
    return "".join(p.get("text", "") for p in parts)


DEFAULT_MODEL = {"anthropic": "claude-haiku-4-5-20251001", "gemini": "gemini-flash-latest"}


def score(job, profile: str, cfg: dict) -> dict | None:
    p, key = provider(cfg)
    if not key or _state["fails"] >= MAX_429:
        return None
    model = cfg.get("model") or DEFAULT_MODEL.get(p, "")
    user = (f"CANDIDATE PROFILE:\n{profile}\n\n"
            f"JOB POSTING\nCompany: {job.company}\nTitle: {job.title}\n"
            f"Location: {job.location or 'n/a'}\n"
            f"Description:\n{(job.description or '(not available)')[:7000]}")
    call = _gemini if p == "gemini" else _anthropic
    try:
        for attempt in range(3):                       # retry transient 5xx (e.g. Gemini 503)
            _throttle(cfg.get("rpm"))
            try:
                text = call(key, model, user)
                break
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code >= 500 and attempt < 2:
                    time.sleep(RETRY_SLEEP * (attempt + 1))
                    continue
                raise
        _state["fails"] = 0
        return parse_score(text)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            _state["fails"] += 1
            if _state["fails"] >= MAX_429:
                print("  [llm] rate/quota limit hit repeatedly - rest of this run is rules-only")
        print(f"  [llm] {p} error for {job.company} / {job.title}: {e}")
        return None
    except Exception as e:  # noqa: BLE001 - scoring failure must not drop the job
        print(f"  [llm] scoring failed for {job.company} / {job.title}: {e}")
        return None