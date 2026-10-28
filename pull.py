"""Fetch transaction obligations, preserving every recipient and pricing-code page."""

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import time

import requests
import yaml


ROOT = Path(__file__).resolve().parent
ENDPOINT = "https://api.usaspending.gov/api/v2/search/spending_by_category/recipient/"
YEARS = range(2020, 2027)
LOGGER = logging.getLogger(__name__)


def fiscal_year_dates(fy):
    return f"{fy - 1}-10-01", f"{fy}-09-30"


def company_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not slug:
        raise ValueError(f"Company name has no usable cache key: {name!r}")
    return slug


def load_config(root=ROOT):
    with (root / "companies.yaml").open() as source:
        companies = yaml.safe_load(source)["companies"]
    with (root / "pricing_codes.yaml").open() as source:
        groups = yaml.safe_load(source)
    if not isinstance(companies, list) or not companies:
        raise ValueError("companies.yaml must contain a nonempty companies list")
    names, slugs = set(), set()
    for company in companies:
        if not isinstance(company, dict):
            raise ValueError(f"Company entries must be mappings: {company!r}")
        for key in ("name", "group", "include_regex"):
            if not isinstance(company.get(key), str) or not company[key].strip():
                raise ValueError(f"Company requires a nonempty {key}: {company!r}")
        terms = company.get("search_terms")
        if (not isinstance(terms, list) or not terms
                or any(not isinstance(term, str) or not term.strip() for term in terms)):
            raise ValueError(f"Company requires a nonempty search_terms list: {company!r}")
        if len({term.casefold() for term in terms}) != len(terms):
            raise ValueError(f"Company search_terms must be unique: {company['name']}")
        subsidiaries = company.get("subsidiaries")
        if (not isinstance(subsidiaries, list)
                or any(not isinstance(name, str) or not name.strip() for name in subsidiaries)):
            raise ValueError(f"Company requires a subsidiaries list of names: {company!r}")
        if company["group"] not in {"new_entrant", "prime"}:
            raise ValueError(f"Invalid company group: {company['group']}")
        slug = company_slug(company["name"])
        if company["name"] in names or slug in slugs:
            raise ValueError(f"Duplicate company/cache key: {company['name']}")
        names.add(company["name"])
        slugs.add(slug)
        try:
            re.compile(company["include_regex"], re.IGNORECASE)
        except re.error as error:
            raise ValueError(f"Invalid regex for {company['name']}: {error}") from error
    if not isinstance(groups, dict) or set(groups) != {"fixed", "cost_plus", "time_materials", "other"}:
        raise ValueError("Pricing groups must be fixed, cost_plus, time_materials, other")
    seen = set()
    for group, codes in groups.items():
        if not isinstance(codes, list) or not codes:
            raise ValueError(f"Pricing group {group} requires a nonempty code list")
        groups[group] = [str(code) for code in codes]
        for code in groups[group]:
            if not re.fullmatch(r"[A-Z0-9]", code) or code in seen:
                raise ValueError(f"Invalid or repeated pricing code: {code}")
            seen.add(code)
    return companies, groups


def build_filters(company, fy, code, search_term=None):
    terms = company["search_terms"]
    search_term = terms[0] if search_term is None else search_term
    if search_term not in terms:
        raise ValueError(f"Search term is not configured for {company['name']}: {search_term!r}")
    start, end = fiscal_year_dates(fy)
    return {
        "time_period": [{"start_date": start, "end_date": end}],
        "agencies": [{"type": "awarding", "tier": "toptier", "name": "Department of Defense"}],
        "award_type_codes": ["A", "B", "C", "D"],
        "recipient_search_text": [search_term],
        "contract_pricing_type_codes": [code],
    }


def cache_path(cache_dir, company, fy, code, search_term=None):
    terms = company["search_terms"]
    search_term = terms[0] if search_term is None else search_term
    try:
        term_index = terms.index(search_term)
    except ValueError as error:
        raise ValueError(f"Search term is not configured for {company['name']}: {search_term!r}") from error
    filename = f"{code}.json" if term_index == 0 else f"{code}__{term_index + 1:02d}_{company_slug(search_term)}.json"
    return cache_dir / company_slug(company["name"]) / str(fy) / filename


