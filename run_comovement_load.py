"""One-off script to load CRSP co-movement edges into the Blind Spot Neo4j graph."""
import logging, os
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("/Users/rsouthward/Developer/fbrain/.env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from neo4j import GraphDatabase
import wrds

from blind_spot.comovement_loader import load_comovement


def read_pgpass(username, hostname="wrds-pgdata.wharton.upenn.edu", port=9737, dbname="wrds"):
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

stats = load_comovement(
    as_of=date(2023, 12, 31),
    wrds_conn=conn,
    driver=driver,
    window_days=252,
    min_obs=120,
    min_partial_corr=0.30,
    database=os.getenv("NEO4J_DATABASE", "neo4j"),
)

print("\n=== CRSP co-movement load complete ===")
for k, v in stats.items():
    print(f"  {k}: {v:,}")

conn.close()
driver.close()
