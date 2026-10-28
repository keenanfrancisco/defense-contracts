import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from pull import build_filters, fetch_pages, fiscal_year_dates, load_config, pull_query
from build_db import build_database, matched_rows, merge_search_results
from make_dashboard import compute_takeaways, latest_full_fy, load_analysis, make_figures, render_dashboard


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/anduril_2026_J.json").read_text())
ANDURIL = {
    "name": "Anduril", "group": "new_entrant", "search_terms": ["Anduril"],
    "subsidiaries": [],
    "include_regex": r"ANDURIL INDUSTRIES,? INC\.?",
}
GROUPS = {"fixed": ["A", "B", "J", "K", "L", "M"],
          "cost_plus": ["R", "S", "T", "U", "V"],
          "time_materials": ["Y", "Z"], "other": ["1", "2", "3"]}


def response(page, status=200):
    result = Mock(status_code=status, headers={})
    result.json.return_value = copy.deepcopy(page)
    result.raise_for_status.side_effect = requests.HTTPError() if status >= 400 else None
    return result


def record(company, fy, code, rows, search_term=None):
    search_term = company["search_terms"][0] if search_term is None else search_term
    return {"company": company["name"], "fy": fy, "pricing_code": code,
            "search_term": search_term,
            "request": {"filters": build_filters(company, fy, code, search_term),
                        "spending_level": "transactions", "limit": 100},
            "pulled_at": "2026-10-27T12:00:00+00:00",
            "pages": [{"results": rows, "page_metadata": {"page": 1, "hasNext": False}}]}


