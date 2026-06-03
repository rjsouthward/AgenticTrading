"""One-off script to run the Lane B candidate generator against a live OptionMetrics snapshot."""
import logging, os
from datetime import date
from pathlib import Path

for line in Path(".env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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


def _get_universe_from_graph(driver, database):
    """Pull all Security node canonical IDs from Neo4j."""
    with driver.session(database=database) as s:
        result = s.run("MATCH (n:Security) RETURN n.canonical_id AS cid")
        return [row["cid"] for row in result if row["cid"]]


wrds_user = os.getenv("WRDS_USERNAME", "rjsouthward")
driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
)
conn = wrds.Connection(wrds_username=wrds_user, wrds_password=read_pgpass(wrds_user))

db       = os.getenv("NEO4J_DATABASE", "neo4j")
as_of    = date(2023, 12, 29)   # last trading day of 2023

universe = _get_universe_from_graph(driver, db)
print(f"\nUniverse: {len(universe)} Security nodes from Neo4j")

candidates = generate(
    universe=universe,
    as_of=as_of,
    wrds_conn=conn,
    window_days=252,
)

print(f"\n=== Lane B candidates as of {as_of} ===")
print(f"  Total candidates: {len(candidates)}")
print(f"  With OM coverage: {sum(1 for c in candidates if c.coverage)}")
print(f"  Straddle measure: {sum(1 for c in candidates if c.measure == 'straddle')}")
print(f"  IV-rank measure:  {sum(1 for c in candidates if c.measure == 'iv_rank')}")
print(f"\n  Top 20 by salience:")
for c in candidates[:20]:
    iv_str = f"iv_rank={c.iv_rank:.3f}" if c.iv_rank is not None else "iv_rank=n/a"
    mv_str = f"implied_move={c.implied_move:.3f}" if c.implied_move is not None else ""
    print(f"    {c.canonical_id:<20} salience={c.salience:.4f}  {iv_str}  {mv_str}  [{c.measure}]")

conn.close()
driver.close()
