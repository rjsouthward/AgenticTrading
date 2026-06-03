"""
Seed a synthetic FlagSession (+ tearsheets and headlines) into Neo4j for
MCP / artifact testing — no WRDS or Polygon needed.

Usage:
    python -m blind_spot.flag_stream.seed_fake [session_id]
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from neo4j import GraphDatabase

from blind_spot.flagger import Flag
from blind_spot.flag_stream.persistence import persist_flags, persist_tearsheet

load_dotenv()


FAKE_FLAGS = [
    Flag(canonical_id="permno:14593", salience=0.92, on_entity_frontier=True,
         on_thesis_frontier=False,
         entity_path=["permno:10107", "permno:14593"], thesis_path=None,
         reason="named customer-supplier relationship: NVDA Q3'23 10-K supplier list"),
    Flag(canonical_id="permno:20482", salience=0.81, on_entity_frontier=True,
         on_thesis_frontier=False,
         entity_path=["permno:10107", "permno:59328", "permno:20482"], thesis_path=None,
         reason="product-market peer within 2 hops of permno:10107"),
    Flag(canonical_id="permno:65875", salience=0.74, on_entity_frontier=True,
         on_thesis_frontier=False,
         entity_path=["permno:14593", "permno:65875"], thesis_path=None,
         reason="direct product-market peer of permno:14593"),
    Flag(canonical_id="permno:77418", salience=0.66, on_entity_frontier=False,
         on_thesis_frontier=False, entity_path=None, thesis_path=None,
         reason="co-moving with permno:10107 (return-based structural link)"),
    Flag(canonical_id="permno:88123", salience=0.58, on_entity_frontier=True,
         on_thesis_frontier=False,
         entity_path=["permno:10107", "permno:88123"], thesis_path=None,
         reason="customer-supplier relationship in supply chain"),
]


def _ts(hh: int, mm: int) -> str:
    return datetime(2024, 1, 15, hh, mm, 0, tzinfo=timezone.utc).isoformat()


FAKE_TEARSHEETS: dict[str, dict] = {
    "permno:14593": {
        "overview": {
            "ticker": "AVGO", "name": "Broadcom Inc.",
            "sector": "Semiconductors",
            "market_cap": 642_000_000_000, "price": 1376.40,
            "change_abs": 19.85, "change_pct": 1.46,
            "summary": (
                "Designs and supplies semiconductors and infrastructure software. "
                "Custom AI silicon for hyperscalers (notably Google TPU and Meta) is "
                "the principal AI-revenue lever."
            ),
            "fetched_at": "2024-01-15T13:30:00+00:00",
        },
        "headlines": [
            {"rank": 1, "published_at": _ts(13, 12), "source": "Reuters",
             "title": "Broadcom outlines $10B+ AI accelerator pipeline for hyperscale customers",
             "url": "https://example.com/avgo-ai-pipeline",
             "summary": "Custom ASIC bookings widen as Meta scales MTIA deployments."},
            {"rank": 2, "published_at": _ts(11, 48), "source": "Bloomberg",
             "title": "Analysts lift AVGO price targets after VMware integration update",
             "url": "https://example.com/avgo-vmware",
             "summary": "Synergy guidance pulled forward; FCF accretion in FY25 looks intact."},
            {"rank": 3, "published_at": _ts(9, 5), "source": "Briefing.com",
             "title": "AVGO mentioned alongside NVDA in semi cap pre-market commentary",
             "url": "https://example.com/avgo-premkt",
             "summary": "Tape action confirms group leadership intact."},
        ],
    },
    "permno:20482": {
        "overview": {
            "ticker": "AMD", "name": "Advanced Micro Devices, Inc.",
            "sector": "Semiconductors",
            "market_cap": 268_000_000_000, "price": 166.21,
            "change_abs": -2.04, "change_pct": -1.21,
            "summary": (
                "x86 CPUs and GPUs; MI300 ramp targets the AI-accelerator share NVDA "
                "currently dominates. Server CPU share against INTC is the cash engine "
                "while MI300 contribution scales."
            ),
            "fetched_at": "2024-01-15T13:30:00+00:00",
        },
        "headlines": [
            {"rank": 1, "published_at": _ts(12, 56), "source": "CNBC",
             "title": "AMD trims after-hours gains as MI300 backlog commentary cools",
             "url": "https://example.com/amd-mi300",
             "summary": "Channel checks suggest H2 ramp is on track; Street modeling holds."},
            {"rank": 2, "published_at": _ts(10, 22), "source": "Bloomberg",
             "title": "Microsoft confirms expanded MI300X deployment for Azure",
             "url": "https://example.com/amd-azure",
             "summary": "Reinforces AMD as second-source AI accelerator inside top hyperscalers."},
        ],
    },
    "permno:65875": {
        "overview": {
            "ticker": "MRVL", "name": "Marvell Technology, Inc.",
            "sector": "Semiconductors",
            "market_cap": 64_500_000_000, "price": 74.18,
            "change_abs": 0.91, "change_pct": 1.24,
            "summary": (
                "Custom silicon (custom AI ASICs) and electro-optics for AI back-end "
                "interconnect. Inflection 25 trades on AI ASIC + optical DSP share."
            ),
            "fetched_at": "2024-01-15T13:30:00+00:00",
        },
        "headlines": [
            {"rank": 1, "published_at": _ts(11, 30), "source": "Bloomberg",
             "title": "Marvell guides AI revenue to >$2.5B run-rate by end of FY25",
             "url": "https://example.com/mrvl-ai-runrate",
             "summary": "Custom ASIC + 1.6T optical DSP cited as the two demand legs."},
            {"rank": 2, "published_at": _ts(9, 41), "source": "Reuters",
             "title": "MRVL added to AI-derivative basket at large prime broker",
             "url": "https://example.com/mrvl-basket",
             "summary": "Flow desk notes accelerating long-only sponsorship."},
        ],
    },
    "permno:77418": {
        "overview": {
            "ticker": "ARM", "name": "Arm Holdings plc",
            "sector": "Semiconductors",
            "market_cap": 153_000_000_000, "price": 149.05,
            "change_abs": 3.27, "change_pct": 2.24,
            "summary": (
                "Licenses CPU IP across mobile, automotive, and (increasingly) data-center. "
                "v9 royalty mix shift drives the per-chip royalty step-up the model leans on."
            ),
            "fetched_at": "2024-01-15T13:30:00+00:00",
        },
        "headlines": [
            {"rank": 1, "published_at": _ts(12, 4), "source": "FT",
             "title": "Arm reports v9 royalty mix at ~25% of total royalty revenue",
             "url": "https://example.com/arm-v9",
             "summary": "Above Street; reinforces royalty rate step-up thesis."},
            {"rank": 2, "published_at": _ts(8, 50), "source": "Briefing.com",
             "title": "ARM follows AI semis higher in pre-market on positive AVGO read-across",
             "url": "https://example.com/arm-premkt",
             "summary": "Group sentiment positive; high-beta name participating fully."},
        ],
    },
    "permno:88123": {
        "overview": {
            "ticker": "SMCI", "name": "Super Micro Computer, Inc.",
            "sector": "Hardware",
            "market_cap": 38_000_000_000, "price": 658.32,
            "change_abs": 14.05, "change_pct": 2.18,
            "summary": (
                "AI-server systems integrator; first-to-market with new NVDA platforms. "
                "Margin trajectory and customer concentration are the central debates."
            ),
            "fetched_at": "2024-01-15T13:30:00+00:00",
        },
        "headlines": [
            {"rank": 1, "published_at": _ts(13, 1), "source": "Bloomberg",
             "title": "Supermicro raises preliminary Q2 revenue and EPS guidance",
             "url": "https://example.com/smci-preann",
             "summary": "Beats Street on both lines; gross margin commentary still pending."},
            {"rank": 2, "published_at": _ts(10, 17), "source": "Reuters",
             "title": "SMCI cited as primary beneficiary of accelerated H100 → H200 transition",
             "url": "https://example.com/smci-h200",
             "summary": "First-to-market reputation reinforced."},
            {"rank": 3, "published_at": _ts(7, 42), "source": "Briefing.com",
             "title": "Supermicro flagged for unusual options volume ahead of pre-announcement",
             "url": "https://example.com/smci-options",
             "summary": "Term-structure inversion 1-week vs 1-month."},
        ],
    },
}


def main() -> None:
    session_id = sys.argv[1] if len(sys.argv) > 1 else "fake-2024-01-15"

    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
    )
    db = os.getenv("NEO4J_DATABASE", "neo4j")
    try:
        n = persist_flags(
            driver,
            session_id=session_id,
            as_of=date(2024, 1, 15),
            flags=FAKE_FLAGS,
            k=20,
            d_e=2,
            database=db,
        )
        print(f"persisted {n} flags to session '{session_id}'")

        for cid, payload in FAKE_TEARSHEETS.items():
            persist_tearsheet(
                driver,
                session_id=session_id,
                canonical_id=cid,
                overview=payload["overview"],
                headlines=payload["headlines"],
                database=db,
            )
        print(f"persisted tearsheets for {len(FAKE_TEARSHEETS)} flags")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
