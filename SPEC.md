# Defense Contract Pricing Dashboard — Spec

## Goal
A public, repeatable analysis of how the Department of Defense pays new defense-tech companies versus the traditional primes, by contract pricing type (fixed-price vs cost-plus), FY2020–FY2026. Output: a SQLite database, a static HTML dashboard (GitHub Pages), and charts for a 5-slide deck. Audience: a recruiter or exec who clicks a link and gets it in 30 seconds.

## Data source
USAspending API v2, no key. Verified 2026-10-27 that `POST https://api.usaspending.gov/api/v2/search/spending_by_category/recipient/` honors `filters.contract_pricing_type_codes`. (Anduril FY2026 DoD: code J ≈ $641M across two recipient rows, code U ≈ $2M.)

Base filter per query:
```json
{"time_period":[{"start_date":"<FY start>","end_date":"<FY end>"}],
 "agencies":[{"type":"awarding","tier":"toptier","name":"Department of Defense"}],
 "award_type_codes":["A","B","C","D"],
 "recipient_search_text":["<search term>"],
 "contract_pricing_type_codes":["<one code>"]}
```
Fiscal year N = Oct 1 of N-1 through Sep 30 of N.

## Companies (config file `companies.yaml`, editable)
- New entrants: Anduril ("Anduril"), Palantir ("Palantir"), Shield AI ("Shield AI").
- Primes: Lockheed Martin ("Lockheed Martin"), RTX ("Raytheon"), Northrop Grumman ("Northrop Grumman"), General Dynamics ("General Dynamics"), Boeing ("Boeing").
Each entry: display name, group (new_entrant|prime), search term, and an `include_regex` applied to returned recipient names (search text is fuzzy and returns unrelated recipients; e.g. "Boeing" must not match a company that merely contains the word). Sum all matching recipient rows (a company appears under several legal entities). Page through results (`limit` 100, `page`) until `hasNext` is false.

## Pricing codes → groups
- fixed: A, B, J, K, L, M
- cost_plus: R, S, T, U, V
- time_materials: Y, Z
- other: 1, 2, 3
Query each code separately and store per code; group in SQL.

## Files
- `companies.yaml`, `pricing_codes.yaml`
- `pull.py` — fetches FY2020–FY2026 for all companies × codes; caches each raw JSON response under `data/raw/` keyed by (company, fy, code) so reruns skip cached calls; polite 0.3s sleep, retries with backoff on 429/5xx; `--refresh` flag ignores cache.
- `build_db.py` — loads raw cache into `data/contracts.db` table `obligations(company, grp, fy, pricing_code, pricing_group, recipient_name, amount)`. Plus views `by_company_year_group` and `share_fixed` (fixed / total per company-year).
- `queries/*.sql` — the analysis queries as readable SQL files (recruiters may read these).
- `make_dashboard.py` — writes `docs/index.html` (self-contained, Plotly via CDN pinned version). Charts: (1) total DoD obligations by year, new entrants vs primes; (2) % fixed-price by company, latest full FY, sorted bar; (3) % fixed-price over time per company, line; (4) Anduril obligations by pricing group, stacked by year. Each chart has a one-sentence takeaway computed from the data (no hardcoded numbers). Footer: data source, pull date, caveats.
- Also export each chart as PNG to `docs/img/` for the deck (kaleido), if that dependency installs cleanly; otherwise skip and note.
- `tests/` — unit tests with recorded fixture JSON (no network): pagination, regex filtering, FY date math, grouping, share computation, cache skip.
- `README.md` — what it shows, how to run (`pip install -r requirements.txt && python pull.py && python build_db.py && python make_dashboard.py`), method, caveats.

## Caveats to state in README and dashboard footer
- Obligations, not outlays; modifications can be negative.
- Recipient matching is by name; subsidiaries may be missed or mis-assigned.
- FY2026 data may still be updating (fiscal year ended 2026-09-30).
- Pricing type is recorded per transaction; large primes have mixed portfolios.

## Constraints
- Python 3, stdlib + requests, pyyaml, plotly (+ kaleido optional). No pandas required (allowed if it simplifies).
- No secrets. `.gitignore`: `data/raw/`, `__pycache__`, `.venv`. Commit `data/contracts.db`? No — gitignore it; `docs/` is committed.
- Don't run the full pull in tests. Do run `pull.py` once for real at the end and report row counts and the four takeaways.
