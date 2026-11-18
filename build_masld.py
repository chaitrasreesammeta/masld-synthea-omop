"""Build masld.json from masld_v1.json.

Changes applied to the v1 module, in order:
  1. MASLD can start from age 35 (was 25).
  2. The diagnosis code is NAFLD 197315008 (v1 used 197321007 Steatosis of liver).
  3. The fibrosis band is assigned before the diagnosis and decides the code:
     low / indeterminate -> NAFLD 197315008, high / cirrhosis -> NASH 442685003.
     Progressing to the high band ends NAFLD and starts NASH. Encounter, procedure and
     care-plan reasons point to the 'masld' attribute, so they cite whichever one is current.
  4. The chance of moving up a band at each follow-up is 2% (was 5%).
  5. AST / ALT / platelet ranges depend on band and age group (<50, 50-64, 65+), taken from
     lab_ranges.py, so FIB-4 lands in the intended band.

Usage:
    python3 build_masld.py masld_v1.json masld.json
"""
import copy
import json
import sys

from lab_ranges import RANGES

NAFLD_CODE = {"system": "SNOMED-CT", "code": "197315008", "display": "Non-alcoholic fatty liver disease (disorder)"}
NASH_CODE = {"system": "SNOMED-CT", "code": "442685003", "display": "Non-alcoholic steatohepatitis (disorder)"}

# Age conditions for routing labs. The 65+ group is the fall-through, so it has no condition.
AGE_GROUP_CONDITIONS = {
    "under50": {"condition_type": "Age", "operator": "<", "quantity": 50, "unit": "years"},
    "50to64": {"condition_type": "Age", "operator": "<", "quantity": 65, "unit": "years"},
    "65plus": None,
}
LABS = ("AST", "ALT", "PLT")

# Lab chains appear twice: at diagnosis ("Dx") and at follow-up ("FU"); each ends in a different state.
LAB_PHASES = {"Dx": "Fibrosis_Dx_Check", "FU": "End_Followup"}

V2_REMARK = (
    "v2: onset eligibility age 35; lab ranges vary by age group (<50, 50-64, 65+) so FIB-4 lands in the "
    "intended band; progression 2% per follow-up. Original kept as masld_v1.json."
)


def set_onset_age(states):
    guard = states["Wait_Until_Adult"]["allow"]
    assert guard["quantity"] == 25
    guard["quantity"] = 35


def set_nafld_code(states):
    code = states["MASLD_Onset"]["codes"][0]
    assert code["code"] == "197321007"
    code["code"], code["display"] = NAFLD_CODE["code"], NAFLD_CODE["display"]


def split_nafld_and_nash(states):
    # Risk_Check -> band first, then the band picks the diagnosis.
    for branch in states["Risk_Check"]["complex_transition"]:
        for option in branch["distributions"]:
            if option["transition"] == "MASLD_Onset":
                option["transition"] = "Assign_Fibrosis_Band"
    for band_state in ("Band_low", "Band_indeterminate"):
        states[band_state]["direct_transition"] = "MASLD_Onset"
    for band_state in ("Band_high", "Band_cirrhosis"):
        states[band_state]["direct_transition"] = "NASH_Onset"

    states["MASLD_Onset"]["direct_transition"] = "MASLD_Diagnosis_Encounter"
    states["MASLD_Onset"]["remarks"] = ["NAFLD: low and indeterminate fibrosis bands."]
    states["NASH_Onset"] = {
        "type": "ConditionOnset",
        "remarks": ["NASH: high and cirrhosis fibrosis bands."],
        "target_encounter": "MASLD_Diagnosis_Encounter",
        "assign_to_attribute": "masld",
        "codes": [NASH_CODE],
        "direct_transition": "MASLD_Diagnosis_Encounter",
    }

    # Indeterminate -> high: end NAFLD, start NASH, then fibrosis as before.
    assert states["Progress_to_high"]["direct_transition"] == "Progressed_Fibrosis"
    states["Progress_to_high"]["direct_transition"] = "End_NAFLD"
    states["End_NAFLD"] = {
        "type": "ConditionEnd",
        "referenced_by_attribute": "masld",
        "direct_transition": "Progressed_NASH",
    }
    states["Progressed_NASH"] = {
        "type": "ConditionOnset",
        "assign_to_attribute": "masld",
        "codes": [NASH_CODE],
        "direct_transition": "Progressed_Fibrosis",
    }

    # Reasons cite the current MASLD condition (NAFLD or NASH) via the attribute.
    changed = 0
    for state in states.values():
        if state.get("reason") == "MASLD_Onset":
            state["reason"] = "masld"
            changed += 1
    assert changed == 5, changed


