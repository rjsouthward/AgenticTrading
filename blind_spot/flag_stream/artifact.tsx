// Blind Spot flag-session artifact.
//
// The MCP render_artifact tool (and preview.py) substitute the sentinel on the
// SESSION line below with the live FlagSession payload before returning this
// source. The result is a self-contained React component with two tabs:
// Flags (sortable table) and Tearsheets (per-company cards + live headlines).

import React, { useMemo, useState, Fragment } from "react";

type Headline = {
  rank: number;
  published_at: string;
  title: string;
  source: string;
  url: string;
  summary: string | null;
};

type Overview = {
  ticker: string;
  name: string;
  sector: string;
  market_cap: number;
  price: number;
  change_abs: number;
  change_pct: number;
  summary: string;
  fetched_at: string;
};

type Flag = {
  rank: number;
  canonical_id: string;
  salience: number;
  reason: string;
  entity_path: string[] | null;
  on_entity_frontier: boolean;
  overview: Overview | null;
  headlines: Headline[];
};

type Session = {
  session_id: string;
  created_at: string;
  as_of: string;
  k: number;
  d_e: number;
  n_flags: number;
  flags: Flag[];
  ticker_lookup: Record<string, string>;
};

const SESSION: Session = __FLAG_DATA__;

const C = {
  navy:        "#0a2540",
  tabIdle:     "#eef2f7",
  tabIdleText: "#0a2540",
  page:        "#ffffff",
  body:        "#1f2937",
  muted:       "#6b7280",
  headerRow:   "#f4f7fb",
  divider:     "#e5e7eb",
  cardBorder:  "#e5e7eb",
  live:        "#10b981",
  liveBg:      "#10b9811a",
  green:       "#047857",
  red:         "#b91c1c",
  bar:         "#0a2540",
  barTrack:    "#e5e7eb",
  rowHover:    "#f4f7fb",
};

export default function BlindSpotFlags() {
  const flags = SESSION.flags ?? [];
  const lookup: Record<string, string> = SESSION.ticker_lookup ?? {};
  const [tab, setTab] = useState("flags");
  const [minSalience, setMinSalience] = useState(0);
  const [onlyFrontier, setOnlyFrontier] = useState(false);
  const [expanded, setExpanded] = useState(null);

  const visible = useMemo(
    () =>
      flags.filter(
        (f) =>
          f.salience >= minSalience && (!onlyFrontier || f.on_entity_frontier),
      ),
    [flags, minSalience, onlyFrontier],
  );

  return (
    <div
      style={{
        fontFamily:
          "Inter, ui-sans-serif, system-ui, -apple-system, 'Helvetica Neue', Arial, sans-serif",
        padding: "28px 36px",
        color: C.body,
        background: C.page,
        minHeight: "100vh",
      }}
    >
      <header style={{ marginBottom: 20 }}>
        <h1
          style={{
            fontSize: 28,
            margin: 0,
            color: C.navy,
            fontWeight: 800,
            letterSpacing: "-0.01em",
          }}
        >
          Blind Spot
        </h1>

        <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
          <Tab id="flags"      label="Flags"      tab={tab} setTab={setTab} />
          <Tab id="tearsheets" label="Tearsheets" tab={tab} setTab={setTab} />
          <Tab id="sessions"   label="Sessions"   tab={tab} setTab={setTab} />
        </div>
      </header>

      <SessionStrip />

      {tab === "flags" && (
        <FlagsTable
          flags={flags}
          visible={visible}
          lookup={lookup}
          minSalience={minSalience}
          setMinSalience={setMinSalience}
          onlyFrontier={onlyFrontier}
          setOnlyFrontier={setOnlyFrontier}
          expanded={expanded}
          setExpanded={setExpanded}
        />
      )}

      {tab === "tearsheets" && <Tearsheets flags={flags} />}

      {tab === "sessions" && (
        <div style={{ padding: 32, color: C.muted }}>
          Session browser coming up next — wire it via the MCP's list_sessions tool.
        </div>
      )}
    </div>
  );
}

function SessionStrip() {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        margin: "10px 0 18px",
      }}
    >
      <span
        style={{
          width: 10,
          height: 10,
          borderRadius: "50%",
          background: C.live,
          boxShadow: `0 0 0 3px ${C.liveBg}`,
        }}
      />
      <span
        style={{
          fontSize: 13,
          fontWeight: 700,
          letterSpacing: "0.08em",
          color: C.body,
        }}
      >
        LIVE
      </span>
      <span style={{ marginLeft: 12, fontSize: 12, color: C.muted }}>
        session <code style={{ color: C.navy }}>{SESSION.session_id}</code> ·
        as_of {SESSION.as_of} · k={SESSION.k} · d_e={SESSION.d_e} ·{" "}
        {SESSION.n_flags} flags · run {SESSION.created_at}
      </span>
    </div>
  );
}