class PullTests(unittest.TestCase):
    def test_fiscal_year_uses_october_through_september(self):
        self.assertEqual(fiscal_year_dates(2020), ("2019-10-01", "2020-09-30"))
        self.assertEqual(fiscal_year_dates(2026), ("2025-10-01", "2026-09-30"))

    def test_query_filters_match_recorded_request(self):
        self.assertEqual(build_filters(ANDURIL, 2026, "J"), FIXTURE["request"]["filters"])

    def test_query_filters_accept_one_term_from_a_multi_term_config(self):
        company = {**ANDURIL, "search_terms": ["Anduril", "Anduril Technologies"]}
        filters = build_filters(company, 2026, "J", search_term="Anduril Technologies")
        self.assertEqual(filters["recipient_search_text"], ["Anduril Technologies"])

    def test_pagination_follows_has_next_and_keeps_same_named_entities(self):
        session, sleep = Mock(), Mock()
        session.post.side_effect = [response(page) for page in FIXTURE["pages"]]
        pages = fetch_pages(FIXTURE["request"]["filters"], session, sleep)
        self.assertEqual(len(pages), 3)
        self.assertEqual(sum(len(page["results"]) for page in pages), 3)
        calls = session.post.call_args_list
        self.assertEqual([call.kwargs["json"]["page"] for call in calls], [1, 2, 3])
        self.assertTrue(all(call.kwargs["json"]["limit"] == 100 for call in calls))
        self.assertTrue(all(call.kwargs["json"]["spending_level"] == "transactions" for call in calls))
        self.assertEqual(sleep.call_count, 3)
        self.assertTrue(all(call.args == (0.3,) for call in sleep.call_args_list))

    def test_bad_response_is_an_error_not_an_empty_result(self):
        for bad in ({"results": []}, {"results": [], "page_metadata": {"hasNext": "false"}},
                    {"results": [], "page_metadata": {"page": 9, "hasNext": False}}):
            with self.subTest(bad=bad):
                session = Mock()
                session.post.return_value = response(bad)
                with self.assertRaises(ValueError):
                    fetch_pages({}, session, Mock())

    def test_429_and_server_errors_retry_but_400_does_not(self):
        page = FIXTURE["pages"][-1]
        page = {**page, "page_metadata": {"page": 1, "hasNext": False}}
        session, sleep = Mock(), Mock()
        limited = response({}, 429)
        limited.headers = {"Retry-After": "4"}
        session.post.side_effect = [limited, response({}, 503), response(page)]
        self.assertEqual(fetch_pages({}, session, sleep), [page])
        self.assertIn(4.0, [call.args[0] for call in sleep.call_args_list])
        self.assertIn(2.0, [call.args[0] for call in sleep.call_args_list])
        session = Mock()
        session.post.return_value = response({}, 400)
        with self.assertRaises(requests.HTTPError):
            fetch_pages({}, session, Mock())
        self.assertEqual(session.post.call_count, 1)

    def test_retry_budget_is_bounded(self):
        session = Mock()
        session.post.return_value = response({}, 503)
        with self.assertRaises(requests.HTTPError):
            fetch_pages({}, session, Mock())
        self.assertEqual(session.post.call_count, 5)

    def test_cache_skip_refresh_and_changed_request(self):
        with tempfile.TemporaryDirectory() as directory:
            session, sleep = Mock(), Mock()
            page = {"results": [], "page_metadata": {"page": 1, "hasNext": False}}
            session.post.return_value = response(page)
            first = pull_query(ANDURIL, 2026, "J", Path(directory), session, sleep)
            second = pull_query(ANDURIL, 2026, "J", Path(directory), session, sleep)
            self.assertEqual(first, second)
            self.assertEqual(session.post.call_count, 1)
            self.assertEqual(len(list(Path(directory).rglob("*.json"))), 1)
            pull_query(ANDURIL, 2026, "J", Path(directory), session, sleep, refresh=True)
            self.assertEqual(session.post.call_count, 2)
            changed = {**ANDURIL, "search_terms": ["Anduril", "Anduril Industries"]}
            pull_query(changed, 2026, "J", Path(directory), session, sleep,
                       search_term="Anduril Industries")
            self.assertEqual(session.post.call_count, 3)
            self.assertEqual(len(list(Path(directory).rglob("*.json"))), 2)

    def test_failed_pull_never_publishes_a_partial_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Mock()
            session.post.side_effect = [response(FIXTURE["pages"][0]), response({}, 400)]
            with self.assertRaises(requests.HTTPError):
                pull_query(ANDURIL, 2026, "J", Path(directory), session, Mock())
            self.assertEqual(list(Path(directory).rglob("*.json")), [])

    def test_configs_have_exact_cohorts_and_pricing_codes(self):
        companies, groups = load_config(ROOT)
        self.assertEqual([company["name"] for company in companies],
                         ["Anduril", "Palantir", "Shield AI", "Lockheed Martin", "RTX",
                          "Northrop Grumman", "General Dynamics", "Boeing"])
        self.assertEqual([company["group"] for company in companies].count("new_entrant"), 3)
        self.assertEqual(groups, GROUPS)

    def test_config_has_exact_search_terms_and_subsidiaries(self):
        companies, _ = load_config(ROOT)
        by_name = {company["name"]: company for company in companies}
        expected = {
            "Lockheed Martin": ["SIKORSKY AIRCRAFT CORPORATION", "SIKORSKY SUPPORT SERVICES INC",
                                "LOCKHEED MISSILES & SPACE COMP", "DERCO AEROSPACE"],
            "RTX": ["RTX BBN TECHNOLOGIES", "GOODRICH CORPORATION",
                    "HAMILTON SUNDSTRAND CORPORATION", "ROSEMOUNT AEROSPACE INC",
                    "SIMMONDS PRECISION PRODUCTS INC", "COLLINS ENGINE NOZZLES, INC"],
            "Northrop Grumman": ["ORBITAL SCIENCES LLC", "ATK LAUNCH SYSTEMS INC.",
                                  "ATK LAUNCH SYSTEMS LLC", "ATK SPACE SYSTEMS LLC",
                                  "ALLIANT TECHSYSTEMS OPERATIONS LLC", "SCALED COMPOSITES",
                                  "SPACE LOGISTICS LLC", "REMOTEC", "COI CERAMICS", "VINNELL ARABIA",
                                  "CUTTING EDGE OPTRONICS INC", "MICRO CRAFT INC"],
            "General Dynamics": ["ELECTRIC BOAT CORPORATION", "BATH IRON WORKS CORPORATION",
                                 "NATIONAL STEEL AND SHIPBUILDING COMPANY", "GULFSTREAM AEROSPACE CORPORATION",
                                 "CSRA LLC", "METRO MACHINE CORP", "AMERICAN OVERSEAS MARINE CO. LLC",
                                 "JET AVIATION ST. LOUIS", "APPLIED PHYSICAL SCIENCES CORP"],
            "Boeing": ["INSITU", "AURORA FLIGHT SCIENCES CORPORATION", "ARGON ST",
                       "MILLENNIUM SPACE SYSTEMS", "SPECTROLAB", "LIQUID ROBOTICS",
                       "TAPESTRY SOLUTIONS", "DIGITAL RECEIVER TECHNOLOGY",
                       "MCDONNELL DOUGLAS HELICOPTER C", "WISK AERO LLC"],
            "Shield AI": ["HERON SYSTEMS", "MARTIN UAV LLC"],
        }
        self.assertEqual({name: by_name[name]["subsidiaries"] for name in expected}, expected)
        self.assertEqual(by_name["RTX"]["search_terms"],
                         ["Raytheon", "RTX", "Pratt & Whitney", "Collins Aerospace", "Rockwell Collins",
                          "Goodrich", "Hamilton Sundstrand", "Rosemount Aerospace", "Simmonds Precision"])
        # Parent-name searches can miss subsidiaries entirely (Electric Boat FY2025), so each is searched directly.
        self.assertIn("Electric Boat", by_name["General Dynamics"]["search_terms"])
        self.assertIn("Sikorsky", by_name["Lockheed Martin"]["search_terms"])
        self.assertTrue(all(isinstance(company["search_terms"], list) for company in companies))
        self.assertTrue(all(isinstance(company["subsidiaries"], list) for company in companies))

    def test_config_rejects_duplicate_codes_and_invalid_regex(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "companies.yaml").write_text("companies:\n- name: Test\n  group: prime\n  search_terms: [Test]\n  subsidiaries: []\n  include_regex: '['\n")
            (root / "pricing_codes.yaml").write_text("fixed: [J]\ncost_plus: [J]\n")
            with self.assertRaises(ValueError):
                load_config(root)