def validate_page(page, number):
    if not isinstance(page, dict) or not isinstance(page.get("results"), list):
        raise ValueError(f"Invalid results on page {number}")
    metadata = page.get("page_metadata", {})
    if (not isinstance(metadata, dict) or type(metadata.get("hasNext")) is not bool
            or metadata.get("page") != number):
        raise ValueError(f"Invalid pagination metadata on page {number}")
    if metadata["hasNext"] and not page["results"]:
        raise ValueError(f"Empty page {number} claims a following page")


def fetch_pages(filters, session, sleep=time.sleep):
    pages = []
    number = 1
    while True:
        payload = {"filters": filters, "spending_level": "transactions", "limit": 100, "page": number}
        for attempt in range(5):
            sleep(0.3)
            try:
                response = session.post(ENDPOINT, json=payload, timeout=(15, 90))
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 4:
                    raise
                LOGGER.warning("Network error on page %s; retry %s/4", number, attempt + 1)
                sleep(float(2 ** attempt))
                continue
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == 4:
                    response.raise_for_status()
                try:
                    retry_after = float(response.headers.get("Retry-After", 0))
                except (TypeError, ValueError):
                    retry_after = 0
                delay = max(float(2 ** attempt), retry_after)
                LOGGER.warning("HTTP %s on page %s; retry %s/4 in %.1fs",
                               response.status_code, number, attempt + 1, delay)
                sleep(delay)
                continue
            response.raise_for_status()
            page = response.json()
            validate_page(page, number)
            pages.append(page)
            break
        if not page["page_metadata"]["hasNext"]:
            return pages
        number += 1


def validate_record(record, company, fy, code, search_term=None):
    terms = company["search_terms"]
    search_term = terms[0] if search_term is None else search_term
    expected = {"filters": build_filters(company, fy, code, search_term), "spending_level": "transactions", "limit": 100}
    if (record.get("company") != company["name"] or record.get("fy") != fy
            or record.get("pricing_code") != code or record.get("request") != expected
            or record.get("search_term", search_term) != search_term):
        raise ValueError(f"Cache request differs from config: {company['name']} FY{fy} {code} {search_term}")
    pages = record.get("pages")
    if not isinstance(pages, list) or not pages or not record.get("pulled_at"):
        raise ValueError("Cache is incomplete or missing its pull timestamp")
    for number, page in enumerate(pages, 1):
        validate_page(page, number)
        if page["page_metadata"]["hasNext"] != (number < len(pages)):
            raise ValueError("Cache has missing or extra pages")


def pull_query(company, fy, code, cache_dir, session, sleep=time.sleep, refresh=False, search_term=None):
    terms = company["search_terms"]
    search_term = terms[0] if search_term is None else search_term
    path = cache_path(cache_dir, company, fy, code, search_term)
    if path.exists() and not refresh:
        try:
            record = json.loads(path.read_text())
            validate_record(record, company, fy, code, search_term)
            return record
        except (ValueError, TypeError, KeyError) as error:
            LOGGER.warning("Refetching invalid/stale cache %s: %s", path, error)
    filters = build_filters(company, fy, code, search_term)
    pages = fetch_pages(filters, session, sleep)
    record = {
        "company": company["name"], "fy": fy, "pricing_code": code,
        "search_term": search_term,
        "request": {"filters": filters, "spending_level": "transactions", "limit": 100},
        "pulled_at": datetime.now(timezone.utc).isoformat(), "endpoint": ENDPOINT, "pages": pages,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Ignore cached responses and fetch every query again")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    companies, groups = load_config()
    codes = [code for group in groups.values() for code in group]
    total = sum(len(company["search_terms"]) for company in companies) * len(YEARS) * len(codes)
    count = 0
    with requests.Session() as session:
        session.headers["User-Agent"] = "defense-contract-pricing-dashboard/1.0 (public research)"
        for company in companies:
            for fy in YEARS:
                for code in codes:
                    for search_term in company["search_terms"]:
                        pull_query(company, fy, code, ROOT / "data/raw", session,
                                   refresh=args.refresh, search_term=search_term)
                        count += 1
                LOGGER.info("%s FY%s complete (%s/%s queries)", company["name"], fy, count, total)
    LOGGER.info("Pull complete: %s query caches, FY2020–FY2026", count)


if __name__ == "__main__":
    main()