function FlagsTable({
  flags, visible, lookup, minSalience, setMinSalience, onlyFrontier, setOnlyFrontier,
  expanded, setExpanded,
}) {
  return (
    <div>
      <div
        style={{
          display: "flex",
          gap: 18,
          marginBottom: 10,
          fontSize: 13,
          alignItems: "center",
          color: C.body,
        }}
      >
        <label>
          Min salience{" "}
          <input
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={minSalience}
            onChange={(e) => setMinSalience(parseFloat(e.target.value))}
            style={{ accentColor: C.navy, verticalAlign: "middle" }}
          />{" "}
          <span style={{ fontVariantNumeric: "tabular-nums" }}>
            {minSalience.toFixed(2)}
          </span>
        </label>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={onlyFrontier}
            onChange={(e) => setOnlyFrontier(e.target.checked)}
            style={{ accentColor: C.navy }}
          />{" "}
          entity-frontier only
        </label>
        <div style={{ marginLeft: "auto", color: C.muted }}>
          {visible.length}/{flags.length} visible
        </div>
      </div>

      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: 14,
          borderTop: `1px solid ${C.divider}`,
        }}
      >
        <thead>
          <tr style={{ background: C.headerRow, textAlign: "left" }}>
            <Th width={56}>Rank</Th>
            <Th width={120}>Name</Th>
            <Th width={170}>Salience</Th>
            <Th width={90}>Frontier</Th>
            <Th>Summary</Th>
          </tr>
        </thead>
        <tbody>
          {visible.map((f) => {
            const isOpen = expanded === f.canonical_id;
            const label = f.overview?.ticker ?? lookup[f.canonical_id] ?? f.canonical_id;
            return (
              <Fragment key={f.canonical_id}>
                <tr
                  onClick={() => setExpanded(isOpen ? null : f.canonical_id)}
                  style={{
                    borderBottom: `1px solid ${C.divider}`,
                    cursor: "pointer",
                    background: isOpen ? C.rowHover : undefined,
                  }}
                >
                  <td style={cell()}>{f.rank}</td>
                  <td
                    style={{
                      ...cell(),
                      fontFamily:
                        "ui-monospace, SFMono-Regular, Menlo, monospace",
                      color: C.navy,
                      fontWeight: 700,
                    }}
                  >
                    {label}
                  </td>
                  <td style={cell()}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <div
                        style={{
                          width: 100, height: 6, background: C.barTrack,
                          borderRadius: 3, overflow: "hidden",
                        }}
                      >
                        <div
                          style={{
                            width: `${Math.min(100, f.salience * 100)}%`,
                            height: "100%", background: C.bar,
                          }}
                        />
                      </div>
                      <span style={{ fontVariantNumeric: "tabular-nums" }}>
                        {f.salience.toFixed(3)}
                      </span>
                    </div>
                  </td>
                  <td style={cell()}>
                    {f.on_entity_frontier ? (
                      <Pill>yes</Pill>
                    ) : (
                      <span style={{ color: C.muted }}>—</span>
                    )}
                  </td>
                  <td style={cell()}>{f.reason}</td>
                </tr>
                {isOpen && f.entity_path && f.entity_path.length > 0 && (
                  <tr style={{ background: C.rowHover }}>
                    <td></td>
                    <td
                      colSpan={4}
                      style={{
                        padding: "4px 16px 14px",
                        fontFamily:
                          "ui-monospace, SFMono-Regular, Menlo, monospace",
                        fontSize: 12,
                        color: C.muted,
                      }}
                    >
                      path: {f.entity_path.map((id) => lookup[id] ?? id).join(" → ")}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>

      {flags.length === 0 && (
        <div style={{ padding: 32, textAlign: "center", color: C.muted }}>
          No flags in this session.
        </div>
      )}
    </div>
  );
}

function Tearsheets({ flags }) {
  if (!flags.length) {
    return (
      <div style={{ padding: 32, textAlign: "center", color: C.muted }}>
        No flags in this session.
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {flags.map((f) => (
        <TearsheetCard key={f.canonical_id} flag={f} />
      ))}
    </div>
  );
}

function TearsheetCard({ flag }) {
  const ov = flag.overview;
  return (
    <div
      style={{
        border: `1px solid ${C.cardBorder}`,
        borderRadius: 10,
        overflow: "hidden",
        background: "#ffffff",
      }}
    >
      <div
        style={{
          display: "flex",
          gap: 24,
          alignItems: "flex-start",
          padding: "18px 22px",
          borderBottom: `1px solid ${C.divider}`,
        }}
      >
        <div style={{ minWidth: 120 }}>
          <div
            style={{
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
              fontSize: 22,
              fontWeight: 800,
              color: C.navy,
              letterSpacing: "-0.01em",
            }}
          >
            {ov ? ov.ticker : flag.canonical_id}
          </div>
          <div style={{ fontSize: 12, color: C.muted, marginTop: 2 }}>
            {flag.canonical_id}
          </div>
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 16, fontWeight: 700, color: C.navy }}>
            {ov ? ov.name : "—"}
          </div>
          <div style={{ fontSize: 12, color: C.muted, marginTop: 4 }}>
            {ov ? (
              <>
                {ov.sector} · market cap {fmtUSD(ov.market_cap, 0)} ·{" "}
                fetched {fmtTime(ov.fetched_at)}
              </>
            ) : (
              "no overview attached"
            )}
          </div>
          {ov && (
            <div style={{ fontSize: 14, color: C.body, marginTop: 10, lineHeight: 1.5 }}>
              {ov.summary}
            </div>
          )}
        </div>

        <div style={{ textAlign: "right", minWidth: 140 }}>
          {ov && (
            <>
              <div
                style={{
                  fontSize: 22,
                  fontWeight: 800,
                  color: C.navy,
                  fontVariantNumeric: "tabular-nums",
                }}
              >
                {fmtUSD(ov.price, 2)}
              </div>
              <div
                style={{
                  fontSize: 13,
                  fontWeight: 700,
                  color: ov.change_abs >= 0 ? C.green : C.red,
                  fontVariantNumeric: "tabular-nums",
                  marginTop: 2,
                }}
              >
                {ov.change_abs >= 0 ? "+" : ""}
                {ov.change_abs.toFixed(2)} ({ov.change_pct >= 0 ? "+" : ""}
                {ov.change_pct.toFixed(2)}%)
              </div>
            </>
          )}
          <div style={{ marginTop: 8 }}>
            <Pill>
              salience {flag.salience.toFixed(3)}
            </Pill>
          </div>
        </div>
      </div>

      <div style={{ padding: "14px 22px 8px" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            marginBottom: 8,
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: C.live,
              boxShadow: `0 0 0 3px ${C.liveBg}`,
            }}
          />
          <span
            style={{
              fontSize: 12,
              fontWeight: 700,
              letterSpacing: "0.08em",
              color: C.body,
            }}
          >
            LIVE NEWS
          </span>
          <span style={{ fontSize: 12, color: C.muted, marginLeft: "auto" }}>
            {flag.headlines.length} headline
            {flag.headlines.length === 1 ? "" : "s"}
          </span>
        </div>

        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: C.headerRow, textAlign: "left" }}>
              <Th width={70}>Time</Th>
              <Th width={120}>Source</Th>
              <Th>Headline</Th>
            </tr>
          </thead>
          <tbody>
            {flag.headlines.map((h) => (
              <tr
                key={h.rank}
                style={{ borderBottom: `1px solid ${C.divider}` }}
              >
                <td
                  style={{
                    ...cell(12),
                    color: C.muted,
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {fmtTime(h.published_at)}
                </td>
                <td style={{ ...cell(12), color: C.navy, fontWeight: 600 }}>
                  {h.source}
                </td>
                <td style={cell(12)}>
                  <a
                    href={h.url}
                    target="_blank"
                    rel="noreferrer"
                    style={{ color: C.navy, textDecoration: "none" }}
                  >
                    {h.title}
                  </a>
                  {h.summary && (
                    <div style={{ color: C.muted, marginTop: 2, fontSize: 12 }}>
                      {h.summary}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {flag.headlines.length === 0 && (
          <div style={{ padding: 16, color: C.muted, fontSize: 13 }}>
            No headlines attached.
          </div>
        )}
      </div>
    </div>
  );
}

function Tab({ id, label, tab, setTab }) {
  const active = tab === id;
  return (
    <div
      onClick={() => setTab(id)}
      style={{
        padding: "10px 22px",
        borderRadius: 8,
        fontSize: 14,
        fontWeight: 600,
        background: active ? C.navy : C.tabIdle,
        color: active ? "#ffffff" : C.tabIdleText,
        cursor: "pointer",
        userSelect: "none",
      }}
    >
      {label}
    </div>
  );
}

function Th({ children, width }) {
  return (
    <th
      style={{
        padding: "12px 16px",
        color: C.navy,
        fontWeight: 700,
        fontSize: 14,
        width,
      }}
    >
      {children}
    </th>
  );
}

function Pill({ children }) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: 999,
        background: C.liveBg,
        color: C.green,
        fontSize: 12,
        fontWeight: 600,
      }}
    >
      {children}
    </span>
  );
}

function cell(fontSize) {
  return { padding: "12px 16px", verticalAlign: "top", fontSize: fontSize || 14 };
}

function fmtUSD(n, decimals) {
  if (n == null) return "—";
  if (decimals === 0 && n >= 1e9) return "$" + (n / 1e9).toFixed(1) + "B";
  if (decimals === 0 && n >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
  return "$" + n.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return hh + ":" + mm;
}
