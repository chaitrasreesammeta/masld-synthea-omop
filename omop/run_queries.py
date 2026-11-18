"""Run a DuckDB query file against omop/omop.duckdb and save the three result tables as CSV.

The query files build temp tables and end with three SELECTs (prevalence, FIB-4 split,
by sex and age). Their results go to omop/results/<prefix>q1_prevalence.csv,
<prefix>q2_fib4_split.csv and <prefix>q3_by_sex_age.csv.

Usage:
    omop/.venv/bin/python omop/run_queries.py                                                # original
    omop/.venv/bin/python omop/run_queries.py queries/masld_at_risk_queries_v3.duckdb.sql v3_
"""
import os
import re
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(HERE, "omop.duckdb")
RESULTS = os.path.join(HERE, "results")
DEFAULT_QUERY_FILE = os.path.join("queries", "masld_at_risk_queries.duckdb.sql")
RESULT_NAMES = ("q1_prevalence", "q2_fib4_split", "q3_by_sex_age")


def split_statements(sql):
    """Drop '--' comments, then split on ';'. (The query files have no ';' or '--' inside strings.)"""
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in without_comments.split(";") if s.strip()]


def run_statements(con, statements):
    """Run every statement; return (columns, rows) for each top-level query (SELECT or WITH ...)."""
    results = []
    for statement in statements:
        relation = con.sql(statement)
        if relation is not None and re.match(r"(?i)(SELECT|WITH)\b", statement):
            results.append((relation.columns, relation.fetchall()))
    return results


def as_text(value):
    return "" if value is None else str(value)


def save_csv(path, columns, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(columns) + "\n")
        for row in rows:
            f.write(",".join(as_text(v) for v in row) + "\n")


def main():
    query_file = os.path.join(HERE, sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY_FILE)
    prefix = sys.argv[2] if len(sys.argv) > 2 else ""
    with open(query_file, encoding="utf-8") as f:
        statements = split_statements(f.read())

    con = duckdb.connect(DATABASE, read_only=True)
    results = run_statements(con, statements)
    assert len(results) == len(RESULT_NAMES), f"expected 3 result tables, got {len(results)}"

    os.makedirs(RESULTS, exist_ok=True)
    for name, (columns, rows) in zip(RESULT_NAMES, results):
        save_csv(os.path.join(RESULTS, f"{prefix}{name}.csv"), columns, rows)
        print(f"== {prefix}{name}")
        print("  " + " | ".join(columns))
        for row in rows:
            print("  " + " | ".join(str(v) for v in row))


if __name__ == "__main__":
    main()
