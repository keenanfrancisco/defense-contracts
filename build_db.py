"""Rebuild a SQLite analysis database from complete, validated raw caches."""

import csv
from contextlib import closing
import json
import math
import re
import sqlite3

from pull import ROOT, YEARS, cache_path, load_config, validate_record

JOINT_VENTURE_MARKERS = re.compile(r"\b(?:JOINT\s+(?:VENTURE|PROJECT)|JV)\b", re.IGNORECASE)


def normalize_recipient_name(name):
    normalized = " ".join(name.casefold().split()).rstrip(" .,:;!?")
    while True:
        without_suffix = re.sub(r"[, ]+(?:inc|llc|corp)\.?$", "", normalized).rstrip(" .,:;!?")
        if without_suffix == normalized:
            break
        normalized = without_suffix
    return " ".join(normalized.split())


def matched_rows(record, company, pricing_group):
    pattern = re.compile(company["include_regex"], re.IGNORECASE)
    subsidiaries = {normalize_recipient_name(name) for name in company["subsidiaries"]}
    accepted, excluded = [], []
    for page in record["pages"]:
        for result in page["results"]:
            name, amount = result.get("name"), result.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
                raise ValueError(f"Invalid obligation amount for {company['name']}: {amount!r}")
            name = name.strip() if isinstance(name, str) else "<missing name>"
            is_joint_venture = bool(JOINT_VENTURE_MARKERS.search(name))
            if not is_joint_venture and (pattern.fullmatch(name)
                                         or normalize_recipient_name(name) in subsidiaries):
                accepted.append((company["name"], company["group"], record["fy"],
                                 record["pricing_code"], pricing_group, name, amount))
            else:
                excluded.append({"company": company["name"], "fy": record["fy"],
                                 "pricing_code": record["pricing_code"], "recipient_name": name,
                                 "amount": amount,
                                 "reason": ("Joint venture marker excluded" if is_joint_venture else
                                            "Name does not match include_regex or exact subsidiaries list")})
    return accepted, excluded


def _recipient_identity(result):
    recipient_id = result.get("recipient_id")
    if recipient_id not in (None, ""):
        return ("recipient_id", str(recipient_id))
    uei = result.get("uei")
    if uei not in (None, ""):
        return ("uei", str(uei))
    code = result.get("code")
    if code not in (None, ""):
        return ("recipient_code", str(code))
    return None


def _search_term(record, company):
    if "search_term" in record:
        return record["search_term"]
    search_text = record["request"]["filters"].get("recipient_search_text", [])
    return search_text[0] if len(search_text) == 1 else company["search_terms"][0]


def merge_search_results(records):
    """Join search results without counting the same USAspending recipient twice.

    A search term filters transactions, not just recipients: "General Dynamics" returns
    only the Electric Boat transactions that mention the parent. Each result is a subset
    of the recipient's true total, so keep the largest one per recipient.
    """
    best = {}
    merged = []
    for record in records:
        for page in record["pages"]:
            for result in page["results"]:
                identity = _recipient_identity(result)
                if identity is None:
                    merged.append(result)
                elif identity not in best or abs(result["amount"]) > abs(best[identity]["amount"]):
                    best[identity] = result
    return merged + list(best.values())


def build_database(database, records, companies, groups, years=YEARS, require_complete=True):
    years = list(years)
    company_map = {company["name"]: company for company in companies}
    code_map = {code: group for group, codes in groups.items() for code in codes}
    expected = {(company["name"], fy, code, term) for company in companies for fy in years
                for code in code_map for term in company["search_terms"]}
    indexed = {}
    for record in records:
        company = company_map.get(record["company"])
        if company is None:
            raise ValueError(f"Unexpected cache key: {record.get('company')!r}")
        key = (record["company"], record["fy"], record["pricing_code"], _search_term(record, company))
        if key not in expected or key in indexed:
            raise ValueError(f"Unexpected or repeated cache key: {key}")
        validate_record(record, company, key[1], key[2], key[3])
        indexed[key] = record
    if require_complete and expected != set(indexed):
        missing = sorted(expected - set(indexed))
        raise ValueError(f"Incomplete cache: {len(missing)} missing queries; first: {missing[:3]}. Run pull.py.")
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.with_suffix(".db.tmp")
    # A fresh temporary database is published only after all validation succeeds.
    if temporary.exists():
        raise ValueError(f"Unfinished build exists at {temporary}; inspect it before rebuilding")
    try:
        with closing(sqlite3.connect(temporary)) as conn, conn:
            conn.executescript((ROOT / "queries/schema.sql").read_text())
            conn.executemany("INSERT INTO coverage VALUES (?, ?, ?)",
                             [(company["name"], company["group"], fy) for company in companies for fy in years])
            grouped = {}
            for (name, fy, code, term), record in indexed.items():
                grouped.setdefault((name, fy, code), []).append(record)
            for (name, fy, code), search_records in grouped.items():
                record = search_records[0]
                merged_record = {**record, "pages": [{"results": merge_search_results(search_records)}]}
                accepted, excluded = matched_rows(merged_record, company_map[name], code_map[code])
                conn.executemany("INSERT INTO obligations VALUES (?, ?, ?, ?, ?, ?, ?)", accepted)
                conn.executemany("INSERT INTO recipient_exclusions VALUES (:company, :fy, :pricing_code, :recipient_name, :amount, :reason)", excluded)
                conn.execute("INSERT INTO pull_metadata VALUES (?, ?, ?, ?)", (name, fy, code, record["pulled_at"]))
            count = conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]
            conn.commit()
        temporary.replace(database)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return count


def main():
    companies, groups = load_config()
    records = []
    for company in companies:
        for fy in YEARS:
            for codes in groups.values():
                for code in codes:
                    for search_term in company["search_terms"]:
                        path = cache_path(ROOT / "data/raw", company, fy, code, search_term)
                        if not path.exists():
                            raise SystemExit(f"Missing {path.relative_to(ROOT)}; run python pull.py first.")
                        records.append(json.loads(path.read_text()))
    database = ROOT / "data/contracts.db"
    count = build_database(database, records, companies, groups)
    with closing(sqlite3.connect(database)) as conn:
        conn.row_factory = sqlite3.Row
        exclusions = conn.execute((ROOT / "queries/recipient_matching.sql").read_text()).fetchall()
    report = ROOT / "docs/recipient_matching.csv"
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["company", "recipient_name", "amount", "first_fy", "last_fy", "result_rows"])
        writer.writerows(exclusions)
    print(f"Built data/contracts.db: {count:,} rows in obligations; {len(records)} complete queries.")
    print(f"Excluded {len(exclusions)} company/name pairs; full list: docs/recipient_matching.csv")


if __name__ == "__main__":
    main()
