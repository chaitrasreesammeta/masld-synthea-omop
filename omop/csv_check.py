"""Recompute the MASLD at-risk query results straight from out/csv (no OMOP tables, no vocabulary).

Code lists are written out by hand. Where a query uses a parent concept, the list holds the codes
that are its OMOP descendants *and occur in this data* (checked in omop.duckdb). So a match with
the DuckDB results means the ETL and the SQL reproduce the intended logic.

Variants:
  v1               masld_at_risk_queries: obesity = Obesity 414916001 condition only
  v2               masld_at_risk_queries_v2: also latest BMI >= 30 in 2 years, or an 'Obese' concept
  v2_no_hierarchy  v2, with hypertension = 59621000 and hyperlipidemia = 55822004 only
                   (no pre-eclampsia / hypertriglyceridemia, which the OMOP hierarchy adds)
  v3               masld_at_risk_queries_v3: same code lists as v2_no_hierarchy

Usage:
    python3 omop/csv_check.py [--variant v1|v2|v2_no_hierarchy|v3]
"""
import collections
import csv
import math
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_DIR = os.path.join(ROOT, "out", "csv")
INDEX_DATE = date(2024, 1, 1)

CONDITION_CODES = {
    "t2dm": {"44054006"},
    "masld": {"197315008", "442685003"},           # NAFLD, NASH (no other descendants in the data)
    "obesity": {"414916001"},                       # Obesity (no descendants in conditions in the data)
    "htn": {"38341003", "59621000", "398254007"},   # Hypertensive disorder, Essential HTN, Pre-eclampsia
    "hld": {"55822004", "302870006"},               # Hyperlipidemia, Hypertriglyceridemia
}
WITHOUT_HIERARCHY = {**CONDITION_CODES, "htn": {"38341003", "59621000"}, "hld": {"55822004"}}
OBESE_CODES = {"162864005", "408512008"}  # descendants of 414915002 'Obese' in the data (OMOP: observation table)
BMI_CODE = "39156-5"

VARIANTS = {  # name -> (condition code lists, use v2 obesity rules)
    "v1": (CONDITION_CODES, False),
    "v2": (CONDITION_CODES, True),
    "v2_no_hierarchy": (WITHOUT_HIERARCHY, True),
    "v3": (WITHOUT_HIERARCHY, True),
}
LAB_CODES = {"1920-8": "ast", "30239-8": "ast", "1742-6": "alt", "76625-3": "alt", "777-3": "plt", "26515-7": "plt"}
LAB_YEARS, BMI_YEARS = 1, 2  # look-back windows


def read_csv(name):
    with open(os.path.join(CSV_DIR, name), encoding="utf-8") as f:
        yield from csv.DictReader(f)


def to_date(text):
    return date.fromisoformat(text[:10])


def years_before_index(years):
    return INDEX_DATE.replace(year=INDEX_DATE.year - years)  # index date is 1 Jan, so no leap-day issue


def active_adults():
    """Age >= 18 (by birth year), a visit in the 3 years up to the index date, alive on it."""
    recent = {row["PATIENT"] for row in read_csv("encounters.csv")
              if years_before_index(3) <= to_date(row["START"]) <= INDEX_DATE}
    cohort = {}
    for row in read_csv("patients.csv"):
        age = INDEX_DATE.year - int(row["BIRTHDATE"][:4])
        died = row["DEATHDATE"] and to_date(row["DEATHDATE"]) <= INDEX_DATE
        if age >= 18 and row["Id"] in recent and not died:
            cohort[row["Id"]] = {"age": age, "sex": {"M": "Male", "F": "Female"}.get(row["GENDER"], "Other/Unknown")}
    return cohort


def condition_groups(code_lists, v2_obesity):
    """Patient -> set of groups (t2dm, masld, obesity, htn, hld) recorded on or before the index date."""
    groups = collections.defaultdict(set)
    for row in read_csv("conditions.csv"):
        if to_date(row["START"]) > INDEX_DATE:
            continue
        for group, codes in code_lists.items():
            if row["CODE"] in codes:
                groups[row["PATIENT"]].add(group)
        if v2_obesity and row["CODE"] in OBESE_CODES:
            groups[row["PATIENT"]].add("obesity")
    return groups


def latest_values(cohort, include_bmi):
    """(patient, lab) -> newest value inside the look-back window. Labs: ast, alt, plt (+ bmi)."""
    latest = {}  # (patient, lab) -> (timestamp, value)
    for row in read_csv("observations.csv"):
        if row["PATIENT"] not in cohort or row["TYPE"] != "numeric":
            continue
        lab = LAB_CODES.get(row["CODE"]) or ("bmi" if include_bmi and row["CODE"] == BMI_CODE else None)
        if not lab:
            continue
        value = float(row["VALUE"])
        window_start = years_before_index(BMI_YEARS if lab == "bmi" else LAB_YEARS)
        if not window_start <= to_date(row["DATE"]) <= INDEX_DATE:
            continue
        if value <= 0 or (lab == "plt" and value > 2000):
            continue
        key = (row["PATIENT"], lab)
        if key not in latest or row["DATE"] > latest[key][0]:
            latest[key] = (row["DATE"], value)
    return {key: value for key, (_, value) in latest.items()}


def fib4_category(age, ast, alt, platelets):
    """Same labels as the SQL: '0 Missing labs', '1 Low', '2 Indeterminate', '3 High'."""
    if None in (ast, alt, platelets):
        return "0 Missing labs"
    score = age * ast / (platelets * math.sqrt(alt))
    if score > 2.67:
        return "3 High"
    if score < (2.0 if age >= 65 else 1.3):
        return "1 Low"
    return "2 Indeterminate"


def age_group(age):
    return "18-39" if age < 40 else "40-64" if age < 65 else "65+"


def run(variant):
    code_lists, v2_obesity = VARIANTS[variant]
    cohort = active_adults()
    groups = condition_groups(code_lists, v2_obesity)
    latest = latest_values(cohort, include_bmi=v2_obesity)

    for patient, person in cohort.items():
        if v2_obesity and latest.get((patient, "bmi"), 0) >= 30:
            groups[patient].add("obesity")
        g = groups[patient]
        person["at_risk"] = int("t2dm" in g or "masld" in g or ("obesity" in g and ("htn" in g or "hld" in g)))
        if person["at_risk"]:
            person["fib4"] = fib4_category(person["age"], *(latest.get((patient, lab)) for lab in ("ast", "alt", "plt")))

    at_risk = sum(p["at_risk"] for p in cohort.values())
    q1 = (len(cohort), at_risk, round(100 * at_risk / len(cohort), 1))
    q2 = collections.Counter(p["fib4"] for p in cohort.values() if p["at_risk"])
    # q3 row: [active adults, at risk, missing, low, indeterminate, high]
    q3 = collections.defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for p in cohort.values():
        row = q3[(p["sex"], age_group(p["age"]))]
        row[0] += 1
        row[1] += p["at_risk"]
        if p["at_risk"]:
            row[2 + int(p["fib4"][0])] += 1
    return q1, q2, q3, cohort


def main():
    variant = sys.argv[sys.argv.index("--variant") + 1] if "--variant" in sys.argv else "v1"
    q1, q2, q3, _ = run(variant)
    print(f"[{variant}] Q1 active_adults={q1[0]} at_risk={q1[1]} pct={q1[2]}")
    print(f"[{variant}] Q2", {k: q2[k] for k in sorted(q2)})
    for key in sorted(q3):
        print(f"[{variant}] Q3", key, "active,at_risk,missing,low,indet,high =", q3[key])


if __name__ == "__main__":
    main()
