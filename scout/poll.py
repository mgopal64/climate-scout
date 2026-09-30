"""Fast path: poll every company's ATS directly, alert on new matching jobs.

  python -m scout.poll              normal run (used by the GitHub Action)
  python -m scout.poll --report     rank ALL currently open matching jobs -> report.md
  python -m scout.poll --dry-run    evaluate + print, don't notify or save state
  python -m scout.poll --no-llm     rules only
  python -m scout.poll --report --email   ...and email it (used by the cloud 'report' workflow)
  python -m scout.poll --force-email      email evaluated jobs (matched or not) to test the
                                          email path; doesn't save state, so real alerts aren't lost
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import ats, config, http, llm
from .pipeline import email_report, evaluate, notify_batch


def fetch_all(companies: list[dict], workers: int):
    ok, errors = [], []

    def one(c):
        return c, ats.fetch_jobs(c["ats"], c["slug"], c["name"])

    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(one, c) for c in companies]
        for f in as_completed(futs):
            try:
                ok.append(f.result())
            except http.NotFound as e:
                errors.append(f"404 {e}")
            except Exception as e:  # noqa: BLE001
                errors.append(str(e)[:160])
    return ok, errors


def evaluate_all(jobs, cfg, profile, use_llm, workers=4):
    out = []
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(evaluate, j, cfg, profile, use_llm=use_llm): j for j in jobs}
        for f in as_completed(futs):
            j = futs[f]
            try:
                out.append((j, f.result()))
            except Exception as e:  # noqa: BLE001
                print(f"  [eval] {j.company} / {j.title}: {e}")
    return out


def _score(v) -> str:
    if v.fit is not None:
        return str(v.fit)
    return f"~{v.est}" if v.est is not None else "-"


def log_verdicts(results) -> None:
    """One line per evaluated job so you can see WHY something was or wasn't a match."""
    ordered = sorted(results, key=lambda jv: (not jv[1].passed, -(jv[1].rank or 0), jv[0].company))
    for j, v in ordered:
        tag = "MATCH" if v.passed else "skip "
        note = "; ".join(filter(None, [v.why] + list(v.flags)))
        line = f"  [{tag}] fit={_score(v)} level={v.level or '-'} | {j.company} | {j.title} | {j.location} | {note}"
        print(line[:400])


def log_errors(errors: list[str]) -> None:
    """Summarize failed boards. Prints every one (not just the first 15) and emits a
    GitHub Actions warning annotation so it shows on the run summary page."""
    if not errors:
        return
    n404 = [e for e in errors if e.startswith("404")]
    other = [e for e in errors if not e.startswith("404")]
    print(f"\n{len(errors)} boards failed ({len(n404)} x 404 -> slug/ATS changed, fix or remove; "
          f"{len(other)} other):")
    for e in sorted(n404) + sorted(other):
        print(f"  [error] {e}")
    print(f"::warning title=climate-scout::{len(errors)} boards failed "
          f"({len(n404)} are 404s - check slugs in the companies list)")


