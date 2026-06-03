"""One-off script to load TNIC 2023 into the Blind Spot Neo4j graph."""
import logging, os
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("/Users/rsouthward/Developer/fbrain/.env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from neo4j import GraphDatabase
import wrds

from blind_spot.graph_loader import ensure_schema, load_tnic


def _read_pgpass(username: str, hostname: str = "wrds-pgdata.wharton.upenn.edu",
                 port: int = 9737, dbname: str = "wrds") -> str:
    """Read password for the given credentials from ~/.pgpass."""
    pgpass = Path("~/.pgpass").expanduser()
    if not pgpass.exists():
        raise FileNotFoundError("~/.pgpass not found — run the pgpass setup command first")
    for line in pgpass.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 5:
            continue
        h, p, db, u, pw = parts
        if (h in ("*", hostname) and p in ("*", str(port)) and
                db in ("*", dbname) and u in ("*", username)):
            return pw
    raise ValueError(f"No pgpass entry found for {username}@{hostname}:{port}/{dbname}")


wrds_user = os.getenv("WRDS_USERNAME", "rjsouthward")
wrds_pass = _read_pgpass(wrds_user)

driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
)

conn = wrds.Connection(wrds_username=wrds_user, wrds_password=wrds_pass)

ensure_schema(driver, database=os.getenv("NEO4J_DATABASE", "neo4j"))

stats = load_tnic(
    filepath="tnic3_data/tnic3_data.txt",
    vintage_year=2023,
    as_of=date(2023, 12, 31),
    wrds_conn=conn,
    driver=driver,
    min_score=0.01,
    batch_size=500,
    database=os.getenv("NEO4J_DATABASE", "neo4j"),
)

print("\n=== TNIC 2023 load complete ===")
for k, v in stats.items():
    print(f"  {k}: {v:,}")

conn.close()
driver.close()
