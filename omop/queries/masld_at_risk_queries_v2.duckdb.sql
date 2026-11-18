-- v2 of masld_at_risk_queries (DuckDB). Same as masld_at_risk_queries.duckdb.sql except obesity
-- is ALSO true when either:
--   (a) the most recent BMI (LOINC 39156-5) in the 2 years up to the index date is >= 30, or
--   (b) the observation table has an obesity concept on or before the index date
--       (descendants of SNOMED 414915002 'Obese' and 414916001 'Obesity'; Synthea's
--        162864005 'BMI 30+ - obesity' is under 'Obese', not under 'Obesity').
-- Changed blocks are marked  -- v2.

SET VARIABLE index_date = DATE '2024-01-01';

DROP TABLE IF EXISTS concept_sets;

CREATE TEMP TABLE concept_sets AS
SELECT s.grp, ca.descendant_concept_id AS concept_id
FROM (VALUES
    ('t2dm',    '44054006'),
    ('masld',   '197315008'),
    ('masld',   '442685003'),
    ('obesity', '414916001'),
    ('htn',     '38341003'),
    ('hld',     '55822004')
) AS s(grp, code)
JOIN concept c           ON c.concept_code = s.code AND c.vocabulary_id = 'SNOMED'
JOIN concept_ancestor ca ON ca.ancestor_concept_id = c.concept_id;

INSERT INTO concept_sets (grp, concept_id)
SELECT
    CASE
        WHEN concept_code IN ('1920-8', '30239-8') THEN 'ast'
        WHEN concept_code IN ('1742-6', '76625-3') THEN 'alt'
        ELSE 'plt'
    END,
    concept_id
FROM concept
WHERE vocabulary_id = 'LOINC'
  AND concept_code IN ('1920-8', '30239-8', '1742-6', '76625-3', '777-3', '26515-7');

-- v2: obesity concepts for the observation table, and BMI
INSERT INTO concept_sets (grp, concept_id)
SELECT DISTINCT 'obesity_obs', ca.descendant_concept_id
FROM concept c
JOIN concept_ancestor ca ON ca.ancestor_concept_id = c.concept_id
WHERE c.vocabulary_id = 'SNOMED' AND c.concept_code IN ('414915002', '414916001');

INSERT INTO concept_sets (grp, concept_id)
SELECT 'bmi', concept_id FROM concept WHERE vocabulary_id = 'LOINC' AND concept_code = '39156-5';


DROP TABLE IF EXISTS cohort;

CREATE TEMP TABLE cohort AS
WITH active_adults AS (
    SELECT
        p.person_id,
        p.gender_concept_id,
        YEAR(getvariable('index_date')) - p.year_of_birth AS age
    FROM person p
    WHERE YEAR(getvariable('index_date')) - p.year_of_birth >= 18
      AND EXISTS (
            SELECT 1 FROM visit_occurrence v
            WHERE v.person_id = p.person_id
              AND v.visit_start_date BETWEEN CAST(getvariable('index_date') - INTERVAL 3 YEAR AS DATE) AND getvariable('index_date')
          )
      AND NOT EXISTS (
            SELECT 1 FROM death d
            WHERE d.person_id = p.person_id
              AND d.death_date <= getvariable('index_date')
          )
),
risk_factors AS (
    SELECT
        co.person_id,
        MAX(CASE WHEN cs.grp = 't2dm'    THEN 1 ELSE 0 END) AS has_t2dm,
        MAX(CASE WHEN cs.grp = 'masld'   THEN 1 ELSE 0 END) AS has_masld,
        MAX(CASE WHEN cs.grp = 'obesity' THEN 1 ELSE 0 END) AS has_obesity,
        MAX(CASE WHEN cs.grp = 'htn'     THEN 1 ELSE 0 END) AS has_htn,
        MAX(CASE WHEN cs.grp = 'hld'     THEN 1 ELSE 0 END) AS has_hld
    FROM condition_occurrence co
    JOIN concept_sets cs ON cs.concept_id = co.condition_concept_id
    WHERE co.condition_start_date <= getvariable('index_date')
    GROUP BY co.person_id
),
-- v2: latest BMI in the 2 years up to index
latest_bmi AS (
    SELECT person_id, value_as_number AS bmi
    FROM (
        SELECT m.person_id, m.value_as_number,
               ROW_NUMBER() OVER (PARTITION BY m.person_id
                                  ORDER BY m.measurement_date DESC, m.measurement_id DESC) AS newest_rank
        FROM measurement m
        JOIN concept_sets cs ON cs.concept_id = m.measurement_concept_id AND cs.grp = 'bmi'
        WHERE m.measurement_date BETWEEN CAST(getvariable('index_date') - INTERVAL 2 YEAR AS DATE) AND getvariable('index_date')
          AND m.value_as_number > 0
    ) x
    WHERE newest_rank = 1
),
-- v2: obesity from BMI or from the observation table
obesity_other AS (
    SELECT person_id FROM latest_bmi WHERE bmi >= 30
    UNION
    SELECT o.person_id
    FROM observation o
    JOIN concept_sets cs ON cs.concept_id = o.observation_concept_id AND cs.grp = 'obesity_obs'
    WHERE o.observation_date <= getvariable('index_date')
)
SELECT
    a.person_id,
    a.gender_concept_id,
    a.age,
    CASE
        WHEN r.has_t2dm = 1                                         THEN 1
        WHEN r.has_masld = 1                                        THEN 1
        WHEN (r.has_obesity = 1 OR ob.person_id IS NOT NULL)         -- v2
             AND (r.has_htn = 1 OR r.has_hld = 1)                     THEN 1
        ELSE 0
    END AS at_risk
