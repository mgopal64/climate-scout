"""Cheap rule filters that run before any LLM call.

Each returns (ok, reason) or (ok, reason, flags). Flags never drop a job on their own;
they are shown in the notification so you can decide.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- title

def _rx(words: list[str]) -> re.Pattern:
    return re.compile("|".join(f"(?:{w})" for w in words), re.I)


def title_verdict(title: str, cfg: dict) -> tuple[bool, str, list[str]]:
    t = title or ""
    flags: list[str] = []
    if cfg.get("exclude_internships", True) and re.search(
            r"\bintern(ship)?\b|\bco-?op\b|\bsummer (associate|analyst)\b", t, re.I):
        return False, "internship", flags
    exclude = _rx(cfg["title_exclude"])
    m = exclude.search(t)
    if m:
        return False, f"title excluded ({m.group(0)})", flags
    if not _rx(cfg["title_include"]).search(t):
        return False, "title not technical", flags
    if re.search(r"\bII\b|\b2\b", t):
        flags.append("level II title")
    if re.search(r"new grad|university|early career|entry|graduate|junior|associate", t, re.I):
        flags.append("early-career title")
    return True, "", flags


# --------------------------------------------------------------------------- location

_US_EXPLICIT = re.compile(r"united states|\bu\.?s\.?a?\b|\bamerica\b", re.I)
_UK = re.compile(r"united kingdom|\bu\.?k\.?\b|london|england|scotland|wales|"
                 r"manchester|edinburgh|oxford|bristol", re.I)
_FOREIGN = re.compile(
    r"\b(?:india|canada|mexico|brazil|argentina|chile|colombia|peru|germany|france|netherlands|"
    r"spain|italy|sweden|norway|denmark|finland|switzerland|austria|belgium|ireland|poland|"
    r"portugal|israel|australia|new zealand|singapore|japan|china|hong kong|korea|taiwan|"
    r"kenya|nigeria|south africa|\buae\b|emirates|dubai|abu dhabi|saudi|philippines|"
    r"indonesia|vietnam|thailand|malaysia|bengaluru|bangalore|chennai|mumbai|delhi|"
    r"hyderabad|pune|toronto|vancouver|montreal|calgary|berlin|munich|hamburg|paris|"
    r"amsterdam|stockholm|copenhagen|oslo|madrid|barcelona|lisbon|dublin|zurich|geneva|"
    r"tel aviv|sydney|melbourne|tokyo|s[aã]o paulo|europe|emea|apac|latam)\b", re.I)
_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
    "georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    "massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    "new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|"
    "oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|"
    "vermont|virginia|washington|west virginia|wisconsin|wyoming|district of columbia")
_US_CITIES = ("san francisco|bay area|oakland|berkeley|palo alto|mountain view|san jose|"
              "los angeles|seattle|boston|cambridge|nyc|brooklyn|chicago|austin|houston|"
              "denver|boulder|pittsburgh|ann arbor|detroit|atlanta|miami|portland|"
              "salt lake|phoenix|san diego|philadelphia|minneapolis|raleigh")
_US_NAMES = re.compile(rf"\b(?:{_US_STATES}|{_US_CITIES})\b", re.I)
_ABBR = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
         "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
         "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
         "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"}
_ABBR_RX = re.compile(r",\s*([A-Z]{2})\b")


def location_ok(location: str, cfg: dict) -> tuple[bool, str]:
    loc = (location or "").strip()
    if not loc:
        return True, "no location listed"
    if _US_EXPLICIT.search(loc):
        return True, ""
    if cfg.get("allow_uk", True) and _UK.search(loc):
        return True, ""
    for extra in cfg.get("extra_allow_locations") or []:
        if extra.lower() in loc.lower():
            return True, ""
    if _FOREIGN.search(loc) or (not cfg.get("allow_uk", True) and _UK.search(loc)):
        return False, f"location: {loc}"
    if _US_NAMES.search(loc) or any(a in _ABBR for a in _ABBR_RX.findall(loc)):
        return True, ""
    if re.search(r"\bremote\b|anywhere", loc, re.I):
        return True, ""
    return False, f"location: {loc}"


# --------------------------------------------------------------------------- description

_NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
              "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
_YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:(?:-|–|—|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b", re.I)
_NEW_GRAD = re.compile(r"new grad|recent grad|early[- ]career|entry[- ]level|"
                       r"university grad|class of 202[5-8]|0\s*(?:-|–|to)\s*[12]\s*years", re.I)
_NO_SPONSOR = re.compile(r"(unable|not able|cannot|can't|won't|will not|do not|does not|"
                         r"not)\s+(to\s+)?(provide\s+|offer\s+)?(visa\s+|immigration\s+)?"
                         r"sponsor", re.I)
_UK_RTW = re.compile(r"right to work in the uk|authori[sz]ed to work in the (uk|united kingdom)",
                     re.I)
_CLEARANCE = re.compile(r"security clearance|ts/sci|secret clearance|clearance required", re.I)
_PHD = re.compile(r"ph\.?d\.?\s+(is\s+)?required|requires?\s+a\s+ph\.?d|ph\.?d\.?\s+in\s+"
                  r"[a-z ,]+\s+required", re.I)


def required_years(desc: str) -> int | None:
    """Smallest 'N years ... experience' requirement found, or None."""
    text = re.sub(r"\b(" + "|".join(_NUM_WORDS) + r")\b",
                  lambda m: _NUM_WORDS[m.group(1).lower()], desc or "", flags=re.I)
    mins = []
    for m in _YEARS.finditer(text):
        around = text[max(0, m.start() - 40): m.end() + 80].lower()
        if "experience" in around:
            n = int(m.group(1))
            if n <= 20:
                mins.append(n)
    return min(mins) if mins else None


def description_verdict(desc: str, cfg: dict) -> tuple[bool, str, list[str]]:
    flags: list[str] = []
    if not desc:
        return True, "", ["no description available"]
    new_grad = bool(_NEW_GRAD.search(desc))
    if new_grad:
        flags.append("new-grad language in posting")
    yrs = required_years(desc)
    if yrs is not None and not new_grad:
        if yrs >= cfg.get("max_years_drop", 3):
            return False, f"requires {yrs}+ yrs", flags
        if yrs >= cfg.get("max_years_flag", 2):
            flags.append(f"asks for {yrs}+ yrs")
    if _NO_SPONSOR.search(desc):
        flags.append("no visa sponsorship")
    if _UK_RTW.search(desc):
        flags.append("UK right-to-work required")
    if _CLEARANCE.search(desc):
        flags.append("security clearance")
    if _PHD.search(desc):
        flags.append("PhD required")
    return True, "", flags


# --------------------------------------------------------------------------- relevance (no LLM)
# (pattern, weight if in title, weight if only in description)
_REL = [
    (re.compile(r"machine learning|\bml\b|data scien|forecast|optimi[sz]|model(l)?(ing|er)|"
                r"simulation|quant|physics", re.I), 3, 1),
    (re.compile(r"geospatial|remote sensing|\bgis\b|earth observation|climate|weather|hydro|"
                r"water|\bgrid\b|power system|energy|emission|carbon|environment", re.I), 2, 1),
    (re.compile(r"software|\bdata\b|research|platform|back.?end|full.?stack|analytics", re.I), 1, 0),
    (re.compile(r"new grad|entry|junior|early career|associate|university|graduate", re.I), 2, 1),
]
_NEG = re.compile(r"hardware|mechanical|electrical|firmware|embedded|manufactur|test engineer|"
                  r"process engineer|civil|structural|\bQA\b|quality", re.I)


def relevance(title: str, desc: str = "") -> int:
    """Free 0-10 estimate of fit from keywords, used to rank when there's no LLM score."""
    score = 3
    for rx, in_title, in_desc in _REL:
        if rx.search(title or ""):
            score += in_title
        elif in_desc and rx.search(desc or ""):
            score += in_desc
    if _NEG.search(title or ""):
        score -= 2
    return max(0, min(10, score))