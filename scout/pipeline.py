"""rules -> description fetch -> LLM -> verdict, plus notifications."""
from __future__ import annotations

import html
import os
import smtplib
import sys
import unicodedata
from email.message import EmailMessage
from dataclasses import dataclass, field

import requests

from . import ats, filters, llm
from .config import norm


@dataclass
class Verdict:
    passed: bool
    reason: str = ""
    flags: list[str] = field(default_factory=list)
    fit: int | None = None
    level: str = ""
    why: str = ""


def evaluate(job: ats.Job, cfg: dict, profile: str, *, use_llm: bool,
             fetch_detail: bool = True) -> Verdict:
    fcfg = cfg["filters"]
    ok, reason, flags = filters.title_verdict(job.title, fcfg)
    if not ok:
        return Verdict(False, reason, flags)
    ok, reason = filters.location_ok(job.location, fcfg)
    if not ok:
        return Verdict(False, reason, flags)
    if fetch_detail:
        ats.ensure_description(job)
    ok, reason, dflags = filters.description_verdict(job.description, fcfg)
    flags += dflags
    if not ok:
        return Verdict(False, reason, flags)
    if not use_llm:
        return Verdict(True, "rules passed", flags)

    s = llm.score(job, profile, cfg["llm"])
    if s is None:
        return Verdict(True, "rules passed (LLM unavailable)", flags + ["unscored"])
    flags += [f"risk: {k}" for k in s["knockouts"]]
    v = Verdict(True, "", flags, s["fit"], s["level"], s["reason"])
    if s["level"] in cfg["llm"].get("drop_levels", []):
        v.passed, v.reason = False, f"LLM level={s['level']}"
    elif s["fit"] < cfg["llm"].get("min_fit", 6):
        v.passed, v.reason = False, f"LLM fit {s['fit']}"
    return v


# --------------------------------------------------------------------------- notify
# One digest per run. Channels (config notify.channels): email (default) and/or ntfy.
#   email: EMAIL_USER + EMAIL_APP_PASSWORD (Gmail app password), optional EMAIL_TO
#   ntfy:  NTFY_TOPIC

def contacts_for(company: str, contacts: dict[str, list[str]]) -> list[str]:
    c = norm(company)
    out: list[str] = []
    for name, people in contacts.items():
        n = norm(name)
        if n and c and (n == c or n in c or c in n):
            out += people
    return out


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()[:250]


def format_message(job: ats.Job, v: Verdict, who: list[str]) -> str:
    lines = [f"{job.company} | {job.location or 'location n/a'}"]
    if v.fit is not None:
        lines.append(f"Fit {v.fit}/10 ({v.level}): {v.why}")
    if who:
        lines.append("YOU KNOW: " + ", ".join(who))
    if v.flags:
        lines.append("Flags: " + "; ".join(v.flags))
    return "\n".join(lines)


def build_digest(entries, overflow: int, source: str) -> tuple[str, str, str]:
    """(subject, plain text, html) for a list of (job, verdict, contacts)."""
    n = len(entries)
    first = entries[0][0] if entries else None
    subject = (f"[climate-scout] {n} new role{'s' if n != 1 else ''}"
               + (f": {first.title} @ {first.company}" if first else "")
               + (f" +{n - 1} more" if n > 1 else ""))
    text, rows = [], []
    for j, v, who in entries:
        text.append(f"{j.title} @ {j.company}\n{format_message(j, v, who)}\n{j.url}\n")
        e = html.escape
        meta = " &middot; ".join(filter(None, [
            e(j.location or "location n/a"),
            f"Fit <b>{v.fit}/10</b> ({e(v.level)}): {e(v.why)}" if v.fit is not None else ""]))
        rows.append(
            f'<div style="margin:0 0 18px"><div style="font-size:16px"><a href="{e(j.url)}">'
            f"{e(j.title)}</a> &mdash; <b>{e(j.company)}</b></div>"
            f'<div style="color:#444">{meta}</div>'
            + (f'<div style="color:#0a7d2c"><b>You know:</b> {e(", ".join(who))}</div>' if who else "")
            + (f'<div style="color:#777;font-size:13px">{e("; ".join(v.flags))}</div>' if v.flags else "")
            + "</div>")
    if overflow:
        text.append(f"(+{overflow} more matches not listed; see the Actions log)")
        rows.append(f"<p><i>+{overflow} more matches not listed; see the Actions log.</i></p>")
    footer = f"Source: {source}. Sorted: people you know first, then fit."
    return (subject, "\n".join(text) + "\n" + footer,
            f'<div style="font-family:Arial,sans-serif">{"".join(rows)}'
            f'<p style="color:#999;font-size:12px">{html.escape(footer)}</p></div>')


def send_email(subject: str, text: str, html_body: str, cfg: dict) -> bool:
    user, pw = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_APP_PASSWORD")
    if not (user and pw):
        print("  [notify] EMAIL_USER / EMAIL_APP_PASSWORD not set - printed only")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, user
    msg["To"] = os.environ.get("EMAIL_TO") or user
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    ncfg = cfg.get("notify", {})
    try:
        with smtplib.SMTP_SSL(ncfg.get("smtp_host", "smtp.gmail.com"),
                              int(ncfg.get("smtp_port", 465)), timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        print(f"  [notify] emailed {msg['To']}: {subject}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [notify] email failed: {e}")
        return False


def _ntfy(job: ats.Job, body: str, high: bool, cfg: dict) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        requests.post(f"{cfg.get('notify', {}).get('ntfy_server', 'https://ntfy.sh').rstrip('/')}/{topic}",
                      data=body.encode("utf-8"), timeout=15,
                      headers={"Title": _ascii(f"{job.title} @ {job.company}"), "Click": job.url,
                               "Tags": "seedling", "Priority": "high" if high else "default"})
    except requests.RequestException as e:
        print(f"  [notify] ntfy failed: {e}")


def notify_batch(items, contacts: dict, cfg: dict, overflow: int = 0, source: str = "poll") -> None:
    """Send one digest for a run's matches: list of (job, verdict)."""
    if not items:
        return
    entries = [(j, v, contacts_for(j.company, contacts)) for j, v in items]
    entries.sort(key=lambda e: (not e[2], -(e[1].fit if e[1].fit is not None else 5)))
    for j, v, who in entries:
        print(f"  >> {j.title} @ {j.company}\n     "
              f"{format_message(j, v, who).replace(chr(10), chr(10) + '     ')}\n     {j.url}")
    channels = cfg.get("notify", {}).get("channels", ["email"])
    if "email" in channels:
        send_email(*build_digest(entries, overflow, source), cfg)
    if "ntfy" in channels:
        for j, v, who in entries:
            _ntfy(j, format_message(j, v, who), bool(who) or (v.fit or 0) >= 8, cfg)


if __name__ == "__main__" and "--test-email" in sys.argv:
    from .config import load
    demo = ats.Job("ashby", "gridcare", "GridCARE", "0", "Optimization Engineer, Grid Systems",
                   "Redwood City, CA", "https://jobs.ashbyhq.com/gridcare")
    ok = send_email(*build_digest([(demo, Verdict(True, flags=["test email"], fit=9, level="early",
                                                    why="test of climate-scout email"), [])],
                                  0, "test"), load())
    print("sent" if ok else "not sent - see message above")