class DatabaseTests(unittest.TestCase):
    def test_regex_is_case_insensitive_full_match_and_preserves_negative_and_zero(self):
        rows = [{"name": "anduril industries, inc.", "amount": -12.34},
                {"name": "ANDURIL INDUSTRIES INC", "amount": 0},
                {"name": "NOT ANDURIL INDUSTRIES, INC.", "amount": 900},
                {"name": "ANDURIL INDUSTRIES, INC. UNRELATED", "amount": 900},
                {"name": None, "amount": 900}]
        accepted, excluded = matched_rows(record(ANDURIL, 2026, "J", rows), ANDURIL, "fixed")
        self.assertEqual([row[-1] for row in accepted], [-12.34, 0])
        self.assertEqual({row["recipient_name"] for row in excluded},
                         {"NOT ANDURIL INDUSTRIES, INC.", "ANDURIL INDUSTRIES, INC. UNRELATED", "<missing name>"})

    def test_boeing_regex_excludes_joint_ventures_and_contains_only_near_misses(self):
        companies, _ = load_config(ROOT)
        boeing = next(company for company in companies if company["name"] == "Boeing")
        rows = [{"name": name, "amount": 1} for name in
                ["THE BOEING COMPANY", "BOEING AEROSPACE OPERATIONS, INC.",
                 "BELL BOEING JOINT PROJECT OFFICE", "BOEING AEROSPACE JOINT VENTURE",
                 "HRL LABORATORIES, LLC", "SPIRIT AEROSYSTEMS, INC.",
                 "JEPPESEN FOREFLIGHT, INC.", "INVENTORY LOCATOR SERVICE, INC.",
                 "ACME BOEING PARTS LLC", "BOEING", "THE BOEING"]]
        accepted, excluded = matched_rows(record(boeing, 2026, "J", rows), boeing, "fixed")
        self.assertEqual(len(accepted), 2)
        excluded_names = [row["recipient_name"] for row in excluded]
        self.assertEqual(len(excluded), 9)
        for name in ("BOEING AEROSPACE JOINT VENTURE", "HRL LABORATORIES, LLC",
                     "SPIRIT AEROSYSTEMS, INC.", "JEPPESEN FOREFLIGHT, INC.",
                     "INVENTORY LOCATOR SERVICE, INC."):
            self.assertIn(name, excluded_names)

    def test_anduril_config_excludes_the_unverified_llc_name(self):
        companies, _ = load_config(ROOT)
        anduril = next(company for company in companies if company["name"] == "Anduril")
        rows = [{"name": "ANDURIL INDUSTRIES, INC.", "amount": 100},
                {"name": "ANDURIL LLC", "amount": 13807.5}]
        accepted, excluded = matched_rows(record(anduril, 2022, "J", rows), anduril, "fixed")
        self.assertEqual([row[5] for row in accepted], ["ANDURIL INDUSTRIES, INC."])
        self.assertEqual([row["recipient_name"] for row in excluded], ["ANDURIL LLC"])

    def test_boeing_config_includes_returned_branded_legal_variants(self):
        companies, _ = load_config(ROOT)
        boeing = next(company for company in companies if company["name"] == "Boeing")
        names = ["BOEING COMPANY, THE", "THE BOEING COMPANY (0674)",
                 "BOEING AEROSPACE OPERATIONS, I", "BOEING COMSATCOM SERVICES, INC.",
                 "BOEING INTELLIGENCE & ANALYTICS, INC.", "BOEING SATELLITE SYSTEMS, INC.",
                 "BOEING-SVS, INC"]
        rows = [{"name": name, "amount": 1} for name in names]
        rows.append({"name": "BOEING SIKORSKY AIRCRAFT SUPPORT, LLC", "amount": 900})
        accepted, excluded = matched_rows(record(boeing, 2026, "J", rows), boeing, "fixed")
        self.assertEqual([row[5] for row in accepted], names)
        self.assertEqual([row["recipient_name"] for row in excluded], ["BOEING SIKORSKY AIRCRAFT SUPPORT, LLC"])

    def test_explicit_subsidiaries_match_exactly_case_insensitively_and_ignore_allowed_suffixes(self):
        companies, _ = load_config(ROOT)
        lockheed = next(company for company in companies if company["name"] == "Lockheed Martin")
        names = ["SIKORSKY AIRCRAFT CORPORATION", "sikorsky support services inc.",
                 "LOCKHEED MISSILES & SPACE COMP, LLC", "Derco Aerospace Corp."]
        rows, excluded = matched_rows(record(lockheed, 2026, "J", [
            *({"name": name, "amount": 1} for name in names),
            {"name": "DERCO AEROSPACE SERVICES LLC", "amount": 5},
        ]), lockheed, "fixed")
        self.assertEqual([row[5] for row in rows], names)
        self.assertEqual([row["recipient_name"] for row in excluded], ["DERCO AEROSPACE SERVICES LLC"])

    def test_rtx_regex_expands_brands_without_including_divested_names_or_jvs(self):
        companies, _ = load_config(ROOT)
        rtx = next(company for company in companies if company["name"] == "RTX")
        names = ["RTX CORPORATION", "PRATT & WHITNEY CANADA CORP.",
                 "PRATT AND WHITNEY, INC.", "COLLINS AEROSPACE, INC.", "ROCKWELL COLLINS, INC.",
                 "RTX BBN TECHNOLOGIES"]
        rows, excluded = matched_rows(record(rtx, 2026, "J", [
            *({"name": name, "amount": 1} for name in names),
            {"name": "NIGHTWING GROUP LLC", "amount": 5},
            {"name": "EVERFOX LLC", "amount": 6},
            {"name": "COLLINS BOEING JOINT VENTURE", "amount": 7},
        ]), rtx, "fixed")
        self.assertEqual([row[5] for row in rows], names)
        self.assertEqual([row["recipient_name"] for row in excluded],
                         ["NIGHTWING GROUP LLC", "EVERFOX LLC", "COLLINS BOEING JOINT VENTURE"])

    def test_null_or_nonnumeric_amount_is_not_silently_zero(self):
        for amount in (None, "not money", True, float("nan"), float("inf")):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                matched_rows(record(ANDURIL, 2026, "J", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": amount}]), ANDURIL, "fixed")

    def test_recorded_entities_are_summed_without_name_deduplication(self):
        cached = {**record(ANDURIL, 2026, "J", []), "pages": FIXTURE["pages"]}
        rows, excluded = matched_rows(cached, ANDURIL, "fixed")
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(sum(row[-1] for row in rows), 640904704.08, places=2)
        self.assertEqual(excluded, [])

    def test_sql_grouping_and_shares_include_all_groups_and_negative_modifications(self):
        other = {**ANDURIL, "name": "No Fixed"}
        records = [record(ANDURIL, 2026, "J", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": 60}]),
                   record(ANDURIL, 2026, "A", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": -10}]),
                   record(ANDURIL, 2026, "U", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": 30}]),
                   record(ANDURIL, 2026, "Y", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": 15}]),
                   record(ANDURIL, 2026, "1", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": 5}]),
                   record(other, 2026, "U", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": 20}]),
                   record(ANDURIL, 2025, "J", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": 0}])]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "contracts.db"
            build_database(database, records, [ANDURIL, other], GROUPS, years=[2025, 2026], require_complete=False)
            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0], 7)
                grouped = dict(conn.execute("SELECT pricing_group, amount FROM by_company_year_group WHERE company='Anduril' AND fy=2026"))
                self.assertEqual(grouped, {"fixed": 50, "cost_plus": 30, "time_materials": 15, "other": 5})
                share = conn.execute("SELECT fixed_amount, total_amount, share_fixed FROM share_fixed WHERE company='Anduril' AND fy=2026").fetchone()
                self.assertEqual(share, (50, 100, 0.5))
                self.assertEqual(conn.execute("SELECT share_fixed FROM share_fixed WHERE company='No Fixed' AND fy=2026").fetchone()[0], 0)
                self.assertIsNone(conn.execute("SELECT share_fixed FROM share_fixed WHERE company='Anduril' AND fy=2025").fetchone()[0])
                self.assertIsNone(conn.execute("SELECT share_fixed FROM share_fixed WHERE company='No Fixed' AND fy=2025").fetchone()[0])
            build_database(database, records, [ANDURIL, other], GROUPS, years=[2025, 2026], require_complete=False)
            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0], 7)

    def test_negative_cost_plus_does_not_inflate_fixed_share(self):
        records = [record(ANDURIL, 2026, "J", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": 100}]),
                   record(ANDURIL, 2026, "R", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": -20}]),
                   record(ANDURIL, 2026, "Y", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": 10}]),
                   record(ANDURIL, 2025, "J", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": -10}]),
                   record(ANDURIL, 2025, "R", [{"name": "ANDURIL INDUSTRIES, INC.", "amount": 5}])]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "contracts.db"
            build_database(database, records, [ANDURIL], GROUPS, years=[2025, 2026], require_complete=False)
            with closing(sqlite3.connect(database)) as conn:
                fixed_amount, total_amount, positive_total, share = conn.execute(
                    "SELECT fixed_amount, total_amount, positive_group_total, share_fixed FROM share_fixed WHERE company='Anduril' AND fy=2026"
                ).fetchone()
                self.assertEqual((fixed_amount, total_amount), (100, 90))
                self.assertEqual(positive_total, 110)
                self.assertAlmostEqual(share, 100 / 110)
                self.assertLessEqual(share, 1)
                negative_fixed, fixed_group_net, zero_share = conn.execute(
                    "SELECT fixed_amount, fixed_group_net, share_fixed FROM share_fixed WHERE company='Anduril' AND fy=2025"
                ).fetchone()
                self.assertEqual((negative_fixed, fixed_group_net), (0, -10))
                self.assertEqual(zero_share, 0)

    def test_database_deduplicates_recipients_returned_by_multiple_search_terms(self):
        company = {**ANDURIL, "search_terms": ["Anduril", "Anduril Technologies"]}
        shared = {"name": "ANDURIL INDUSTRIES, INC.", "amount": 100,
                  "recipient_id": "same-recipient", "uei": "ABC", "code": "123"}
        distinct = {"name": "ANDURIL INDUSTRIES, INC.", "amount": 25,
                    "recipient_id": "other-recipient", "uei": "DEF", "code": "456"}
        records = [record(company, 2026, "J", [shared], "Anduril"),
                   record(company, 2026, "J", [shared, distinct], "Anduril Technologies")]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "contracts.db"
            build_database(database, records, [company], GROUPS, years=[2026], require_complete=False)
            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT SUM(amount) FROM obligations").fetchone()[0], 125)

    def test_incomplete_cache_does_not_replace_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "contracts.db"
            database.write_bytes(b"existing database")
            with self.assertRaises(ValueError):
                build_database(database, [], [ANDURIL], GROUPS, years=[2026])
            self.assertEqual(database.read_bytes(), b"existing database")


    def test_merge_keeps_most_complete_result_per_recipient(self):
        parent = {"search_term": "General Dynamics", "pages": [{"results": [
            {"name": "ELECTRIC BOAT CORPORATION", "recipient_id": "eb", "amount": 0.37}]}]}
        direct = {"search_term": "Electric Boat", "pages": [{"results": [
            {"name": "ELECTRIC BOAT CORPORATION", "recipient_id": "eb", "amount": 1.87}]}]}
        for records in ([parent, direct], [direct, parent]):
            merged = merge_search_results(records)
            self.assertEqual([row["amount"] for row in merged], [1.87])


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.companies, _ = load_config(ROOT)
        companies = {company["name"]: company for company in self.companies}
        samples = [
            ("Anduril", 2019, "J", [("ANDURIL INDUSTRIES, INC.", 1000), ("OLD ANDURIL HIT", -1500)]),
            ("Anduril", 2020, "J", [("ANDURIL INDUSTRIES, INC.", 20)]),
            ("Anduril", 2025, "J", [("ANDURIL INDUSTRIES, INC.", -10.25)]),
            ("Anduril", 2025, "R", [("ANDURIL INDUSTRIES, INC.", 5.15)]),
            ("Anduril", 2026, "J", [("ANDURIL INDUSTRIES, INC.", 120.13), ("UNRELATED & <ANDURIL>", 100)]),
            ("Anduril", 2026, "A", [("ANDURIL INDUSTRIES, INC.", -20.13), ("UNRELATED & <ANDURIL>", 100)]),
            ("Anduril", 2026, "R", [("ANDURIL INDUSTRIES, INC.", -20.11)]),
            ("Anduril", 2026, "Y", [("ANDURIL INDUSTRIES, INC.", 10.10)]),
            ("Anduril", 2027, "J", [("ANDURIL INDUSTRIES, INC.", 9000), ("UNRELATED & <ANDURIL>", 300)]),
            ("Palantir", 2026, "J", [("PALANTIR TECHNOLOGIES INC", -12.34), ("UNRELATED & <ANDURIL>", 100)]),
            ("Palantir", 2026, "R", [("PALANTIR TECHNOLOGIES INC", 2.34)]),
            ("Shield AI", 2026, "R", [("SHIELD AI INC", 20.01)]),
            ("Boeing", 2026, "J", [("THE BOEING COMPANY", 30)]),
            ("Boeing", 2026, "R", [("THE BOEING COMPANY", 10)]),
            ("Lockheed Martin", 2026, "J", [("LOCKHEED MARTIN CORPORATION", 100)]),
        ]
        records = [record(companies[company], fy, code, [{"name": name, "amount": amount} for name, amount in rows])
                   for company, fy, code, rows in samples]
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "contracts.db"
        build_database(self.database, records, self.companies, GROUPS,
                       years=[2019, 2020, 2025, 2026, 2027], require_complete=False)

    def analysis(self):
        with patch("make_dashboard.latest_full_fy", return_value=2026):
            return load_analysis(self.database)

    def test_reconciliation_matches_latest_shares_and_balances_net_totals(self):
        with closing(sqlite3.connect(self.database)) as conn:
            conn.row_factory = sqlite3.Row
            for fy in (2025, 2026):
                params = {"latest_fy": fy}
                rows = [dict(row) for row in conn.execute((ROOT / "queries/reconciliation.sql").read_text(), params)]
                latest = {row["company"]: dict(row) for row in conn.execute((ROOT / "queries/latest_fixed_share.sql").read_text(), params)}
                self.assertEqual(len(rows), len(self.companies))
                for row in rows:
                    with self.subTest(fy=fy, company=row["company"]):
                        self.assertEqual(row["share_fixed"], latest[row["company"]]["share_fixed"])
                        self.assertLessEqual(row["negative_adjustment"], 0)
                        self.assertAlmostEqual(row["positive_group_total"] + row["negative_adjustment"], row["total_net"], delta=0.01)
                        total = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM obligations WHERE company=? AND fy=?",
                                             (row["company"], fy)).fetchone()[0]
                        self.assertAlmostEqual(row["total_net"], total, delta=0.01)
                by_company = {row["company"]: row for row in rows}
                self.assertIsNone(by_company["RTX"]["share_fixed"])
                if fy == 2026:
                    anduril = by_company["Anduril"]
                    self.assertEqual((anduril["total_net"], anduril["positive_group_total"], anduril["negative_adjustment"]),
                                     (89.99, 110.10, -20.11))
                    self.assertEqual((anduril["fixed_group_net"], anduril["fixed_numerator"]), (100, 100))
                    self.assertEqual((by_company["Palantir"]["fixed_group_net"], by_company["Palantir"]["fixed_numerator"]), (-12.34, 0))
                    self.assertEqual(by_company["Shield AI"]["share_fixed"], 0)

    def test_exclusion_sensitivity_keeps_signs_all_excluded_years_and_overlapping_names(self):
        rows = {row["company"]: row for row in self.analysis()["exclusion_sensitivity"]}
        self.assertEqual(len(rows), len(self.companies))
        anduril = rows["Anduril"]
        self.assertAlmostEqual(anduril["included_total"], 104.89)
        self.assertEqual(anduril["excluded_total"], -1000)
        self.assertAlmostEqual(anduril["excluded_share"], -1000 / 104.89)
        self.assertEqual((anduril["largest_excluded"], anduril["largest_excluded_amount"]), ("OLD ANDURIL HIT", -1500))
        self.assertEqual(rows["Palantir"]["excluded_total"], 100)
        self.assertEqual(rows["Palantir"]["excluded_share"], -10)
        self.assertIsNone(rows["RTX"]["excluded_share"])
        self.assertIsNone(rows["RTX"]["largest_excluded"])

    def test_history_uses_eight_ordered_panels_with_shared_percent_scale(self):
        figures = make_figures(self.analysis(), self.companies)
        self.assertEqual(len(figures), 4)
        history = figures[2]
        expected = ["Anduril", "Palantir", "Shield AI", "Lockheed Martin", "Boeing", "RTX", "Northrop Grumman", "General Dynamics"]
        self.assertEqual([title.text for title in history.layout.annotations], expected)
        self.assertFalse(history.layout.showlegend)
        self.assertEqual(history.layout.height, 560)
        for index, trace in enumerate(history.data):
            with self.subTest(company=trace.name):
                self.assertEqual(trace.name, expected[index])
                self.assertEqual(trace.xaxis, "x" if index == 0 else f"x{index + 1}")
                self.assertEqual(trace.line.color.replace(" ", ""), "rgb(94,109,35)" if index < 3 else "rgb(68,89,119)")
                self.assertEqual((trace.line.width, trace.marker.size), (2.5, 6))
                self.assertEqual(trace.hovertemplate, f"{trace.name} FY%{{x}}: %{{y:.1f}}%<extra></extra>")
                self.assertTrue(all(2020 <= fy <= 2026 for fy in trace.x))
                xaxis = history.layout["xaxis" if index == 0 else f"xaxis{index + 1}"]
                yaxis = history.layout["yaxis" if index == 0 else f"yaxis{index + 1}"]
                self.assertEqual(list(xaxis.tickvals), list(range(2020, 2027)))
                self.assertEqual(list(xaxis.ticktext), ["FY20", "", "", "", "", "", "FY26"])
                self.assertEqual(list(yaxis.range), [0, 100])
                self.assertEqual(yaxis.ticksuffix, "%")
                self.assertTrue(yaxis.matches or index == 4)

    def test_reconciliation_is_visible_with_two_numeric_tables_and_computed_takeaway(self):
        data = self.analysis()
        with tempfile.TemporaryDirectory() as directory, patch("plotly.graph_objects.Figure.write_image"):
            render_dashboard(data, make_figures(data, self.companies), compute_takeaways(data), Path(directory))
            html = (Path(directory) / "index.html").read_text()
        self.assertIn('<a href="#reconciliation">Reconciliation</a>', html)
        self.assertLess(html.index("<section id='anduril-pricing-mix'"), html.index("<section id='reconciliation'"))
        section = html.split("<section id='reconciliation'", 1)[1].split("</section>", 1)[0]
        self.assertIn("How the numbers reconcile", section)
        self.assertEqual(section.count("<table>"), 2)
        self.assertEqual(section.count("class='table-scroll'"), 2)
        self.assertNotIn("<details>", section)
        self.assertIn("class='numeric'", section)
        self.assertIn("$32.45", section)
        self.assertIn("OLD ANDURIL HIT (Anduril, -$1.5K)", section)
        self.assertIn("UNRELATED &amp; &lt;ANDURIL&gt;", section)
        self.assertIn("Searches can overlap", section)


class AnalysisTests(unittest.TestCase):
    def test_latest_full_fiscal_year_boundary(self):
        self.assertEqual(latest_full_fy("2026-09-29"), 2025)
        self.assertEqual(latest_full_fy("2026-10-01"), 2026)
        self.assertEqual(latest_full_fy("2027-06-01"), 2026)

    def test_four_takeaways_are_computed_and_handle_zero_baselines(self):
        data = {
            "latest_fy": 2026,
            "cohort_totals": [{"fy": 2026, "grp": "new_entrant", "amount": 200},
                              {"fy": 2026, "grp": "prime", "amount": 800}],
            "latest_shares": [{"company": "Anduril", "grp": "new_entrant", "fy": 2026, "share_fixed": 0.75},
                              {"company": "Boeing", "grp": "prime", "fy": 2026, "share_fixed": 0.25}],
            "share_history": [{"company": "Anduril", "fy": 2020, "share_fixed": 0.5},
                              {"company": "Anduril", "fy": 2026, "share_fixed": 0.75},
                              {"company": "Boeing", "fy": 2020, "share_fixed": None},
                              {"company": "Boeing", "fy": 2026, "share_fixed": 0.25}],
            "anduril_mix": [{"fy": 2026, "pricing_group": "fixed", "amount": 150},
                            {"fy": 2026, "pricing_group": "cost_plus", "amount": 50}],
        }
        takeaways = compute_takeaways(data)
        self.assertEqual(len(takeaways), 4)
        self.assertIn("20.0%", takeaways[0])
        self.assertIn("Anduril", takeaways[1])
        self.assertIn("75.0%", takeaways[1])
        self.assertIn("positive pricing-group obligations", takeaways[1])
        self.assertIn("25.0 percentage points", takeaways[2])
        self.assertIn("75.0%", takeaways[3])
        data["latest_shares"][0]["share_fixed"] = 0.6
        self.assertIn("60.0%", compute_takeaways(data)[1])
        self.assertEqual(len(compute_takeaways({"latest_fy": 2026, "cohort_totals": [], "latest_shares": [], "share_history": [], "anduril_mix": []})), 4)

    def test_latest_share_takeaway_names_all_tied_leaders(self):
        data = {"latest_fy": 2026, "cohort_totals": [], "share_history": [], "anduril_mix": [],
                "latest_shares": [{"company": "Palantir", "share_fixed": 1},
                                  {"company": "Shield AI", "share_fixed": 1},
                                  {"company": "Boeing", "share_fixed": 0.5}]}
        takeaway = compute_takeaways(data)[1]
        self.assertIn("Palantir and Shield AI", takeaway)
        self.assertIn("tie", takeaway)
        self.assertIn("100.0%", takeaway)


if __name__ == "__main__":
    unittest.main()
