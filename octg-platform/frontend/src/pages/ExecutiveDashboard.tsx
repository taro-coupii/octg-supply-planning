import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ALLOCATION_HORIZONS,
  AllocationHorizon,
  api,
  BusinessUnitOut,
  CoverageByStatus,
  CustomerSummary,
  DemandTrendHorizon,
  ExecutiveCoverage,
  ExecutiveDashboard as ExecutiveDashboardData,
  ExecutiveSupplyRisk,
  Figure,
  FirstRunout,
  IncomingSupply,
  InventoryUtilisationOut,
  QuantityByUnit,
  SoftAllocationCoverage,
} from "../api/client";
import LoadError from "../components/LoadError";
import Freshness from "../components/Freshness";
import ScopeChecks from "../components/ScopeChecks";
import { DEFAULT_PROFILE_SCOPE, DEFAULT_STATUS_SCOPE } from "../lib/enums";

/**
 * The Executive Dashboard.
 *
 * This screen goes to management, and that makes honesty the hard requirement
 * rather than a nicety. Every block in `GET /dashboard/executive` carries an
 * `available` boolean plus a `reason`, and the backend sends `null` — never 0 —
 * for a figure it could not compute. The rule is implemented in one place
 * (`Unavailable` / `FigureValue`) so no individual block can quietly opt out.
 *
 * Coverage is QUANTITY-based here, not well-count based: `coverage_pct` is a
 * quantity ratio and is the headline. The well-count ratio (`well_coverage_pct`)
 * and the four well counts survive only as a secondary reference line — a well
 * short by 50 and a well short by 40,000 count alike by well and nothing alike
 * by quantity.
 */

function pct(value: number) {
  return `${value.toFixed(1)}%`;
}

function qty(value: number) {
  return value.toLocaleString();
}

function day(iso: string) {
  return iso.slice(0, 10);
}

function stamp(iso: string) {
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}`;
}

function unitQty(value: number, unit: string | null | undefined) {
  return unit ? `${qty(value)} ${unit}` : qty(value);
}

/** MT headlines: one decimal is plenty at dashboard altitude. */
function mt(value: number) {
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} t`;
}

/**
 * Compact MT figure for table cells and fact rows. An unavailable conversion
 * renders as a labelled refusal with the reason on hover — never a dash, which
 * would be indistinguishable from "zero-ish".
 */
function MtInline({ figure }: { figure: Figure }) {
  if (!figure.available || figure.value === null) {
    return (
      <span className="exec-mt-na" title={figure.reason ?? undefined}>
        MT n/a
      </span>
    );
  }
  return (
    <span
      className="exec-mt-inline num"
      title={figure.reason ?? undefined}
    >
      {mt(figure.value)}
      {figure.reason ? "*" : ""}
    </span>
  );
}

/** Small breakdown line for a `quantities_by_unit` array with >1 unit. */
function UnitBreakdown({ units }: { units: QuantityByUnit[] }) {
  if (units.length <= 1) return null;
  return (
    <div className="exec-unit-breakdown">
      {units.map((u) => (
        <span key={u.unit_of_measure} className="exec-unit-chip">
          {qty(u.quantity)} {u.unit_of_measure}
        </span>
      ))}
    </div>
  );
}

/* ---------------- The one honest-refusal renderer ---------------- */

function Unavailable({ reason }: { reason: string | null }) {
  return (
    <div className="exec-unavailable">
      <span className="exec-unavailable-tag">Not available</span>
      <span className="exec-unavailable-reason">
        {reason ??
          "The backend reported this figure as unavailable but supplied no reason. It is unknown, not zero."}
      </span>
    </div>
  );
}

function FigureValue({
  figure,
  render,
  zeroNote,
}: {
  figure: Figure;
  render: (value: number) => string;
  zeroNote?: string;
}) {
  if (!figure.available || figure.value === null) {
    return <Unavailable reason={figure.reason} />;
  }
  return (
    <>
      <span className="exec-figure num">{render(figure.value)}</span>
      {figure.value === 0 && zeroNote && (
        <span className="exec-measured-zero">{zeroNote}</span>
      )}
    </>
  );
}

