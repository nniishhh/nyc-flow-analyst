#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1"]
# ///
"""The NYC Flow analyst's one tool: run queries, log them, and build answer cards.

Every query is appended to trace.jsonl in the cache, so the answer card can show its
work: the endpoint URL, the exact query, the row count, the time it took, and a
preview of the rows.

Commands:
  soql "<SoQL>" --save NAME   Send SoQL to the MTA origin-destination endpoint on
                              data.ny.gov. The server aggregates the ~80M trip rows;
                              the result is saved as local table NAME.
  sql "<SQL>" [--save NAME]   Run DuckDB SQL over saved tables plus the lookups
                              `stations` and `neighborhoods`. SELECT/WITH only.
  card SPEC.json              Build the answer card from a spec and the trace
                              (spec format in build_card below).
  neighborhood NAME           List the station complexes inside a neighborhood.
  tables                      List saved tables and their columns.
  trace [--json] [--clear]    Print the run log, or start a fresh one.

Macros in soql and sql, expanded before the query runs (the expanded query is logged):
  {{neighborhood:Williamsburg}}   that neighborhood's station ids, for
                                  origin_station_complex_id IN (...)
  {{month_count}}                 the number of months published, for averaging

Results, the trace, and built cards live in ~/.cache/nyc-flow-analyst
(override with NYC_FLOW_CACHE), so the skill folder itself stays read-only.

Usage:
  uv run nyc.py soql "SELECT ... LIMIT 500" --save sat_trips --label "Saturday trips"
  uv run nyc.py sql "SELECT ... FROM sat_trips t JOIN stations s ..." --label "By neighborhood"
  uv run nyc.py card ~/.cache/nyc-flow-analyst/spec.json
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import duckdb

SKILL = Path(__file__).resolve().parent
STATIONS_CSV = SKILL / "stations.csv"
TEMPLATE = SKILL / "card.html"
CACHE = Path(os.environ.get("NYC_FLOW_CACHE", Path.home() / ".cache" / "nyc-flow-analyst"))
DB_PATH = CACHE / "nyc_flow.duckdb"
TRACE = CACHE / "trace.jsonl"
CARD = CACHE / "card.html"

DATASET_ID = "28vm-gjqr"
ENDPOINT = f"https://data.ny.gov/resource/{DATASET_ID}.json"
DATASET_PAGE = f"https://data.ny.gov/d/{DATASET_ID}"
DATASET_TITLE = "MTA Subway Origin-Destination Ridership Estimate: Beginning 2026"

PREVIEW_ROWS = 20
CARD_PREVIEW_ROWS = 5
DEFAULT_SOQL_LIMIT = 1000
MAX_MAP_FLOWS = 40
LOOKUPS = ("stations", "neighborhoods")
TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
NEIGHBORHOOD_MACRO = re.compile(r"\{\{\s*neighborhood\s*:\s*([^}]+?)\s*\}\}")


def fail(message: str) -> None:
    sys.exit(message)


# ── Database and lookups ─────────────────────────────────────────────────────

def connect() -> duckdb.DuckDBPyConnection:
    CACHE.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(f"""
        CREATE OR REPLACE VIEW stations AS
        SELECT station_id, station, borough, latitude, longitude, neighborhood
        FROM read_csv_auto('{STATIONS_CSV}')""")
    con.execute(f"""
        CREATE OR REPLACE VIEW neighborhoods AS
        SELECT neighborhood, mode(borough) AS borough,
               any_value(neighborhood_latitude) AS latitude,
               any_value(neighborhood_longitude) AS longitude,
               count(*) AS stations
        FROM read_csv_auto('{STATIONS_CSV}') GROUP BY neighborhood""")
    return con


def normalize(name: str) -> str:
    """Match names regardless of case, spacing, or en/em dashes."""
    name = name.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", name).strip().lower()


def station_ids(con: duckdb.DuckDBPyConnection, neighborhood: str) -> list[int]:
    ids: dict[str, list[int]] = {}
    labels: dict[str, str] = {}
    for name, station_id in con.execute("SELECT neighborhood, station_id FROM stations").fetchall():
        ids.setdefault(normalize(name), []).append(int(station_id))
        labels[normalize(name)] = name

    key = normalize(neighborhood)
    if key not in ids:
        close = difflib.get_close_matches(key, ids, n=5, cutoff=0.5)
        hint = ", ".join(labels[c] for c in close) or 'run: nyc.py sql "SELECT neighborhood FROM neighborhoods"'
        fail(f"Unknown neighborhood {neighborhood!r}. Did you mean: {hint}")
    return sorted(ids[key])


def months_available() -> list[int]:
    """Months published so far, cached for a day because MTA adds one a month."""
    cache = CACHE / "months.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        return json.loads(cache.read_text())
    query = urllib.parse.quote("SELECT month GROUP BY month ORDER BY month")
    with urllib.request.urlopen(f"{ENDPOINT}?$query={query}", timeout=120) as response:
        months = [int(row["month"]) for row in json.load(response)]
    cache.write_text(json.dumps(months))
    return months


def expand_macros(con: duckdb.DuckDBPyConnection, soql: str) -> str:
    def neighborhood_ids(match: re.Match) -> str:
        return ", ".join(str(i) for i in station_ids(con, match.group(1)))

    soql = NEIGHBORHOOD_MACRO.sub(neighborhood_ids, soql)
    if "{{month_count}}" in soql:
        soql = soql.replace("{{month_count}}", str(len(months_available())))
    return soql


# ── Values and printing ──────────────────────────────────────────────────────

def coerce(value):
    """Socrata returns every value as a string; turn numbers back into numbers."""
    if isinstance(value, str):
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        if re.fullmatch(r"-?\d*\.\d+", value):
            return round(float(value), 4)
    if isinstance(value, float):
        return round(value, 4)
    return value


def format_cell(value) -> str:
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}" if abs(value) >= 10000 else str(value)
    return "" if value is None else str(value)


def format_table(columns: list[str], rows: list[dict], limit: int) -> str:
    if not rows:
        return "  (no rows)"
    cells = [[format_cell(row.get(c)) for c in columns] for row in rows[:limit]]
    widths = [min(48, max(len(c), *(len(r[i]) for r in cells))) for i, c in enumerate(columns)]

    def line(values: list[str]) -> str:
        return "  " + "  ".join(v[:w].ljust(w) for v, w in zip(values, widths))

    lines = [line(columns), line(["─" * w for w in widths]), *(line(r) for r in cells)]
    if len(rows) > limit:
        lines.append(f"  … {len(rows) - limit} more rows")
    return "\n".join(lines)


def print_step(entry: dict, rows: list[dict], show: int) -> None:
    label = f" · {entry['label']}" if entry.get("label") else ""
    print(f"step {entry['step']} · {entry['mode']} · {entry['rows']} rows · {entry['elapsed_s']} s{label}")
    if entry.get("url"):
        print(f"endpoint: {entry['endpoint']}")
        print(f"url:      {entry['url']}")
    print("query:")
    print("\n".join("  " + line for line in entry["query"].strip().splitlines()))
    if entry.get("saved_as"):
        print(f"saved as: {entry['saved_as']}")
    print("rows:")
    print(format_table(entry["columns"], rows, show))


# ── Trace ────────────────────────────────────────────────────────────────────

def read_trace() -> list[dict]:
    if not TRACE.exists():
        return []
    return [json.loads(line) for line in TRACE.read_text().splitlines() if line.strip()]


def log_step(entry: dict) -> dict:
    entry = {
        "step": len(read_trace()) + 1,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **entry,
    }
    with open(TRACE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


# ── Queries ──────────────────────────────────────────────────────────────────

def check_table_name(name: str | None) -> None:
    if name and (not TABLE_NAME.match(name) or name in LOOKUPS):
        fail(f"--save takes lowercase letters, digits and underscores, and not a lookup name: {name!r}")


def save_rows(con: duckdb.DuckDBPyConnection, name: str, rows: list[dict]) -> None:
    raw = CACHE / "raw"
    raw.mkdir(exist_ok=True)
    path = raw / f"{name}.json"
    path.write_text(json.dumps(rows))
    con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM read_json_auto('{path}')")


def fetch_soql(soql: str) -> list[dict]:
    url = f"{ENDPOINT}?$query={urllib.parse.quote(soql)}"
    headers = {"User-Agent": "nyc-flow-analyst (github.com/nniishhh/nyc-flow-analyst)", "Accept": "application/json"}
    if os.environ.get("SOCRATA_APP_TOKEN"):
        headers["X-App-Token"] = os.environ["SOCRATA_APP_TOKEN"]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        body = err.read().decode(errors="replace")[:800]
        fail(f"SoQL error {err.code} from {ENDPOINT}\nquery: {soql}\n{body}")


def run_soql(args) -> None:
    check_table_name(args.save)
    con = connect()
    soql = expand_macros(con, args.query)
    if not re.search(r"\bLIMIT\b", soql, re.I):
        soql = f"{soql.rstrip().rstrip(';')} LIMIT {DEFAULT_SOQL_LIMIT}"

    started = time.monotonic()
    payload = fetch_soql(soql)
    elapsed = round(time.monotonic() - started, 2)

    # Point columns arrive as GeoJSON objects; the analysis never needs them.
    rows = [{k: coerce(v) for k, v in row.items() if not isinstance(v, dict)} for row in payload]
    if args.save and rows:
        save_rows(con, args.save, rows)

    entry = log_step({
        "mode": "soql",
        "label": args.label,
        "endpoint": ENDPOINT,
        "dataset": DATASET_PAGE,
        "url": f"{ENDPOINT}?$query={urllib.parse.quote(soql)}",
        "query": soql,
        "rows": len(rows),
        "columns": list(rows[0]) if rows else [],
        "elapsed_s": elapsed,
        "saved_as": args.save if rows else None,
        "preview": rows[:PREVIEW_ROWS],
    })
    print_step(entry, rows, args.show)


def run_sql(args) -> None:
    check_table_name(args.save)
    single_statement = ";" not in args.query.strip().rstrip(";")
    if not re.match(r"^\s*(SELECT|WITH)\b", args.query, re.I) or not single_statement:
        fail("sql accepts a single SELECT or WITH query.")

    con = connect()
    query = expand_macros(con, args.query)
    started = time.monotonic()
    try:
        result = con.execute(query)
        columns = [d[0] for d in result.description]
        rows = [{c: coerce(v) for c, v in zip(columns, record)} for record in result.fetchall()]
    except duckdb.Error as err:
        fail(f"SQL error: {err}\nquery: {query}")
    elapsed = round(time.monotonic() - started, 2)

    if args.save and rows:
        save_rows(con, args.save, rows)

    entry = log_step({
        "mode": "sql",
        "label": args.label,
        "engine": "DuckDB (local)",
        "query": query.strip(),
        "rows": len(rows),
        "columns": columns,
        "elapsed_s": elapsed,
        "saved_as": args.save if rows else None,
        "preview": rows[:PREVIEW_ROWS],
    })
    print_step(entry, rows, args.show)


# ── Answer card ──────────────────────────────────────────────────────────────
#
# Spec (JSON); give `map`, `chart`, or both:
#   {
#     "headline": "63% more leave Williamsburg on Saturday",   the insight, under 80 characters
#     "subhead":  "Trips starting noon to 6 p.m., average day, Jan–Aug 2026",
#     "kpis":     [{"label": "Saturday riders", "value": "20,100"}],   0–3, optional
#     "map": {
#       "place":     "Williamsburg",                a neighborhood name from stations.csv
#       "direction": "departures" | "arrivals",
#       "series":    ["Saturday", "Wednesday"],     1–4 names; buttons switch between them
#       "flows":     [{"neighborhood": "Chelsea", "values": [1863, 910]}, ...],   up to 40; top 12 drawn
#       "totals":    [20077, 12336],                optional, all riders per series, for "% of trips"
#       "note":      "Arc width = riders"           optional
#     },
#     "chart": {
#       "type": "hbar" | "bar" | "line",
#       "labels": [...], "series": [{"name": "Saturday", "values": [...]}],   1–4 series
#       "unit": "riders", "emphasis": [0], "xTitle": "", "yTitle": ""        optional
#     },
#     "caveat":    "Modeled MTA estimates; destinations are inferred.",
#     "followups": ["Compare with a Wednesday", "Show it hour by hour"],
#     "steps":     [1, 2]                           optional; default is every logged step
#   }

def card_steps(wanted: list[int] | None) -> list[dict]:
    steps = read_trace()
    if wanted:
        steps = [s for s in steps if s["step"] in set(wanted)]
    if not steps:
        fail("No logged queries. Run soql or sql first; the card only shows queries that actually ran.")
    fields = ("mode", "label", "endpoint", "dataset", "url", "engine", "query", "rows", "columns", "elapsed_s")
    return [{**{k: s.get(k) for k in fields}, "preview": s.get("preview", [])[:CARD_PREVIEW_ROWS]} for s in steps]


def card_map(spec: dict) -> dict:
    with open(STATIONS_CSV) as f:
        stations = list(csv.DictReader(f))
    centers = {s["neighborhood"]: (float(s["neighborhood_latitude"]), float(s["neighborhood_longitude"])) for s in stations}

    def locate(name: str) -> dict:
        if name not in centers:
            fail(f"Unknown neighborhood {name!r} in the map spec; use a name from stations.csv.")
        lat, lon = centers[name]
        return {"name": name, "lat": lat, "lon": lon}

    flows = []
    for flow in spec["flows"][:MAX_MAP_FLOWS]:
        if len(flow["values"]) != len(spec["series"]):
            fail(f"Map flow {flow['neighborhood']!r} has {len(flow['values'])} values for {len(spec['series'])} series.")
        if flow["neighborhood"] != spec["place"]:  # trips within the same neighborhood have no arc
            flows.append({**locate(flow["neighborhood"]), "values": flow["values"]})

    return {
        "place": locate(spec["place"]),
        "direction": spec.get("direction", "departures"),
        "series": spec["series"],
        "flows": flows,
        "totals": spec.get("totals"),
        "note": spec.get("note", "Arc width = riders"),
        "stations": [[round(float(s["latitude"]), 3), round(float(s["longitude"]), 3)] for s in stations],
    }


def render_card(answer: dict) -> str:
    """Fill the template and drop its comments; the chat streams the card, so lean is faster."""
    html = re.sub(r"<!--[\s\S]*?-->\n?", "", TEMPLATE.read_text())
    start, end = html.index("<style>"), html.index("</style>")
    style = re.sub(r"\n{2,}", "\n", re.sub(r"/\*[\s\S]*?\*/", "", html[start:end]))
    html = html[:start] + style + html[end:]
    lines = [re.sub(r"\s{2,}// .*$", "", line) for line in html.splitlines() if not line.strip().startswith("//")]
    html = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    data = json.dumps(answer, ensure_ascii=False).replace("</", "<\\/")
    return html.replace("/*ANSWER*/null", data)


def run_card(args) -> None:
    spec = json.loads(Path(args.spec).read_text())
    if "chart" not in spec and "map" not in spec:
        fail("The spec needs a chart, a map, or both.")
    for series in spec.get("chart", {}).get("series", []):
        if len(series["values"]) != len(spec["chart"]["labels"]):
            fail(f"Chart series {series['name']!r} has {len(series['values'])} values for {len(spec['chart']['labels'])} labels.")

    answer = {
        "headline": spec["headline"],
        "subhead": spec.get("subhead", ""),
        "kpis": spec.get("kpis", [])[:3],
        "map": card_map(spec["map"]) if "map" in spec else None,
        "chart": spec.get("chart"),
        "caveat": spec.get("caveat", ""),
        "followups": spec.get("followups", [])[:3],
        "source": {"title": DATASET_TITLE, "url": DATASET_PAGE},
        "steps": card_steps(spec.get("steps")),
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    CARD.write_text(render_card(answer))
    print(CARD)


# ── Other commands ───────────────────────────────────────────────────────────

def run_neighborhood(args) -> None:
    con = connect()
    ids = ", ".join(map(str, station_ids(con, args.name)))
    stations = con.execute(
        f"SELECT station_id, station, borough, neighborhood FROM stations WHERE station_id IN ({ids}) ORDER BY station"
    ).fetchall()
    for station_id, station, borough, neighborhood in stations:
        print(f"  {station_id:>4}  {station}  ({borough}, {neighborhood})")


def run_tables(_args) -> None:
    con = connect()
    tables = con.execute(
        "SELECT table_name, table_type FROM information_schema.tables ORDER BY table_type, table_name"
    ).fetchall()
    for name, table_type in tables:
        columns = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
            [name],
        ).fetchall()
        count = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        kind = "lookup" if table_type == "VIEW" else "saved"
        print(f"{name} ({kind}, {count:,} rows): " + ", ".join(f"{c} {t.lower()}" for c, t in columns))


def run_trace(args) -> None:
    if args.clear:
        TRACE.unlink(missing_ok=True)
        print("trace cleared")
        return
    entries = read_trace()
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=1))
        return
    for e in entries:
        print(f"step {e['step']} · {e['mode']} · {e['rows']} rows · {e['elapsed_s']} s · {e.get('label') or ''}")
        print("  " + (e.get("url") or e.get("engine", "")))
        print("\n".join("    " + line for line in e["query"].splitlines()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("soql", run_soql), ("sql", run_sql)):
        command = commands.add_parser(name)
        command.add_argument("query")
        command.add_argument("--save", help="name to save the result as, for later sql")
        command.add_argument("--label", help="short description shown in the trace and the card")
        command.add_argument("--show", type=int, default=15, help="rows to print")
        command.set_defaults(handler=handler)

    command = commands.add_parser("card")
    command.add_argument("spec")
    command.set_defaults(handler=run_card)

    command = commands.add_parser("neighborhood")
    command.add_argument("name")
    command.set_defaults(handler=run_neighborhood)

    commands.add_parser("tables").set_defaults(handler=run_tables)

    command = commands.add_parser("trace")
    command.add_argument("--json", action="store_true")
    command.add_argument("--clear", action="store_true")
    command.set_defaults(handler=run_trace)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
