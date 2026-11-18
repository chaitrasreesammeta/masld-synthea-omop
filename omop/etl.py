"""Synthea CSV -> OMOP CDM v5.4 subset in DuckDB, following the OHDSI ETL-Synthea mapping rules.

The vocabulary comes from the public OHDSI WebAPI instead of an Athena download. Every
concept_id used is fetched from the API; the few fixed ids (gender, race, ...) are checked
against it by name.

Usage:
    omop/.venv/bin/python omop/etl.py fetch   # query the WebAPI, write omop/vocab_cache/*.json
    omop/.venv/bin/python omop/etl.py build   # build omop/omop.duckdb from out/csv + the cache

Tables built: person, observation_period, visit_occurrence, condition_occurrence,
measurement, observation, death, concept, concept_ancestor.

ETL-Synthea rules followed:
  - source code -> source concept (vocabulary + code) -> standard concept via "Maps to".
    Each row goes to the table for its standard concept's domain:
      conditions.csv:   Condition -> condition_occurrence, any other domain -> observation
      observations.csv: Measurement -> measurement, any other domain -> observation
  - gender M/F -> 8507/8532; race white/black/asian/native/hawaiian -> 8527/8516/8515/8657/8557,
    otherwise 0; ethnicity hispanic/nonhispanic -> 38003563/38003564
  - visit: inpatient -> 9201, emergency/urgentcare -> 9203, everything else -> 9202 (outpatient)
  - type concept 32817 (EHR); observation_period = first to last visit per person

Limitation: the WebAPI gives one separation distance per ancestor pair, so
concept_ancestor.min_levels_of_separation = max_levels_of_separation = that distance.
"""
import csv
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_DIR = os.path.join(ROOT, "out", "csv")
OMOP_DIR = os.path.join(ROOT, "omop")
CACHE_DIR = os.path.join(OMOP_DIR, "vocab_cache")
DATABASE = os.path.join(OMOP_DIR, "omop.duckdb")
WEBAPI = "https://atlas-demo.ohdsi.org/WebAPI"

# Fixed concept ids -> expected names; fetch() stops if the WebAPI disagrees.
FIXED_CONCEPTS = {
    8507: "MALE", 8532: "FEMALE",
    8527: "White", 8516: "Black or African American", 8515: "Asian",
    8657: "American Indian or Alaska Native", 8557: "Native Hawaiian or Other Pacific Islander",
    38003563: "Hispanic or Latino", 38003564: "Not Hispanic or Latino",
    9201: "Inpatient Visit", 9202: "Outpatient Visit", 9203: "Emergency Room Visit",
    32817: "EHR",
}
GENDER = {"M": 8507, "F": 8532}
RACE = {"white": 8527, "black": 8516, "asian": 8515, "native": 8657, "hawaiian": 8557}
ETHNICITY = {"hispanic": 38003563, "nonhispanic": 38003564}
VISIT = {"inpatient": 9201, "emergency": 9203, "urgentcare": 9203}
OUTPATIENT_VISIT = 9202
EHR = 32817

# Synthea unit spelling -> OMOP UCUM concept_code spelling (the concept itself is looked up via the API).
UNIT_ALIASES = {"U/L": "[U]/L", "kU/L": "k[U]/L", "ng/dl": "ng/dL", "m[IU]/L": "m[IU]/L", "{INR}": "{INR}",
                "/a": "/a", "{count}": "{count}", "{score}": "{score}", "{T-score}": "{T-score}"}

CONCEPT_FIELDS = ("CONCEPT_ID", "CONCEPT_NAME", "DOMAIN_ID", "VOCABULARY_ID", "CONCEPT_CLASS_ID",
                  "STANDARD_CONCEPT", "CONCEPT_CODE", "INVALID_REASON")
LOOKUP_BATCH_SIZE = 100
PARALLEL_REQUESTS = 4


# --------------------------------------------------------------------------------------------
# WebAPI access
# --------------------------------------------------------------------------------------------