/* ---------------- Demand trend ---------------- */

function HorizonCard({ h }: { h: DemandTrendHorizon }) {
  return (
    <div className="exec-horizon">
      <div className="exec-horizon-head">
        <span className="exec-horizon-months">{h.months} months</span>
        <span className="exec-horizon-window">
          {day(h.window_start)} → {day(h.window_end)}
        </span>
      </div>

      <div className="exec-horizon-current">
        <span className="exec-figure-big num">
          <FigureValue figure={h.current_tonnes} render={mt} />
        </span>
        <span className="exec-horizon-lines">
          across {h.current_line_count} demand line
          {h.current_line_count === 1 ? "" : "s"}{" "}
          <span className="exec-iu-converted">(converted to MT)</span>
        </span>
        {/* Native units stay the ground truth, demoted below the MT headline. */}
        <span className="exec-mt-native num">
          {unitQty(h.current_total, h.unit_of_measure)}
        </span>
        {h.quantities_by_unit && <UnitBreakdown units={h.quantities_by_unit} />}
      </div>

      <dl className="exec-compare">
        <div>
          <dt>Previous (as of {day(h.previous_as_of)})</dt>
          <dd>
            <FigureValue
              figure={h.previous_tonnes}
              render={mt}
              zeroNote="A measured zero — the demand book genuinely held nothing in this window then."
            />
            {h.previous.available && h.previous.value !== null && (
              <span className="exec-mt-native num">{qty(h.previous.value)}</span>
            )}
          </dd>
        </div>
        <div>
          <dt>Change (MT)</dt>
          <dd>
            <FigureValue
              figure={h.change_pct_tonnes}
              render={(v) => `${v > 0 ? "+" : ""}${v.toFixed(1)}%`}
            />
          </dd>
        </div>
      </dl>

      <p className="exec-definition">{h.definition}</p>
    </div>
  );
}

/* ---------------- Coverage ---------------- */

// Display names for the backend's CamelCase enum values. Presentation only —
// class names and API payloads keep the raw enum.
const STATUS_LABEL: Record<string, string> = {
  Covered: "Covered",
  CoveredViaSubstitute: "Covered via substitute",
  PendingApproval: "Pending approval",
  Uncovered: "Uncovered",
  Unrecoverable: "Unrecoverable",
  NotEvaluated: "Not evaluated",
};
const statusLabel = (s: string) => STATUS_LABEL[s] ?? s;

