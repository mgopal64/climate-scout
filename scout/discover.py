"""Build data/companies.json from every source, and run the Getro catch-all sweep.

Sources:
  1. Getro VC/community boards (config.getro_boards) - every job carries the company's
     ATS link, which gives us its slug directly.
  2. List pages (config.page_sources, e.g. ClimateTechList) - any ATS link on the page.
  3. seeds.yaml - your own targets.
  4. Name-based slug guessing for companies with no ATS link (cached, marked guessed).

  python -m scout.discover             full run (daily GitHub Action)
  python -m scout.discover --no-guess  skip slug guessing (faster)
  python -m scout.discover --dry-run   don't write files / notify
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import ats, config, consider, getro, llm, sources
from .config import norm
from .pipeline import evaluate, notify_batch

GUESS_TTL_DAYS = 14
_LEGAL = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|gmbh|pbc|plc|sa|bv)\b\.?", re.I)


def slug_candidates(name: str) -> list[str]:
    n = _LEGAL.sub("", name.lower())
    out = [re.sub(r"[^a-z0-9]", "", n), re.sub(r"[^a-z0-9]+", "-", n).strip("-")]
    return [s for i, s in enumerate(out) if s and s not in out[:i]]


def similar(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    return bool(a and b) and (a == b or a in b or b in a
                              or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8)


def guess(name: str) -> tuple[str, str] | None:
    cands = slug_candidates(name)
    for s in cands:
        board = ats.probe_greenhouse(s)
        if board is not None and similar(board or s, name):
            return "greenhouse", s
    for s in cands + [name]:
        if ats.has_jobs("ashby", s):
            return "ashby", s
    for s in cands:
        if ats.has_jobs("lever", s):
            return "lever", s
    return None


class Registry:
    def __init__(self):
        self.by_key: dict[tuple[str, str], dict] = {}
        self.per_source: dict[str, set] = defaultdict(set)

    def add(self, a: str, slug: str, name: str, source: str, guessed: bool = False):
        key = (a, slug.lower())
        c = self.by_key.get(key)
        if c is None:
            c = self.by_key[key] = {"ats": a, "slug": slug, "name": name or slug,
                                    "sources": [], "guessed": guessed}
        if name and c["name"] == c["slug"] and name != slug:
            c["name"] = name
        if source not in c["sources"]:
            c["sources"].append(source)
        if not guessed:
            c["guessed"] = False
        self.per_source[source].add(key)

    def names(self, pollable_only=False) -> set[str]:
        return {norm(c["name"]) for c in self.by_key.values()
                if not pollable_only or c["ats"] in ats.POLLABLE} | \
               {norm(c["slug"]) for c in self.by_key.values()
                if not pollable_only or c["ats"] in ats.POLLABLE}

    def to_list(self) -> list[dict]:
        return sorted(self.by_key.values(), key=lambda c: (c["name"].lower(), c["ats"]))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-guess", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-pages", type=int, default=getro.MAX_PAGES,
                    help="cap Getro pages per board (testing)")
    args = ap.parse_args(argv)

    t0 = time.time()
    cfg = config.load()
    state = config.State.load()
    reg = Registry()
    errors: list[str] = []
    unresolved: dict[str, tuple[str, str]] = {}
    unsupported = Counter()
    board_items: list[tuple[str, str, ats.Job]] = []   # (board, raw ATS url, job)
    board_stats: list[str] = []

    for c in config.load_companies():                       # keep what we already know
        for s in c.get("sources") or ["previous run"]:
            reg.add(c["ats"], c["slug"], c["name"], s, c.get("guessed", False))

    # 1. Getro boards
    for b in cfg.get("getro_boards", []):
        try:
            net = b.get("network_id") or getro.resolve_network_id(b["url"])
            items = getro.walk_jobs(net, b["url"], max_pages=args.max_pages)
        except Exception as e:  # noqa: BLE001
            errors.append(f"getro {b['name']}: {e}")
            print(f"[getro] {b['name']}: FAILED {e}")
            continue
        orgs = set()
        for it in items:
            org = it.get("organization") or {}
            name = " ".join((org.get("name") or "").split())
            orgs.add(name)
            hit = ats.detect_ats(it.get("url"))
            if hit and hit[0] in ats.POLLABLE:
                reg.add(hit[0], hit[1], name, b["name"])
            else:
                if hit:
                    unsupported[hit[0]] += 1
                if name:
                    unresolved.setdefault(norm(name), (name, b["name"]))
            board_items.append((b["name"], it.get("url") or "", getro.to_job(it, b["url"])))
        board_stats.append(f"| {b['name']} | {net} | {len(items)} | {len(orgs)} |")
        print(f"[getro] {b['name']} (net {net}): {len(items)} jobs, {len(orgs)} companies")

    # 1b. Consider boards (MCJ, ...)
    for b in cfg.get("consider_boards", []):
        try:
            items = consider.walk_jobs(b["url"], b["board_id"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"consider {b['name']}: {e}")
            print(f"[consider] {b['name']}: FAILED {e}")
            continue
        orgs = set()
        for it in items:
            job = consider.to_job(it)
            orgs.add(job.company)
            raw = it.get("url") or it.get("applyUrl") or ""
            hit = ats.detect_ats(raw) or ats.detect_ats(it.get("applyUrl"))
            if hit and hit[0] in ats.POLLABLE:
                reg.add(hit[0], hit[1], job.company, b["name"])
            else:
                if hit:
                    unsupported[hit[0]] += 1
                if job.company:
                    unresolved.setdefault(norm(job.company), (job.company, b["name"]))
            board_items.append((b["name"], raw, job))
        board_stats.append(f"| {b['name']} (Consider) | {b['board_id']} | {len(items)} | {len(orgs)} |")
        print(f"[consider] {b['name']}: {len(items)} jobs, {len(orgs)} companies")

    # 2. list pages (ClimateTechList, ...)
    for src in cfg.get("page_sources", []):
        found, errs = sources.extract_from_pages(src["urls"])
        errors += [f"{src['name']}: {e}" for e in errs]
        for a, s in found:
            if a in ats.POLLABLE:
                reg.add(a, s, s, src["name"])
            else:
                unsupported[a] += 1
        print(f"[pages] {src['name']}: {len(found)} ATS boards from {len(src['urls'])} pages")

    # 3. seeds
    for s in config.load_seeds():
        if s.get("ats") and s.get("slug"):
            reg.add(s["ats"], s["slug"], s["name"], "seeds")
        else:
            unresolved.setdefault(norm(s["name"]), (s["name"], "seeds"))

    # 4. guess slugs for companies without an ATS link
    known = reg.names()
    unresolved = {k: v for k, v in unresolved.items() if k and k not in known}
    cache_path = config.DATA / "guess_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=GUESS_TTL_DAYS)

    def due(k):
        e = cache.get(k)
        return e is None or (e["result"] is None and datetime.fromisoformat(e["checked"]) < stale)

    todo = [(k, v) for k, v in unresolved.items() if due(k)]
    if not args.no_guess and todo:
        print(f"[guess] probing {len(todo)} company names...")
        with ThreadPoolExecutor(cfg["discovery"].get("guess_workers", 8)) as ex:
            for (k, _), res in zip(todo, ex.map(lambda kv: guess(kv[1][0]), todo)):
                cache[k] = {"result": list(res) if res else None, "checked": now.isoformat()}
    guessed = []
    for k, (name, src) in unresolved.items():
        hit = (cache.get(k) or {}).get("result")
        if hit:
            reg.add(hit[0], hit[1], name, src, guessed=True)
            guessed.append(f"{name} -> {hit[0]}/{hit[1]}")
    still = sorted(v[0] for k, v in unresolved.items() if not (cache.get(k) or {}).get("result"))

    companies = reg.to_list()
    pollable = [c for c in companies if c["ats"] in ats.POLLABLE]

    # 5. board catch-all: new board jobs at companies we can't poll directly.
    #    Each board baselines silently the first time it's seen, so adding a board never floods you.
    pollable_names = reg.names(pollable_only=True)
    by_board: dict[str, list] = defaultdict(list)
    dedupe = set()
    for bname, raw, job in board_items:
        if not job.company or norm(job.company) in pollable_names:
            continue
        dk = raw.split("?")[0] or f"{norm(job.company)}|{job.title.lower()}"
        if dk in dedupe:
            continue
        dedupe.add(dk)
        job.job_id = hashlib.sha1(dk.encode()).hexdigest()[:16]
        by_board[bname].append(job)
    n_catch_alerts = 0
    if board_items and not args.dry_run:
        new = []
        for bname, jobs in by_board.items():
            bk = f"board:{bname}"
            if bk not in state.initialized:
                state.mark(j.key for j in jobs)
                state.initialized.add(bk)
                print(f"[catch-all] {bname}: baselined {len(jobs)} jobs")
            else:
                new += [j for j in jobs if not state.is_seen(j.key)]
        use_llm = llm.available(cfg["llm"])
        profile, contacts = config.load_profile(), config.load_contacts()
        passed = []
        for j in new:
            v = evaluate(j, cfg, profile, use_llm=use_llm, fetch_detail=False)
            if v.passed:
                v.flags.append("via daily board sweep")
                passed.append((j, v))
        passed.sort(key=lambda jv: -(jv[1].fit or 0))
        cap = cfg.get("poll", {}).get("max_notifications_per_run", 25)
        notify_batch(passed[:cap], contacts, cfg, overflow=max(0, len(passed) - cap),
                     source="daily board sweep")
        n_catch_alerts = min(len(passed), cap)
        state.mark(j.key for j in new)
        print(f"[catch-all] {len(new)} new jobs, {len(passed)} matches, {n_catch_alerts} alerts")

    # 6. write outputs
    per_ats = Counter(c["ats"] for c in companies)
    report = [
        f"# Discovery report - {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"**{len(pollable)} pollable companies** "
        f"({', '.join(f'{a}: {n}' for a, n in per_ats.most_common())})\n",
        "## Getro boards", "| Board | Network | Jobs | Companies |", "|---|---|---|---|",
        *board_stats, "",
        "## Companies contributed per source",
        *[f"- {s}: {len(keys)}" for s, keys in sorted(reg.per_source.items())], "",
        f"## Unsupported ATS links seen (covered by daily catch-all)\n"
        f"{dict(unsupported) or 'none'}\n",
        f"## Guessed slugs - sanity check these ({len(guessed)})",
        *[f"- {g}" for g in sorted(guessed)], "",
        f"## No ATS found ({len(still)}) - add to seeds.yaml with ats/slug if you care",
        ", ".join(still), "",
        f"## Errors ({len(errors)})", *[f"- {e}" for e in errors],
    ]
    if not args.dry_run:
        config.save_companies(companies)
        cache_path.write_text(json.dumps(cache, indent=0))
        (config.DATA / "discovery_report.md").write_text("\n".join(report) + "\n")
        state.save()
    print(f"\n{len(pollable)} pollable companies ({dict(per_ats)}); "
          f"{len(guessed)} guessed; {len(still)} unresolved; {len(errors)} errors; "
          f"{time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()