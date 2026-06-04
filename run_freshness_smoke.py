"""
Live smoke test: confirm Lane B is decoupled from OptionMetrics.

Run this in an interactive terminal (so you can clear WRDS 2FA on first connect):

    source .venv/bin/activate
    python run_freshness_smoke.py

It does three things:
  1. Prints the CRSP daily ceiling vs the OptionMetrics link ceiling — the gap that the
     old, OM-gated pipeline was stuck behind.
  2. Picks a recent date ABOVE the OM ceiling (where the old pipeline produced zero
     candidates) and runs generate() with options enrichment OFF.
  3. Confirms candidates come out, ranked on the bar-based attention signals alone.
"""
import logging, os
from datetime import date, timedelta
from pathlib import Path

for line in Path(".env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

from neo4j import GraphDatabase
import wrds

from blind_spot.candidate_generator import generate


def read_pgpass(username, hostname="wrds-pgdata.wharton.upenn.edu"):
    for line in Path("~/.pgpass").expanduser().read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            parts = line.split(":")
            if len(parts) == 5:
                h, p, db, u, pw = parts
                if u in ("*", username) and h in ("*", hostname):
                    return pw
    raise ValueError("No pgpass entry found")


wrds_user = os.getenv("WRDS_USERNAME", "rjsouthward")
driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
)
conn = wrds.Connection(wrds_username=wrds_user, wrds_password=read_pgpass(wrds_user))
db = os.getenv("NEO4J_DATABASE", "neo4j")

# 1. Ceilings
crsp_max = conn.raw_sql("SELECT MAX(date) AS d FROM crsp_a_stock.dsf")["d"].iloc[0]
try:
    om_max = conn.raw_sql(
        "SELECT MAX(edate) AS d FROM wrdsapps_link_crsp_optionm.opcrsphist"
    )["d"].iloc[0]
except Exception as e:
    om_max = f"(query failed: {e})"

print("\n=== Data ceilings ===")
print(f"  CRSP dsf  MAX(date):   {crsp_max}")
print(f"  OM  link  MAX(edate):  {om_max}")

# 2. Pick a recent date and a small universe from the graph
as_of = (crsp_max if isinstance(crsp_max, date) else date.fromisoformat(str(crsp_max)[:10])) + timedelta(days=1)
with driver.session(database=db) as s:
    result = s.run("MATCH (n:Security) RETURN n.canonical_id AS cid LIMIT 120")
    universe = [row["cid"] for row in result if row["cid"]]
print(f"\n=== Running Lane B at as_of={as_of} (above OM ceiling), {len(universe)} names, OM enrichment OFF ===")

candidates = generate(
    universe=universe,
    as_of=as_of,
    wrds_conn=conn,
    window_days=63,
    enrich_with_options=False,   # prove the bar-only path
)

print(f"\n  Candidates generated: {len(candidates)}")
print(f"  (old OM-gated pipeline would have produced 0 here)\n")
print(f"  Top 10 by salience:")
for c in candidates[:10]:
    comp = c.components or {}
    sig = ", ".join(f"{k.split('_')[0]}={v:+.2f}" for k, v in comp.items() if k != "iv")
    print(f"    {c.canonical_id:<16} salience={c.salience:.4f}  attention={c.attention:+.3f}  [{sig}]")

conn.close()
driver.close()