def write_report(results, path) -> None:
    passed = [(j, v) for j, v in results if v.passed]
    passed.sort(key=lambda jv: (-jv[1].rank, jv[0].company))
    scored = any(v.fit is not None for _, v in passed)
    lines = [f"# climate-scout report - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"{len(passed)} matching open roles (of {len(results)} evaluated). "
             + ("Fit = LLM score; ~N = keyword estimate where unscored."
                if scored else "~N = free keyword-relevance estimate (run without --no-llm for LLM fit)."),
             "",
             "| Fit | Level | Company | Title | Location | Flags / why |",
             "|---|---|---|---|---|---|"]
    for j, v in passed:
        note = "; ".join(filter(None, [v.why] + v.flags)).replace("|", "/")
        score = v.fit if v.fit is not None else (f"~{v.est}" if v.est is not None else "-")
        lines.append(f"| {score} | {v.level or '-'} | "
                     f"{j.company} | [{j.title}]({j.url}) | {j.location} | {note} |")
    path.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {path} ({len(passed)} matches)")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--email", action="store_true", help="with --report: email the ranked report")
    ap.add_argument("--force-email", action="store_true",
                    help="email evaluated jobs whether or not they matched (tests the email path; "
                         "state is not saved). Also enabled by env FORCE_EMAIL=1")
    ap.add_argument("--limit", type=int, default=0, help="only poll the first N companies")
    args = ap.parse_args(argv)
    force_email = (args.force_email or os.environ.get("FORCE_EMAIL") == "1") \
        and not args.report and not args.dry_run

    t0 = time.time()
    cfg = config.load()
    state = config.State.load()
    profile, contacts = config.load_profile(), config.load_contacts()
    use_llm = not args.no_llm and llm.available(cfg["llm"])

    companies = [c for c in config.load_companies() if c["ats"] in ats.POLLABLE]
    if args.limit:
        companies = companies[:args.limit]
    if not companies:
        print("no companies yet - run `python -m scout.discover` first")
        return
    print(f"polling {len(companies)} companies "
          f"(llm={cfg['llm'].get('provider', 'anthropic') if use_llm else 'off - rules only'})")

    fetched, errors = fetch_all(companies, cfg["poll"].get("workers", 8))
    candidates, baselined = [], 0
    for c, jobs in fetched:
        ck = f"{c['ats']}:{c['slug'].lower()}"
        if args.report:
            candidates += jobs
        elif ck not in state.initialized:
            state.mark(j.key for j in jobs)
            state.initialized.add(ck)
            baselined += 1
        else:
            candidates += [j for j in jobs if not state.is_seen(j.key)]

    total_open = sum(len(j) for _, j in fetched)
    print(f"fetched {total_open} open jobs; {len(errors)} companies errored; "
          f"{baselined} newly baselined; {len(candidates)} to evaluate")

    # Email-path test: if nothing is new, sample a few open jobs (one per company) so
    # there is something to send.
    if force_email and not candidates:
        candidates = [jobs[0] for _, jobs in fetched if jobs][:3]
        print(f"[force-email] no new jobs; sampling {len(candidates)} open jobs to test email")

    results = evaluate_all(candidates, cfg, profile, use_llm)

    if args.report:
        path = config.ROOT / "report.md"
        write_report(results, path)
        if args.email:
            email_report(results, contacts, cfg, path)
    else:
        log_verdicts(results)
        passed = [(j, v) for j, v in results if v.passed]
        passed.sort(key=lambda jv: -jv[1].rank)
        cap = cfg["poll"].get("max_notifications_per_run", 15)
        if args.dry_run:
            for j, v in passed[:cap]:
                print(f"  [dry] {j.title} @ {j.company} fit={v.fit} {v.flags}")
        else:
            to_send = passed
            if force_email:
                to_send = sorted(results, key=lambda jv: -(jv[1].rank or 0))
                print(f"[force-email] sending {min(len(to_send), cap)} jobs regardless of match")
            notify_batch(to_send[:cap], contacts, cfg, overflow=max(0, len(to_send) - cap),
                         source="direct ATS poll" + (" (FORCED TEST)" if force_email else ""))
        print(f"{len(passed)} matches of {len(results)} new jobs")
        if not args.dry_run and not force_email:
            state.mark(j.key for j in candidates)
            state.save()

    log_errors(errors)
    print(f"done in {time.time() - t0:.0f}s at {datetime.now(timezone.utc).isoformat()}")

    # Optional: fail the run (red X) if too many boards break. Set poll.max_errors in config.
    max_errors = cfg["poll"].get("max_errors")
    if max_errors is not None and len(errors) > max_errors:
        print(f"::error::{len(errors)} boards failed (> max_errors={max_errors})")
        sys.exit(1)


if __name__ == "__main__":
    main()