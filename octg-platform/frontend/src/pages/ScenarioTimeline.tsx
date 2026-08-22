/**
 * Scenario Planning — TIMELINE VIEW.
 *
 * A second INPUT METHOD for the exact same `ScenarioOverride` rows the
 * form-based editor writes. Nothing here is a new scenario concept:
 *
 *   - it reads the lines from `ScenarioImpact.line_changes`, the same preview the
 *     form view renders its impact tables from, using the `*_after` values so a
 *     block always sits where the scenario currently says it sits;
 *   - a drop calls `api.addScenarioOverride` / `api.deleteScenarioOverride` —
 *     POST/DELETE /scenarios/{id}/overrides — which is what `AddOverrideForm`
 *     calls, with the same payload shape;
 *   - it then calls the parent's `onChanged`, which is `ScenarioEditor.reload`,
 *     so the override table, the impact tiles and the Apply panel all refresh
 *     from the server rather than from any local copy.
 *
 * There is therefore NO separate timeline state to drift: the timeline is a
 * rendering of the overrides, exactly as the tables are.
 *
 * WHY LANE-PER-DEMAND-LINE, GROUPED UNDER A WELL HEADER
 * ----------------------------------------------------
 * A well carries several lines at different ROS dates and different products, and
 * the thing being dragged is a LINE (a `DemandLine` override names one line id).
 * Stacking a well's lines into one row would overlap blocks whose dates are close
 * and — worse — make the drag target ambiguous, when the override it produces is
 * unambiguously per line. So each line gets its own lane, and the lanes are
 * grouped under a well header so the well reading ("this rig's whole programme")
 * survives. That keeps "plot demand on a timeline" literal at the granularity the
 * override vocabulary actually works at.
 *
 * WHY ON-ORDER SITS IN ITS OWN BAND, AND WHAT DRAGGING IT MEANS
 * -------------------------------------------------------------
 * Its own band because incoming supply is not reserved to any well, so drawing it
 * inside a well's lanes would imply an allocation that does not exist. It IS
 * draggable, and the drag restates the earliest expected arrival date as a
 * `PoArrival.arrival_date` override — which moves the RUNOUT PROJECTION and
 * deliberately nothing else. See `OnOrderBand` and `ON_ORDER_DRAG_REASON`.
 *
 * SIMULATING AN ORDER THAT DOES NOT EXIST, AND WHY IT IS CLICK-THEN-DRAG
 * ---------------------------------------------------------------------
 * The band also creates HYPOTHETICAL purchase orders — "what if we placed an
 * emergency order for 5000 metres arriving next March?" — as
 * `PoArrival.new_order` overrides. Two interaction shapes were considered:
 *
 *   (A) DRAG-TO-CREATE: rubber-band a new block out of empty space in the band,
 *       sized by drag distance and positioned by drop point.
 *   (B) CLICK-TO-PLACE, THEN DRAG: an "add" affordance drops a default-sized block,
 *       which is then dragged exactly like any other block to set date and quantity.
 *
 * (B) WAS CHOSEN, and not only because it is less work. `moveDrag` below is built
 * entirely around `baseX + dx` and `baseQty + qtyDelta` — it MOVES AN EXISTING
 * BLOCK. (A) needs a different state machine: no base block to measure from, two
 * live endpoints, and a commit step that must tell a deliberate draw from a stray
 * click on the band's background. That is a SECOND gesture implementation in a file
 * whose stated invariant is that one `moveDrag` serves every band, so no two bands
 * can disagree about which day a pixel is.
 *
 * (B) needs none of it. Once the override row exists, a hypothetical order IS just
 * another block with a base x and a base quantity — and unlike a real on-order block
 * it has BOTH axes to offer, because a planner owns both its date and its quantity.
 * So it reuses the DEMAND block's two-axis body drag verbatim (sideways = date,
 * up/down or the right edge = quantity) and gets a richer gesture than (A) would
 * have given, with no new gesture code at all.
 *
 * The block is drawn UNMISTAKABLY DIFFERENTLY from a real on-order block (dashed
 * border, hatched fill, "hypothetical" label): a real PO is an Oracle fact and this
 * is a planner's hypothesis, and the two must never be read off the chart as the
 * same kind of thing. It is also a MOMENT, not a span — quantity is its width, the
 * way a demand block works — whereas a real on-order block's width is the spread
 * between its first and last arrival. See `NEW_ORDER_REASON`.
 */

import { useEffect, useRef, useState } from "react";
import {
  api,
  InventoryPosition,
  LineCoverageChange,
  MrpRowChange,
  ScenarioDetail,
  ScenarioOverride,
  SupplyRunoutChange,
} from "../api/client";
import { formatDay } from "./MrpSummary";

/* ------------------------------------------------------------------ *
 * The time axis
 * ------------------------------------------------------------------ */

/** One month column, in px. The axis and every block share this constant. */
const MONTH_PX = 96;
/** Widest a demand block gets, at the largest quantity on screen. */
const QTY_PX = 130;
/** Narrowest a demand block gets, so the smallest quantity is still readable. */
const QTY_MIN_PX = 46;
/** Pointer travel before a drag commits to "date" or "quantity". */
const AXIS_LOCK_PX = 4;
/** Quantity drags snap to this. */
const QTY_SNAP = 10;

type Axis = {
  /** First month of the horizon, as {y, m} with m 0-based. */
  y0: number;
  m0: number;
  months: { y: number; m: number }[];
  width: number;
};

const MONTH_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function daysInMonth(y: number, m: number) {
  return new Date(y, m + 1, 0).getDate();
}

/** Parse the leading YYYY-MM-DD of an ISO string as a LOCAL calendar day.
 *
 * Deliberately not `new Date(iso)`: these ROS values carry a time component
 * (…T21:20:34) and letting the browser's timezone shift them could move a block
 * a whole day off the date the tables print beside it. The day is the fact. */
function parseDay(iso: string) {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return { y, m: m - 1, d };
}