def webapi(method, path, body=None, tries=5):
    """Call the WebAPI and return parsed JSON, retrying with back-off."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(WEBAPI + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "User-Agent": "synthea-omop-etl"})
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except Exception as error:
            if attempt == tries:
                raise RuntimeError(f"WebAPI {method} {path} failed: {error}")
            time.sleep(2 * attempt)


def concept_fields(concept):
    """Keep only the fields we store."""
    return {field: concept.get(field) for field in CONCEPT_FIELDS}


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, value, **options):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, **options)


def related(concept_id):
    """'Maps to' targets and all ancestors (with distance) of one concept, cached on disk."""
    path = os.path.join(CACHE_DIR, f"related_{concept_id}.json")
    if os.path.exists(path):
        return read_json(path)

    result = {"maps_to": [], "ancestors": []}
    for other in webapi("GET", f"/vocabulary/concept/{concept_id}/related"):
        for relationship in other["RELATIONSHIPS"]:
            name, distance = relationship["RELATIONSHIP_NAME"], relationship["RELATIONSHIP_DISTANCE"]
            if name == "Non-standard to Standard map (OMOP)" and distance == 1:
                result["maps_to"].append(concept_fields(other))
            if name == "Has ancestor of":
                result["ancestors"].append([concept_fields(other), distance])
    write_json(path, result)
    return result


# --------------------------------------------------------------------------------------------
# fetch: build the vocabulary cache
# --------------------------------------------------------------------------------------------

def source_codes():
    """Distinct codes in the CSVs, by the vocabulary we look them up in."""
    def distinct(file, column):
        with open(os.path.join(CSV_DIR, file), encoding="utf-8") as f:
            return {row[column] for row in csv.DictReader(f)}
    return {
        "SNOMED": sorted(distinct("conditions.csv", "CODE")),
        "LOINC": sorted(distinct("observations.csv", "CODE")),
        "UCUM": sorted(distinct("observations.csv", "UNITS") - {""}),
    }


def verify_fixed_concepts():
    found = {c["CONCEPT_ID"]: c for c in webapi("POST", "/vocabulary/lookup/identifiers", list(FIXED_CONCEPTS))}
    for concept_id, name in FIXED_CONCEPTS.items():
        if concept_id not in found or found[concept_id]["CONCEPT_NAME"].lower() != name.lower():
            raise SystemExit(f"Fixed concept {concept_id} ({name}) not confirmed by WebAPI: {found.get(concept_id)}")
    write_json(os.path.join(CACHE_DIR, "fixed.json"), [concept_fields(c) for c in found.values()])


def lookup_source_concepts(codes):
    """(vocabulary, code) -> source concept. Prefers a valid concept when a code has several."""
    source = {}
    for vocabulary, code_list in codes.items():
        for start in range(0, len(code_list), LOOKUP_BATCH_SIZE):
            batch = code_list[start:start + LOOKUP_BATCH_SIZE]
            for concept in webapi("POST", "/vocabulary/lookup/sourcecodes", batch):
                if concept["VOCABULARY_ID"] != vocabulary or concept["CONCEPT_CODE"] not in code_list:
                    continue
                key = (vocabulary, concept["CONCEPT_CODE"])
                previous = source.get(key)
                if previous is None or (previous["INVALID_REASON"] != "V" and concept["INVALID_REASON"] == "V"):
                    source[key] = concept_fields(concept)

    # Second try for misses: Synthea spells some units differently from OMOP's UCUM codes, and some
    # observations.csv codes are SNOMED rather than LOINC. Results are keyed by where the code is used.
    retries = [("UCUM", UNIT_ALIASES[unit], unit) for unit in codes["UCUM"]
               if ("UCUM", unit) not in source and unit in UNIT_ALIASES]
    retries += [("SNOMED", code, code) for code in codes["LOINC"] if ("LOINC", code) not in source]
    if retries:
        for concept in webapi("POST", "/vocabulary/lookup/sourcecodes", sorted({lookup for _, lookup, _ in retries})):
            for vocabulary, lookup, original in retries:
                if (concept["VOCABULARY_ID"] == vocabulary and concept["CONCEPT_CODE"] == lookup
                        and concept["INVALID_REASON"] == "V"):
                    used_as = "UCUM" if vocabulary == "UCUM" else "LOINC"
                    source.setdefault((used_as, original), concept_fields(concept))
    return source


def map_to_standard(source):
    """One mapping entry per source code, with its standard target concept(s)."""
    non_standard = [c["CONCEPT_ID"] for c in source.values() if c["STANDARD_CONCEPT"] != "S"]
    with ThreadPoolExecutor(PARALLEL_REQUESTS) as pool:
        relations = dict(zip(non_standard, pool.map(related, non_standard)))

    mapping = []
    for (vocabulary, code), concept in sorted(source.items()):
        if concept["STANDARD_CONCEPT"] == "S":
            targets = [concept]
        else:
            targets = relations[concept["CONCEPT_ID"]]["maps_to"]
        mapping.append({"vocab": vocabulary, "code": code, "source": concept, "targets": targets})
    return mapping


def fetch():
    os.makedirs(CACHE_DIR, exist_ok=True)
    verify_fixed_concepts()

    codes = source_codes()
    source = lookup_source_concepts(codes)
    missing = [(vocabulary, code) for vocabulary, code_list in codes.items()
               for code in code_list if (vocabulary, code) not in source]
    mapping = map_to_standard(source)

    # Cache the ancestors of every standard concept the data uses (units don't need them).
    standard_ids = sorted({t["CONCEPT_ID"] for m in mapping for t in m["targets"] if m["vocab"] != "UCUM"})
    with ThreadPoolExecutor(PARALLEL_REQUESTS) as pool:
        list(pool.map(related, standard_ids))

    write_json(os.path.join(CACHE_DIR, "mapping.json"),
               {"mapping": mapping, "missing": missing, "standard_ids": standard_ids}, indent=1)
    print(f"source codes mapped: {len(mapping)}  not found in vocabulary: {len(missing)} {missing[:10]}")
    print(f"standard concepts with ancestors fetched: {len(standard_ids)}")


# --------------------------------------------------------------------------------------------
# build: create the CDM tables
# --------------------------------------------------------------------------------------------

def load_vocabulary():
    """Return (concepts by id, concept_ancestor rows, source-to-standard rows) from the cache."""
    cache = read_json(os.path.join(CACHE_DIR, "mapping.json"))
    concepts = {c["CONCEPT_ID"]: c for c in read_json(os.path.join(CACHE_DIR, "fixed.json"))}

    source_to_standard = []  # (vocab, code, source_concept_id, target_concept_id, target_domain)
    for entry in cache["mapping"]:
        source = entry["source"]
        concepts[source["CONCEPT_ID"]] = source
        if not entry["targets"]:
            source_to_standard.append((entry["vocab"], entry["code"], source["CONCEPT_ID"], 0, None))
        for target in entry["targets"]:
            concepts[target["CONCEPT_ID"]] = target
            source_to_standard.append((entry["vocab"], entry["code"], source["CONCEPT_ID"],
                                       target["CONCEPT_ID"], target["DOMAIN_ID"]))
    for vocabulary, code in cache["missing"]:
        source_to_standard.append((vocabulary, code, 0, 0, None))

    ancestors = set()  # (ancestor_id, descendant_id, distance)
    for concept_id in cache["standard_ids"]:
        ancestors.add((concept_id, concept_id, 0))
        for ancestor, distance in related(concept_id)["ancestors"]:
            concepts[ancestor["CONCEPT_ID"]] = ancestor
            ancestors.add((ancestor["CONCEPT_ID"], concept_id, distance))
    return concepts, ancestors, source_to_standard


def create_vocabulary_tables(con, concepts, ancestors, source_to_standard):
    con.execute("""
        CREATE TABLE concept (
            concept_id INTEGER PRIMARY KEY, concept_name VARCHAR, domain_id VARCHAR, vocabulary_id VARCHAR,
            concept_class_id VARCHAR, standard_concept VARCHAR, concept_code VARCHAR,
            valid_start_date DATE, valid_end_date DATE, invalid_reason VARCHAR)""")
    con.executemany(
        "INSERT INTO concept VALUES (?, ?, ?, ?, ?, ?, ?, DATE '1970-01-01', DATE '2099-12-31', ?)",
        [(c["CONCEPT_ID"], c["CONCEPT_NAME"], c["DOMAIN_ID"], c["VOCABULARY_ID"], c["CONCEPT_CLASS_ID"],
          c["STANDARD_CONCEPT"] if c["STANDARD_CONCEPT"] in ("S", "C") else None,
          c["CONCEPT_CODE"],
          None if c["INVALID_REASON"] == "V" else c["INVALID_REASON"])
         for c in concepts.values()])

    con.execute("""
        CREATE TABLE concept_ancestor (
            ancestor_concept_id INTEGER, descendant_concept_id INTEGER,
            min_levels_of_separation INTEGER, max_levels_of_separation INTEGER)""")
    con.executemany("INSERT INTO concept_ancestor VALUES (?, ?, ?, ?)",
                    [(a, d, distance, distance) for a, d, distance in sorted(ancestors)])

    # Working table, dropped at the end of the build.
    con.execute("""
        CREATE TABLE source_to_standard (
            vocab VARCHAR, code VARCHAR, source_concept_id INTEGER,
            target_concept_id INTEGER, target_domain VARCHAR)""")
    con.executemany("INSERT INTO source_to_standard VALUES (?, ?, ?, ?, ?)", source_to_standard)


def read_csv_sql(file):
    return f"read_csv('{CSV_DIR}/{file}', header = true, all_varchar = true)"


def case_map(column, mapping, default):
    """SQL CASE expression mapping a column's values to concept ids."""
    whens = " ".join(f"WHEN '{value}' THEN {concept_id}" for value, concept_id in mapping.items())
    return f"CASE {column} {whens} ELSE {default} END"


