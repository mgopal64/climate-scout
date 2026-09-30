"""Loading config, company list, state, profile and contacts.

Private data (profile, contacts) is read from env vars first so the repo can be public:
  PROFILE_TEXT   your background, used by the LLM fit check
  CONTACTS_JSON  {"Company": ["Name", ...]} - flags jobs where you know someone
Local fallbacks: profile.md / contacts.json (both gitignored).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STATE = ROOT / "state"


def load() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def load_seeds() -> list[dict]:
    p = ROOT / "seeds.yaml"
    return (yaml.safe_load(p.read_text()) or {}).get("companies", []) if p.exists() else []


def load_companies() -> list[dict]:
    p = DATA / "companies.json"
    return json.loads(p.read_text()) if p.exists() else []


def save_companies(companies: list[dict]) -> None:
    DATA.mkdir(exist_ok=True)
    (DATA / "companies.json").write_text(json.dumps(companies, indent=1, ensure_ascii=False))


def load_profile() -> str:
    if os.environ.get("PROFILE_TEXT"):
        return os.environ["PROFILE_TEXT"]
    for name in ("profile.md", "profile.example.md"):
        p = ROOT / name
        if p.exists():
            return p.read_text()
    return ""


def load_contacts() -> dict[str, list[str]]:
    raw = os.environ.get("CONTACTS_JSON")
    if not raw and (ROOT / "contacts.json").exists():
        raw = (ROOT / "contacts.json").read_text()
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        print("[config] CONTACTS_JSON is not valid JSON; ignoring")
        return {}


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class State:
    """seen job keys + which companies have been baselined."""

    def __init__(self, seen=None, initialized=None, getro_initialized=False):
        self.seen: dict[str, str] = seen or {}
        self.initialized: set[str] = set(initialized or [])
        self.getro_initialized: bool = getro_initialized

    @classmethod
    def load(cls) -> "State":
        p = STATE / "seen.json"
        if not p.exists():
            return cls()
        d = json.loads(p.read_text())
        return cls(d.get("seen"), d.get("initialized"), d.get("getro_initialized", False))

    def save(self) -> None:
        STATE.mkdir(exist_ok=True)
        (STATE / "seen.json").write_text(json.dumps({
            "getro_initialized": self.getro_initialized,
            "initialized": sorted(self.initialized),
            "seen": self.seen}, separators=(",", ":")))

    def is_seen(self, key: str) -> bool:
        return key in self.seen

    def mark(self, keys) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for k in keys:
            self.seen.setdefault(k, now)
