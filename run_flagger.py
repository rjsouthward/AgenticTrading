"""One-off script to run expansion + flagger against live data."""
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
from blind_spot.flagger import pull_seeds_from_fbrain, flag_blind_spots


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

db    = os.getenv("NEO4J_DATABASE", "neo4j")
as_of = date(2023, 12, 29)

# 1. Pull universe from Neo4j
with driver.session(database=db) as s:
    result = s.run("MATCH (n:Security) RETURN n.canonical_id AS cid")
    universe = [row["cid"] for row in result if row["cid"]]
print(f"\nUniverse: {len(universe)} :Security nodes")

# 2. Lane B candidates
candidates = generate(universe=universe, as_of=as_of, wrds_conn=conn, window_days=252)
print(f"Candidates: {len(candidates)} generated")

# 3. Pull seeds from fbrain pages
seeds = pull_seeds_from_fbrain(driver, as_of=as_of, wrds_conn=conn, database=db)
print(f"Seeds: {len(seeds)} from fbrain pages")
for s in seeds[:10]:
    print(f"  {s.canonical_id:<20} weight={s.weight:.4f}  slug={s.slug}")

# 4. Analyst list (empty for demo — replace with actual resolved list)
a_final: set[str] = set()

# 5. Flag blind spots
flags = flag_blind_spots(
    candidates=candidates,
    a_final=a_final,
    seeds=seeds,
    driver=driver,
    k=20,
    d_e=2,
    database=db,
)

print(f"\n=== Top {len(flags)} blind spot flags as of {as_of} ===")
for i, f in enumerate(flags, 1):
    path_str = " → ".join(f.entity_path) if f.entity_path else "n/a"
    print(f"\n  {i}. {f.canonical_id}")
    print(f"     salience={f.salience:.4f}  entity_frontier={f.on_entity_frontier}")
    print(f"     reason:  {f.reason}")
    print(f"     path:    {path_str}")

conn.close()
driver.close()