def create_person_and_death(con):
    con.execute(f"""
        CREATE TEMP TABLE patients AS
        SELECT row_number() OVER (ORDER BY Id)::INTEGER AS person_id, *
        FROM {read_csv_sql('patients.csv')}""")
    con.execute(f"""
        CREATE TABLE person AS
        SELECT
            person_id,
            {case_map('GENDER', GENDER, 0)} AS gender_concept_id,
            year(BIRTHDATE::DATE) AS year_of_birth,
            month(BIRTHDATE::DATE) AS month_of_birth,
            day(BIRTHDATE::DATE) AS day_of_birth,
            BIRTHDATE::TIMESTAMP AS birth_datetime,
            {case_map('RACE', RACE, 0)} AS race_concept_id,
            {case_map('ETHNICITY', ETHNICITY, 0)} AS ethnicity_concept_id,
            NULL::INTEGER AS location_id,
            NULL::INTEGER AS provider_id,
            NULL::INTEGER AS care_site_id,
            Id AS person_source_value,
            GENDER AS gender_source_value,
            0 AS gender_source_concept_id,
            RACE AS race_source_value,
            0 AS race_source_concept_id,
            ETHNICITY AS ethnicity_source_value,
            0 AS ethnicity_source_concept_id
        FROM patients""")
    con.execute(f"""
        CREATE TABLE death AS
        SELECT
            person_id,
            DEATHDATE::DATE AS death_date,
            DEATHDATE::TIMESTAMP AS death_datetime,
            {EHR} AS death_type_concept_id,
            0 AS cause_concept_id,
            NULL AS cause_source_value,
            0 AS cause_source_concept_id
        FROM patients
        WHERE DEATHDATE IS NOT NULL""")


