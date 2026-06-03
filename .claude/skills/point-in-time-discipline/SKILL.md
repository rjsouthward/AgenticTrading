---
name: point-in-time-discipline
description: >
  Use whenever pulling, joining, or stamping any time-stamped market or fundamental data for
  Blind Spot — OptionMetrics IV/vol surface, CRSP returns, Compustat segments or financials,
  TNIC/VTNIC vintages, Capital IQ events, or gbrain seeds. Ensures every value is knowable
  strictly before the session timestamp T0 and prevents lookahead leakage, which fails silently
  (no error, just an inflated eval). Trigger on any WRDS query, any "as of" / snapshot logic,
  any reward or eval computation, and any backtest replay.
---

# Point-in-time discipline

Lookahead leakage does not throw — it silently inflates every metric. Treat the as-of rule as a
hard invariant, and add a determinism test (re-running on the same vintage yields the identical
result) for any function that touches dated data.

## The rule

Every value used to construct or score a session at timestamp `T0` must have been **knowable
strictly before `T0`**. "Knowable" = publicly available as of that wall-clock moment, not
stamped to a fiscal/effective period that was only filed later.

## Source-by-source

- **OptionMetrics (IvyDB US, `optionm_all`):** daily and point-in-time by construction. Snapshot the vol surface / IV at the trading date ≤ `T0`. For IV rank, the trailing min/max window must **end before `T0`**. Never use a surface dated `T0` to flag a move that happens at the `T0` open — use the prior close.
- **CRSP daily (`crsp_a_stock`):** point-in-time by date. Co-movement / partial-correlation windows must **end strictly before `T0`**. CRSP is survivorship-bias-free — use it, not a free price API, precisely for this.
- **Compustat Segments (`comp_segments_hist_daily`) and financials:** THE classic trap. Records are stamped to `datadate` (fiscal period) but were not public until the **filing date**, months later — and standard Compustat is **restated/backfilled**. Stamp every `:SUPPLIES` edge and every fundamental to its **filing date**, not `datadate`.
  - `comp_pit` (Compustat Point-in-Time) is **NOT licensed** here. Mitigate with `contrib_as_filed_financials` (as-filed values) plus filing dates from the WRDS SEC platform (`wrdssec_all` / `wrds_sec_search`). If a filing date is unavailable, apply a conservative lag (e.g. fiscal-year-end + reporting lag) and flag the edge as lag-estimated.
- **TNIC / VTNIC (external):** annual vintages. Use the vintage whose release date precedes `T0`. Do not use the year-Y classification to expand a universe for a session earlier in year Y.
- **Capital IQ Key Developments (`ciq_keydev`):** use the announcement/disclosure timestamp, not any later revision.
- **gbrain seeds:** snapshot the analyst's seeds **before the current session writes back**. If today's engagement lands in the seed store before you snapshot, the universe peeks at today's attention to predict today's blind spots — a silent leak identical in kind to a post-open IV snapshot.

## Required checks before finalizing any dated pull

1. Identify the wall-clock `T0` for the session/row.
2. For every field, confirm its public-availability date ≤ `T0` (filing date for fundamentals/segments; trade date for prices/IV; release date for TNIC; write-time for seeds).
3. For windows, confirm the window **ends before** `T0` (strict).
4. Add/extend the determinism test: same archived vintage in → identical output. A diff is a leak.
