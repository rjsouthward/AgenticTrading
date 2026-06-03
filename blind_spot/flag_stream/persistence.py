"""
Neo4j persistence for Blind Spot flag sessions.

Schema
------
    (:FlagSession {session_id, created_at, as_of, k, d_e, n_flags})
        -[:HAS_FLAG {rank}]->
    (:FlagItem   {canonical_id, salience, reason, entity_path[],
                  on_entity_frontier})
        -[:OVERVIEW]->
    (:Tearsheet  {ticker, name, sector, market_cap, price,
                  change_abs, change_pct, summary, fetched_at})
        -[:HAS_HEADLINE {rank}]->
    (:Headline   {published_at, title, source, url, summary})

`session_id` is the unique key. persist_flags overwrites any prior session
with the same id so the MCP always reads the latest pipeline output. Tearsheets
are optional — load_flags returns flags with overview=None if absent.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neo4j import Driver
    from blind_spot.flagger import Flag


def persist_flags(
    driver: "Driver",
    session_id: str,
    as_of: date,
    flags: list["Flag"],
    k: int,
    d_e: int,
    database: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "canonical_id": f.canonical_id,
            "salience": float(f.salience),
            "reason": f.reason,
            "entity_path": list(f.entity_path or []),
            "on_entity_frontier": bool(f.on_entity_frontier),
            "rank": i,
        }
        for i, f in enumerate(flags, start=1)
    ]

    with driver.session(database=database) as s:
        s.run(
            """
            MATCH (sess:FlagSession {session_id: $sid})
            OPTIONAL MATCH (sess)-[:HAS_FLAG]->(fi:FlagItem)
            DETACH DELETE fi, sess
            """,
            sid=session_id,
        )
        s.run(
            """
            CREATE (sess:FlagSession {
                session_id: $sid,
                created_at: $now,
                as_of:      $as_of,
                k:          $k,
                d_e:        $d_e,
                n_flags:    $n_flags
            })
            WITH sess
            UNWIND $rows AS row
            CREATE (fi:FlagItem {
                canonical_id:       row.canonical_id,
                salience:           row.salience,
                reason:             row.reason,
                entity_path:        row.entity_path,
                on_entity_frontier: row.on_entity_frontier
            })
            CREATE (sess)-[:HAS_FLAG {rank: row.rank}]->(fi)
            """,
            sid=session_id,
            now=now,
            as_of=as_of.isoformat(),
            k=k,
            d_e=d_e,
            n_flags=len(rows),
            rows=rows,
        )

    return len(rows)


def load_flags(
    driver: "Driver",
    session_id: str,
    database: str | None = None,
) -> dict[str, Any] | None:
    with driver.session(database=database) as s:
        meta_row = s.run(
            """
            MATCH (sess:FlagSession {session_id: $sid})
            RETURN sess.created_at AS created_at,
                   sess.as_of      AS as_of,
                   sess.k          AS k,
                   sess.d_e        AS d_e,
                   sess.n_flags    AS n_flags
            """,
            sid=session_id,
        ).single()
        if meta_row is None:
            return None

        rows = s.run(
            """
            MATCH (sess:FlagSession {session_id: $sid})-[r:HAS_FLAG]->(fi:FlagItem)
            OPTIONAL MATCH (fi)-[:OVERVIEW]->(t:Tearsheet)
            OPTIONAL MATCH (t)-[h:HAS_HEADLINE]->(n:Headline)
            WITH r, fi, t, h, n
            ORDER BY r.rank ASC, h.rank ASC
            WITH r, fi, t,
                 collect(CASE WHEN n IS NULL THEN NULL ELSE {
                   rank:         h.rank,
                   published_at: n.published_at,
                   title:        n.title,
                   source:       n.source,
                   url:          n.url,
                   summary:      n.summary
                 } END) AS raw_headlines
            RETURN r.rank                AS rank,
                   fi.canonical_id       AS canonical_id,
                   fi.salience           AS salience,
                   fi.reason             AS reason,
                   fi.entity_path        AS entity_path,
                   fi.on_entity_frontier AS on_entity_frontier,
                   CASE WHEN t IS NULL THEN NULL ELSE {
                     ticker:      t.ticker,
                     name:        t.name,
                     sector:      t.sector,
                     market_cap:  t.market_cap,
                     price:       t.price,
                     change_abs:  t.change_abs,
                     change_pct:  t.change_pct,
                     summary:     t.summary,
                     fetched_at:  t.fetched_at
                   } END                  AS overview,
                   [x IN raw_headlines WHERE x IS NOT NULL] AS headlines
            ORDER BY rank ASC
            """,
            sid=session_id,
        )
        flags = [dict(r) for r in rows]

    return {
        "session_id": session_id,
        "created_at": meta_row["created_at"],
        "as_of":      meta_row["as_of"],
        "k":          meta_row["k"],
        "d_e":        meta_row["d_e"],
        "n_flags":    meta_row["n_flags"],
        "flags":      flags,
    }


def persist_tearsheet(
    driver: "Driver",
    session_id: str,
    canonical_id: str,
    overview: dict,
    headlines: list[dict],
    database: str | None = None,
) -> None:
    """Attach a Tearsheet (+ headline list) to the FlagItem for canonical_id in the
    given session. Overwrites any prior tearsheet on the same FlagItem."""
    with driver.session(database=database) as s:
        s.run(
            """
            MATCH (sess:FlagSession {session_id: $sid})-[:HAS_FLAG]->(fi:FlagItem {canonical_id: $cid})
            OPTIONAL MATCH (fi)-[:OVERVIEW]->(t:Tearsheet)
            OPTIONAL MATCH (t)-[:HAS_HEADLINE]->(h:Headline)
            DETACH DELETE h, t
            """,
            sid=session_id, cid=canonical_id,
        )
        s.run(
            """
            MATCH (sess:FlagSession {session_id: $sid})-[:HAS_FLAG]->(fi:FlagItem {canonical_id: $cid})
            CREATE (t:Tearsheet)
            SET t.ticker     = $o.ticker,
                t.name       = $o.name,
                t.sector     = $o.sector,
                t.market_cap = $o.market_cap,
                t.price      = $o.price,
                t.change_abs = $o.change_abs,
                t.change_pct = $o.change_pct,
                t.summary    = $o.summary,
                t.fetched_at = $o.fetched_at
            CREATE (fi)-[:OVERVIEW]->(t)
            WITH t
            UNWIND $headlines AS h
            CREATE (n:Headline)
            SET n.published_at = h.published_at,
                n.title        = h.title,
                n.source       = h.source,
                n.url          = h.url,
                n.summary      = h.summary
            CREATE (t)-[:HAS_HEADLINE {rank: h.rank}]->(n)
            """,
            sid=session_id, cid=canonical_id, o=overview, headlines=headlines,
        )


def list_sessions(driver: "Driver", database: str | None = None) -> list[dict]:
    with driver.session(database=database) as s:
        rows = s.run(
            """
            MATCH (sess:FlagSession)
            RETURN sess.session_id AS session_id,
                   sess.created_at AS created_at,
                   sess.as_of      AS as_of,
                   sess.n_flags    AS n_flags
            ORDER BY sess.created_at DESC
            """
        )
        return [dict(r) for r in rows]


def delete_session(
    driver: "Driver", session_id: str, database: str | None = None
) -> bool:
    with driver.session(database=database) as s:
        summary = s.run(
            """
            MATCH (sess:FlagSession {session_id: $sid})
            OPTIONAL MATCH (sess)-[:HAS_FLAG]->(fi:FlagItem)
            DETACH DELETE fi, sess
            """,
            sid=session_id,
        ).consume()
        return summary.counters.nodes_deleted > 0
