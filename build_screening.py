"""Build masld_screening.json, a companion Synthea module to masld.json.

Adults 18+ with T2DM, obesity (latest BMI >= 30 or an obesity condition) or essential
hypertension get a check-up with AST, ALT and platelets every 11 months, whether or not
they have MASLD:
  - without MASLD: normal ranges (AST 15-30 U/L, ALT 15-35 U/L, platelets 180-350 10^3/uL)
  - with MASLD: the band x age-group ranges from lab_ranges.py, chosen by the person attribute
    'masld_fibrosis_band' that masld.json sets (modules share person attributes).

Why a separate module: a Synthea module is one state machine with no parallel branches, and
this loop has to run alongside masld.json's own diagnosis and follow-up timeline.

Why 11 months: Synthea moves in 7-day steps, so the real gap is 11 months to 11 months + 1 week.
An exact 12-month delay plus that slack could leave a 12-month window with no labs, and the
FIB-4 query looks at exactly a 12-month window.

Usage:
    python3 build_screening.py masld_screening.json
"""
import json
import sys

from lab_ranges import RANGES

# Text stored in the module's Initial state.
MODULE_DESCRIPTION = (
    "Yearly FIB-4 lab loop for adults 18+ with T2DM, obesity (latest BMI >= 30 or an obesity\n"
    "condition) or essential hypertension, whether or not they have MASLD. Each pass is a\n"
    "check-up encounter with AST, ALT and platelets:\n"
    "  - no MASLD  -> normal ranges (AST 15-30 U/L, ALT 15-35 U/L, platelets 180-350 10*3/uL)\n"
    "  - MASLD     -> the same band x age-group ranges masld.json uses (lab_ranges.py), read from\n"
    "                 the shared person attribute 'masld_fibrosis_band' that masld.json sets.\n"
    "It is a separate module because a Synthea module is one state machine with no parallel\n"
    "branches; this loop has to run alongside masld.json's own diagnosis/follow-up timeline."
)

NORMAL_RANGES = {"ast": (15, 30), "alt": (15, 35), "plt": (180, 350)}
MONTHS_BETWEEN_CHECKUPS = 11


def snomed(code, display):
    return {"system": "SNOMED-CT", "code": code, "display": display}


LAB_CODES = {  # lab -> (LOINC code, unit)
    "AST": ({"system": "LOINC", "code": "1920-8",
             "display": "Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma"}, "U/L"),
    "ALT": ({"system": "LOINC", "code": "1742-6",
             "display": "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma"}, "U/L"),
    "PLT": ({"system": "LOINC", "code": "777-3",
             "display": "Platelets [#/volume] in Blood by Automated count"}, "10*3/uL"),
}

# Age conditions for routing labs. The 65+ group is the fall-through, so it has no condition.
AGE_GROUP_CONDITIONS = {
    "under50": {"condition_type": "Age", "operator": "<", "quantity": 50, "unit": "years"},
    "50to64": {"condition_type": "Age", "operator": "<", "quantity": 65, "unit": "years"},
    "65plus": None,
}

AT_RISK = {"condition_type": "Or", "conditions": [
    {"condition_type": "Active Condition",
     "codes": [snomed("44054006", "Diabetes mellitus type 2 (disorder)")]},
    {"condition_type": "Observation",
     "codes": [{"system": "LOINC", "code": "39156-5", "display": "Body mass index (BMI) [Ratio]"}],
     "operator": ">=", "value": 30},
    {"condition_type": "Active Condition", "codes": [
        snomed("162864005", "Body mass index 30+ - obesity (finding)"),
        snomed("408512008", "Body mass index 40+ - severely obese (finding)"),
        snomed("414916001", "Obesity (disorder)")]},
    {"condition_type": "Active Condition",
     "codes": [snomed("59621000", "Essential hypertension (disorder)")]},
]}