def create_visits(con):
    # The last three columns are extras (not CDM) kept for convenience when checking the data.
    con.execute(f"""
        CREATE TABLE visit_occurrence AS
        SELECT
            row_number() OVER (ORDER BY e.START, e.Id)::INTEGER AS visit_occurrence_id,
            p.person_id,
            {case_map('e.ENCOUNTERCLASS', VISIT, OUTPATIENT_VISIT)} AS visit_concept_id,
            e.START::DATE AS visit_start_date,
            e.START::TIMESTAMP AS visit_start_datetime,
            e.STOP::DATE AS visit_end_date,
            e.STOP::TIMESTAMP AS visit_end_datetime,
            {EHR} AS visit_type_concept_id,
            NULL::INTEGER AS provider_id,
            NULL::INTEGER AS care_site_id,
            e.Id AS visit_source_value,
            0 AS visit_source_concept_id,
            0 AS admitted_from_concept_id,
            NULL AS admitted_from_source_value,
            0 AS discharged_to_concept_id,
            NULL AS discharged_to_source_value,
            NULL::INTEGER AS preceding_visit_occurrence_id,
            e.ENCOUNTERCLASS AS encounter_class,
            e.CODE AS encounter_code,
            e.REASONCODE AS reason_code
        FROM {read_csv_sql('encounters.csv')} e
        JOIN patients p ON p.Id = e.PATIENT""")
    con.execute(f"""
        CREATE TABLE observation_period AS
        SELECT
            row_number() OVER (ORDER BY person_id)::INTEGER AS observation_period_id,
            person_id,
            min(visit_start_date) AS observation_period_start_date,
            max(visit_end_date) AS observation_period_end_date,
            {EHR} AS period_type_concept_id
        FROM visit_occurrence
        GROUP BY person_id""")


