# How defense gets paid

A repeatable comparison of Department of Defense contract pricing for **Anduril,
Palantir, and Shield AI** versus **Lockheed Martin, RTX (Raytheon search), Northrop
Grumman, General Dynamics, and Boeing**, FY2020–FY2026.

Open [`docs/index.html`](docs/index.html) for four interactive charts and a
data-computed takeaway beside each chart. The page includes its data and styles;
it loads a version-pinned Plotly.js file from the CDN, so charts need an internet
connection. Expand the data tables for exact values or without JavaScript.

## Run

Python 3.10 or newer is required by the pinned dependencies. No API key is needed.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt && python pull.py && python build_db.py && python make_dashboard.py
```

The first pull makes at least 1,344 sequential API requests (12 search terms × 7
fiscal years × 16 pricing codes), plus additional pages. Each network attempt waits
0.3 seconds. HTTP 429/5xx responses and connection/time-out errors retry up to
four times with exponential backoff; numeric `Retry-After` delays are respected.
Other HTTP errors stop the pull instead of becoming zero obligations.

Rerun the same commands to reuse the cache. `python pull.py --refresh` fetches all
queries again. Each search term has its own cache; changing one invalidates only
that term's cache. Changing only matching rules requires rebuilding the database
and dashboard.

Optional PNG exports use Kaleido and an existing Chrome/Chromium installation:

```sh
pip install kaleido==1.5.0
python make_dashboard.py
```

If Kaleido or Chrome is unavailable, the HTML still builds and reports why PNG
export was skipped. Successful exports are in `docs/img/`, at 2400 × 1350 pixels
(16:9) for slides. A machine-readable run report is in `docs/report.json`.

For a local browser preview:

```sh
python -m http.server 8000 --directory docs
```

Open <http://localhost:8000>. The `docs/` folder can also be served directly by
GitHub Pages: choose the branch and `/docs` folder in the repository's Pages
settings. No build service is needed. No deployment is performed by these scripts.

## Method

1. **Query transaction obligations.** POST to the public
   [USAspending recipient category endpoint](https://api.usaspending.gov/api/v2/search/spending_by_category/recipient/)
   with `spending_level: transactions`, awarding agency Department of Defense,
   contract award types A–D, one fiscal year, one company search term, and one
   pricing code. Each configured search term is queried separately. FY N is
   October 1 of N−1 through September 30 of N. See the
   [official API contract](https://github.com/fedspendingtransparency/usaspending-api/blob/master/usaspending_api/api_contracts/contracts/v2/search/spending_by_category/recipient.md).
2. **Collect and cache every page.** Use `limit: 100` and increment `page` until
   `page_metadata.hasNext` is false. A complete query is saved atomically as
   `data/raw/<company_slug>/<fy>/<pricing_code>.json`; additional terms use a
   term-specific suffix. Each envelope preserves all original page responses,
   request filters, endpoint, and UTC pull timestamp. An incomplete or invalid
   query is never published as a completed cache.
3. **Match names conservatively.** `companies.yaml` supplies the display name,
   group, `search_terms`, a case-insensitive `include_regex`, and explicit exact
   `subsidiaries`. Regex matching uses the entire stripped recipient name
   (`re.fullmatch`), not substring containment. Subsidiary names match without
   regard to case and tolerate trailing punctuation or INC, LLC, or CORP.
   Duplicate recipient IDs returned by multiple searches count once; distinct
   legal-entity results remain separate, including identical names, zero amounts,
   and negative modifications.
4. **Group and divide in SQL.** `build_db.py` requires all expected query caches,
   validates them against the current search config, and builds
   `data/contracts.db`. The `obligations` table has the specified company, cohort,
   year, pricing code, pricing group, recipient name, and amount columns.
   `by_company_year_group` sums net obligations by pricing group. `share_fixed`
   divides the fixed group's net amount floored at zero by the sum of each group's
   net amount floored at zero across all configured groups. Negative group totals
   remain visible in net obligations but contribute zero to this ratio. A zero
   positive-group total produces SQL `NULL`, not 0%. Amounts are USD and SQL
   aggregates round to cents.
5. **Compute four chart takeaways.** Readable queries in [`queries/`](queries/)
   produce cohort totals, latest full FY rankings, share histories, and Anduril's
   pricing mix. The latest full FY is selected from ended fiscal years in the
   database. Trend takeaways compare each company's earliest non-null share with
   its share in that latest FY, then select the largest absolute percentage-point
   change. No takeaway numbers are embedded in the implementation.

Pricing groups are editable in `pricing_codes.yaml`:

| Group | Transaction pricing codes |
| --- | --- |
| Fixed-price | A, B, J, K, L, M |
| Cost-reimbursement (cost-plus, cost-no-fee, cost-sharing) | R, S, T, U, V |
| Time & materials | Y, Z |
| Other | 1, 2, 3 |

The database additionally records completed company-years, per-query pull dates,
and rejected recipient rows. It is rebuilt atomically, so validation failures
leave the previous database in place. Raw caches, the SQLite database, the
virtual environment, and bytecode are gitignored. The static `docs/` output is
intended for version control.

## Recipient matching and caveats

- **Obligations, not outlays; modifications can be negative.** These are net
  transaction amounts, not contract ceilings, revenue, cash paid, or profits.
- **Recipient matching is by name; other subsidiaries may be missed or mis-assigned.**
  Configured exact subsidiaries count in every fiscal year, regardless of
  acquisition date. Joint ventures and uncertain names remain excluded. This is
  a named-recipient comparison, not a consolidated corporate-family analysis;
  historical ownership and divestitures are not reconstructed.
- **FY2026 data may still be updating (fiscal year ended 2026-09-30).** A refresh
  can change the results; the dashboard displays the cache's UTC pull window.
- **Pricing type is recorded per transaction; large primes have mixed
  portfolios.** A large fixed-price share alone does not measure technical risk,
  profitability, or procurement quality. Negative net pricing-group totals are
  excluded from the share denominator and the fixed numerator is floored at zero.
- **Scope is limited to the configured searches and pricing codes.** RTX uses
  separate searches for Raytheon, RTX, Pratt & Whitney, Collins Aerospace, and
  Rockwell Collins. Nightwing and Everfox remain excluded as divested businesses.
  The two cohorts do not represent all DoD contractors. Blank or unlisted pricing
  codes are outside the denominator.

Every excluded name is published in
[`docs/recipient_matching.csv`](docs/recipient_matching.csv), with its queried
company, net amount, fiscal-year range, and result-row count. The same list is
included in `docs/report.json`. It includes possible subsidiaries and joint
ventures whose corporate attribution was **not verified**; exclusion does not
establish that a recipient is unrelated. Values can overlap across fuzzy company
searches and must not be added together as a spending universe.

The live pull also returned **ANDURIL LLC** (UEI `JJP3TT4EA9G7`), distinct from
the returned Anduril Industries UEIs. Its affiliation with Anduril Industries
was not verified, so the default regex excludes it. Its FY2022 fixed-price
obligations are retained in the matching report rather than assigned to the
defense-tech company.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The tests use `unittest`, temporary SQLite databases, mocked HTTP sessions, and
[`tests/fixtures/anduril_2026_J.json`](tests/fixtures/anduril_2026_J.json), recorded
from the live API on 2026-10-27. That fixture preserves three pages, including two
separate nonzero recipient entities with the same legal name and a zero-dollar
entity on the last page. Tests make no network requests and never run the full
pull. They cover pagination, subsidiary and regex matching, near misses,
multi-term cache handling and overlap deduplication, fiscal-year math, SQL
grouping and shares, cache skip/refresh/invalidation, bounded retries, and
computed takeaways.