def loop_states():
    """The check-up loop, without the lab states."""
    return {
        "Initial": {"type": "Initial", "remarks": [MODULE_DESCRIPTION], "direct_transition": "Wait_Until_18"},
        "Wait_Until_18": {
            "type": "Guard",
            "allow": {"condition_type": "Age", "operator": ">=", "quantity": 18, "unit": "years"},
            "direct_transition": "Wait_Until_At_Risk",
        },
        "Wait_Until_At_Risk": {
            "type": "Guard",
            "remarks": ["T2DM, obesity (BMI >= 30 or obesity condition) or hypertension."],
            "allow": AT_RISK,
            "direct_transition": "Screening_Encounter",
        },
        "Screening_Encounter": {
            "type": "Encounter",
            "encounter_class": "ambulatory",
            "codes": [snomed("185349003", "Encounter for check up (procedure)")],
            "direct_transition": "Screening_Labs",
        },
        "End_Screening_Encounter": {"type": "EncounterEnd", "direct_transition": "Screening_Delay"},
        "Screening_Delay": {
            "type": "Delay",
            "exact": {"quantity": MONTHS_BETWEEN_CHECKUPS, "unit": "months"},
            "direct_transition": "Wait_Until_At_Risk",
        },
    }


def add_lab_chain(states, prefix, ranges):
    """Add AST -> ALT -> platelets observation states; return the first state's name."""
    names = [f"{prefix}_{lab}" for lab in LAB_CODES]
    for i, (lab, (code, unit)) in enumerate(LAB_CODES.items()):
        low, high = ranges[lab.lower()]
        states[names[i]] = {
            "type": "Observation",
            "category": "laboratory",
            "unit": unit,
            "codes": [code],
            "range": {"low": low, "high": high},
            "direct_transition": names[i + 1] if i + 1 < len(names) else "End_Screening_Encounter",
        }
    return names[0]


def add_lab_states(states):
    """MASLD band x age-group chains, plus a normal-range chain for people without MASLD."""
    band_routes = []
    for band, age_groups in RANGES.items():
        router = f"Screening_Labs_{band}"
        states[router] = {"type": "Simple", "conditional_transition": []}
        for age_group, ranges in age_groups.items():
            route = {"transition": add_lab_chain(states, f"Screening_{band}_{age_group}", ranges)}
            if AGE_GROUP_CONDITIONS[age_group]:
                route["condition"] = AGE_GROUP_CONDITIONS[age_group]
            states[router]["conditional_transition"].append(route)
        band_routes.append({
            "transition": router,
            "condition": {"condition_type": "Attribute", "attribute": "masld_fibrosis_band",
                          "operator": "==", "value": band},
        })
    band_routes.append({"transition": add_lab_chain(states, "Screening_normal", NORMAL_RANGES)})
    states["Screening_Labs"] = {
        "type": "Simple",
        "remarks": ["MASLD band ranges if masld.json set a band, else normal ranges."],
        "conditional_transition": band_routes,
    }


def check_transitions(states):
    """Every transition goes to an existing state, and every state except Initial is reachable."""
    targets = set()
    for state in states.values():
        if "direct_transition" in state:
            targets.add(state["direct_transition"])
        for transition in state.get("conditional_transition", []):
            targets.add(transition["transition"])
    assert targets <= set(states), targets - set(states)
    unreachable = set(states) - targets - {"Initial"}
    assert not unreachable, unreachable


def main():
    destination = sys.argv[1]
    states = loop_states()
    add_lab_states(states)
    check_transitions(states)
    # The loop has no Terminal state: Synthea stops it at death or the end of the simulation.
    module = {
        "name": "MASLD screening labs",
        "remarks": ["Companion to masld.json (shares the masld_fibrosis_band attribute). Not clinically validated."],
        "gmf_version": 2,
        "states": states,
    }
    with open(destination, "w", encoding="utf-8") as f:
        json.dump(module, f, indent=2)
    print(f"wrote {destination}: {len(states)} states")


if __name__ == "__main__":
    main()
