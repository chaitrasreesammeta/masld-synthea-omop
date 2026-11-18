"""AST / ALT / platelet ranges for the MASLD modules, and a check that they give the right FIB-4 band.

FIB-4 = age * AST / (platelets * sqrt(ALT)). Each fibrosis band gets lab ranges per age group,
chosen so the FIB-4 score usually lands in the matching band. Running this file simulates
random patients in each band and age group and prints how often FIB-4 lands where intended.

Used by build_masld.py and build_screening.py.

Usage:
    python3 lab_ranges.py
"""
import math
import random

# Age group -> (youngest, oldest) age used when simulating. MASLD starts at 35;
# patients are at most about 81 at a visit.
AGE_GROUPS = {
    "under50": (35, 50),
    "50to64": (50, 65),
    "65plus": (65, 81),
}

# RANGES[band][age_group] = {"ast": (low, high), "alt": (low, high), "plt": (low, high)}
# AST and ALT in U/L, platelets in 10^3/uL. Cirrhosis should score High, like the high band.
RANGES = {
    "low": {
        "under50": {"ast": (18, 28), "alt": (25, 45), "plt": (220, 340)},
        "50to64":  {"ast": (18, 26), "alt": (30, 45), "plt": (240, 350)},
        "65plus":  {"ast": (18, 26), "alt": (25, 40), "plt": (220, 330)},
    },
    "indeterminate": {
        "under50": {"ast": (46, 52), "alt": (38, 44), "plt": (160, 172)},
        "50to64":  {"ast": (40, 46), "alt": (40, 46), "plt": (175, 190)},
        "65plus":  {"ast": (40, 43), "alt": (38, 42), "plt": (192, 200)},
    },
    "high": {
        "under50": {"ast": (85, 110), "alt": (45, 60), "plt": (90, 115)},
        "50to64":  {"ast": (65, 90), "alt": (45, 65), "plt": (105, 135)},
        "65plus":  {"ast": (55, 85), "alt": (45, 65), "plt": (115, 150)},
    },
    "cirrhosis": {
        "under50": {"ast": (95, 130), "alt": (35, 55), "plt": (65, 95)},
        "50to64":  {"ast": (75, 110), "alt": (35, 55), "plt": (70, 100)},
        "65plus":  {"ast": (60, 100), "alt": (35, 55), "plt": (75, 110)},
    },
}

# The FIB-4 category each band should produce.
EXPECTED_CATEGORY = {"low": "Low", "indeterminate": "Indeterminate", "high": "High", "cirrhosis": "High"}

SIMULATED_PATIENTS = 20_000
RANDOM_SEED = 1


def fib4(age, ast, alt, platelets):
    """FIB-4 score."""
    return age * ast / (platelets * math.sqrt(alt))


def fib4_category(score, age):
    """Low / Indeterminate / High. The Low cut-off is 1.3, or 2.0 from age 65."""
    low_cutoff = 2.0 if age >= 65 else 1.3
    if score < low_cutoff:
        return "Low"
    if score <= 2.67:
        return "Indeterminate"
    return "High"


def share_in_expected_category(band, age_group, rng):
    """Fraction of simulated patients whose FIB-4 lands in the band's expected category."""
    youngest, oldest = AGE_GROUPS[age_group]
    labs = RANGES[band][age_group]
    hits = 0
    for _ in range(SIMULATED_PATIENTS):
        age = rng.uniform(youngest, oldest)
        ast, alt, platelets = (rng.uniform(*labs[lab]) for lab in ("ast", "alt", "plt"))
        hits += fib4_category(fib4(age, ast, alt, platelets), age) == EXPECTED_CATEGORY[band]
    return hits / SIMULATED_PATIENTS


def main():
    rng = random.Random(RANDOM_SEED)
    for band, age_groups in RANGES.items():
        for age_group in age_groups:
            share = share_in_expected_category(band, age_group, rng)
            print(f"{band:14s} {age_group:8s} {100 * share:5.1f}% in {EXPECTED_CATEGORY[band]}")


if __name__ == "__main__":
    main()