FROM active_adults a
LEFT JOIN risk_factors r ON r.person_id = a.person_id
LEFT JOIN obesity_other ob ON ob.person_id = a.person_id;           -- v2


DROP TABLE IF EXISTS fib4;

CREATE TEMP TABLE fib4 AS
WITH labs_in_window AS (
    SELECT
        m.person_id,
        cs.grp AS lab,
        m.value_as_number AS value,
        ROW_NUMBER() OVER (
            PARTITION BY m.person_id, cs.grp
            ORDER BY m.measurement_date DESC, m.measurement_id DESC
        ) AS newest_rank
    FROM measurement m
    JOIN concept_sets cs ON cs.concept_id = m.measurement_concept_id
    WHERE cs.grp IN ('ast', 'alt', 'plt')
      AND m.measurement_date BETWEEN CAST(getvariable('index_date') - INTERVAL 1 YEAR AS DATE) AND getvariable('index_date')
      AND m.value_as_number > 0
      AND NOT (cs.grp = 'plt' AND m.value_as_number > 2000)
),
latest_labs AS (
    SELECT
        person_id,
        MAX(CASE WHEN lab = 'ast' THEN value END) AS ast,
        MAX(CASE WHEN lab = 'alt' THEN value END) AS alt,
        MAX(CASE WHEN lab = 'plt' THEN value END) AS platelets
    FROM labs_in_window
    WHERE newest_rank = 1
    GROUP BY person_id
),
scores AS (
    SELECT
        c.person_id,
        c.age,
        (c.age * l.ast) / (l.platelets * SQRT(l.alt)) AS fib4
    FROM cohort c
    LEFT JOIN latest_labs l ON l.person_id = c.person_id
    WHERE c.at_risk = 1
)
SELECT
    person_id,
    fib4,
    CASE
        WHEN fib4 IS NULL             THEN '0 Missing labs'
        WHEN fib4 > 2.67              THEN '3 High'
        WHEN age >= 65 AND fib4 < 2.0 THEN '1 Low'
        WHEN age <  65 AND fib4 < 1.3 THEN '1 Low'
        ELSE                               '2 Indeterminate'
    END AS fib4_category
FROM scores;


-- Query 1: At-risk prevalence

SELECT
    COUNT(*)                                                 AS active_adults,
    SUM(at_risk)                                             AS at_risk,
    CAST(100.0 * SUM(at_risk) / COUNT(*) AS DECIMAL(5, 1))   AS pct_at_risk
FROM cohort;


-- Query 2: FIB-4 split among at-risk

SELECT
    fib4_category,
    COUNT(*)                                                           AS n,
    CAST(100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS DECIMAL(5, 1))    AS pct_of_at_risk
FROM fib4
GROUP BY fib4_category
ORDER BY fib4_category;


-- Query 3: By sex and age group

WITH labeled AS (
    SELECT
        c.at_risk,
        f.fib4_category,
        CASE c.gender_concept_id
            WHEN 8507 THEN 'Male'
            WHEN 8532 THEN 'Female'
            ELSE 'Other/Unknown'
        END AS sex,
        CASE
            WHEN c.age < 40 THEN '18-39'
            WHEN c.age < 65 THEN '40-64'
            ELSE '65+'
        END AS age_group
    FROM cohort c
    LEFT JOIN fib4 f ON f.person_id = c.person_id
)
SELECT
    sex,
    age_group,
    COUNT(*)                                                                AS active_adults,
    SUM(at_risk)                                                            AS at_risk,
    CAST(100.0 * SUM(at_risk) / COUNT(*) AS DECIMAL(5, 1))                  AS pct_at_risk,
    SUM(CASE WHEN fib4_category = '0 Missing labs'  THEN 1 ELSE 0 END)      AS fib4_missing,
    SUM(CASE WHEN fib4_category = '1 Low'           THEN 1 ELSE 0 END)      AS fib4_low,
    SUM(CASE WHEN fib4_category = '2 Indeterminate' THEN 1 ELSE 0 END)      AS fib4_indeterminate,
    SUM(CASE WHEN fib4_category = '3 High'          THEN 1 ELSE 0 END)      AS fib4_high
FROM labeled
GROUP BY sex, age_group
ORDER BY sex, age_group;
