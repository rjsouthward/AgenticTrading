---
name: entity-resolution
description: >
  Use whenever a company name, ticker, or vendor identifier must be turned into Blind Spot's
  canonical id, or whenever two datasets are joined on a company — resolving the analyst's
  A_final list, Compustat segment customer names, OptionMetrics secids, IBES tickers, TNIC/VTNIC
  firms, or any edge endpoint. A silent id mismatch corrupts the reward without erroring, so this
  must run before any set operation (intersection/complement) or cross-dataset join.
---

# Entity resolution

The canonical key is **CRSP `permno`**. Resolve everything to `permno` *before* any set op or
join. A wrong or missing link does not throw — it books a false miss (analyst named it, you
didn't match) or a phantom edge, and quietly corrupts the eval.

## The hub-and-spoke model

`permno` is the hub. Spokes (all WRDS linking products are subscribed):

- **gvkey ↔ permno:** CRSP/Compustat Merged link (`crsp_a_ccm`, e.g. the CCM link history table). Required for everything Compustat (segments, fundamentals).
- **secid ↔ permno:** OptionMetrics↔CRSP linking suite (`wrdsapps_link_crsp_optionm`). Required to attach candidate-generator output (IV) to graph nodes.
- **IBES ticker ↔ permno:** IBES↔CRSP linking suite (`wrdsapps_link_crsp_ibes`). For coverage/estimate joins.
- **parent ↔ subsidiary:** WRDS Subsidiaries (`wrdsapps_subsidiary`). A named customer is often a subsidiary; resolve up to the listed parent's `permno`.

> Confirm exact table/column names via the WRDS MCP reference tools (`wrds_mcp_reference_tools`)
> before relying on them — schema names vary by library version.

## The hard case: Compustat segment customer names

Segment customer disclosures (`comp_segments_hist_daily`) give a free-text customer name, not an
id. Resolve via the **WRDS Supply Chain linking suite** (`wrdsapps_link_supplychain`) rather than
fuzzy-matching strings yourself. Then map the linked gvkey → permno via CCM.

- Many customers are anonymized ("Customer A") or are foreign/private/government entities that
  will not map. **Log the disclosure with `customer_id = None`** — do not drop it (it's a known
  concentration with no graphable counterparty) and do not guess a match.
- Resolution is directional: the *supplier* discloses; resolve both ends, but expect the customer
  side to be the lossy one (large-customer/small-supplier bias of the 10% disclosure rule).

## Procedure

1. Normalize the input (name/ticker/secid/gvkey) and identify its native id space.
2. Map to `permno` through the appropriate linking table above (resolve subsidiaries to parent).
3. If no link resolves, return `None` and log — never substitute a fuzzy or best-guess id.
4. Only after both endpoints (for edges) or all members (for sets) are `permno` do you join or
   compute intersections/complements.

## Why this is a skill, not a one-off

It is invoked by every edge source and by the analyst list, on every session, and it fails
silently. The WRDS MCP will happily return rows on whatever id you give it; this skill is the
guardrail that ensures the id is the right one before the join.
