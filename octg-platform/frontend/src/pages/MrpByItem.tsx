import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  ByItemAnalysis,
  InventoryPosition,
  LeadTimeBreakdown,
  MrpRecommendation,
  RunoutPoint,
} from "../api/client";
import Breadcrumbs from "../components/Breadcrumbs";
import CoverageBadge from "../components/CoverageBadge";
import LeadTimePanel, { LeadTimeCell } from "../components/LeadTime";
import LoadError from "../components/LoadError";
import ReasonText from "../components/ReasonText";
import ScopeNote from "../components/ScopeNote";
import { formatDay, isPastDay } from "./MrpSummary";

function monthOf(value: string) {
  return value.slice(0, 7);
}

/* ---------------- Inventory ---------------- */

/**
 * `assigned` and `on_order` are Oracle-owned. Until that feed is integrated the
 * API sends 0, which a planner would read as a real measured zero. So when
 * oracle_integrated is false we render an em dash plus an explicit
 * "not integrated" label instead of the number.
 */
function InventorySection({ inventory }: { inventory: InventoryPosition }) {
  const integrated = inventory.oracle_integrated;
  return (
    <section className="card mrp-block">
      <h3>Inventory</h3>
      <div className="inv-grid">
        <div className="inv-cell">
          <span className="inv-label">On Hand</span>
          <span className="inv-value num">
            {inventory.on_hand.toLocaleString()} {inventory.unit_of_measure}
          </span>
          <span className="inv-note">Measured in this system.</span>
        </div>
        <div className={`inv-cell${integrated ? "" : " inv-cell-missing"}`}>
          <span className="inv-label">Assigned</span>
          <span className="inv-value num">
            {integrated ? `${inventory.assigned.toLocaleString()} ${inventory.unit_of_measure}` : "—"}
          </span>
          <span className="inv-note">
            {integrated ? "From Oracle." : "Not integrated — Oracle feed not connected."}
          </span>
        </div>
        <div className={`inv-cell${integrated ? "" : " inv-cell-missing"}`}>
          <span className="inv-label">On Order</span>
          <span className="inv-value num">
            {integrated && inventory.on_order != null
              ? `${inventory.on_order.toLocaleString()} ${inventory.unit_of_measure}`
              : "—"}
          </span>
          <span className="inv-note">
            {integrated ? "From Oracle." : "Not integrated — Oracle feed not connected."}
          </span>
          {((inventory.on_order_poed ?? 0) > 0 ||
            (inventory.on_order_booked ?? 0) > 0 ||
            (inventory.on_order_unstated ?? 0) > 0) && (
            <span className="inv-note">
              {(inventory.on_order_booked ?? 0) > 0 &&
                `Booked ${(inventory.on_order_booked ?? 0).toLocaleString()}`}
              {(inventory.on_order_booked ?? 0) > 0 &&
              (inventory.on_order_poed ?? 0) > 0
                ? " · "
                : ""}
              {(inventory.on_order_poed ?? 0) > 0 &&
                `PO'ed ${(inventory.on_order_poed ?? 0).toLocaleString()}`}
              {(inventory.on_order_unstated ?? 0) > 0 &&
                ` · stage not stated ${(inventory.on_order_unstated ?? 0).toLocaleString()}`}
            </span>
          )}
        </div>
      </div>
      {!integrated && (
        <p className="inv-warning">
          Assigned and On Order are owned by Oracle and are not yet integrated. They are
          unknown, not zero — treat this inventory position as incomplete.
        </p>
      )}
    </section>
  );
}

/* ---------------- Runout chart ---------------- */

