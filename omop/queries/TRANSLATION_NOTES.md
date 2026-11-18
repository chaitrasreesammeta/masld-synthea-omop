# SQL Server -> DuckDB translation of masld_at_risk_queries.sql

Source: `~/Desktop/masld_at_risk_queries.sql` (copied as `masld_at_risk_queries.sqlserver.sql`).
Translation: `masld_at_risk_queries.duckdb.sql`. Syntax only; no logic changed.

| # | SQL Server (line) | DuckDB |
|---|---|---|
| 1 | `DECLARE @index_date DATE = '2025-12-31';` (1) | `SET VARIABLE index_date = DATE '2024-01-01';` (value changed to 2024-01-01 on request) |
| 2 | every `@index_date` (37, 39, 43, 48, 61, 93) | `getvariable('index_date')` |
| 3 | `IF OBJECT_ID('tempdb..#concept_sets') IS NOT NULL DROP TABLE #concept_sets;` (3), same for `#cohort` (31), `#fib4` (79) | `DROP TABLE IF EXISTS concept_sets;` / `cohort` / `fib4` |
| 4 | `SELECT ... INTO #concept_sets FROM ...` (5-16) | `CREATE TEMP TABLE concept_sets AS SELECT ... FROM ...` |
| 5 | `WITH ... SELECT ... INTO #cohort FROM ...` (33-76) | `CREATE TEMP TABLE cohort AS WITH ... SELECT ... FROM ...` (INTO clause removed) |
| 6 | `WITH ... SELECT ... INTO #fib4 FROM scores` (81-127) | `CREATE TEMP TABLE fib4 AS WITH ... SELECT ... FROM scores` |
| 7 | `#concept_sets`, `#cohort`, `#fib4` references (18, 60, 91, 112, 136, 145, 166, 167) | `concept_sets`, `cohort`, `fib4` |
| 8 | `DATEADD(year, -3, @index_date)` (43) | `CAST(getvariable('index_date') - INTERVAL 3 YEAR AS DATE)` |
| 9 | `DATEADD(year, -1, @index_date)` (93) | `CAST(getvariable('index_date') - INTERVAL 1 YEAR AS DATE)` |

Unchanged because DuckDB accepts them as written: `YEAR()`, `SQRT()`, `(VALUES ...) AS s(grp, code)`,
`ROW_NUMBER() OVER (...)`, `SUM(COUNT(*)) OVER ()`, `CAST(... AS DECIMAL(5, 1))`, `CASE`, `EXISTS`,
`INSERT INTO ... SELECT`, `BETWEEN` (inclusive in both).

The `CAST(... AS DATE)` in rows 8-9 keeps the comparison date-to-date like SQL Server's `DATEADD` on a DATE;
DuckDB's `DATE - INTERVAL` returns a TIMESTAMP.