def set_progression_chance(states):
    for name in ("Maybe_Progress_low", "Maybe_Progress_indeterminate", "Maybe_Progress_high"):
        progress, stay = states[name]["distributed_transition"]
        assert progress["distribution"] == 0.05
        progress["distribution"], stay["distribution"] = 0.02, 0.98


def add_lab_chain(states, templates, phase, band, age_group, ranges, next_state):
    """Add AST -> ALT -> platelets observation states; return the first state's name."""
    names = [f"Labs_{phase}_{lab}_{band}_{age_group}" for lab in LABS]
    for i, lab in enumerate(LABS):
        state = copy.deepcopy(templates[lab])
        state["range"] = {"low": ranges[lab.lower()][0], "high": ranges[lab.lower()][1]}
        state.pop("direct_transition", None)
        state["direct_transition"] = names[i + 1] if i + 1 < len(names) else next_state
        states[names[i]] = state
    return names[0]


def make_labs_age_dependent(states):
    for phase, next_state in LAB_PHASES.items():
        # Reuse the v1 low-band states as templates for codes and units, then replace all lab states.
        templates = {lab: states[f"Labs_{phase}_{lab}_low"] for lab in LABS}
        for name in [n for n in states if n.startswith(f"Labs_{phase}_")]:
            del states[name]

        band_routes = []
        for band, age_groups in RANGES.items():
            age_routes = []
            for age_group, ranges in age_groups.items():
                first = add_lab_chain(states, templates, phase, band, age_group, ranges, next_state)
                route = {"transition": first}
                if AGE_GROUP_CONDITIONS[age_group]:
                    route["condition"] = AGE_GROUP_CONDITIONS[age_group]
                age_routes.append(route)

            router = f"Labs_{phase}_{band}"
            states[router] = {
                "type": "Simple",
                "remarks": [f"Route {band}-band labs by age group (<50, 50-64, 65+) so FIB-4 matches the band."],
                "conditional_transition": age_routes,
            }
            band_routes.append({
                "transition": router,
                "condition": {"condition_type": "Attribute", "attribute": "masld_fibrosis_band",
                              "operator": "==", "value": band},
            })
        band_routes.append({"transition": f"Labs_{phase}_low"})  # no band set: treat as low
        states[f"Labs_{phase}"]["conditional_transition"] = band_routes


def transition_targets(state):
    """Every state name this state can move to."""
    if "direct_transition" in state:
        yield state["direct_transition"]
    for key in ("conditional_transition", "distributed_transition", "complex_transition"):
        for transition in state.get(key, []):
            if "transition" in transition:
                yield transition["transition"]
            for option in transition.get("distributions", []):
                yield option["transition"]


def main():
    source, destination = sys.argv[1], sys.argv[2]
    with open(source, encoding="utf-8") as f:
        module = json.load(f)
    states = module["states"]

    # Order matters: new states are added in this order, which fixes their order in the JSON.
    set_onset_age(states)
    set_nafld_code(states)
    split_nafld_and_nash(states)
    set_progression_chance(states)
    make_labs_age_dependent(states)
    module["remarks"] = module.get("remarks", []) + [V2_REMARK]

    broken = [(name, target) for name, state in states.items()
              for target in transition_targets(state) if target not in states]
    assert not broken, broken

    with open(destination, "w", encoding="utf-8") as f:
        json.dump(module, f, indent=2)
    print(f"wrote {destination}: {len(states)} states")


if __name__ == "__main__":
    main()
