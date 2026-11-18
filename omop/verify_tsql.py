"""Check a SQL Server query file without a SQL Server.

  1. Parse every statement with sqlglot's T-SQL dialect.
  2. Rewrite the T-SQL into DuckDB mechanically (DECLARE, #temp tables, SELECT ... INTO,
     DATEADD, @index_date), run it on omop.duckdb with index date 2024-01-01, and compare
     with the saved results of the hand-written DuckDB version (from run_queries.py).

Usage:
    omop/.venv/bin/python omop/verify_tsql.py queries/masld_at_risk_queries_v3.sqlserver.sql v3_
    (second argument: the result prefix run_queries.py used for the DuckDB version)
"""
import csv
import os
import re
import sys

import duckdb
import sqlglot

from run_queries import DATABASE, RESULT_NAMES, RESULTS, as_text, run_statements, split_statements

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_DATE = "2024-01-01"


def tsql_to_duckdb(tsql):
    """Rewrite the T-SQL constructs used in the query files; return DuckDB statements."""
    sql = re.sub(r"DECLARE @index_date DATE = '[\d-]+';",
                 f"SET VARIABLE index_date = DATE '{INDEX_DATE}';", tsql)
    sql = re.sub(r"IF OBJECT_ID\('tempdb\.\.#(\w+)'\) IS NOT NULL DROP TABLE #\w+;",
                 r"DROP TABLE IF EXISTS \1;", sql)
    sql = re.sub(r"DATEADD\(year, -(\d+), @index_date\)",
                 r"CAST(getvariable('index_date') - INTERVAL \1 YEAR AS DATE)", sql)
    sql = sql.replace("@index_date", "getvariable('index_date')")

    statements = []
    for statement in split_statements(sql):
        select_into = re.search(r"\bINTO #(\w+)\b", statement)
        if select_into and not statement.upper().startswith("INSERT"):
            # SELECT ... INTO #t FROM ...  ->  CREATE TEMP TABLE t AS SELECT ... FROM ...
            statement = (f"CREATE TEMP TABLE {select_into.group(1)} AS "
                         + statement.replace(select_into.group(0), "", 1))
        statements.append(statement.replace("#", ""))
    return statements


def saved_results(prefix):
    tables = []
    for name in RESULT_NAMES:
        with open(os.path.join(RESULTS, f"{prefix}{name}.csv"), encoding="utf-8") as f:
            tables.append([tuple(row) for row in csv.reader(f)][1:])  # skip header
    return tables


def main():
    with open(os.path.join(HERE, sys.argv[1]), encoding="utf-8") as f:
        tsql = f.read()
    prefix = sys.argv[2] if len(sys.argv) > 2 else ""

    parsed = [s for s in sqlglot.parse(tsql, read="tsql") if s is not None]
    print("T-SQL statements parsed:", len(parsed))

    con = duckdb.connect(DATABASE, read_only=True)
    got = [[tuple(as_text(v) for v in row) for row in rows]
           for _, rows in run_statements(con, tsql_to_duckdb(tsql))]
    identical = got == saved_results(prefix)
    print("results identical to DuckDB version:", identical)
    sys.exit(0 if identical else 1)


if __name__ == "__main__":
    main()