def stage_conditions_and_observations(con):
    """Join conditions.csv and observations.csv to persons, visits and the vocabulary."""
    con.execute(f"""
        CREATE TEMP TABLE staged_conditions AS
        SELECT
            p.person_id,
            v.visit_occurrence_id,
            c.START::DATE AS start_date,
            c.STOP::DATE AS end_date,
            c.CODE AS code,
            s.source_concept_id,
            s.target_concept_id,
            s.target_domain
        FROM {read_csv_sql('conditions.csv')} c
        JOIN patients p ON p.Id = c.PATIENT
        LEFT JOIN visit_occurrence v ON v.visit_source_value = c.ENCOUNTER
        LEFT JOIN source_to_standard s ON s.vocab = 'SNOMED' AND s.code = c.CODE""")
    con.execute(f"""
        CREATE TEMP TABLE staged_observations AS
        SELECT
            p.person_id,
            v.visit_occurrence_id,
            o.DATE::TIMESTAMP AS observed_at,
            o.CODE AS code,
            o.VALUE AS value,
            o.UNITS AS units,
            o.TYPE AS value_type,
            s.source_concept_id,
            s.target_concept_id,
            s.target_domain,
            coalesce(u.target_concept_id, 0) AS unit_concept_id
        FROM {read_csv_sql('observations.csv')} o
        JOIN patients p ON p.Id = o.PATIENT
        LEFT JOIN visit_occurrence v ON v.visit_source_value = o.ENCOUNTER
        LEFT JOIN source_to_standard s ON s.vocab = 'LOINC' AND s.code = o.CODE
        LEFT JOIN source_to_standard u ON u.vocab = 'UCUM' AND u.code = o.UNITS""")


# Row ids are numbered in a fully specified order (with tie-breakers) so every build gives the
# same ids. The queries use measurement_id to break ties between labs on the same date.

def create_condition_occurrence(con):
    # Condition domain, plus codes with no standard concept (kept with concept_id 0).
    con.execute(f"""
        CREATE TABLE condition_occurrence AS
        SELECT
            row_number() OVER (ORDER BY start_date, person_id, code, end_date, visit_occurrence_id)::INTEGER
                AS condition_occurrence_id,
            person_id,
            coalesce(target_concept_id, 0) AS condition_concept_id,
            start_date AS condition_start_date,
            start_date::TIMESTAMP AS condition_start_datetime,
            end_date AS condition_end_date,
            end_date::TIMESTAMP AS condition_end_datetime,
            {EHR} AS condition_type_concept_id,
            0 AS condition_status_concept_id,
            NULL AS stop_reason,
            NULL::INTEGER AS provider_id,
            visit_occurrence_id,
            NULL::INTEGER AS visit_detail_id,
            code AS condition_source_value,
            coalesce(source_concept_id, 0) AS condition_source_concept_id,
            NULL AS condition_status_source_value
        FROM staged_conditions
        WHERE coalesce(target_domain, 'Condition') = 'Condition'""")


def create_measurement(con):
    # Measurement domain, plus codes with no standard concept (kept with concept_id 0).
    con.execute(f"""
        CREATE TABLE measurement AS
        SELECT
            row_number() OVER (ORDER BY observed_at, person_id, code, visit_occurrence_id, value, units)::INTEGER
                AS measurement_id,
            person_id,
            coalesce(target_concept_id, 0) AS measurement_concept_id,
            observed_at::DATE AS measurement_date,
            observed_at AS measurement_datetime,
            NULL AS measurement_time,
            {EHR} AS measurement_type_concept_id,
            0 AS operator_concept_id,
            CASE WHEN value_type = 'numeric' THEN TRY_CAST(value AS DOUBLE) END AS value_as_number,
            0 AS value_as_concept_id,
            unit_concept_id,
            NULL::DOUBLE AS range_low,
            NULL::DOUBLE AS range_high,
            NULL::INTEGER AS provider_id,
            visit_occurrence_id,
            NULL::INTEGER AS visit_detail_id,
            code AS measurement_source_value,
            coalesce(source_concept_id, 0) AS measurement_source_concept_id,
            units AS unit_source_value,
            0 AS unit_source_concept_id,
            value AS value_source_value
        FROM staged_observations
        WHERE coalesce(target_domain, 'Measurement') = 'Measurement'""")


