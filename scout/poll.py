"""Fast path: poll every company's ATS directly, alert on new matching jobs.

  python -m scout.poll              normal run (used by the GitHub Action)
  python -m scout.poll --report     rank ALL currently open matching jobs -> report.md
  python -m scout.poll --dry-run    evaluate + print, don't notify or save state
  python -m scout.poll --no-llm     rules only
"""
from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import ats, config, http, llm
from .pipeline import evaluate, notify_batch


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


def write_report(results, path) -> None:
    passed = [(j, v) for j, v in results if v.passed]
    passed.sort(key=lambda jv: (-(jv[1].fit if jv[1].fit is not None else 5), jv[0].company))
    lines = [f"# climate-scout report - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"{len(passed)} matching open roles (of {len(results)} evaluated)\n",
             "| Fit | Level | Company | Title | Location | Flags / why |",
             "|---|---|---|---|---|---|"]
    for j, v in passed:
        note = "; ".join(filter(None, [v.why] + v.flags)).replace("|", "/")
        lines.append(f"| {v.fit if v.fit is not None else '-'} | {v.level or '-'} | "
                     f"{j.company} | [{j.title}]({j.url}) | {j.location} | {note} |")
    path.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {path} ({len(passed)} matches)")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="only poll the first N companies")
    args = ap.parse_args(argv)

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

    results = evaluate_all(candidates, cfg, profile, use_llm)

    if args.report:
        write_report(results, config.ROOT / "report.md")
    else:
        passed = [(j, v) for j, v in results if v.passed]
        passed.sort(key=lambda jv: -(jv[1].fit or 0))
        cap = cfg["poll"].get("max_notifications_per_run", 25)
        if args.dry_run:
            for j, v in passed[:cap]:
                print(f"  [dry] {j.title} @ {j.company} fit={v.fit} {v.flags}")
        else:
            notify_batch(passed[:cap], contacts, cfg, overflow=max(0, len(passed) - cap),
                         source="direct ATS poll")
        print(f"{len(passed)} matches of {len(results)} new jobs")
        if not args.dry_run:
            state.mark(j.key for j in candidates)
            state.save()

    for e in errors[:15]:
        print(f"  [error] {e}")
    print(f"done in {time.time() - t0:.0f}s at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()