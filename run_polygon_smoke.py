"""
Live smoke: Lane B running on Polygon.io bars — no WRDS required after bootstrapping.

Usage (interactive terminal so WRDS 2FA can clear on first run):

    source .venv/bin/activate
    python run_polygon_smoke.py

Requires POLYGON_API_KEY in .env.

What it does
------------
1. Bootstraps permno→ticker from CRSP (one-time; WRDS required here).
2. Runs Lane B via PolygonBarSource at yesterday's date — the production flow.
3. Prints the top 10 candidates with per-signal component breakdown.

Once the ticker map is bootstrapped (step 1), everything after it runs without
WRDS. In production, cache the map as JSON and skip the CRSP call entirely.
"""
import json, logging, os
from datetime import date, timedelta
from pathlib import Path

for line in Path(".env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from neo4j import GraphDatabase
import wrds

from blind_spot.candidate_generator import generate
from blind_spot.market_data import PolygonBarSource, build_permno_ticker_map


def read_pgpass(username, hostname="wrds-pgdata.wharton.upenn.edu"):
    for line in Path("~/.pgpass").expanduser().read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            parts = line.split(":")
            if len(parts) == 5:
                h, p, db, u, pw = parts
                if u in ("*", username) and h in ("*", hostname):
                    return pw
    raise ValueError("No pgpass entry found")


POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
if not POLYGON_API_KEY:
    raise SystemExit("Set POLYGON_API_KEY in .env")

TICKER_MAP_CACHE = Path("data/permno_ticker_map.json")

driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
)
db = os.getenv("NEO4J_DATABASE", "neo4j")

# --- 1. Universe from graph --------------------------------------------------
with driver.session(database=db) as s:
    result = s.run("MATCH (n:Security) RETURN n.canonical_id AS cid LIMIT 200")
    universe = [row["cid"] for row in result if row["cid"]]
print(f"\nUniverse: {len(universe)} :Security nodes")

# --- 2. Permno → ticker map (cached) ----------------------------------------
# Bootstrap at the CRSP ceiling, not today() — the stocknames PIT filter would
# return zero rows for any date beyond the WRDS data window.
if TICKER_MAP_CACHE.exists() and TICKER_MAP_CACHE.stat().st_size > 2:
    raw_map = json.loads(TICKER_MAP_CACHE.read_text())
    permno_to_ticker = {int(k): v for k, v in raw_map.items()}
    print(f"Loaded ticker map from cache ({len(permno_to_ticker)} entries)")
else:
    print("Bootstrapping permno→ticker from CRSP (requires WRDS)…")
    wrds_user = os.getenv("WRDS_USERNAME", "rjsouthward")
    conn = wrds.Connection(wrds_username=wrds_user, wrds_password=read_pgpass(wrds_user))
    crsp_max = conn.raw_sql("SELECT MAX(date) AS d FROM crsp_a_stock.dsf")["d"].iloc[0]
    bootstrap_as_of = (crsp_max if isinstance(crsp_max, date)
                       else date.fromisoformat(str(crsp_max)[:10]))
    print(f"  Using CRSP ceiling {bootstrap_as_of} for the PIT stocknames query")
    permnos = [int(cid.split(":")[1]) for cid in universe if cid.startswith("permno:")]
    permno_to_ticker = build_permno_ticker_map(permnos, bootstrap_as_of, conn)
    conn.close()
    TICKER_MAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TICKER_MAP_CACHE.write_text(json.dumps(permno_to_ticker))
    print(f"Cached {len(permno_to_ticker)} mappings to {TICKER_MAP_CACHE}")

# --- 3. Lane B via Polygon (no WRDS) ----------------------------------------
# as_of = T0. The bar source returns bars with date <= T0 - 1, so the last bar
# is yesterday's close. Running as_of = today is fine; default_window handles
# the "strictly before T0" offset for us.
as_of = date.today()
print(f"\n=== Lane B at as_of={as_of} (Polygon bars, OM enrichment OFF) ===")

bar_source = PolygonBarSource(
    api_key=POLYGON_API_KEY,
    permno_to_ticker=permno_to_ticker,
    max_workers=8,        # parallel fetch threads
    rate_per_min=100,     # Polygon Starter tier ceiling
)

candidates = generate(
    universe=universe,
    as_of=as_of,
    bar_source=bar_source,
    wrds_conn=None,            # no WRDS needed for bar-based salience
    window_days=63,
    enrich_with_options=False,
)

print(f"\nCandidates: {len(candidates)}")
print(f"(WRDS-gated pipeline on this date would have produced 0)\n")
print("Top 10 by salience:")
for c in candidates[:10]:
    comp = c.components or {}
    sig = "  ".join(
        f"{k.split('_')[0]}={v:+.2f}"
        for k, v in comp.items() if k != "iv"
    )
    print(f"  {c.canonical_id:<16} salience={c.salience:.4f}  attn={c.attention:+.3f}  [{sig}]")

driver.close()