function RunoutChart({
  runout,
  runoutMonth,
  runoutWithOrder,
  runoutMonthWithOrder,
  recommendation,
}: {
  runout: RunoutPoint[];
  runoutMonth: string | null;
  runoutWithOrder: RunoutPoint[];
  runoutMonthWithOrder: string | null;
  recommendation: MrpRecommendation | null;
}) {
  if (runout.length === 0) {
    return (
      <section className="card mrp-block">
        <h3>Runout</h3>
        <div className="empty">No runout series for this product.</div>
      </section>
    );
  }

  const unit = runout[0]?.unit_of_measure;
  const orderMonth = recommendation ? monthOf(recommendation.recommended_order_date) : null;
  const shipMonth = recommendation ? monthOf(recommendation.required_ship_date) : null;
  const hasCompare = runoutWithOrder.length > 0;

  const withOrderByMonth = new Map(runoutWithOrder.map((p) => [p.month, p]));

  const maxAbs = Math.max(
    ...runout.map((p) => Math.abs(p.closing_balance)),
    ...runoutWithOrder.map((p) => Math.abs(p.closing_balance)),
    1
  );
  const H = 110; // px for the tallest bar in either direction

  // Does the recommended order actually close the gap, and by when?
  let compareNote: { text: string; improves: boolean } | null = null;
  if (hasCompare) {
    if (runoutMonth && !runoutMonthWithOrder) {
      compareNote = {
        text: `With the recommended order placed, the balance never goes negative within the projected horizon — the order closes the gap (baseline runout was ${runoutMonth}).`,
        improves: true,
      };
    } else if (runoutMonth && runoutMonthWithOrder && runoutMonthWithOrder !== runoutMonth) {
      compareNote = {
        text: `With the recommended order placed, runout moves from ${runoutMonth} to ${runoutMonthWithOrder}.`,
        improves: true,
      };
    } else if (!runoutMonth && !runoutMonthWithOrder) {
      compareNote = {
        text: "No shortage in either series — the recommended order does not change the outlook.",
        improves: false,
      };
    } else if (runoutMonth && runoutMonthWithOrder === runoutMonth) {
      compareNote = {
        text: `The recommended order does not move the runout month (still ${runoutMonth}) — it does not close the gap.`,
        improves: false,
      };
    }
  }

  return (
    <section className="card mrp-block">
      <h3>Runout</h3>
      <p className="runout-caption">
        {runoutMonth
          ? `Baseline balance crosses zero in ${runoutMonth} — that is the runout month.`
          : "Baseline balance never crosses zero within the projected horizon."}
      </p>

      {hasCompare && (
        <div className="runout-legend">
          <span className="runout-legend-item">
            <span className="runout-legend-swatch runout-legend-swatch-actual" />
            Actual (baseline)
          </span>
          <span className="runout-legend-item">
            <span className="runout-legend-swatch runout-legend-swatch-rec" />
            With recommended order placed (hypothetical)
          </span>
        </div>
      )}

      <div className="table-scroll">
        <div className="runout-chart" role="img" aria-label={
          runoutMonth
            ? `Monthly closing balance. Runout month is ${runoutMonth}.`
            : "Monthly closing balance. No runout within the horizon."
        }>
          {runout.map((p) => {
            const neg = p.closing_balance < 0;
            const h = Math.round((Math.abs(p.closing_balance) / maxAbs) * H);
            const isRunout = p.month === runoutMonth;

            const withOrder = withOrderByMonth.get(p.month);
            const negRec = withOrder ? withOrder.closing_balance < 0 : false;
            const hRec = withOrder
              ? Math.round((Math.abs(withOrder.closing_balance) / maxAbs) * H)
              : 0;
            const isRunoutRec = hasCompare && p.month === runoutMonthWithOrder;

            return (
              <div
                key={p.month}
                className={`runout-col${isRunout ? " runout-col-crossing" : ""}${
                  isRunoutRec ? " runout-col-rec-crossing" : ""
                }`}
                title={`${p.month}: baseline closing ${p.closing_balance.toLocaleString()}${
                  withOrder
                    ? `; with recommended order ${withOrder.closing_balance.toLocaleString()}`
                    : ""
                } (demand ${p.demand.toLocaleString()})`}
              >
                <div className="runout-pos">
                  {!neg && <div className="runout-bar runout-bar-pos" style={{ height: h }} />}
                  {hasCompare && withOrder && !negRec && (
                    <div className="runout-bar runout-bar-rec" style={{ height: hRec }} />
                  )}
                </div>
                <div className="runout-axis" />
                <div className="runout-neg">
                  {neg && <div className="runout-bar runout-bar-neg" style={{ height: h }} />}
                  {hasCompare && withOrder && negRec && (
                    <div className="runout-bar runout-bar-rec" style={{ height: hRec }} />
                  )}
                </div>
                <div className="runout-month">
                  {p.month.slice(5)}
                  <br />
                  <span className="runout-year">{p.month.slice(0, 4)}</span>
                </div>
                <div className="runout-flags">
                  {isRunout && <span className="runout-flag runout-flag-zero">runout</span>}
                  {isRunoutRec && !isRunout && (
                    <span className="runout-flag runout-flag-zero">runout (w/ order)</span>
                  )}
                  {p.month === orderMonth && (
                    <span className="runout-flag runout-flag-order">order</span>
                  )}
                  {p.month === shipMonth && (
                    <span className="runout-flag runout-flag-ship">ship</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {compareNote && (
        <p
          className={`runout-compare-note ${
            compareNote.improves ? "runout-compare-note-improves" : "runout-compare-note-same"
          }`}
        >
          {compareNote.text}
        </p>
      )}

    </section>
  );
}

/* ---------------- Monthly ledger ----------------
   The one full-plan table (2026-08-12 product-owner rework): opening carried
   from last month (ownership split), incoming split into REAL on-order and
   SUGGESTED order (a promise and a suggestion are different facts, hence
   different colours), outgoing demand, ownership-split ending balance. The
   chart above keeps its question-specific series; this is the readable
   month-by-month account. */

function LedgerSection({
  ledger,
  ledgerRunoutMonth,
  undatedOnOrder,
  unit,
}: {
  ledger: RunoutPoint[];
  ledgerRunoutMonth: string | null;
  undatedOnOrder: number;
  unit: string;
}) {
  if (ledger.length === 0) return null;
  const q = (v: number) => v.toLocaleString();
  return (
    <section className="card mrp-block">
      <h3>Monthly ledger</h3>
      <p className="runout-caption">
        The full plan, month by month: opening stock (carried from the previous
        month), incoming — <span className="ledger-onorder-text">on order
        (promised POs)</span> and <span className="ledger-suggested-text">
        suggested order (this platform&apos;s recommendation — not placed)
        </span> — minus demand, giving the ending balance. Ownership is split
        throughout because customer-owned steel is drawn first and is never
        ours to reassign.
      </p>
      <div className="table-scroll">
        <table className="runout-table ledger-table">
          <thead>
            <tr>
              <th>Month</th>
              <th>Opening</th>
              <th className="ledger-sub">· Customer-owned</th>
              <th className="ledger-sub">· Company</th>
              <th className="ledger-onorder">+ On order</th>
              <th className="ledger-suggested">+ Suggested order</th>
              <th>− Demand</th>
              <th className="ledger-sub">· Contingency</th>
              <th>Ending</th>
              <th className="ledger-sub">· Customer-owned</th>
              <th className="ledger-sub">· Company</th>
            </tr>
          </thead>
          <tbody>
            {ledger.map((p) => (
              <tr
                key={p.month}
                className={p.month === ledgerRunoutMonth ? "row-crossing" : undefined}
              >
                <td className="num">{p.month}</td>
                <td className="num">{q(p.opening_balance)}</td>
                <td className="num ledger-sub">{q(p.opening_customer_owned)}</td>
                <td className="num ledger-sub">{q(p.opening_company)}</td>
                <td className="num ledger-onorder">
                  {p.incoming_on_order > 0 ? q(p.incoming_on_order) : "·"}
                </td>
                <td className="num ledger-suggested">
                  {p.incoming_recommended > 0 ? q(p.incoming_recommended) : "·"}
                </td>
                <td className="num">{p.demand > 0 ? q(p.demand) : "·"}</td>
                <td className="num ledger-sub">
                  {p.demand_contingency > 0 ? q(p.demand_contingency) : "·"}
                </td>
                <td className="num">
                  <strong>{q(p.closing_balance)}</strong>
                  {p.month === ledgerRunoutMonth ? " (runout)" : ""}
                </td>
                <td className="num ledger-sub">{q(p.closing_customer_owned)}</td>
                <td className="num ledger-sub">{q(p.closing_company)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="runout-caption">
        All quantities in {unit}.
        {undatedOnOrder > 0 && (
          <>
            {" "}
            A further {q(undatedOnOrder)} {unit} is on order WITHOUT a promised
            arrival date — it has no month to land in, so it appears in no row
            above.
          </>
        )}
        {ledgerRunoutMonth
          ? ` Even with everything on order and the suggested order placed, the balance goes negative in ${ledgerRunoutMonth}.`
          : " With everything on order and the suggested order placed, the balance stays positive across the horizon."}
      </p>
    </section>
  );
}

/* ---------------- Recommendation ---------------- */

function RecommendationCard({
  rec,
  runoutMonth,
}: {
  rec: MrpRecommendation;
  runoutMonth: string | null;
}) {
  const past = isPastDay(rec.recommended_order_date);
  return (
    <div className={`rec-card${rec.unrecoverable ? " rec-card-bad" : " rec-card-ok"}`}>
      <div className="rec-head">
        <strong>
          {rec.quantity.toLocaleString()} {rec.unit_of_measure}
        </strong>
        <span className={`badge ${rec.unrecoverable ? "badge-Unrecoverable" : "badge-Covered"}`}>
          {rec.unrecoverable ? "Unrecoverable — escalate" : "Orderable"}
        </span>
      </div>
      <dl className="rec-facts">
        <div>
          <dt>ROS</dt>
          <dd className="num">{formatDay(rec.ros_date)}</dd>
        </div>
        <div>
          <dt>Required ship</dt>
          <dd className="num">{formatDay(rec.required_ship_date)}</dd>
        </div>
        <div>
          <dt>Recommended order</dt>
          <dd className="num">{formatDay(rec.recommended_order_date)}</dd>
        </div>
        <div>
          <dt>Lead time</dt>
          {/* Never the bare scalar — it is 0 when not modelled. */}
          <dd>
            <LeadTimeCell
              lead={rec.lead_time}
              fallbackMonths={rec.lead_time_months}
            />
          </dd>
        </div>
      </dl>
      {rec.lead_time && !rec.lead_time.modelled && (
        <p className="rec-lt-unmodelled">
          These dates were computed with an unmodelled lead time, so treat them as
          unfounded rather than optimistic: the order date below is not a date you
          can plan to.
        </p>
      )}
      {past ? (
        <p className="rec-past">
          The recommended order date {formatDay(rec.recommended_order_date)} is already in
          the past. Placing this order today still lands after the required ship date, so
          the ROS cannot be met by mill order — this needs escalation, not a purchase
          order.
        </p>
      ) : (
        <p className="rec-timeline">
          Order by {formatDay(rec.recommended_order_date)} to ship by{" "}
          {formatDay(rec.required_ship_date)}
          {runoutMonth ? `, ahead of the ${runoutMonth} runout.` : "."}
        </p>
      )}
      <div className="rec-reason">
        <ReasonText reason={rec.reason} />
      </div>
    </div>
  );
}

/* ---------------- Page ---------------- */

export default function MrpByItem() {
  const { productId } = useParams<{ productId: string }>();
  const [data, setData] = useState<ByItemAnalysis | null>(null);
  const [error, setError] = useState<unknown>(null);
  /**
   * By Item legitimately fails when the product has no InventoryOnHand row in
   * any Business Unit (424), or the customer is unmapped (409) — the inventory
   * engine refuses to invent a quantity. But lead time depends only on the
   * product's attributes, so it is knowable either way. `GET /mrp/lead-time/{id}`
   * exists for exactly this case: when the analysis cannot be built we still show
   * the breakdown next to the explained failure, instead of an empty screen.
   */
  const [leadOnly, setLeadOnly] = useState<LeadTimeBreakdown | null>(null);

  useEffect(() => {
    if (!productId) return;
    setData(null);
    setError(null);
    setLeadOnly(null);
    api
      .getMrpByItem(productId)
      .then(setData)
      .catch((e) => {
        setError(e);
        api.getLeadTime(productId).then(setLeadOnly).catch(() => setLeadOnly(null));
      });
  }, [productId]);

  if (error) {
    return (
      <div>
        <Breadcrumbs trail={[{ label: "MRP", to: "/mrp" }, { label: "Item" }]} />
        <h1>{productId}</h1>
        <p>
          <Link to="/products">Back to Products</Link> ·{" "}
          <Link to="/mrp">MRP Summary</Link>
        </p>
        <LoadError what="By Item analysis" error={error} />
        {leadOnly ? (
          <>
            <p className="lt-still-known">
              The inventory position is unavailable, but lead time depends only on
              this product&apos;s attributes — so it is still known and shown
              below.
            </p>
            <LeadTimePanel lead={leadOnly} title="Lead time (still known)" />
          </>
        ) : (
          <p className="lt-still-known">
            Lead time could not be retrieved either.
          </p>
        )}
      </div>
    );
  }
  if (!data)
    return (
      <div>
        <Breadcrumbs trail={[{ label: "MRP", to: "/mrp" }, { label: "Item" }]} />
        <p>Loading...</p>
      </div>
    );

  // Prefer the full list; fall back to the single headline recommendation when a
  // backend without `recommendations` is serving.
  const recs =
    data.recommendations && data.recommendations.length > 0
      ? data.recommendations
      : data.recommendation
      ? [data.recommendation]
      : [];

  return (
    <div>
      <Breadcrumbs
        trail={[
          { label: "MRP", to: "/mrp" },
          { label: data.product_description ?? "Item" },
        ]}
      />
      <h1>{data.product_description ?? data.product_id}</h1>
      <p>
        Why do we need this order? <Link to="/products">Back to Products</Link> ·{" "}
        <Link to="/mrp">MRP Summary</Link>
      </p>

      <section className="card mrp-block">
        <h3>Demand</h3>
        {/*
          A demand line listed here does not always ASK for this product. Coverage
          charges consumption to whichever product actually satisfied it, so when
          this product is acting as a substitute the line belongs to a different
          product. A "Product" column would settle that at a glance, but
          `ByItemDemandLine` carries no product_id or product_description — the
          payload does not support one, so it is said in words rather than
          fabricated from the page's own product. (Backend: adding product_id /
          product_description to ByItemDemandLine would let this become a column.)
        */}
        <p className="exec-note-quiet">
          These are the demand lines this product&apos;s inventory is consumed by.
          Where this product is acting as a substitute, the line may have been
          raised against a different product — consumption is charged to whichever
          product actually satisfied it.
        </p>
        {data.demand_lines.length === 0 ? (
          <div className="empty">No consuming wells.</div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Well</th>
                  <th>Customer</th>
                  <th>Profile</th>
                  <th>Qty</th>
                  <th>ROS</th>
                  <th>Coverage</th>
                  <th>Reason</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {data.demand_lines.map((l) => (
                  <tr key={l.demand_line_id}>
                    <td>
                      <Link to={`/wells/${l.well_id}`}>{l.well_name}</Link>
                    </td>
                    <td>{l.customer_name ?? "—"}</td>
                    <td>{l.profile}</td>
                    <td className="num">
                      {l.quantity.toLocaleString()} {l.unit_of_measure}
                    </td>
                    <td className="num">{formatDay(l.ros_date)}</td>
                    <td>
                      <CoverageBadge status={l.coverage_status} />
                    </td>
                    <td className="mrp-reason">
                      <ReasonText reason={l.coverage_reason} />
                    </td>
                    <td>
                      {/* Same rule as the Well Workspace: a line that is not
                          yet satisfied links straight to substitution. */}
                      {l.coverage_status &&
                      ["Uncovered", "PendingApproval", "Unrecoverable"].includes(
                        l.coverage_status
                      ) ? (
                        <Link
                          to={`/demand-lines/${l.demand_line_id}/substitution`}
                        >
                          Substitutes
                        </Link>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {data.lead_time && <LeadTimePanel lead={data.lead_time} />}

      <ScopeNote />

      <InventorySection inventory={data.inventory} />

      <RunoutChart
        runout={data.runout}
        runoutMonth={data.runout_month}
        runoutWithOrder={data.runout_with_recommended_order ?? []}
        runoutMonthWithOrder={data.runout_month_with_recommended_order ?? null}
        recommendation={data.recommendation}
      />

      <LedgerSection
        ledger={data.ledger ?? []}
        ledgerRunoutMonth={data.ledger_runout_month ?? null}
        undatedOnOrder={data.ledger_undated_on_order ?? 0}
        unit={data.unit_of_measure}
      />

      <section className="card mrp-block">
        <h3>Recommendation</h3>
        {recs.length === 0 ? (
          <div className="empty">No order recommended for this product.</div>
        ) : (
          <div className="rec-list">
            {recs.map((r) => (
              <RecommendationCard
                key={`${r.product_id}-${r.unrecoverable}-${r.recommended_order_date}`}
                rec={r}
                runoutMonth={data.runout_month}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
