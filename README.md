# climate-scout

Near-real-time alerts for **early-career climate tech roles**, filtered for fit and pushed to your phone.

```
 discover (daily)                                 poll (every 10 min)
 ───────────────                                  ───────────────────
 7 climate VC boards (Getro) ─┐                   for each company:
 ClimateTechList pages ───────┼─> companies.json ─>  hit its ATS API directly
 seeds.yaml ──────────────────┤                      diff vs. seen jobs
 slug guessing (cached) ──────┘                      rules → Claude fit check → ntfy push
 + catch-all: new Getro jobs at companies we can't poll (Workday, custom ATS)
```

**Sources:** Climate Draft (Lowercarbon + dozens of climate VCs), MCJ, Breakthrough Energy Ventures,
Breakthrough Energy Fellows, Elemental Impact, Energy Impact Partners, Energize Capital,
ClimateTechList (homepage, university page, 10 verticals), your seeds.

**Pollable ATSs:** Greenhouse, Lever (+EU), Ashby, Workable, SmartRecruiters.

## Setup (~2 hours)

1. **Local run first**
   ```bash
   pip install -r requirements.txt
   python -m pytest -q                    # 57 offline tests
   python -m scout.discover               # ~5-10 min first time (slug guessing)
   cat data/discovery_report.md           # check sources, guesses, errors
   ```
2. **Tonight's list:** rank everything open *right now*:
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...    # optional; rules-only without it
   python -m scout.poll --report          # writes report.md, sorted by fit
   ```
3. **Phone alerts:** install the **ntfy** app, subscribe to a long random topic
   (e.g. `scout-7f3k9q2m...`). Anyone who knows the topic can read it, so make it unguessable.
4. **GitHub:** push this repo, **public** (unlimited Actions minutes; private repos get 2,000
   min/month, so change poll.yml's cron to `*/30`). Add repo secrets:
   | Secret | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | your key |
   | `NTFY_TOPIC` | your topic |
   | `PROFILE_TEXT` | contents of `profile.md` |
   | `CONTACTS_JSON` | contents of `contacts.json` |
   `profile.md` and `contacts.json` are gitignored, so they stay off GitHub.
5. Actions tab → run **discover**, then **poll**, manually once. The first poll *baselines*
   (no alerts). After that you only hear about genuinely new postings.

## Tuning (config.yaml)

- `filters.title_include / title_exclude`: regexes on job titles
- `filters.allow_uk`: London/UK on or off
- `filters.max_years_drop`: drop postings requiring ≥ N years (default 3)
- `llm.min_fit`, `llm.drop_levels`: how picky the Claude check is
- `seeds.yaml`: add targets; pin `ats` + `slug` if a guess is wrong
- Add any Getro board (`getro_boards`) or any company-list page (`page_sources`)

## What each alert shows

Title @ company, location, fit score + one-line reason, **"YOU KNOW: …"** if you have a contact
there (high priority), and flags such as `asks for 2+ yrs`, `no visa sponsorship`,
`UK right-to-work required`, `new-grad language in posting`.

## Known limits

- Latency: GitHub cron is best-effort and can lag 5-30 min under load. For true 2-3 min
  polling, run `python -m scout.poll` on AWS Lambda + EventBridge (same code, state in S3/DynamoDB).
- Workday / custom ATS companies are only covered by the daily Getro sweep.
- Big-tech careers sites (Google, Amazon, Microsoft) aren't covered.
- Getro's API is undocumented. If the discovery report shows a board failing, pin its
  `network_id` in config or check whether its response shape changed.