function ByStatusBar({ rows, unit }: { rows: CoverageByStatus[]; unit: string | null }) {
  const total = rows.reduce((s, r) => s + r.quantity, 0);
  if (total <= 0) return null;
  return (
    <div className="exec-coverage-status">
      <div className="exec-split-bar exec-status-bar" role="img" aria-label="Coverage by status">
        {rows
          .filter((r) => r.quantity > 0)
          .map((r) => (
            <div
              key={r.status}
              className={`exec-status-seg exec-status-seg-${r.status}`}
              style={{ width: `${(r.quantity / total) * 100}%` }}
              title={`${statusLabel(r.status)}: ${qty(r.quantity)}${unit ? " " + unit : ""}`}
            />
          ))}
      </div>
      <div className="exec-status-legend" aria-hidden="true">
        {rows
          .filter((r) => r.quantity > 0)
          .map((r) => (
            <span key={r.status} className="exec-status-legend-item">
              <span className={`exec-channel-dot exec-status-seg-${r.status}`} />
              {statusLabel(r.status)}
            </span>
          ))}
      </div>
      <div className="table-scroll">
        <table className="exec-status-table">
          <thead>
            <tr>
              <th>Status</th>
              <th>MT</th>
              <th>Quantity</th>
              <th>Lines</th>
              <th>Wells</th>
              <th>Counts as covered</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.status}>
                <td>
                  <span className={`badge badge-${r.status}`}>{statusLabel(r.status)}</span>
                </td>
                <td className="num">
                  <MtInline figure={r.tonnes} />
                </td>
                <td className="num">
                  {unitQty(r.quantity, r.unit_of_measure)}
                  <UnitBreakdown units={r.quantities_by_unit} />
                </td>
                <td className="num">{r.line_count}</td>
                <td className="num">{r.well_count}</td>
                <td>{r.counts_as_covered ? "Yes" : "No"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CoverageBlock({ coverage }: { coverage: ExecutiveCoverage }) {
  // The MT ratio exists even for a mixed-unit book (tonnes are one unit), so
  // it is preferred; the native ratio steps in only when the MT one is
  // refused (e.g. a partial conversion). Neither existing keeps the honest
  // refusal path.
  const headlinePct = coverage.coverage_pct_tonnes?.available
    ? coverage.coverage_pct_tonnes.value
    : coverage.coverage_pct;
  if (!coverage.available || headlinePct === null) {
    return (
      <section className="card exec-block">
        <h3>Coverage</h3>
        <Unavailable reason={coverage.reason} />
      </section>
    );
  }
  return (
    <section className="card exec-block">
      <h3>Coverage</h3>
      <div className="exec-headline">
        <span className="exec-figure-big num">{pct(headlinePct)}</span>
        <span className="exec-headline-label">
          of in-scope demand <strong>quantity</strong> covered
          {coverage.covered_tonnes?.available &&
          coverage.total_tonnes?.available ? (
            <>
              {" "}
              ({mt(coverage.covered_tonnes.value ?? 0)} of{" "}
              {mt(coverage.total_tonnes.value ?? 0)}{" "}
              <span className="exec-iu-converted">converted to MT</span>)
            </>
          ) : (
            coverage.covered_quantity !== null &&
            coverage.total_quantity !== null && (
              <>
                {" "}
                (
                {unitQty(coverage.covered_quantity, coverage.unit_of_measure)} of{" "}
                {unitQty(coverage.total_quantity, coverage.unit_of_measure)})
              </>
            )
          )}
        </span>
      </div>

      {coverage.by_status.length > 0 && (
        <ByStatusBar rows={coverage.by_status} unit={coverage.unit_of_measure} />
      )}

      {/* Well counts are demoted to a reference row — never the headline. */}
      <div className="exec-well-reference">
        <span className="exec-well-reference-label">
          Reference — by WELL count (do not confuse with the quantity figure above):
        </span>
        <dl className="exec-facts">
          <div>
            <dt>Well coverage</dt>
            <dd className="num">
              {coverage.well_coverage_pct === null ? "—" : pct(coverage.well_coverage_pct)}
            </dd>
          </div>
          <div>
            <dt>Covered wells</dt>
            <dd className="num">{coverage.covered_well_count ?? "—"}</dd>
          </div>
          <div>
            <dt>Uncovered wells</dt>
            <dd className="num exec-bad">{coverage.uncovered_well_count ?? "—"}</dd>
          </div>
          <div>
            <dt>Evaluated</dt>
            <dd className="num">{coverage.evaluated_well_count ?? "—"}</dd>
          </div>
          <div>
            <dt>Not evaluated</dt>
            <dd className="num">{coverage.unevaluated_well_count ?? "—"}</dd>
          </div>
        </dl>
      </div>
      <p className="exec-note-quiet">
        A well that is not evaluated has no demand inside the coverage engine&apos;s
        scope. It is neither covered nor uncovered and is excluded from both
        percentages above rather than counted as a failure.
      </p>
    </section>
  );
}

/* ---------------- Supply risk ---------------- */

function SupplyRiskBlock({
  risk,
  runout,
}: {
  risk: ExecutiveSupplyRisk;
  runout: FirstRunout;
}) {
  // ONE block for "what cannot be covered and when it first bites" -- the
  // unrecoverable headline and the first-runout well list answer the same
  // management question at two zoom levels, and splitting them made the page
  // read as two problems.
  return (
    <section className="card exec-block">
      <h3>Supply risk &amp; first runout</h3>
      {!risk.available || risk.unrecoverable_quantity === null ? (
        <Unavailable reason={risk.reason} />
      ) : (
        <>
          <div className="exec-headline">
            <span className="exec-figure-big num exec-bad">
              <FigureValue figure={risk.unrecoverable_tonnes} render={mt} />
            </span>
            <span className="exec-headline-label">
              not coverable even if ordered today{" "}
              <span className="exec-iu-converted">(converted to MT)</span>
              {" · "}
              {risk.unrecoverable_line_count ?? "—"} lines ·{" "}
              {risk.affected_well_count ?? "—"} wells
              {runout.earliest_first_runout_date && (
                <>
                  {" "}· first runout{" "}
                  <strong className="exec-bad">
                    {day(runout.earliest_first_runout_date)}
                  </strong>
                </>
              )}
            </span>
          </div>
        </>
      )}
      {runout.available && runout.wells.length > 0 && (
        <div className="table-scroll">
          <table className="exec-status-table">
            <thead>
              <tr>
                <th>Well</th>
                <th>Customer</th>
                <th>First runout</th>
                <th>Shortfall (MT)</th>
              </tr>
            </thead>
            <tbody>
              {runout.wells.map((w) => (
                <tr key={w.well_id}>
                  <td>
                    <Link to={`/wells/${w.well_id}`}>{w.well_name}</Link>
                  </td>
                  <td>{w.customer_name}</td>
                  <td className="num">{day(w.first_runout_date)}</td>
                  <td className="num">
                    <MtInline figure={w.shortfall_tonnes} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {runout.available && runout.wells.length === 0 && (
        <p className="empty">No well in this scope has a first-runout date.</p>
      )}
      {runout.truncated && (
        <p className="exec-note-quiet">
          Showing {runout.returned_well_count} of {runout.total_well_count} wells.
          The full list is on the Coverage Workspace.
        </p>
      )}
      {risk.note && <p className="exec-caveat">{risk.note}</p>}
    </section>
  );
}

/* ---------------- Soft allocation coverage ---------------- */

/**
 * This platform's OWN soft-allocation coverage — NOT Oracle's hard
 * allocation. Replaces the old `allocation` block entirely. Channels are
 * rendered in the draw-priority order the backend sends them, as a stacked
 * bar with quantities+units, and `note` (the anti-confusion sentence) is
 * always rendered verbatim.
 */
function SoftAllocationBlock({
  allocation,
  horizon,
  onHorizon,
  busy,
}: {
  allocation: SoftAllocationCoverage;
  horizon: AllocationHorizon;
  onHorizon: (h: AllocationHorizon) => void;
  busy: boolean;
}) {
  const total = allocation.total_quantity ?? 0;
  return (
    <section className="card exec-block">
      <div className="exec-block-head">
        <h3>Soft allocation coverage</h3>
        <div className="exec-horizon-picker" role="group" aria-label="Soft allocation horizon">
          {ALLOCATION_HORIZONS.map((m) => (
            <button
              key={m}
              type="button"
              className={`exec-horizon-btn${
                m === horizon ? " exec-horizon-btn-on" : ""
              }`}
              aria-pressed={m === horizon}
              disabled={busy}
              onClick={() => onHorizon(m)}
            >
              {m}m
            </button>
          ))}
        </div>
      </div>

      <p className="exec-note-quiet">
        How THIS platform's own soft allocation satisfied demand with ROS in
        the next {allocation.horizon_months} months. Not Oracle's hard
        allocation.
      </p>

      {!allocation.available || allocation.channels.length === 0 ? (
        <Unavailable reason={allocation.reason} />
      ) : (
        <>
          <div
            className="exec-split-bar exec-channel-bar"
            role="img"
            aria-label="Soft allocation channels"
          >
            {allocation.channels
              .filter((c) => c.quantity > 0)
              .map((c) => (
                <div
                  key={c.key}
                  className={`exec-channel-seg exec-channel-seg-${c.key}`}
                  style={{ width: total > 0 ? `${(c.quantity / total) * 100}%` : "0%" }}
                  title={`${c.label}: ${unitQty(c.quantity, c.unit_of_measure)} (${pct(c.pct)})`}
                />
              ))}
          </div>
          <div className="table-scroll">
            <table className="exec-status-table">
              <thead>
                <tr>
                  <th>Channel (draw order)</th>
                  <th>MT</th>
                  <th>Quantity</th>
                  <th>Lines</th>
                  <th>Share</th>
                </tr>
              </thead>
              <tbody>
                {allocation.channels.map((c) => (
                  <tr key={c.key}>
                    <td>
                      <span className={`exec-channel-dot exec-channel-seg-${c.key}`} />
                      {c.label}
                    </td>
                    <td className="num">
                      <MtInline figure={c.tonnes} />
                    </td>
                    <td className="num">
                      {unitQty(c.quantity, c.unit_of_measure)}
                      <UnitBreakdown units={c.quantities_by_unit} />
                    </td>
                    <td className="num">{c.line_count}</td>
                    <td className="num">{pct(c.pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <dl className="exec-facts">
            <div>
              <dt>Total in horizon (MT)</dt>
              <dd className="num">
                <MtInline figure={allocation.total_tonnes} />
              </dd>
            </div>
            <div>
              <dt>Total in horizon</dt>
              <dd className="num">
                {allocation.total_quantity === null
                  ? "—"
                  : unitQty(allocation.total_quantity, allocation.unit_of_measure)}
              </dd>
            </div>
            <div>
              <dt>Assignment provenance</dt>
              <dd>{allocation.assignment_source ?? "—"}</dd>
            </div>
          </dl>
        </>
      )}
      {/* The anti-confusion text the owner asked for by name — verbatim. */}
      {allocation.note && <p className="exec-caveat">{allocation.note}</p>}
    </section>
  );
}

/* ---------------- Inventory utilisation ---------------- */

/** Same horizon vocabulary as soft allocation; the two horizons are independent. */
const IU_HORIZONS = ALLOCATION_HORIZONS;
type IuHorizon = AllocationHorizon;

/**
 * "Tied" vs "idle" on-hand steel, in native units, with an MT convenience
 * total layered on top (C-05). The MT figures are a DISPLAY-LAYER convenience
 * only — `tied_by_unit` / `not_tied_by_unit` (native units) are the ground
 * truth. The split bar is drawn only when both sides collapse to one shared
 * unit; drawing a bar over mixed units would imply a conversion this platform
 * refuses to invent.
 */
function InventoryUtilisationBlock({
  iu,
  horizon,
  onHorizon,
  busy,
}: {
  iu: InventoryUtilisationOut;
  horizon: IuHorizon;
  onHorizon: (h: IuHorizon) => void;
  busy: boolean;
}) {
  const singleUnit =
    iu.tied_by_unit.length === 1 &&
    iu.not_tied_by_unit.length === 1 &&
    iu.tied_by_unit[0].unit_of_measure === iu.not_tied_by_unit[0].unit_of_measure;

  const tied = singleUnit ? iu.tied_by_unit[0].quantity : 0;
  const notTied = singleUnit ? iu.not_tied_by_unit[0].quantity : 0;
  const splitTotal = tied + notTied;

  return (
    <section className="card exec-block">
      <div className="exec-block-head">
        <h3>Inventory utilisation</h3>
        <div className="exec-horizon-picker" role="group" aria-label="Inventory utilisation horizon">
          {IU_HORIZONS.map((m) => (
            <button
              key={m}
              type="button"
              className={`exec-horizon-btn${m === horizon ? " exec-horizon-btn-on" : ""}`}
              aria-pressed={m === horizon}
              disabled={busy}
              onClick={() => onHorizon(m)}
            >
              {m}m
            </button>
          ))}
        </div>
      </div>

      {!iu.available ? (
        <Unavailable reason={iu.reason} />
      ) : (
        <>
          <div className="exec-headline exec-iu-headlines">
            <div>
              <FigureValue
                figure={iu.tied_tonnes}
                render={(v) => unitQty(v, "t")}
                zeroNote="A measured zero — nothing on-hand is tied to demand this horizon."
              />
              <span className="exec-headline-label">
                tied to demand <span className="exec-iu-converted">(converted to MT)</span>
              </span>
            </div>
            <div>
              <FigureValue
                figure={iu.not_tied_tonnes}
                render={(v) => unitQty(v, "t")}
                zeroNote="A measured zero — all on-hand is tied to demand this horizon."
              />
              <span className="exec-headline-label">
                idle <span className="exec-iu-converted">(converted to MT)</span>
              </span>
            </div>
          </div>

          <div className="exec-iu-native">
            <div>
              <span className="exec-iu-native-label">Tied, native units</span>
              <span className="exec-qty-multi">
                {iu.tied_by_unit.map((u) => (
                  <span key={u.unit_of_measure} className="exec-qty-unit">
                    {qty(u.quantity)} {u.unit_of_measure}
                  </span>
                ))}
              </span>
            </div>
            <div>
              <span className="exec-iu-native-label">Idle, native units</span>
              <span className="exec-qty-multi">
                {iu.not_tied_by_unit.map((u) => (
                  <span key={u.unit_of_measure} className="exec-qty-unit">
                    {qty(u.quantity)} {u.unit_of_measure}
                  </span>
                ))}
              </span>
            </div>
          </div>

          {singleUnit && splitTotal > 0 ? (
            <div
              className="exec-split-bar exec-iu-split-bar"
              role="img"
              aria-label="Tied vs idle inventory"
            >
              <div
                className="exec-iu-seg exec-iu-seg-tied"
                style={{ width: `${(tied / splitTotal) * 100}%` }}
                title={`Tied: ${unitQty(tied, iu.tied_by_unit[0].unit_of_measure)}`}
              />
              <div
                className="exec-iu-seg exec-iu-seg-idle"
                style={{ width: `${(notTied / splitTotal) * 100}%` }}
                title={`Idle: ${unitQty(notTied, iu.not_tied_by_unit[0].unit_of_measure)}`}
              />
            </div>
          ) : (
            <p className="exec-note-quiet">
              {/* A bar over mixed units would draw two different physical
                  quantities as though they were comparable lengths — the
                  dimensional lie this module exists to refuse. */}
              No split bar: tied and idle on-hand are not both in a single
              shared unit for this scope.
            </p>
          )}

          {iu.products.length > 0 && (
            <details className="exec-iu-details">
              <summary>
                Per-product detail ({iu.products.length} positions, most idle
                first)
              </summary>
              <div className="table-scroll">
                <table className="exec-status-table">
                  <thead>
                    <tr>
                      <th>Product</th>
                      <th>On-hand (MT)</th>
                      <th>Tied (MT)</th>
                      <th>Not tied (MT)</th>
                      <th>Overdue demand</th>
                    </tr>
                  </thead>
                  <tbody>
                    {/* Sorted by idle (not-tied) tonnes, largest first --
                        the block exists to surface idle steel. Rows whose MT
                        conversion is unavailable sort last and say so. Keyed
                        on (business_unit, product): unscoped, one product in
                        two BUs is legitimately two rows. */}
                    {[...iu.products]
                      .sort(
                        (a, b) =>
                          (b.not_tied_tonnes.value ?? -1) -
                          (a.not_tied_tonnes.value ?? -1)
                      )
                      .map((p) => (
                        <tr key={`${p.business_unit_id}:${p.product_id}`}>
                          <td>{p.product_description ?? p.product_id}</td>
                          <td className="num">
                            {p.tied_tonnes.available &&
                            p.not_tied_tonnes.available &&
                            p.tied_tonnes.value !== null &&
                            p.not_tied_tonnes.value !== null ? (
                              mt(p.tied_tonnes.value + p.not_tied_tonnes.value)
                            ) : (
                              <span className="exec-mt-na" title="Cannot convert to MT">
                                MT n/a · {qty(p.on_hand_quantity)} {p.unit_of_measure}
                              </span>
                            )}
                          </td>
                          <td className="num">
                            <MtInline figure={p.tied_tonnes} />
                          </td>
                          <td className="num">
                            <MtInline figure={p.not_tied_tonnes} />
                          </td>
                          <td className="num">
                            {/* Native units, deliberately: this is a slice of
                                demand_in_window, not an MT headline. Counted
                                into the tie AND labelled (2026-08-12). */}
                            {p.demand_overdue > 0 ? (
                              <span
                                className="surplus-overdue"
                                title="Demand whose ROS month has already passed. It still counts toward the tie; it is labelled rather than silently blended."
                              >
                                {qty(p.demand_overdue)} {p.unit_of_measure}
                              </span>
                            ) : (
                              <span className="mor-quiet">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}

          {iu.unconvertible.length > 0 && (
            <div className="exec-iu-unconvertible">
              <p className="exec-iu-panel-title">
                Real quantities excluded from the MT headline
              </p>
              <ul>
                {iu.unconvertible.map((u, i) => (
                  <li key={i}>
                    {u.product_description ?? u.product_id} — {u.side}:{" "}
                    {unitQty(u.quantity, u.unit_of_measure)}. {u.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {iu.unknown_position.length > 0 && (
            <div className="exec-iu-unknown">
              <p className="exec-iu-panel-title">
                On-hand position unknown ({iu.unknown_position_count}) — excluded from
                both totals, not counted as zero
              </p>
              <ul>
                {iu.unknown_position.map((u, i) => (
                  <li key={i}>
                    {u.product_description ?? u.product_id} ({u.unit_of_measure})
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      {iu.reason && iu.available && <p className="exec-note-quiet">{iu.reason}</p>}
      {iu.note && <p className="exec-caveat">{iu.note}</p>}
    </section>
  );
}

/* ---------------- Page ---------------- */

export default function ExecutiveDashboard() {
  const [customers, setCustomers] = useState<CustomerSummary[]>([]);
  const [units, setUnits] = useState<BusinessUnitOut[]>([]);
  // `?customer=` / `?bu=` are honoured so Administration can deep-link.
  const [params, setParams] = useSearchParams();
  const customerId = params.get("customer") ?? "";
  const businessUnitId = params.get("bu") ?? "";

  function setScope(next: { customer?: string; bu?: string }) {
    const merged = {
      customer: next.customer !== undefined ? next.customer : customerId,
      bu: next.bu !== undefined ? next.bu : businessUnitId,
    };
    const q: Record<string, string> = {};
    if (merged.customer) q.customer = merged.customer;
    if (merged.bu) q.bu = merged.bu;
    setParams(q);
  }

  const [horizon, setHorizon] = useState<AllocationHorizon>(12);
  const [iuHorizon, setIuHorizon] = useState<IuHorizon>(12);
  // Demand scope (well status / line profile). null = the user has not
  // touched the boxes: no params are sent, the SERVER's effective default
  // applies, and the checkboxes are then seeded from the response's
  // status_scope/profile_scope -- so an admin-persisted default renders
  // correctly instead of the shipped constants (the CoverageWorkspace
  // pattern). Once touched, the explicit selection is sent.
  const [statuses, setStatuses] = useState<string[] | null>(null);
  const [profiles, setProfiles] = useState<string[] | null>(null);
  const [data, setData] = useState<ExecutiveDashboardData | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api.getCustomers().then(setCustomers).catch(() => setCustomers([]));
    api.getBusinessUnits().then(setUnits).catch(() => setUnits([]));
  }, []);

  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .getExecutiveDashboard({
        customer_id: customerId || undefined,
        business_unit_id: businessUnitId || undefined,
        allocation_horizon_months: horizon,
        inventory_utilisation_horizon_months: iuHorizon,
        status: statuses ?? undefined,
        profile: profiles ?? undefined,
      })
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
        setFetchedAt(new Date());
      })
      .catch((e) => {
        if (!live) return;
        setError(e);
        setData(null);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [customerId, businessUnitId, horizon, iuHorizon, statuses, profiles, reload]);


  // A customer belongs to at most one BU, so narrowing the BU selector to
  // customers actually in the chosen BU keeps the two controls from producing
  // the "empty by construction" 400 as a matter of course (the backend still
  // guards it, but the UI should not invite the contradiction).
  const customerOptions = businessUnitId
    ? customers.filter((c) => c.business_unit_id === businessUnitId)
    : customers;

  return (
    <div>
      <div className="scenario-head">
        <div>
          <h1>Executive Dashboard</h1>
          <p className="scenario-sub">
            {data ? data.scope.description : "Demand trend, coverage, supply risk and soft allocation."}
            {data && <> Generated {stamp(data.generated_at)}.</>}
          </p>
        </div>
        {/* .exec-filters wraps -- the previous inline flex row could not, and
            on a 390px phone it forced the whole page 523px wide. */}
        <div className="exec-filters">
          <label className="filter-field">
            <span>Business Unit</span>
            <select
              value={businessUnitId}
              onChange={(e) => {
                const bu = e.target.value;
                // Drop a customer that would contradict the new BU.
                const stillValid =
                  !bu || customers.find((c) => c.id === customerId)?.business_unit_id === bu;
                setScope({ bu, customer: stillValid ? undefined : "" });
              }}
            >
              <option value="">All Business Units</option>
              {units.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.name}
                </option>
              ))}
            </select>
          </label>
          <label className="filter-field">
            <span>Customer</span>
            <select
              value={customerId}
              onChange={(e) => setScope({ customer: e.target.value })}
            >
              <option value="">All customers</option>
              {customerOptions.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <div className="filter-field">
            <span>Demand scope</span>
            <ScopeChecks
              statuses={statuses ?? data?.status_scope ?? DEFAULT_STATUS_SCOPE}
              profiles={profiles ?? data?.profile_scope ?? DEFAULT_PROFILE_SCOPE}
              onStatuses={setStatuses}
              onProfiles={setProfiles}
            />
          </div>
        </div>
      </div>

      {data && (
        <p className="exec-scope-name">
          <Freshness
            fetchedAt={fetchedAt}
            busy={loading}
            onRefresh={() => setReload((r) => r + 1)}
          />
          {"  "}Scope: <strong>{data.scope.label}</strong>
          <span className="exec-scope-demand">
            {" "}· status {data.status_scope.join(", ")} · profile{" "}
            {data.profile_scope.join(", ")}
          </span>
        </p>
      )}

      {data && data.skipped_customers.length > 0 && (
        <p className="exec-scope-projection" role="status">
          {data.skipped_customers.join(", ")} could not be recomputed for this
          scope (no Business Unit or missing inventory rows) — their figures
          keep the stored default-scope verdicts.
        </p>
      )}

      {data && !data.scope_is_default && (
        <p className="exec-scope-projection" role="status">
          Recomputed read-only for status [{data.status_scope.join(", ")}] /
          profile [{data.profile_scope.join(", ")}] — every block below uses
          this scope. These are NOT the official stored verdicts; set the
          checkboxes back to Confirmed / Primary + Contingency to return to
          them.
        </p>
      )}

      <p className="exec-honesty">
        Every block below reports whether its figure could be computed. Where it
        could not, the reason is printed in place of the number — a zero is never
        substituted for an unknown.
      </p>

      {error !== null && (
        <LoadError what="the executive dashboard" error={error} />
      )}

      {loading && !data && <p>Loading…</p>}

      {data && (
        <>
          {data.notes.length > 0 && (
            <section className="exec-notes">
              <h3>What these figures do and do not include</h3>
              <ul>
                {data.notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            </section>
          )}

          <section className="card exec-block">
            <h3>Demand trend</h3>
            <div className="exec-horizons">
              {data.demand_trend.horizons.map((h) => (
                <HorizonCard key={h.months} h={h} />
              ))}
            </div>
            {data.demand_trend.notes.map((n, i) => (
              <p key={i} className="exec-note-quiet">
                {n}
              </p>
            ))}
            <p className="exec-note-quiet">
              The prior-period figure is reconstructed from demand revision
              history, so it is unavailable for demand lines created before that
              history existed. Where it is unavailable the reason is shown
              instead of a number.
            </p>
          </section>

          <CoverageBlock coverage={data.coverage} />
          <SupplyRiskBlock risk={data.supply_risk} runout={data.first_runout} />
          <SoftAllocationBlock
            allocation={data.soft_allocation_coverage}
            horizon={horizon}
            onHorizon={setHorizon}
            busy={loading}
          />
          <InventoryUtilisationBlock
            iu={data.inventory_utilisation}
            horizon={iuHorizon}
            onHorizon={setIuHorizon}
            busy={loading}
          />

        </>
      )}
    </div>
  );
}