def create_observation(con):
    # Mapped rows from either CSV whose standard concept is in another domain.
    con.execute(f"""
        CREATE TABLE observation AS
        SELECT
            row_number() OVER (ORDER BY observation_date, person_id, observation_source_value,
                                     observation_datetime, visit_occurrence_id, value_as_string, value_as_number)::INTEGER
                AS observation_id,
            *
        FROM (
            SELECT
                person_id,
                target_concept_id AS observation_concept_id,
                observed_at::DATE AS observation_date,
                observed_at AS observation_datetime,
                {EHR} AS observation_type_concept_id,
                CASE WHEN value_type = 'numeric' THEN TRY_CAST(value AS DOUBLE) END AS value_as_number,
                CASE WHEN value_type = 'numeric' THEN NULL ELSE value END AS value_as_string,
                0 AS value_as_concept_id,
                0 AS qualifier_concept_id,
                unit_concept_id,
                NULL::INTEGER AS provider_id,
                visit_occurrence_id,
                code AS observation_source_value,
                source_concept_id AS observation_source_concept_id,
                units AS unit_source_value
            FROM staged_observations
            WHERE target_domain IS NOT NULL AND target_domain <> 'Measurement'

            UNION ALL

            SELECT
                person_id,
                target_concept_id,
                start_date,
                start_date::TIMESTAMP,
                {EHR},
                NULL,
                NULL,
                0,
                0,
                0,
                NULL,
                visit_occurrence_id,
                code,
                source_concept_id,
                NULL
            FROM staged_conditions
            WHERE target_domain IS NOT NULL AND target_domain <> 'Condition'
        )""")


def check_and_report(con):
    hypertension_link = con.execute("""
        SELECT count(*)
        FROM concept_ancestor a
        JOIN concept parent ON parent.concept_id = a.ancestor_concept_id
        JOIN concept child ON child.concept_id = a.descendant_concept_id
        WHERE parent.vocabulary_id = 'SNOMED' AND parent.concept_code = '38341003'
          AND child.vocabulary_id = 'SNOMED' AND child.concept_code = '59621000'""").fetchone()[0]
    assert hypertension_link == 1, "59621000 Essential hypertension is not a descendant of 38341003 in concept_ancestor"

    count = lambda sql: con.execute(sql).fetchone()[0]
    for table in ("person", "death", "observation_period", "visit_occurrence", "condition_occurrence",
                  "measurement", "observation", "concept", "concept_ancestor"):
        print(f"{table:22s} {count(f'SELECT count(*) FROM {table}'):>9}")
    print("unmapped (concept_id 0): condition",
          count("SELECT count(*) FROM condition_occurrence WHERE condition_concept_id = 0"),
          " measurement",
          count("SELECT count(*) FROM measurement WHERE measurement_concept_id = 0"))
    print("domain routing of conditions.csv codes:",
          con.execute("""SELECT coalesce(target_domain, 'unmapped'), count(*) FROM staged_conditions
                         GROUP BY 1 ORDER BY 2 DESC""").fetchall())


def build():
    import duckdb  # only needed for build

    concepts, ancestors, source_to_standard = load_vocabulary()
    if os.path.exists(DATABASE):
        os.remove(DATABASE)
    con = duckdb.connect(DATABASE)

    create_vocabulary_tables(con, concepts, ancestors, source_to_standard)
    create_person_and_death(con)
    create_visits(con)
    stage_conditions_and_observations(con)
    create_condition_occurrence(con)
    create_measurement(con)
    create_observation(con)
    con.execute("DROP TABLE source_to_standard")

    check_and_report(con)
    con.close()


COMMANDS = {"fetch": fetch, "build": build}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]]()