function dayIso(y: number, m: number, d: number) {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${y}-${pad(m + 1)}-${pad(d)}`;
}

function buildAxis(isoDates: string[]): Axis | null {
  const days = isoDates.filter(Boolean).map(parseDay);
  if (days.length === 0) return null;
  const key = (p: { y: number; m: number }) => p.y * 12 + p.m;
  let lo = key(days[0]);
  let hi = lo;
  for (const p of days) {
    lo = Math.min(lo, key(p));
    hi = Math.max(hi, key(p));
  }
  // One month of padding each side so a block at the extreme is not flush
  // against the edge and can still be dragged outwards.
  lo -= 1;
  hi += 1;
  const months: { y: number; m: number }[] = [];
  for (let k = lo; k <= hi; k++) months.push({ y: Math.floor(k / 12), m: k % 12 });
  return {
    y0: months[0].y,
    m0: months[0].m,
    months,
    width: months.length * MONTH_PX,
  };
}

/** Day -> px. The single mapping; `xToDay` is its exact inverse. */
function dayToX(axis: Axis, iso: string) {
  const { y, m, d } = parseDay(iso);
  const mi = (y * 12 + m) - (axis.y0 * 12 + axis.m0);
  const frac = (d - 1) / daysInMonth(y, m);
  return (mi + frac) * MONTH_PX;
}

/** px -> YYYY-MM-DD, snapped to a whole day. */
function xToDay(axis: Axis, x: number): string {
  const clamped = Math.max(0, Math.min(axis.width - 0.001, x));
  const mi = Math.min(axis.months.length - 1, Math.floor(clamped / MONTH_PX));
  const { y, m } = axis.months[mi];
  const dim = daysInMonth(y, m);
  const frac = clamped / MONTH_PX - mi;
  const d = Math.max(1, Math.min(dim, Math.round(frac * dim) + 1));
  return dayIso(y, m, d);
}

/* ------------------------------------------------------------------ *
 * Override lookup — the timeline reads the SAME rows the table shows
 * ------------------------------------------------------------------ */

type DemandField = "ros_date" | "quantity";

function findOverride(
  overrides: ScenarioOverride[],
  lineId: string,
  field: DemandField
) {
  return (
    overrides.find(
      (o) =>
        o.target_kind === "DemandLine" &&
        o.field_name === field &&
        o.target_demand_line_id === lineId
    ) ?? null
  );
}

/* ------------------------------------------------------------------ *
 * On-order
 * ------------------------------------------------------------------ */

/**
 * What dragging an on-order block DOES, and what it deliberately does not.
 *
 * This block used to be display-only, on the grounds that `PoArrival.arrival_date`
 * was UNMODELLED — nothing read it, so the preview would not move and offering the
 * drag would have been dishonest. That is no longer true: the engine now folds real
 * `InventoryOnOrder` rows into the runout month-walk and reports the shift in
 * `ScenarioImpact.supply_runout_changes`, so the drag has a real effect to produce.
 *
 * The limits are still real and are stated on the block rather than hidden:
 *   - it moves the RUNOUT PROJECTION, not a coverage verdict;
 *   - it shifts the EARLIEST arrival and every later PO for the product moves with
 *     it by the same number of days (the schedule keeps its shape) — which is why
 *     a product-scoped override is honest even though a product can have many POs;
 *   - it can never be APPLIED, because InventoryOnOrder is a read-only Oracle
 *     projection.
 */
const ON_ORDER_DRAG_REASON =
  "Drag sideways to restate the EARLIEST expected arrival date. Any later " +
  "purchase order for this product shifts by the same number of days, so the " +
  "delivery schedule keeps its shape. This moves the RUNOUT PROJECTION and " +
  "nothing else: coverage verdicts are decided from on-hand stock alone, so " +
  "Covered/Uncovered will not change, and neither will the MRP recommendation " +
  "rows that derive from them. The override can be previewed but never applied — " +
  "purchase orders are Oracle-owned and this platform holds a read-only copy.";

const ON_ORDER_UNDATED_REASON =
  "This purchase order has NO promised arrival date, so it has no position on the " +
  "timeline to drag. Restating a date it does not have would invent supply " +
  "arriving in a month. It is counted in the on-order total and excluded from the " +
  "runout projection. Get the date acknowledged in Oracle first.";

/**
 * What a HYPOTHETICAL order block is, and what it is not.
 *
 * Stated on the block itself rather than only in the help text, because this is the
 * one block on the chart that does not correspond to anything real, and a planner
 * arriving at a shared scenario mid-conversation must not have to infer that.
 */
const NEW_ORDER_REASON =
  "HYPOTHETICAL — this purchase order DOES NOT EXIST. It is your own assertion, " +
  "recorded as a PoArrival.new_order override, added to the RUNOUT PROJECTION at " +
  "its month beside whatever real purchase orders this product already has. " +
  "No purchase order was created anywhere: Oracle owns purchase orders and this " +
  "platform holds a read-only copy it never writes to. " +
  "It moves NO coverage verdict however large it is — coverage is decided from " +
  "on-hand stock alone — and so it moves no MRP recommendation row either. " +
  "It can NEVER be applied: applying it would mean placing an order, and mill " +
  "ordering is a human last-resort decision this platform recommends but does not " +
  "execute.\n\n" +
  "Drag sideways to change its arrival date, up/down (or grab the right edge) to " +
  "change the quantity. The × removes it — it is just an override row.";

/** Days out from today a newly-added hypothetical order is placed at.
 *
 * A quarter is a deliberately unremarkable starting point, not a lead-time claim:
 * this platform models real lead times in `app.engines.lead_time` and a default here
 * that LOOKED like one would be a second, wrong answer. The planner drags it to the
 * date they mean, and the number they land on is the one the override records. */
const NEW_ORDER_DEFAULT_DAYS = 90;

type OnOrderRow = {
  productId: string;
  description: string;
  position: InventoryPosition;
};

/** Every hypothetical order this scenario asserts for a product.
 *
 * A LIST, unlike `findArrivalOverride`'s single row, and for the reason the resolver
 * keeps a list too: "2000 in March and 3000 in June" is one coherent question, so
 * each block is its OWN override row identified by its own id. That is also why a
 * re-drag here updates one row by id rather than doing the delete-the-only-one dance
 * the arrival-date drag has to do. */
function findNewOrderOverrides(overrides: ScenarioOverride[], productId: string) {
  return overrides
    .filter(
      (o) =>
        o.target_kind === "PoArrival" &&
        o.field_name === "new_order" &&
        o.target_product_id === productId
    )
    .sort((a, b) => (a.value_date ?? "").localeCompare(b.value_date ?? ""));
}

/** Find this scenario's PoArrival override for a product, if any. */
function findArrivalOverride(overrides: ScenarioOverride[], productId: string) {
  return (
    overrides.find(
      (o) =>
        o.target_kind === "PoArrival" &&
        o.field_name === "arrival_date" &&
        o.target_product_id === productId
    ) ?? null
  );
}

/**
 * Incoming supply, in its OWN band rather than inside a well's rows.
 *
 * `GET /mrp/by-item/{id}` reports on-order summed across EVERY Business Unit
 * (`app.engines.inventory.total_on_order_all_bus`) — it is not scoped to this
 * scenario's customer and it is not reserved to any well. Drawing it inside a
 * well's lanes would imply an allocation that does not exist, so it gets a band
 * of its own, labelled with that scope and with the `on_order_source`
 * provenance, and the demand rows stay strictly per-well.
 */
function OnOrderBand({
  rows,
  axis,
  editable,
  drag,
  overrides,
  onBegin,
  onMove,
  onEnd,
  onBeginNewOrder,
  onEndNewOrder,
  onAddNewOrder,
  onRemoveNewOrder,
  widthForQty,
}: {
  rows: OnOrderRow[];
  axis: Axis;
  editable: boolean;
  drag: DragState | null;
  overrides: ScenarioOverride[];
  onBegin: (e: React.PointerEvent, row: OnOrderRow, day: string) => void;
  onMove: (e: React.PointerEvent) => void;
  onEnd: (row: OnOrderRow, baseDay: string) => void;
  /** Begin a two-axis drag on an existing hypothetical block. `fromHandle` picks the
   *  quantity axis, exactly as it does for a demand block. */
  onBeginNewOrder: (
    e: React.PointerEvent,
    row: OnOrderRow,
    override: ScenarioOverride,
    fromHandle: boolean
  ) => void;
  onEndNewOrder: (row: OnOrderRow, override: ScenarioOverride) => void;
  onAddNewOrder: (row: OnOrderRow) => void;
  onRemoveNewOrder: (row: OnOrderRow, override: ScenarioOverride) => void;
  /** The same quantity->px scale the demand blocks are drawn with, passed in rather
   *  than recomputed so a hypothetical order's width means the same thing as a demand
   *  block's width on the same chart. */
  widthForQty: (q: number) => number;
}) {
  const hasRealOnOrder = (r: OnOrderRow) => {
    const p = r.position;
    if (p.on_order === null || p.on_order === undefined) return false;
    return p.on_order > 0 || (p.on_order_undated ?? 0) > 0;
  };

  // WHICH ROWS THE BAND SHOWS, AND WHY IT IS NOT JUST "THOSE WITH REAL ON-ORDER".
  //
  // The main reason to simulate a new order is that there is NOTHING on order for the
  // product — that is what an emergency order is for. Filtering the band down to
  // products with existing purchase orders would hide exactly the rows a planner most
  // needs to act on, so while the scenario is editable EVERY demanded product gets a
  // row and can be given a hypothetical order. A read-only scenario falls back to the
  // rows that have something to show.
  const withHypothetical = (r: OnOrderRow) =>
    findNewOrderOverrides(overrides, r.productId).length > 0;
  const plottable = editable
    ? rows
    : rows.filter((r) => hasRealOnOrder(r) || withHypothetical(r));

  if (plottable.length === 0) {
    return (
      <div className="tl-band">
        <div className="tl-band-head">
          Incoming supply (on order) — nothing to plot
        </div>
        <div className="tl-empty-note">
          None of the products these wells demand has a projected purchase-order
          quantity (<code>on_order_source: "unavailable"</code>). That is unknown,
          not zero, so nothing is drawn rather than a zero bar.
        </div>
      </div>
    );
  }

  return (
    <div className="tl-band">
      <div className="tl-band-head">Incoming supply (on order)</div>
      <div className="tl-band-note">
        Quantities on order across <strong>all Business Units</strong>, not scoped
        to this customer and not reserved to any well — which is why they sit in
        their own band rather than inside a well&apos;s rows.{" "}
        {editable ? (
          <>
            <strong>Drag a dated block sideways</strong> to restate its earliest
            expected arrival. That moves the <em>runout projection</em> only —
            coverage verdicts are decided from on-hand stock alone and will not
            change — and the resulting override can be previewed but never applied.
          </>
        ) : (
          <>Read-only: this scenario can no longer be edited.</>
        )}
      </div>
      {plottable.map((r) => {
        const p = r.position;
        const dated = p.on_order ?? 0;
        const undated = p.on_order_undated ?? 0;
        const earliest = p.on_order_earliest_arrival ?? null;
        const latest = p.on_order_latest_arrival ?? null;
        const override = findArrivalOverride(overrides, r.productId);
        // The EFFECTIVE earliest arrival: the override's date when this scenario
        // restates it, so a block sits where the scenario says it lands rather than
        // snapping back to Oracle's date after a drop. Same contract the demand
        // blocks get from `ros_date_after`.
        const effectiveEarliest = override?.value_date
          ? override.value_date.slice(0, 10)
          : earliest;
        // Later POs move with the earliest one, so the block's WIDTH (the spread
        // between first and last arrival) is preserved across a shift.
        const baseX = earliest ? dayToX(axis, earliest) : 0;
        const xEnd = latest ? dayToX(axis, latest) : baseX;
        const w = Math.max(QTY_MIN_PX, xEnd - baseX);
        const x = effectiveEarliest ? dayToX(axis, effectiveEarliest) : 0;

        const isDragging =
          drag !== null && drag.kind === "on_order" && drag.productId === r.productId;
        const ghostX = isDragging ? dayToX(axis, drag.day) : 0;

        const newOrders = findNewOrderOverrides(overrides, r.productId);
        const hypotheticalTotal = newOrders.reduce(
          (sum, o) => sum + (o.value_number ?? 0),
          0
        );

        return (
          <div className="tl-row" key={r.productId}>
            <div className="tl-label tl-label-supply">
              <span className="tl-label-main">{r.description}</span>
              <span className="tl-label-sub">
                on order · source: {p.on_order_source ?? "unknown"}
                {override && " · arrival overridden"}
                {/* The hypothetical total is labelled as such and kept SEPARATE
                    from the real on-order figure, never summed into it — a real PO
                    is an Oracle fact and this is the planner's hypothesis. */}
                {hypotheticalTotal > 0 && (
                  <>
                    {" · "}
                    <span className="tl-label-hypo">
                      +{hypotheticalTotal.toLocaleString()} hypothetical
                    </span>
                  </>
                )}
              </span>
              {editable && (
                <button
                  type="button"
                  className="tl-add-order"
                  data-testid={`add-hypothetical-${r.productId}`}
                  title={
                    "Add a HYPOTHETICAL purchase order for this product — one that " +
                    "does not exist — placed " +
                    NEW_ORDER_DEFAULT_DAYS +
                    " days out at a default quantity, which you then drag to the " +
                    "date and size you mean.\n\n" +
                    NEW_ORDER_REASON
                  }
                  onClick={() => onAddNewOrder(r)}
                >
                  + hypothetical order
                </button>
              )}
            </div>
            <div className="tl-plot" style={{ width: axis.width }}>
              {isDragging && (
                <div className="tl-ghost" style={{ left: ghostX, width: w }}>
                  <span className="tl-ghost-text">{drag.day}</span>
                </div>
              )}
              {dated > 0 && effectiveEarliest && (
                <div
                  className={
                    "tl-onorder" +
                    (editable ? " tl-onorder-draggable" : "") +
                    (override ? " tl-onorder-overridden" : "")
                  }
                  data-testid={`onorder-${r.productId}`}
                  data-arrival={effectiveEarliest}
                  style={{ left: x, width: w }}
                  title={
                    `${dated.toLocaleString()} ${p.unit_of_measure} on order · ` +
                    `arrivals ${effectiveEarliest}${latest && latest !== earliest ? ` — (+${Math.round((xEnd - baseX) / MONTH_PX * 30)}d spread)` : ""}` +
                    (override
                      ? `\n\nThis scenario restates the earliest arrival: Oracle promises ${earliest}.`
                      : "") +
                    `\n\n${ON_ORDER_DRAG_REASON}`
                  }
                  onPointerDown={(e) =>
                    onBegin(e, r, effectiveEarliest)
                  }
                  onPointerMove={onMove}
                  onPointerUp={() => onEnd(r, effectiveEarliest)}
                >
                  <span className="tl-onorder-text">
                    +{dated.toLocaleString()} {p.unit_of_measure}
                  </span>
                </div>
              )}
              {dated > 0 && !effectiveEarliest && (
                <div
                  className="tl-onorder tl-onorder-undated"
                  style={{ left: 0, width: QTY_PX }}
                  title={
                    `${dated.toLocaleString()} ${p.unit_of_measure} on order, arrival date unknown.` +
                    `\n\n${ON_ORDER_UNDATED_REASON}`
                  }
                >
                  <span className="tl-onorder-text">
                    +{dated.toLocaleString()} {p.unit_of_measure} · arrival date
                    unknown
                  </span>
                </div>
              )}
              {undated > 0 && (
                <div
                  className="tl-onorder tl-onorder-undated"
                  style={{ left: 0, width: QTY_PX }}
                  title={
                    `${undated.toLocaleString()} ${p.unit_of_measure} raised but not ` +
                    `acknowledged — no promised arrival date.` +
                    `\n\n${ON_ORDER_UNDATED_REASON}`
                  }
                >
                  <span className="tl-onorder-text">
                    +{undated.toLocaleString()} {p.unit_of_measure} · arrival date
                    unknown
                  </span>
                </div>
              )}

              {/* Nothing real to draw, and nothing hypothetical yet. Said in words
                  rather than left as an empty lane, and "unknown" is distinguished
                  from "zero" — the same rule the band's empty state follows. */}
              {dated === 0 && undated === 0 && newOrders.length === 0 && (
                <div className="tl-onorder-none">
                  {p.on_order === null || p.on_order === undefined
                    ? "on-order quantity unavailable (unknown, not zero)"
                    : "nothing on order"}
                  {editable && " — add a hypothetical order to model one"}
                </div>
              )}

              {/* HYPOTHETICAL ORDERS. One block per override row, drawn
                  unmistakably differently from the real blocks above: these are not
                  Oracle facts. Width is QUANTITY (a moment, like a demand block),
                  not an arrival spread — which is also what makes the vertical drag
                  axis mean something. */}
              {newOrders.map((o) => {
                const day = (o.value_date ?? "").slice(0, 10);
                if (!day) return null;
                const qty = o.value_number ?? 0;
                const dragging =
                  drag !== null &&
                  drag.kind === "new_order" &&
                  drag.overrideId === o.id;
                const hx =
                  dragging && drag.mode === "ros_date"
                    ? dayToX(axis, drag.day)
                    : dayToX(axis, day);
                const hw =
                  dragging && drag.mode === "quantity"
                    ? widthForQty(drag.qty)
                    : widthForQty(qty);
                return (
                  <div key={o.id}>
                    {dragging && drag.mode !== null && (
                      <div className="tl-ghost" style={{ left: hx, width: hw }}>
                        <span className="tl-ghost-text">
                          {drag.mode === "ros_date"
                            ? drag.day
                            : `${drag.qty.toLocaleString()} ${p.unit_of_measure ?? ""}`}
                        </span>
                      </div>
                    )}
                    <div
                      className={
                        "tl-neworder" +
                        (editable ? " tl-neworder-drag" : "") +
                        (dragging ? " tl-neworder-dragging" : "")
                      }
                      data-testid={`neworder-${o.id}`}
                      data-arrival={day}
                      data-quantity={qty}
                      style={{ left: dayToX(axis, day), width: widthForQty(qty) }}
                      title={
                        `HYPOTHETICAL ORDER: ${qty.toLocaleString()} ` +
                        `${p.unit_of_measure ?? ""} arriving ${day}\n\n` +
                        NEW_ORDER_REASON
                      }
                      onPointerDown={(e) => onBeginNewOrder(e, r, o, false)}
                      onPointerMove={onMove}
                      onPointerUp={() => onEndNewOrder(r, o)}
                      onPointerCancel={() => onEndNewOrder(r, o)}
                    >
                      <span className="tl-neworder-text">
                        ?{qty.toLocaleString()} {p.unit_of_measure ?? ""}
                      </span>
                      {editable && (
                        <>
                          <span
                            className="tl-resize"
                            title="Drag to change the hypothetical quantity."
                            onPointerDown={(e) => onBeginNewOrder(e, r, o, true)}
                            onPointerMove={onMove}
                            onPointerUp={(e) => {
                              e.stopPropagation();
                              onEndNewOrder(r, o);
                            }}
                          />
                          {/* Removing it is just deleting the override row — there
                              is nothing else anywhere to clean up. */}
                          <button
                            type="button"
                            className="tl-neworder-remove"
                            data-testid={`remove-neworder-${o.id}`}
                            title="Remove this hypothetical order (deletes the override)."
                            onPointerDown={(e) => e.stopPropagation()}
                            onClick={(e) => {
                              e.stopPropagation();
                              onRemoveNewOrder(r, o);
                            }}
                          >
                            ×
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * What an on-order drag actually did
 * ------------------------------------------------------------------ */

/**
 * The consequence panel for `PoArrival` overrides, rendered under the chart that
 * produced them so the answer appears where the gesture happened.
 *
 * It states the NEGATIVES as explicitly as the positives, because those are the
 * ones a planner would otherwise assume wrongly: coverage did not move, MRP did
 * not move, and the override cannot be applied. `runout_month_changed === false`
 * is reported as a real finding ("the shift does not change the month the balance
 * goes negative"), never as a failed drag.
 */
function SupplyRunoutPanel({
  changes,
  mrpChanges,
  applyBlockers,
}: {
  changes: SupplyRunoutChange[];
  mrpChanges: MrpRowChange[];
  applyBlockers: string[];
}) {
  if (changes.length === 0) return null;
  const movedMrp = mrpChanges.filter((m) => m.kind !== "unchanged").length;

  return (
    <div className="tl-supply-impact">
      <div className="tl-supply-impact-head">
        Incoming-supply impact — <em>runout projection</em>
      </div>
      <table className="tl-supply-impact-table">
        <thead>
          <tr>
            <th>Product</th>
            <th>Earliest arrival</th>
            <th>Shift</th>
            <th>Runout month</th>
            <th>On order</th>
            <th>Hypothetical</th>
          </tr>
        </thead>
        <tbody>
          {changes.map((c) => (
            <tr key={c.product_id}>
              <td>{c.product_description ?? c.product_id}</td>
              <td>
                {c.arrival_before ?? "—"} &rarr;{" "}
                <strong>{c.arrival_after ?? "—"}</strong>
              </td>
              <td>
                {c.shift_days === 0
                  ? "—"
                  : `${c.shift_days > 0 ? "+" : ""}${c.shift_days} d`}
              </td>
              <td>
                {c.runout_month_changed ? (
                  <strong>
                    {c.runout_month_before ?? "never"} &rarr;{" "}
                    {c.runout_month_after ?? "never"}
                  </strong>
                ) : (
                  <span className="tl-supply-impact-flat">
                    {c.runout_month_after ?? "never runs out"} (unchanged)
                  </span>
                )}
              </td>
              <td>
                {c.on_order_dated_quantity.toLocaleString()} {c.unit_of_measure}
                {c.on_order_undated_quantity > 0 && (
                  <>
                    {" "}
                    <span
                      className="tl-supply-impact-flat"
                      title={
                        "Raised but not acknowledged — no promised arrival date, so " +
                        "it has no month to be projected at and is excluded from " +
                        "both curves. It is NOT shifted onto a date."
                      }
                    >
                      (+{c.on_order_undated_quantity.toLocaleString()} undated,
                      not projected)
                    </span>
                  </>
                )}
              </td>
              {/* THE HYPOTHETICAL COLUMN, kept in its own cell rather than added
                  into "On order". The neighbouring figure is a sum of real
                  Oracle-projected purchase orders a planner can go and verify; this
                  one is the planner's own invention. Merging them would destroy the
                  only distinction that matters here. */}
              <td>
                {c.hypothetical_quantity > 0 ? (
                  <>
                    <span className="tl-hypo-figure">
                      ?{c.hypothetical_quantity.toLocaleString()}{" "}
                      {c.unit_of_measure}
                    </span>
                    <div className="tl-hypo-detail">
                      {c.hypothetical_orders.map(([qty, when], i) => (
                        <div key={i}>
                          {qty.toLocaleString()} arriving {when}
                        </div>
                      ))}
                      {/* A SIZING CHECK, and worded as one. It does NOT say the MRP
                          recommendation went away — it cannot have, because MRP rows
                          derive from coverage verdicts and coverage never saw this
                          order. Saying "gap closed" here would be the single most
                          tempting lie on this screen. */}
                      {c.mrp_recommended_quantity !== null && (
                        <div
                          className={
                            c.hypothetical_covers_recommendation
                              ? "tl-hypo-covers"
                              : "tl-hypo-short"
                          }
                          title={
                            "MRP recommends ordering " +
                            c.mrp_recommended_quantity.toLocaleString() +
                            " of this product. This compares the size of your " +
                            "hypothetical order against that recommendation.\n\n" +
                            "It does NOT mean the recommendation row disappeared. " +
                            "MRP recommendations are derived from coverage verdicts, " +
                            "coverage is decided from on-hand stock alone, and " +
                            "incoming supply — real or hypothetical — is never read " +
                            "inside it. The recommendation still stands; what this " +
                            "tells you is whether the order you drew would be big " +
                            "enough to satisfy it."
                          }
                        >
                          {c.hypothetical_covers_recommendation
                            ? `covers the ${c.mrp_recommended_quantity.toLocaleString()} MRP recommends`
                            : `short of the ${c.mrp_recommended_quantity.toLocaleString()} MRP recommends`}
                        </div>
                      )}
                    </div>
                  </>
                ) : (
                  <span className="tl-supply-impact-flat">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {changes.some((c) => c.hypothetical_quantity > 0) && (
        <p className="tl-supply-impact-note tl-supply-impact-hypo">
          <strong>The “Hypothetical” column is not supply.</strong> Those purchase
          orders <em>do not exist</em> — in Oracle or here. They are your own
          assertion, folded into the <em>after</em> runout curve at their stated month
          beside whatever real purchase orders the product has, and{" "}
          <strong>nothing was created anywhere</strong>: this platform holds Oracle&apos;s
          purchase orders as a read-only copy and never writes a row into it. The
          &ldquo;On order&rdquo; figure beside it stays a sum of real rows you can go and
          verify — the two are never added together.
        </p>
      )}
      <p className="tl-supply-impact-note">
        Both runout months come from an <strong>on-order-aware</strong> projection —
        one at the dates Oracle promised, one at the dates above — so the difference
        is the shift and nothing else. Neither figure is the By Item runout month,
        which is on-hand-only by design.
      </p>
      <p className="tl-supply-impact-note">
        <strong>No coverage verdict moved,</strong> and none can: coverage is decided
        from on-hand stock alone and incoming supply is read beside that verdict,
        never inside it. Whether steel landing before ROS may <em>cover</em> a line
        is a coverage-rule question this platform has not answered.{" "}
        {movedMrp === 0 ? (
          <>
            The MRP recommendation rows are unchanged too, as they must be — they are
            derived from those same verdicts.
          </>
        ) : (
          <>
            {movedMrp} MRP row(s) do differ, but from this scenario&apos;s{" "}
            <em>other</em> overrides — an arrival date cannot move a recommendation.
          </>
        )}
      </p>
      {applyBlockers.length > 0 && (
        <div className="tl-supply-impact-blockers">
          <strong>Cannot be applied.</strong>
          <ul>
            {applyBlockers.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Drag state
 * ------------------------------------------------------------------ */

type DragState = {
  /**
   * Which band the gesture started in. An on-order drag is horizontal ONLY —
   * there is no quantity axis for it, because `PoArrival` has exactly one
   * overridable field (`arrival_date`) and a PO quantity is not a scenario's to
   * restate. So `mode` is pre-locked to "arrival_date" for it and the
   * axis-lock branch in `moveDrag` never runs.
   */
  kind: "demand" | "on_order" | "new_order";
  /** Set for kind === "demand". */
  lineId: string;
  /** Set for kind === "on_order" and "new_order" — the product concerned. */
  productId: string;
  /**
   * Set for kind === "new_order": the `PoArrival.new_order` override row this block
   * IS. Identified by id rather than by product, because a product may carry several
   * hypothetical orders ("2000 in March and 3000 in June") and dragging one must not
   * touch the others — unlike an `arrival_date` override, of which there is exactly
   * one per product.
   */
  overrideId: string;
  /**
   * null until the pointer has moved past AXIS_LOCK_PX, or the grab was a handle.
   *
   * A "new_order" drag uses the DemandField values -- "ros_date" for the horizontal
   * axis and "quantity" for the vertical -- rather than gaining two names of its own.
   * That is not laziness about naming: it is what lets `moveDrag` and the ghost
   * rendering be reused verbatim. The values are read only to decide WHICH AXIS the
   * gesture committed to; the override it writes is decided by `kind`, so a
   * hypothetical order can never be written as a DemandLine override.
   */
  mode: DemandField | "arrival_date" | null;
  /**
   * True when the gesture started on the resize handle. It decides which pointer
   * AXIS feeds the quantity — the handle is horizontal (dx), a body drag is
   * vertical (-dy) — and it must be read from the GRAB rather than from `mode`,
   * because a body drag that has locked into quantity mode has `mode ===
   * "quantity"` too and would otherwise start reading dx and stop responding.
   */
  fromHandle: boolean;
  startClientX: number;
  startClientY: number;
  /** The block's x and quantity when the drag started. */
  baseX: number;
  baseQty: number;
  dx: number;
  dy: number;
  /** Live candidate values, recomputed on every move. */
  day: string;
  qty: number;
};

/* ------------------------------------------------------------------ *
 * The view
 * ------------------------------------------------------------------ */

export default function ScenarioTimeline({
  scenario,
  lines,
  editable,
  onChanged,
  supplyRunoutChanges = [],
  mrpChanges = [],
  applyBlockers = [],
  businessUnitId = null,
}: {
  scenario: ScenarioDetail;
  /** `impact.line_changes` — already carries this scenario's effective values. */
  lines: LineCoverageChange[];
  editable: boolean;
  /** ScenarioEditor.reload — refetches the scenario AND the preview. */
  onChanged: () => void;
  /** `impact.supply_runout_changes` — what an on-order drag actually did. */
  supplyRunoutChanges?: SupplyRunoutChange[];
  /** `impact.mrp_changes`, so the panel can state that MRP did NOT move rather
   *  than leaving the planner to wonder whether it was checked. */
  mrpChanges?: MrpRowChange[];
  /** `impact.apply_blockers` verbatim from the preview. Rendered, never re-worded. */
  applyBlockers?: string[];
  /** `impact.business_unit_id` -- the scenario customer's own Business Unit.
   *
   *  A hypothetical new order is scoped to (BU, product) like a real
   *  `InventoryOnOrder` row, and `validate` refuses any BU but this one. Taken from
   *  the PREVIEW rather than from `scenario` (which has no BU column) so the timeline
   *  cannot name a Business Unit different from the one the figures on screen were
   *  resolved under. */
  businessUnitId?: string | null;
}) {
  const overrides = scenario.overrides;
  const [onOrder, setOnOrder] = useState<Map<string, InventoryPosition>>(new Map());
  const [units, setUnits] = useState<Map<string, string>>(new Map());
  const [supplyLoading, setSupplyLoading] = useState(true);
  const [supplyFailed, setSupplyFailed] = useState<string[]>([]);
  const [drag, setDrag] = useState<DragState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastDrop, setLastDrop] = useState<string | null>(null);
  const dragRef = useRef<DragState | null>(null);

  const productIds = Array.from(new Set(lines.map((l) => l.product_id)));
  const productKey = productIds.slice().sort().join(",");

  // On-order for exactly the products these wells demand. One call per product;
  // a product with no inventory row anywhere makes /mrp/by-item raise, which is
  // recorded and reported rather than swallowed into a silent gap.
  useEffect(() => {
    if (productIds.length === 0) {
      setSupplyLoading(false);
      return;
    }
    let live = true;
    setSupplyLoading(true);
    Promise.all(
      productIds.map((id) =>
        api
          .getMrpByItem(id)
          .then((a) => ({ id, analysis: a }))
          .catch(() => ({ id, analysis: null }))
      )
    ).then((results) => {
      if (!live) return;
      const pos = new Map<string, InventoryPosition>();
      const um = new Map<string, string>();
      const failed: string[] = [];
      for (const r of results) {
        if (r.analysis === null) {
          failed.push(r.id);
          continue;
        }
        pos.set(r.id, r.analysis.inventory);
        um.set(r.id, r.analysis.unit_of_measure);
      }
      setOnOrder(pos);
      setUnits(um);
      setSupplyFailed(failed);
      setSupplyLoading(false);
    });
    return () => {
      live = false;
    };
  }, [productKey]);

  const axis = buildAxis([
    ...lines.map((l) => l.ros_date_after),
    ...Array.from(onOrder.values()).flatMap((p) =>
      [p.on_order_earliest_arrival, p.on_order_latest_arrival].filter(
        (d): d is string => !!d
      )
    ),
    // The OVERRIDDEN arrival dates too. A PO dragged past the last real date would
    // otherwise land outside the plotted horizon and vanish from the chart that
    // was just used to move it.
    ...overrides
      .filter((o) => o.target_kind === "PoArrival" && o.value_date)
      .map((o) => o.value_date as string),
  ]);

  const maxQty = Math.max(1, ...lines.map((l) => l.quantity_after));
  const pxPerQty = QTY_PX / maxQty;
  const widthForQty = (q: number) =>
    Math.max(QTY_MIN_PX, Math.min(QTY_PX, q * pxPerQty));

  /* ---------------- drag plumbing (native pointer events) ---------------- */

  const beginDrag = (
    e: React.PointerEvent,
    line: LineCoverageChange,
    mode: DemandField | null
  ) => {
    if (!editable || !axis || busy) return;
    e.preventDefault();
    e.stopPropagation();
    // Capture keeps the move/up stream on this block even when the pointer
    // leaves it, which it will — a block is 22px tall and a quantity drag is
    // vertical. It throws for a pointer id the browser does not consider active,
    // so it is best-effort: without capture the gesture still works while the
    // pointer stays over the block, and losing the gesture is better than
    // throwing out of the handler.
    try {
      (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    } catch {
      /* no capture available — see above */
    }
    const state: DragState = {
      kind: "demand",
      lineId: line.demand_line_id,
      productId: "",
      overrideId: "",
      mode,
      fromHandle: mode === "quantity",
      startClientX: e.clientX,
      startClientY: e.clientY,
      baseX: dayToX(axis, line.ros_date_after),
      baseQty: line.quantity_after,
      dx: 0,
      dy: 0,
      day: line.ros_date_after.slice(0, 10),
      qty: line.quantity_after,
    };
    dragRef.current = state;
    setDrag(state);
    setError(null);
  };

  const moveDrag = (e: React.PointerEvent) => {
    const cur = dragRef.current;
    if (!cur || !axis) return;
    const dx = e.clientX - cur.startClientX;
    const dy = e.clientY - cur.startClientY;
    let mode = cur.mode;
    if (mode === null && Math.max(Math.abs(dx), Math.abs(dy)) >= AXIS_LOCK_PX) {
      // Whichever axis the pointer committed to first wins, and stays won for
      // the rest of the gesture — a drag must not flip meaning mid-flight.
      mode = Math.abs(dx) >= Math.abs(dy) ? "ros_date" : "quantity";
    }
    // The resize handle drags the block's right EDGE, so it reads dx; a body drag
    // reads -dy (up = more). Both go through the same px-per-quantity scale the
    // block's WIDTH is drawn with, so what you see is what you get.
    const qtyDelta = cur.fromHandle ? dx / pxPerQty : -dy / pxPerQty;
    const next: DragState = {
      ...cur,
      mode,
      dx,
      dy,
      day: xToDay(axis, cur.baseX + dx),
      qty: Math.max(
        0,
        Math.round((cur.baseQty + qtyDelta) / QTY_SNAP) * QTY_SNAP
      ),
    };
    dragRef.current = next;
    setDrag(next);
  };

  const endDrag = async (line: LineCoverageChange) => {
    const cur = dragRef.current;
    dragRef.current = null;
    setDrag(null);
    // `kind` is checked as well as `mode`: the two bands share `dragRef`, and a
    // demand handler must never try to interpret an arrival gesture (nor the
    // reverse) — that would write a DemandLine override from an on-order drop.
    if (!cur || cur.kind !== "demand" || cur.mode === null) return;
    if (cur.mode === "arrival_date") return;
    // `kind` above already excludes a "new_order" gesture, which shares this
    // handler's `mode` vocabulary ("ros_date"/"quantity") precisely so `moveDrag`
    // could be reused. Writing a DemandLine override from a hypothetical-order drag
    // is therefore impossible by the kind check, not by the mode values.

    const field: DemandField = cur.mode;
    const baseDay = line.ros_date_after.slice(0, 10);
    if (field === "ros_date" && cur.day === baseDay) return;
    if (field === "quantity" && cur.qty === cur.baseQty) return;

    setBusy(true);
    setError(null);
    try {
      // POST /overrides is a pure APPEND — it is NOT idempotent per
      // (line, field): posting twice leaves two rows for the same field, and
      // `ScenarioOverrides.__init__` keeps whichever it reads last, so the
      // scenario would hold an ambiguous pair. Confirmed by reading
      // `app.api.scenarios.add_override`. So a re-drag DELETES the previous row
      // for this (line, field) first, leaving exactly one.
      const existing = findOverride(overrides, line.demand_line_id, field);
      if (existing) {
        await api.deleteScenarioOverride(scenario.id, existing.id);
      }
      // Identical payload shape to AddOverrideForm.submit: value_date goes
      // through `new Date(...).toISOString()`, value_number through `Number()`,
      // and the value columns this field does not use are explicitly null.
      await api.addScenarioOverride(scenario.id, {
        target_kind: "DemandLine",
        field_name: field,
        target_demand_line_id: line.demand_line_id,
        target_product_id: null,
        target_business_unit_id: null,
        target_to_product_id: null,
        value_number: field === "quantity" ? Number(cur.qty) : null,
        value_date: field === "ros_date" ? new Date(cur.day).toISOString() : null,
        value_text: null,
        note:
          field === "ros_date"
            ? `dragged on the timeline: ROS ${baseDay} → ${cur.day}`
            : `dragged on the timeline: quantity ${cur.baseQty.toLocaleString()} → ${cur.qty.toLocaleString()}`,
      });
      setLastDrop(
        field === "ros_date"
          ? `${line.well_name} · ${line.product_description ?? line.product_id}: ROS ${baseDay} → ${cur.day}`
          : `${line.well_name} · ${line.product_description ?? line.product_id}: quantity ${cur.baseQty.toLocaleString()} → ${cur.qty.toLocaleString()}`
      );
      // Refetch the scenario AND the preview: the consequence is the point of
      // the gesture, so it must not need a second click.
      onChanged();
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(false);
    }
  };

  /* ------- on-order arrival drag: the same gesture, one axis ---------------
   *
   * Deliberately reuses `moveDrag` verbatim rather than growing a second move
   * handler. `moveDrag` already maps `baseX + dx` through `xToDay`, which is the
   * whole of what an arrival drag needs, and sharing it is what guarantees an
   * on-order block snaps to the same day boundaries as a demand block — a second
   * implementation would be one rounding rule away from the two bands disagreeing
   * about which day a pixel is.
   */

  const beginArrivalDrag = (e: React.PointerEvent, row: OnOrderRow, day: string) => {
    if (!editable || !axis || busy) return;
    e.preventDefault();
    e.stopPropagation();
    try {
      (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    } catch {
      /* best-effort, same as beginDrag */
    }
    const state: DragState = {
      kind: "on_order",
      lineId: "",
      productId: row.productId,
      overrideId: "",
      // Pre-locked: there is no vertical meaning for this block, so the gesture
      // must not be able to lock into "quantity" and stop responding sideways.
      mode: "arrival_date",
      fromHandle: false,
      startClientX: e.clientX,
      startClientY: e.clientY,
      baseX: dayToX(axis, day),
      baseQty: 0,
      dx: 0,
      dy: 0,
      day,
      qty: 0,
    };
    dragRef.current = state;
    setDrag(state);
    setError(null);
  };

  const endArrivalDrag = async (row: OnOrderRow, baseDay: string) => {
    const cur = dragRef.current;
    dragRef.current = null;
    setDrag(null);
    if (!cur || cur.kind !== "on_order" || cur.day === baseDay) return;

    setBusy(true);
    setError(null);
    try {
      // Same delete-then-recreate as the demand blocks, and for the same reason:
      // POST /overrides is a pure APPEND, so a re-drag would otherwise leave two
      // PoArrival rows for one product and `ScenarioOverrides.__init__` would keep
      // whichever it read last — an ambiguous scenario.
      const existing = findArrivalOverride(overrides, row.productId);
      if (existing) {
        await api.deleteScenarioOverride(scenario.id, existing.id);
      }
      await api.addScenarioOverride(scenario.id, {
        target_kind: "PoArrival",
        field_name: "arrival_date",
        // Scoped to the PRODUCT, and to nothing else. On-order is not owned by a
        // well or a demand line — it is material arriving for a product — so
        // naming a line here would imply a reservation that does not exist.
        target_product_id: row.productId,
        target_demand_line_id: null,
        target_business_unit_id: null,
        target_to_product_id: null,
        value_number: null,
        value_date: new Date(cur.day).toISOString(),
        value_text: null,
        note: `dragged on the timeline: earliest arrival ${baseDay} → ${cur.day}`,
      });
      setLastDrop(
        `${row.description}: earliest arrival ${baseDay} → ${cur.day} ` +
          `(runout projection only — no coverage verdict moves)`
      );
      onChanged();
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(false);
    }
  };

  /* ------- hypothetical new orders: create, drag on BOTH axes, remove --------
   *
   * Interaction shape (B) — see the module docstring for why (A), drag-to-create,
   * was rejected. A click creates the override row; from then on the block is dragged
   * by the SAME `moveDrag` every other block uses, so it snaps to the same day
   * boundaries and the same quantity increments.
   */

  /** A starting quantity for a newly-placed block.
   *
   * This customer's total demand for the product, snapped — so the default block is
   * visible on the chart and means something ("about enough to cover what this
   * customer wants") rather than being an arbitrary round number the planner has to
   * drag away from.
   *
   * Explicitly NOT a recommendation. The preview reports separately whether the
   * quantity covers what MRP actually recommends (`mrp_recommended_quantity`), and
   * inventing a lead-time-aware default here would be a second, worse answer to a
   * question the engines already answer properly. */
  const defaultQtyFor = (productId: string) => {
    const demand = lines
      .filter((l) => l.product_id === productId)
      .reduce((sum, l) => sum + l.quantity_after, 0);
    const snapped = Math.round(demand / QTY_SNAP) * QTY_SNAP;
    return Math.max(QTY_SNAP, snapped);
  };

  const addNewOrder = async (row: OnOrderRow) => {
    if (!editable || busy) return;
    if (!businessUnitId) {
      setError(
        "This scenario's customer is not mapped to a Business Unit, so there is no " +
          "inventory pool for a hypothetical order to be placed into. Map the " +
          "customer to a Business Unit first."
      );
      return;
    }
    const qty = defaultQtyFor(row.productId);
    const when = new Date();
    when.setDate(when.getDate() + NEW_ORDER_DEFAULT_DAYS);
    const day = dayIso(when.getFullYear(), when.getMonth(), when.getDate());

    setBusy(true);
    setError(null);
    try {
      await api.addScenarioOverride(scenario.id, {
        target_kind: "PoArrival",
        field_name: "new_order",
        // (BU, product) — the same scope a real InventoryOnOrder row has. Both are
        // REQUIRED for this field, and the BU being present is half of what makes
        // the row impossible to mistake for an arrival_date restatement.
        target_product_id: row.productId,
        target_business_unit_id: businessUnitId,
        target_demand_line_id: null,
        target_to_product_id: null,
        // BOTH value columns. A hypothetical order is a quantity landing on a date
        // and neither half means anything alone.
        value_number: qty,
        value_date: new Date(day).toISOString(),
        value_text: null,
        note:
          `hypothetical order added on the timeline: ${qty.toLocaleString()} ` +
          `arriving ${day} (does not exist — projection only)`,
      });
      setLastDrop(
        `${row.description}: HYPOTHETICAL order of ${qty.toLocaleString()} arriving ` +
          `${day} added (runout projection only — no purchase order was created, ` +
          `and no coverage verdict moves)`
      );
      onChanged();
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(false);
    }
  };

  const beginNewOrderDrag = (
    e: React.PointerEvent,
    row: OnOrderRow,
    override: ScenarioOverride,
    fromHandle: boolean
  ) => {
    if (!editable || !axis || busy) return;
    e.preventDefault();
    e.stopPropagation();
    try {
      (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    } catch {
      /* best-effort, same as beginDrag */
    }
    const day = (override.value_date ?? "").slice(0, 10);
    const qty = override.value_number ?? 0;
    const state: DragState = {
      kind: "new_order",
      lineId: "",
      productId: row.productId,
      overrideId: override.id,
      // NOT pre-locked, unlike an arrival drag: a planner owns BOTH this block's
      // date and its quantity, so both axes are live and whichever the pointer
      // commits to first wins — exactly as on a demand block.
      mode: fromHandle ? "quantity" : null,
      fromHandle,
      startClientX: e.clientX,
      startClientY: e.clientY,
      baseX: dayToX(axis, day),
      baseQty: qty,
      dx: 0,
      dy: 0,
      day,
      qty,
    };
    dragRef.current = state;
    setDrag(state);
    setError(null);
  };

  const endNewOrderDrag = async (row: OnOrderRow, override: ScenarioOverride) => {
    const cur = dragRef.current;
    dragRef.current = null;
    setDrag(null);
    if (!cur || cur.kind !== "new_order" || cur.overrideId !== override.id) return;
    if (cur.mode === null) return;

    const baseDay = (override.value_date ?? "").slice(0, 10);
    const baseQty = override.value_number ?? 0;
    const day = cur.mode === "ros_date" ? cur.day : baseDay;
    const qty = cur.mode === "quantity" ? cur.qty : baseQty;
    if (day === baseDay && qty === baseQty) return;
    // A quantity dragged to zero would be REFUSED by `validate` — a hypothetical
    // order of nothing is not a what-if — so the gesture is clamped to one snap
    // increment rather than sent and rejected. Removing the order is what the ×
    // is for, and keeping the block alive is better than a 400 to interpret.
    const safeQty = Math.max(QTY_SNAP, qty);

    setBusy(true);
    setError(null);
    try {
      // Delete-then-recreate, as everywhere else on this timeline, because POST
      // /overrides is a pure APPEND and there is no PATCH. Keyed on THIS override's
      // id, so a product's other hypothetical orders are untouched — the difference
      // from the arrival-date drag, where there is only ever one row per product.
      await api.deleteScenarioOverride(scenario.id, override.id);
      await api.addScenarioOverride(scenario.id, {
        target_kind: "PoArrival",
        field_name: "new_order",
        target_product_id: row.productId,
        target_business_unit_id:
          override.target_business_unit_id ?? businessUnitId,
        target_demand_line_id: null,
        target_to_product_id: null,
        value_number: safeQty,
        value_date: new Date(day).toISOString(),
        value_text: null,
        note:
          `hypothetical order dragged on the timeline: ` +
          `${safeQty.toLocaleString()} arriving ${day} (does not exist — ` +
          `projection only)`,
      });
      setLastDrop(
        cur.mode === "ros_date"
          ? `${row.description}: hypothetical order moved to ${day}`
          : `${row.description}: hypothetical order resized to ${safeQty.toLocaleString()}`
      );
      onChanged();
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(false);
    }
  };

  const removeNewOrder = async (row: OnOrderRow, override: ScenarioOverride) => {
    if (!editable || busy) return;
    setBusy(true);
    setError(null);
    try {
      // Deleting the override IS removing the block. There is nothing else to clean
      // up anywhere, because nothing else was ever created — no InventoryOnOrder row
      // exists or ever existed for this hypothesis.
      await api.deleteScenarioOverride(scenario.id, override.id);
      setLastDrop(`${row.description}: hypothetical order removed`);
      onChanged();
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(false);
    }
  };

  /* ---------------- render ---------------- */

  if (lines.length === 0) {
    return (
      <div className="card">
        <div className="empty">
          This customer has no demand the coverage engine evaluates, so there is
          nothing to plot.
        </div>
      </div>
    );
  }
  if (!axis) {
    return (
      <div className="card">
        <div className="empty">No dated demand to plot.</div>
      </div>
    );
  }

  // Wells in ROS order, and each well's lines in ROS order inside it.
  const byWell = new Map<string, LineCoverageChange[]>();
  for (const l of lines) {
    const arr = byWell.get(l.well_id);
    if (arr) arr.push(l);
    else byWell.set(l.well_id, [l]);
  }
  const wells = Array.from(byWell.entries()).map(([wellId, ls]) => ({
    wellId,
    name: ls[0].well_name,
    lines: ls
      .slice()
      .sort((a, b) => a.ros_date_after.localeCompare(b.ros_date_after)),
  }));
  wells.sort((a, b) =>
    a.lines[0].ros_date_after.localeCompare(b.lines[0].ros_date_after)
  );

  const onOrderRows: OnOrderRow[] = productIds
    .filter((id) => onOrder.has(id))
    .map((id) => ({
      productId: id,
      description:
        lines.find((l) => l.product_id === id)?.product_description ?? id,
      position: onOrder.get(id)!,
    }))
    .sort((a, b) => a.description.localeCompare(b.description));

  const dragOverrides = overrides.filter(
    (o) =>
      (o.target_kind === "DemandLine" || o.target_kind === "PoArrival") &&
      (o.note ?? "").startsWith("dragged on the timeline")
  );

  return (
    <div className="tl-wrap">
      <div className="tl-howto">
        <p>
          <strong>Drag a demand block</strong> sideways to move its ROS date, or up
          and down (or grab its right edge) to change its quantity. Dates snap to a
          whole day; quantities snap to {QTY_SNAP}. On drop, the move is written as
          a <code>DemandLine</code> override — the same row the form view creates —
          and the impact above is recomputed straight away.
        </p>
        <p className="tl-howto-sub">
          One lane per demand line, grouped under its well: an override names one
          line, so one line is what you drag. Block <em>width</em> is quantity, not
          duration — a ROS date is a moment, not a span.
        </p>
        <p className="tl-howto-sub">
          <strong>Drag an on-order block</strong> sideways to restate the earliest
          expected arrival of that product&apos;s incoming supply, written as a{" "}
          <code>PoArrival.arrival_date</code> override. It moves the{" "}
          <em>runout projection</em> and nothing else: coverage verdicts are decided
          from on-hand stock alone, so Covered/Uncovered will not change, and neither
          will the MRP recommendation rows derived from them. On-order blocks are{" "}
          <em>spans</em>, not moments — the width is the spread between the first and
          last arrival, and later purchase orders shift with the first one.
        </p>
        <p className="tl-howto-sub">
          <strong>&ldquo;+ hypothetical order&rdquo;</strong> in the incoming-supply band
          simulates a purchase order that <em>does not exist</em> — &ldquo;what if we
          placed an emergency order arriving next March?&rdquo; It drops a block{" "}
          {NEW_ORDER_DEFAULT_DAYS} days out which you then drag like any other: sideways
          for its arrival date, up/down (or the right edge) for its quantity. The ×
          removes it. It is written as a <code>PoArrival.new_order</code> override and{" "}
          <strong>no purchase order is created anywhere</strong> — it is added to the{" "}
          <em>runout projection</em> only. Like a real arrival date it moves no coverage
          verdict however large it is, and it can <strong>never be applied</strong>:
          applying it would mean placing an order, which is a human decision this
          platform recommends but does not execute.
        </p>
        {!editable && (
          <p className="tl-howto-sub">
            This scenario is Applied and immutable, so the blocks are read-only.
          </p>
        )}
      </div>

      <div className="tl-legend">
        <span className="tl-legend-item">
          <span className="tl-swatch tl-swatch-demand" /> demand line (draggable)
        </span>
        <span className="tl-legend-item">
          <span className="tl-swatch tl-swatch-supply" /> on order — arrival date
          draggable (runout projection only)
        </span>
        <span className="tl-legend-item">
          <span className="tl-swatch tl-swatch-hypo" /> HYPOTHETICAL order — does not
          exist; drag date and quantity
        </span>
        <span className="tl-legend-item">
          <span className="tl-swatch tl-swatch-ghost" /> drag preview
        </span>
        <span className="tl-legend-item">
          border colour = coverage status (same vocabulary as the coverage badges)
        </span>
      </div>

      <SupplyRunoutPanel
        changes={supplyRunoutChanges}
        mrpChanges={mrpChanges}
        applyBlockers={applyBlockers}
      />

      {(drag || lastDrop || error || busy) && (
        <div className="tl-status">
          {drag && drag.mode === "ros_date" && (
            <span className="tl-status-live">
              moving ROS &rarr; <strong>{drag.day}</strong>
            </span>
          )}
          {drag && drag.mode === "quantity" && (
            <span className="tl-status-live">
              changing quantity &rarr;{" "}
              <strong>{drag.qty.toLocaleString()}</strong>
            </span>
          )}
          {drag && drag.mode === "arrival_date" && (
            <span className="tl-status-live">
              moving earliest arrival &rarr; <strong>{drag.day}</strong>
            </span>
          )}
          {drag && drag.mode === null && (
            <span className="tl-status-live">
              drag sideways for the date, up/down for the quantity…
            </span>
          )}
          {busy && <span className="tl-status-live">saving the override…</span>}
          {!drag && !busy && lastDrop && (
            <span className="tl-status-done">
              override saved from the timeline — {lastDrop}. Impact above has been
              recomputed.
            </span>
          )}
          {error && <span className="form-error">{error}</span>}
        </div>
      )}

      <div className="tl-scroll">
        <div className="tl-inner">
          <div className="tl-row tl-axis-row">
            <div className="tl-label tl-label-axis">Well / demand line</div>
            <div className="tl-plot tl-axis" style={{ width: axis.width }}>
              {axis.months.map((mo, i) => (
                <div
                  className="tl-month"
                  key={`${mo.y}-${mo.m}`}
                  style={{ left: i * MONTH_PX, width: MONTH_PX }}
                >
                  <span className="tl-month-name">{MONTH_NAMES[mo.m]}</span>
                  <span className="tl-month-year">{mo.y}</span>
                </div>
              ))}
            </div>
          </div>

          {supplyLoading ? (
            <div className="tl-band">
              <div className="tl-band-head">
                Incoming supply (on order) — loading…
              </div>
            </div>
          ) : (
            <OnOrderBand
              rows={onOrderRows}
              axis={axis}
              editable={editable}
              drag={drag}
              overrides={overrides}
              onBegin={beginArrivalDrag}
              onMove={moveDrag}
              onEnd={endArrivalDrag}
              onBeginNewOrder={beginNewOrderDrag}
              onEndNewOrder={endNewOrderDrag}
              onAddNewOrder={addNewOrder}
              onRemoveNewOrder={removeNewOrder}
              widthForQty={widthForQty}
            />
          )}
          {supplyFailed.length > 0 && (
            <div className="tl-empty-note">
              {supplyFailed.length} product(s) have no inventory row in any
              Business Unit, so <code>/mrp/by-item</code> has no position to report
              and no on-order block is drawn for them. Unknown, not zero.
            </div>
          )}

          {wells.map((w) => (
            <div className="tl-band" key={w.wellId}>
              <div className="tl-band-head tl-band-head-well">{w.name}</div>
              {w.lines.map((l) => {
                const isDragging = drag?.lineId === l.demand_line_id;
                const x = dayToX(axis, l.ros_date_after);
                const width = widthForQty(l.quantity_after);
                const dateOv = findOverride(overrides, l.demand_line_id, "ros_date");
                const qtyOv = findOverride(overrides, l.demand_line_id, "quantity");
                const moved = !!dateOv || !!qtyOv;
                const unit = units.get(l.product_id) ?? "";
                const ghostX = isDragging && drag.mode === "ros_date"
                  ? dayToX(axis, drag.day)
                  : x;
                const ghostW = isDragging && drag.mode === "quantity"
                  ? widthForQty(drag.qty)
                  : width;
                return (
                  <div className="tl-row" key={l.demand_line_id}>
                    <div className="tl-label">
                      <span className="tl-label-main">
                        {l.product_description ?? l.product_id}
                      </span>
                      <span className="tl-label-sub">
                        {l.quantity_after.toLocaleString()} {unit} ·{" "}
                        {formatDay(l.ros_date_after)} · {l.status_after}
                        {moved ? " · overridden" : ""}
                      </span>
                    </div>
                    <div className="tl-plot" style={{ width: axis.width }}>
                      {/* Where the BASE plan put this line, kept visible so a
                          moved block reads as a move rather than as the fact. */}
                      {moved && (
                        <div
                          className="tl-base-marker"
                          style={{
                            left: dayToX(axis, l.ros_date_before),
                            width: widthForQty(l.quantity_before),
                          }}
                          title={`base plan: ${formatDay(l.ros_date_before)}, ${l.quantity_before.toLocaleString()} ${unit}`}
                        />
                      )}
                      {isDragging && drag.mode !== null && (
                        <div
                          className="tl-ghost"
                          style={{ left: ghostX, width: ghostW }}
                        >
                          <span className="tl-ghost-text">
                            {drag.mode === "ros_date"
                              ? drag.day
                              : `${drag.qty.toLocaleString()} ${unit}`}
                          </span>
                        </div>
                      )}
                      <div
                        className={
                          `tl-block tl-block-${l.status_after}` +
                          (editable ? " tl-block-drag" : "") +
                          (isDragging ? " tl-block-dragging" : "") +
                          (moved ? " tl-block-moved" : "")
                        }
                        style={{ left: x, width }}
                        data-line-id={l.demand_line_id}
                        data-testid={`tl-block-${l.demand_line_id}`}
                        title={
                          `${l.well_name} · ${l.product_description ?? l.product_id}\n` +
                          `${l.quantity_after.toLocaleString()} ${unit} · ROS ${formatDay(l.ros_date_after)}\n` +
                          `coverage (this scenario): ${l.status_after}` +
                          (editable
                            ? "\n\nDrag sideways to move the ROS date, up/down (or the right edge) to change the quantity."
                            : "")
                        }
                        onPointerDown={(e) => beginDrag(e, l, null)}
                        onPointerMove={moveDrag}
                        onPointerUp={() => endDrag(l)}
                        onPointerCancel={() => {
                          dragRef.current = null;
                          setDrag(null);
                        }}
                      >
                        <span className="tl-block-text">
                          {l.quantity_after.toLocaleString()} {unit}
                        </span>
                        {editable && (
                          <span
                            className="tl-resize"
                            title="Drag to change the quantity."
                            onPointerDown={(e) => beginDrag(e, l, "quantity")}
                            onPointerMove={moveDrag}
                            onPointerUp={(e) => {
                              e.stopPropagation();
                              endDrag(l);
                            }}
                          />
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      </div>

      <div className="tl-record">
        <h3>Recording this arrangement</h3>
        <p>
          {dragOverrides.length === 0
            ? "Nothing has been dragged yet. "
            : `${dragOverrides.length} override(s) on this scenario came from the timeline. `}
          <strong>Every drag is already saved.</strong> A drop writes a
          <code> ScenarioOverride</code> row on this scenario immediately — the same
          row the form view writes — so the arrangement you see is the scenario, and
          it is shared with everyone on the project. There is no separate
          &ldquo;snapshot&rdquo; to take and none is stored: the impact is recomputed
          from these overrides on every read, so it can never go stale against them.
        </p>
        <p className="tl-howto-sub">
          When the arrangement has settled, the <strong>Apply to base plan</strong>{" "}
          panel at the bottom of this page is the moment it becomes real. Until then
          everything on screen is a what-if.
        </p>
      </div>
    </div>
  );
}
