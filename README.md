# masld-synthea-omop

Synthetic MASLD (metabolic dysfunction-associated steatotic liver disease) patients generated with
[Synthea](https://github.com/synthetichealth/synthea), converted to an OMOP CDM subset in DuckDB, and
used to test MASLD at-risk / FIB-4 queries before running them on real data.

**Synthetic test data only. The modules are not clinically validated.**

## What's here

| Path | What it is |
|---|---|
| `masld.json` | Current Synthea MASLD module |
| `masld_screening.json` | Companion module: yearly AST/ALT/platelet check-ups for at-risk adults (see below) |
| `masld_v1.json`, `masld_v2.json` | Earlier versions of `masld.json` |
| `lab_ranges.py` | Band × age-group lab ranges, with a Monte Carlo check that FIB-4 lands in the intended band |
| `build_masld.py` | Rebuilds `masld.json` from `masld_v1.json` |
| `build_screening.py` | Generates `masld_screening.json` |
| `analyze.py` | Quick report from Synthea CSV output (condition counts, risk-factor rates, FIB-4 bands) |
| `omop/etl.py` | Synthea CSV → OMOP CDM v5.4 subset in DuckDB |
| `omop/vocab_cache/` | *Not in the repo.* OMOP vocabulary subset fetched from the public OHDSI WebAPI; create it with `etl.py fetch` |
| `omop/queries/` | At-risk queries: original (SQL Server + DuckDB), v2 (DuckDB), v3 (SQL Server + DuckDB), translation notes |
| `omop/run_queries.py` | Runs a DuckDB query file and saves results |
| `omop/csv_check.py` | Recomputes the query results straight from the Synthea CSVs (independent check) |
| `omop/verify_tsql.py` | Parses a SQL Server file as T-SQL, converts it to DuckDB, runs it, compares with the DuckDB version |
| `omop/results/` | Results for the current data (index date 2024-01-01) |

## The modules

**`masld.json`**
- Adults become eligible at 35. After a random 0–30 year delay, adults with T2DM, obesity or
  hypertension get MASLD with probability 0.55, others 0.12.
- Each patient gets a fibrosis band: low 70%, indeterminate 15%, high 10%, cirrhosis 5%.
- Diagnosis code depends on band: low/indeterminate → 197315008 *Non-alcoholic fatty liver disease*;
  high/cirrhosis → 442685003 *Non-alcoholic steatohepatitis*. Progression to high ends NAFLD and starts NASH.
- High band also gets 62484002 *Hepatic fibrosis*; cirrhosis band gets 19943007 *Cirrhosis of liver*.
- AST, ALT and platelet ranges depend on band and age group (<50, 50–64, 65+) so that
  FIB-4 = age × AST / (platelets × √ALT) falls in the intended band.
- Follow-up every 2–3 years (low band) or yearly (others); 2% chance per follow-up of moving up a band.

**`masld_screening.json`**
- Adults 18+ with T2DM, obesity (latest BMI ≥ 30 or an obesity condition) or essential hypertension
  get a check-up every 11 months with AST, ALT and platelets, whether or not they have MASLD.
- Without MASLD: normal ranges (AST 15–30 U/L, ALT 15–35 U/L, platelets 180–350 ×10³/µL).
  With MASLD: the same band-based ranges as `masld.json` (it reads the shared `masld_fibrosis_band` attribute).
- It's a separate module because a Synthea module is a single state machine; this loop has to run
  alongside `masld.json`'s own timeline. 11 months (not 12) so Synthea's 7-day time step can't leave a
  12-month window without labs.

## Reproduce

Needs Java 11+ and Python 3.

```bash
curl -L -o synthea-with-dependencies.jar \
  https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar
mkdir -p modules && cp masld.json masld_screening.json modules/

java -Dfile.encoding=UTF-8 -jar synthea-with-dependencies.jar -s 42 -cs 42 -p 300 -a 30-80 -d modules \
  --exporter.baseDirectory=./out --exporter.clinical_note.export=true --exporter.csv.export=true \
  --exporter.fhir.export=true --exporter.years_of_history=0 Indiana

python3 -m venv omop/.venv && omop/.venv/bin/pip install -r omop/requirements.txt
omop/.venv/bin/python omop/etl.py fetch        # builds omop/vocab_cache from the OHDSI WebAPI (needs internet)
omop/.venv/bin/python omop/etl.py build        # builds omop/omop.duckdb from out/csv + omop/vocab_cache
omop/.venv/bin/python omop/run_queries.py                                              # original
omop/.venv/bin/python omop/run_queries.py queries/masld_at_risk_queries_v3.duckdb.sql v3_
python3 omop/csv_check.py --variant v1 ; python3 omop/csv_check.py --variant v3
omop/.venv/bin/python omop/verify_tsql.py queries/masld_at_risk_queries_v3.sqlserver.sql v3_
```

`--exporter.years_of_history=0` matters: Synthea's default exports only the last 10 years of
observations, which drops labs from older MASLD encounters.

The Synthea jar is the moving `master-branch-latest` build, so a rerun with a newer jar can give a
different population than the committed results.

## OMOP ETL

`omop/etl.py` follows the mapping rules of OHDSI [ETL-Synthea](https://github.com/OHDSI/ETL-Synthea)
in Python. The R package itself needs a full Athena vocabulary loaded, which this project doesn't use.

- Tables: `person`, `observation_period`, `visit_occurrence`, `condition_occurrence`, `measurement`,
  `observation`, `death`, `concept`, `concept_ancestor`.
- Each source code is looked up as a source concept, mapped to its standard concept via "Maps to",
  and routed to the table for that concept's domain. Condition-domain codes go to
  `condition_occurrence`, Measurement-domain codes to `measurement`, and everything else to `observation`.
- Vocabulary: every concept_id comes from the public OHDSI WebAPI (`atlas-demo.ohdsi.org`) and is
  cached in `omop/vocab_cache/`. That includes the fixed ids for gender, race, ethnicity, visit and type
  concepts, which are each checked by name. `concept_ancestor` holds every ancestor of every standard
  concept that appears in the data, plus self rows.

Known limitations:
- The WebAPI gives one separation distance per ancestor pair, so `min_levels_of_separation` =
  `max_levels_of_separation`.
- `concept` holds only the concepts in the data and their ancestors, not the full vocabulary. Concept
  sets built from descendants still resolve correctly, because any descendant that occurs in the data is present.
- Synthea-only codes (QALY, DALY, `99999-x`, `X9999-x`) and "Refugee" (446654005) have no standard
  concept and get concept_id 0.

## Vocabulary details that affect the queries

- 162864005 *Body mass index 30+ - obesity* is an **Observation**-domain concept. It lands in
  `observation`, not `condition_occurrence`. In the hierarchy it sits under 414915002 *Obese*,
  not under 414916001 *Obesity*.
- 442685003 NASH is **not** a descendant of 197315008 NAFLD in OMOP. A MASLD concept set needs both codes.
- 197321007 *Steatosis of liver* (used by `masld_v1.json`) is a **parent** of NAFLD, so descendants of
  NAFLD don't include it.
- Descendants of 38341003 *Hypertensive disorder* include 398254007 *Pre-eclampsia*. Descendants of
  55822004 *Hyperlipidemia* include 302870006 *Hypertriglyceridemia*. v3 excludes both.

## Query versions

| File | Obesity counts as | Hypertension / hyperlipidemia |
|---|---|---|
| `masld_at_risk_queries.sqlserver.sql` (original) and `.duckdb.sql` (translation) | condition under 414916001 | full OMOP descendants |
| `masld_at_risk_queries_v2.duckdb.sql` | + latest BMI ≥ 30 in 2 years, + *Obese*/*Obesity* concepts in `observation` | full OMOP descendants |
| `masld_at_risk_queries_v3.sqlserver.sql` / `_v3.duckdb.sql` | same as v2 | excludes pre-eclampsia and hypertriglyceridemia |

At-risk = T2DM, or MASLD (197315008/442685003), or obesity together with hypertension or
hyperlipidemia. FIB-4 uses the newest AST, ALT and platelets in the 12 months before the index date.

- Index dates: the original `masld_at_risk_queries.sqlserver.sql` is kept as written and uses
  `@index_date = '2025-12-31'`. `masld_at_risk_queries_v3.sqlserver.sql`, the DuckDB files and all
  results here use 2024-01-01.
- In the SQL Server v3, `grp` is cast to `VARCHAR(20)`. Otherwise SQL Server sizes the column as
  `varchar(7)` from the `VALUES` literals, and inserting `'obesity_obs'` fails.
- `omop/queries/TRANSLATION_NOTES.md` lists every syntax change between SQL Server and DuckDB.

## Current results (index date 2024-01-01)

352 synthetic patients (300 alive at the end of the simulation); 307 active adults at the index date.

| | Original | v3 |
|---|---|---|
| At risk | 103 (33.6%) | 146 (47.6%) |
| FIB-4 missing labs | 0 | 0 |
| FIB-4 low | 74 | 114 |
| FIB-4 indeterminate | 14 | 17 |
| FIB-4 high | 15 | 15 |

These figures are checked three ways:
- `omop/csv_check.py` recomputes them straight from the Synthea CSVs, and every cell matches.
- `omop/verify_tsql.py` converts both SQL Server files to DuckDB, runs them, and gets the same results
  as the DuckDB versions.
- The SQL Server files have not been run on SQL Server itself.

Because FIB-4 includes age, normal labs can still score up to about 2.6 at age 60 and 3.4 at age 80.
Some at-risk people without MASLD therefore land in indeterminate or high.

## Licensing notes

- Synthea is Apache-2.0.
- The vocabulary cache (`omop/vocab_cache/`) is not committed: it holds concept names and codes from
  SNOMED CT, LOINC and other vocabularies, which carry their own license terms. Run
  `omop/.venv/bin/python omop/etl.py fetch` to rebuild it from the OHDSI WebAPI.
