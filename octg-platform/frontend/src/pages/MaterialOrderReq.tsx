import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, CustomerSummary, MorGrid, MorRow } from "../api/client";
import LoadError from "../components/LoadError";
import Freshness from "../components/Freshness";
import ScopeNote from "../components/ScopeNote";

/**
 * Material Order Requirements — the monthly order grid, mirroring the
 * customer workbook's "Material Order Req" tab.
 *
 * DESIGN NOTES (the pilot of the "calm grid" language)
 * ----------------------------------------------------
 * This screen tries the direction the whole app is meant to move toward:
 * summary first (one sentence: how many products need orders, how many are
 * already late), detail on demand (a row expands to its month-by-month
 * ledger), and state carried by FORM before number — the urgency chip
 * (LATE / ORDER BY month / OK) is readable from across a desk. Density is
 * opt-in, never the default.
 */

const HORIZONS = [12, 18, 24] as const;
type Horizon = (typeof HORIZONS)[number];

function fmtQty(v: number) {
  return v.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

function fmtMonth(iso: string) {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-GB", { month: "short", year: "2-digit" });
}

function UrgencyChip({ row }: { row: MorRow }) {
  if (!row.available) {
    return <span className="mor-chip mor-chip-unknown">POSITION UNKNOWN</span>;
  }
  if (!row.order_flag) {
    return <span className="mor-chip mor-chip-ok">COVERED</span>;
  }
  if (row.already_late) {
    return <span className="mor-chip mor-chip-late">ORDER OVERDUE</span>;
  }
  if (row.first_order_by) {
    return (
      <span className="mor-chip mor-chip-due">
        ORDER BY {fmtMonth(row.first_order_by)}
      </span>
    );
  }
  // Requirement exists but no lead-time model: the deadline is unknowable,
  // and saying so beats inventing one.
  return <span className="mor-chip mor-chip-nolt">NO LEAD-TIME MODEL</span>;
}

function StripLegend() {
  // One fixed legend for every row: the strip is the projected ENDING BALANCE
  // month by month, and only two marks sit on top of it.
  return (
    <div className="mor-legend" aria-hidden="true">
      <span>
        <span className="mor-legend-swatch mor-legend-pos" /> stock covers demand
      </span>
      <span>
        <span className="mor-legend-swatch mor-legend-safety" /> into safety stock
      </span>
      <span>
        <span className="mor-legend-swatch mor-legend-neg" /> stock short
      </span>
      <span>
        <span className="mor-legend-mark mor-mark-order">▲</span> place order by this
        month
      </span>
      <span>
        <span className="mor-legend-mark mor-mark-safety">▽</span> dips into safety
        stock
      </span>
      <span>
        <span className="mor-legend-mark mor-mark-runout">▼</span> physical runout
        (balance below zero)
      </span>
    </div>
  );
}

function RowGrid({ row, months }: { row: MorRow; months: string[] }) {
  // The strip tells ONE story: the projected ending balance. Green months are
  // fine, red months are short, and the height shows how much — relative to
  // this row's own peak, so a product is readable against itself. The two
  // marks (▲ order-by, ▼ runout) are the only decorations; everything else
  // lives in the tooltip and the expanded ledger table.
  const peak = Math.max(...row.cells.map((c) => Math.abs(c.projected_balance)), 1);
  // TWO different runouts, deliberately kept apart (product-owner request,
  // 2026-08-12): dipping INTO the safety buffer (balance below the planner-set
  // level but still positive — the buffer is doing its job, the deadline is
  // soft) versus PHYSICAL runout (balance below zero — steel is genuinely
  // missing). Conflating them made every safety-triggered order read like an
  // imminent stockout.
  const safety = row.safety_stock ?? 0;
  const runoutMonth = row.cells.find((c) => c.projected_balance < 0)?.month;
  const safetyMonth =
    safety > 0
      ? row.cells.find(
          (c) => c.projected_balance >= 0 && c.projected_balance < safety
        )?.month
      : undefined;
  const orderByMonths = new Set(
    row.cells.map((c) => c.order_by_month).filter(Boolean) as string[]
  );
  return (
    <div className="mor-strip" role="img" aria-label="Projected ending balance by month">
      {row.cells.map((c) => {
        const short = c.projected_balance < 0;
        const inSafety =
          !short && safety > 0 && c.projected_balance < safety;
        return (
          <div
            key={c.month}
            className={
              "mor-cell " +
              (short
                ? "mor-cell-neg"
                : inSafety
                ? "mor-cell-safety"
                : "mor-cell-pos")
            }
            title={
              `${fmtMonth(c.month)}` +
              (c.month === runoutMonth
                ? " — PHYSICAL RUNOUT (balance below zero)"
                : "") +
              (c.month === safetyMonth
                ? ` — DIPS INTO SAFETY STOCK (below ${fmtQty(safety)} ${row.unit_of_measure}, still positive)`
                : "") +
              (orderByMonths.has(c.month) ? " — PLACE ORDER" : "") +
              `\nIncoming: ${fmtQty(c.arrivals)} ${row.unit_of_measure}\n` +
              `Outgoing (demand): ${fmtQty(c.demand)} ${row.unit_of_measure}\n` +
              `Ending balance: ${fmtQty(c.projected_balance)} ${row.unit_of_measure}\n` +
              (c.order_requirement > 0
                ? `Order: ${fmtQty(c.order_requirement)} ${row.unit_of_measure}` +
                  (c.order_by_month ? ` (place by ${fmtMonth(c.order_by_month)})` : "")
                : "No order needed")
            }
          >
            {/* ONE marker rail per cell. The two marks used to be separately
                absolute-positioned at the same spot, so an order-by and a
                runout in the SAME month overprinted each other into an
                unreadable smudge; in a shared flex rail they sit side by
                side. */}
            {(orderByMonths.has(c.month) ||
              c.month === runoutMonth ||
              c.month === safetyMonth) && (
              <span className="mor-marks" aria-hidden="true">
                {orderByMonths.has(c.month) && (
                  <span className="mor-mark mor-mark-order">▲</span>
                )}
                {c.month === safetyMonth && (
                  <span className="mor-mark mor-mark-safety">▽</span>
                )}
                {c.month === runoutMonth && (
                  <span className="mor-mark mor-mark-runout">▼</span>
                )}
              </span>
            )}
            <span
              className={"mor-balbar " + (short ? "mor-balbar-neg" : "mor-balbar-pos")}
              style={{
                height: `${Math.max(12, (Math.abs(c.projected_balance) / peak) * 100)}%`,
              }}
            />
          </div>
        );
      })}
    </div>
  );
}

function DetailTable({ row }: { row: MorRow }) {
  // Same physical-vs-safety split as the strip — see RowGrid.
  const safety = row.safety_stock ?? 0;
  const runoutMonth = row.cells.find((c) => c.projected_balance < 0)?.month;
  const safetyMonth =
    safety > 0
      ? row.cells.find(
          (c) => c.projected_balance >= 0 && c.projected_balance < safety
        )?.month
      : undefined;
  const active = row.cells.filter(
    (c) => c.demand > 0 || c.arrivals > 0 || c.order_requirement > 0
  );
  if (active.length === 0) {
    return <p className="mor-quiet">No demand inside this horizon.</p>;
  }
  return (
    <div className="table-scroll">
      <table className="mor-detail">
        <thead>
          <tr>
            <th>Month</th>
            <th>Incoming</th>
            <th>Outgoing (demand)</th>
            <th>Ending balance</th>
            <th>Order requirement</th>
            <th>Place order by</th>
          </tr>
        </thead>
        <tbody>
          {active.map((c) => (
            <tr
              key={c.month}
              className={
                (c.order_requirement > 0 ? "mor-detail-req " : "") +
                (c.month === runoutMonth ? "mor-detail-runout" : "")
              }
            >
              <td>
                {fmtMonth(c.month)}
                {c.month === runoutMonth && (
                  <span className="mor-runout-tag">PHYSICAL RUNOUT</span>
                )}
                {c.month === safetyMonth && (
                  <span className="mor-safety-tag">INTO SAFETY STOCK</span>
                )}
              </td>
              <td className="num">{c.arrivals > 0 ? fmtQty(c.arrivals) : "·"}</td>
              <td className="num">{c.demand > 0 ? fmtQty(c.demand) : "·"}</td>
              <td className={"num" + (c.projected_balance < 0 ? " mor-neg" : "")}>
                {fmtQty(c.projected_balance)}
              </td>
              <td className="num">
                {c.order_requirement > 0 ? <strong>{fmtQty(c.order_requirement)}</strong> : "·"}
              </td>
              <td className="num">
                {c.order_by_month ? (
                  <span className="mor-orderby-tag">{fmtMonth(c.order_by_month)}</span>
                ) : (
                  "—"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ProductRow({ row, months }: { row: MorRow; months: string[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={"mor-row" + (row.already_late ? " mor-row-late" : "")}>
      <button
        type="button"
        className="mor-row-head"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <div className="mor-row-id">
          <Link
            to={`/mrp/by-item/${row.product_id}`}
            className="mor-product"
            onClick={(e) => e.stopPropagation()}
          >
            {row.product_description ?? row.product_id}
          </Link>
          <span className="mor-meta">
            {row.available && row.lead_time_modelled
              ? `lead time ${row.lead_time_months} mo`
              : row.available
              ? "lead time not modelled"
              : ""}
            {row.safety_stock !== null &&
              ` · safety ${fmtQty(row.safety_stock)} ${row.unit_of_measure}`}
          </span>
          {row.total_overdue_demand > 0 && (
            <span
              className="mor-meta surplus-overdue"
              title={
                "Demand whose ROS month has already passed. It is folded into " +
                "the first month of the grid (lateness does not cancel demand) " +
                "and labelled here rather than silently blended in."
              }
            >
              incl. {fmtQty(row.total_overdue_demand)} {row.unit_of_measure}{" "}
              overdue demand
            </span>
          )}
        </div>
        {row.available ? (
          <>
            <RowGrid row={row} months={months} />
            <div className="mor-row-figures">
              <span className="mor-req num">
                {row.order_flag ? (
                  <>
                    {fmtQty(row.total_order_requirement)}{" "}
                    <span className="mor-unit">{row.unit_of_measure}</span>
                  </>
                ) : (
                  <span className="mor-quiet">—</span>
                )}
              </span>
              <UrgencyChip row={row} />
            </div>
          </>
        ) : (
          <div className="mor-row-unavailable">
            <UrgencyChip row={row} />
            <span className="mor-quiet">{row.reason}</span>
          </div>
        )}
        <span className="mor-caret" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && row.available && (
        <div className="mor-row-body">
          {row.position && (
            <p className="mor-position">
              On hand {fmtQty(row.position.on_hand)} {row.unit_of_measure}
              {row.position.customer_owned != null &&
                row.position.customer_owned > 0 && (
                  <> · customer-owned {fmtQty(row.position.customer_owned)}</>
                )}
              {row.position.on_order != null && (
                <> · on order {fmtQty(row.position.on_order)}</>
              )}
              {(row.position.on_order_undated ?? 0) > 0 && (
                <>
                  {" "}
                  · undated PO {fmtQty(row.position.on_order_undated ?? 0)}{" "}
                  <span className="mor-quiet">(not netted — no promised month)</span>
                </>
              )}
            </p>
          )}
          <DetailTable row={row} />
        </div>
      )}
    </div>
  );
}

export default function MaterialOrderReq() {
  const [grid, setGrid] = useState<MorGrid | null>(null);
  const [customers, setCustomers] = useState<CustomerSummary[]>([]);
  // Filters live in the URL so a filtered grid is a shareable link.
  const [searchParams, setSearchParams] = useSearchParams();
  const customerId = searchParams.get("customer") ?? "";
  const horizon = (Number(searchParams.get("horizon")) || 18) as Horizon;
  const setCustomerId = (v: string) =>
    setSearchParams(
      (p) => {
        v ? p.set("customer", v) : p.delete("customer");
        return p;
      },
      { replace: true }
    );
  const setHorizon = (v: Horizon) =>
    setSearchParams(
      (p) => {
        v === 18 ? p.delete("horizon") : p.set("horizon", String(v));
        return p;
      },
      { replace: true }
    );
  // Sort lives in the URL like every filter on this screen (QA 2026-08-14:
  // bookmarks reproduced the filters but lost the sort). No param = server
  // order (urgency), matching the platform's no-param-means-default rule.
  const sortBy = searchParams.get("sort") ?? "server";
  const setSortBy = (v: string) =>
    setSearchParams(
      (p) => {
        v === "server" ? p.delete("sort") : p.set("sort", v);
        return p;
      },
      { replace: true }
    );
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(true);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api.getCustomers().then(setCustomers).catch(() => {});
  }, []);

  useEffect(() => {
    setBusy(true);
    api
      .getOrderRequirements(customerId || undefined, horizon)
      .then((g) => {
        setGrid(g);
        setError(null);
        setFetchedAt(new Date());
      })
      .catch((e) => setError(e))
      .finally(() => setBusy(false));
  }, [customerId, horizon, reload]);

  const summary = useMemo(() => {
    if (!grid) return null;
    const flagged = grid.rows.filter((r) => r.order_flag);
    const late = flagged.filter((r) => r.already_late);
    return { flagged: flagged.length, late: late.length, total: grid.rows.length };
  }, [grid]);

  const sortedRows = useMemo(() => {
    if (!grid) return [];
    if (sortBy === "server") return grid.rows; // urgency: the server's order
    return [...grid.rows].sort((a, b) => {
      if (sortBy === "product")
        return (a.product_description ?? a.product_id).localeCompare(
          b.product_description ?? b.product_id
        );
      // total to order, largest first
      return b.total_order_requirement - a.total_order_requirement;
    });
  }, [grid, sortBy]);

  return (
    <div className="mor-page">
      <header className="mor-head">
        <div>
          <h2>Material Order Requirements</h2>
          {summary && (
            <p className="mor-summary">
              {summary.flagged === 0 ? (
                "Nothing needs ordering inside this horizon."
              ) : (
                <>
                  <strong>{summary.flagged}</strong> of {summary.total} products need a
                  mill order
                  {summary.late > 0 && (
                    <>
                      {" "}
                      — <strong className="mor-neg">{summary.late} already past their
                      order-by month</strong>
                    </>
                  )}
                  .
                </>
              )}
            </p>
          )}
        </div>
        <div className="mor-controls">
          <Freshness
            fetchedAt={fetchedAt}
            busy={busy}
            onRefresh={() => setReload((r) => r + 1)}
          />
          <select
            aria-label="Sort"
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
          >
            <option value="server">Sort: most urgent first</option>
            <option value="total">Sort: total to order (largest)</option>
            <option value="product">Sort: product name</option>
          </select>
          <select
            aria-label="Customer"
            value={customerId}
            onChange={(e) => setCustomerId(e.target.value)}
          >
            <option value="">All customers</option>
            {customers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <div className="exec-horizon-picker" role="group" aria-label="Horizon">
            {HORIZONS.map((m) => (
              <button
                key={m}
                type="button"
                className={`exec-horizon-btn${m === horizon ? " exec-horizon-btn-on" : ""}`}
                aria-pressed={m === horizon}
                disabled={busy}
                onClick={() => setHorizon(m)}
              >
                {m}m
              </button>
            ))}
          </div>
        </div>
      </header>

      <ScopeNote />

      {error != null ? (
        <LoadError what="the order requirements grid" error={error} />
      ) : null}

      {grid && error == null && (
        <>
          <StripLegend />
          <div className="mor-axis" aria-hidden="true">
            <span className="mor-axis-label" />
            <div className="mor-axis-months">
              {grid.months.map((m) => (
                <span key={m}>{fmtMonth(m)}</span>
              ))}
            </div>
            <span className="mor-axis-figures">Total to order</span>
          </div>
          <div className="mor-rows">
            {sortedRows.map((r) => (
              <ProductRow key={r.product_id} row={r} months={grid.months} />
            ))}
            {grid.rows.length === 0 && (
              <p className="empty">No in-scope demand inside this horizon.</p>
            )}
          </div>
          <footer className="mor-notes">
            {grid.notes.map((n, i) => (
              <p key={i}>{n}</p>
            ))}
          </footer>
        </>
      )}
    </div>
  );
}
