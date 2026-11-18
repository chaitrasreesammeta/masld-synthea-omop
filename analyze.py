"""Quick MASLD report from Synthea CSV output.

Prints:
  - how many patients have NAFLD, NASH, hepatic fibrosis and cirrhosis
  - MASLD rate in patients with vs without T2DM, obesity or hypertension (ever recorded)
  - FIB-4 band at every MASLD encounter, and at each patient's first MASLD encounter
  - first-encounter FIB-4 band against the most advanced stage ever recorded

Usage:
    python3 analyze.py out/csv
"""
import collections
import csv
import os
import sys
from datetime import date

from lab_ranges import fib4, fib4_category

NAFLD, NASH = "197315008", "442685003"   # masld.json codes (masld_v1.json used 197321007)
FIBROSIS, CIRRHOSIS = "62484002", "19943007"
MASLD_CODES = {NAFLD, NASH}

RISK_FACTORS = {
    "T2DM (44054006)": {"44054006"},
    "Obesity (162864005 BMI30+ / 408512008 BMI40+)": {"162864005", "408512008"},
    "Hypertension (59621000)": {"59621000"},
}
LAB_CODES = {"1920-8": "ast", "1742-6": "alt", "777-3": "plt"}
CATEGORIES = ["Low", "Indeterminate", "High"]
STAGES = ["Steatosis only", "Fibrosis", "Cirrhosis"]


def read_csv(folder, name):
    with open(os.path.join(folder, name), encoding="utf-8") as f:
        yield from csv.DictReader(f)


def age_on(birthdate, when):
    """Age in whole years on a date (both ISO strings)."""
    born, on = date.fromisoformat(birthdate[:10]), date.fromisoformat(when[:10])
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def print_condition_counts(patients_with):
    for code, name in [(NAFLD, "NAFLD"), (NASH, "NASH"), (FIBROSIS, "Hepatic fibrosis"), (CIRRHOSIS, "Cirrhosis of liver")]:
        print(f"  {name} [{code}]: {len(patients_with[code])}")


def print_rates_by_risk_factor(all_patients, masld, patients_with):
    def rate(group):
        if not group:
            return "n/a"
        with_masld = len(group & masld)
        return f"{with_masld}/{len(group)} = {100 * with_masld / len(group):.1f}%"

    print("\nMASLD rate by risk factor (ever recorded):")
    any_factor = set()
    for name, codes in RISK_FACTORS.items():
        has_factor = set().union(*(patients_with[code] for code in codes))
        any_factor |= has_factor
        print(f"  {name}: with {rate(has_factor)}   without {rate(all_patients - has_factor)}")
    print(f"  Any of the three: with {rate(any_factor)}   without {rate(all_patients - any_factor)}")


def main():
    folder = sys.argv[1]
    patients = {row["Id"]: row for row in read_csv(folder, "patients.csv")}
    patients_with = collections.defaultdict(set)  # condition code -> patient ids
    for row in read_csv(folder, "conditions.csv"):
        patients_with[row["CODE"]].add(row["PATIENT"])
    masld = patients_with[NAFLD] | patients_with[NASH]

    print(f"Patients in patients.csv: {len(patients)}")
    print_condition_counts(patients_with)
    print(f"  Any MASLD (NAFLD or NASH): {len(masld)}")
    print_rates_by_risk_factor(set(patients), masld, patients_with)

    # FIB-4 at encounters whose reason is NAFLD or NASH.
    encounters = {row["Id"]: row for row in read_csv(folder, "encounters.csv") if row["REASONCODE"] in MASLD_CODES}
    labs = collections.defaultdict(dict)  # encounter id -> {"ast": ..., "alt": ..., "plt": ...}
    for row in read_csv(folder, "observations.csv"):
        if row["ENCOUNTER"] in encounters and row["CODE"] in LAB_CODES:
            labs[row["ENCOUNTER"]][LAB_CODES[row["CODE"]]] = float(row["VALUE"])

    per_encounter = collections.Counter()
    first_category = {}  # patient -> FIB-4 category at their first MASLD encounter with labs
    missing = 0
    for encounter_id, encounter in sorted(encounters.items(), key=lambda item: item[1]["START"]):
        lab = labs[encounter_id]
        if len(lab) < 3:
            missing += 1
            continue
        age = age_on(patients[encounter["PATIENT"]]["BIRTHDATE"], encounter["START"])
        category = fib4_category(fib4(age, lab["ast"], lab["alt"], lab["plt"]), age)
        per_encounter[category] += 1
        first_category.setdefault(encounter["PATIENT"], category)

    print(f"\nMASLD encounters (REASONCODE NAFLD/NASH): {len(encounters)}; "
          f"with all 3 labs: {sum(per_encounter.values())}; missing labs: {missing}")
    print("FIB-4 bands, all MASLD encounters:", {c: per_encounter[c] for c in CATEGORIES})
    per_patient = collections.Counter(first_category.values())
    print(f"FIB-4 bands, per patient at first MASLD encounter (n={len(first_category)}):",
          {c: per_patient[c] for c in CATEGORIES})

    def stage(patient):
        if patient in patients_with[CIRRHOSIS]:
            return "Cirrhosis"
        if patient in patients_with[FIBROSIS]:
            return "Fibrosis"
        return "Steatosis only"

    crosstab = collections.Counter((stage(p), category) for p, category in first_category.items())
    print("Cross-tab (ever-recorded stage x first-encounter FIB-4 band):")
    for s in STAGES:
        print(f"  {s:15s}", {c: crosstab[(s, c)] for c in CATEGORIES})


if __name__ == "__main__":
    main()
