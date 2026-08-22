import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  api,
  CoverageStatus,
  LineCoverageChange,
  MrpRowChange,
  OverrideFields,
  OverrideValueType,
  ScenarioApplyResult,
  ScenarioDetail,
  ScenarioImpact,
  ScenarioOverride,
  ScenarioStatus,
  ScenarioTargetKind,
} from "../api/client";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import CoverageBadge from "../components/CoverageBadge";
import ReasonText from "../components/ReasonText";
import { formatDay } from "./MrpSummary";
import ScenarioTimeline from "./ScenarioTimeline";

const LIFECYCLE: ScenarioStatus[] = ["Draft", "Review", "Discussion"];

/**
 * How the overrides are BUILT. Not two features and not two datasets — one set
 * of `ScenarioOverride` rows with two editors over it. Both write through
 * POST/DELETE /scenarios/{id}/overrides and both re-read via `reload`, so a drag
 * shows up in the form view's table and a form entry shows up as a moved block.
 */
type OverrideView = "form" | "timeline";

/**
 * "NotEvaluated" is a real coverage outcome — the line sits outside the coverage
 * engine's status/profile filters, so the engine has no verdict for it. It is NOT
 * a CoverageStatus, so it cannot go through CoverageBadge, and rendering it as a
 * blank would hide the most interesting kind of demand override there is
 * ("what if this Planned line became Confirmed?").
 */
function StatusCell({ status }: { status: string | null }) {
  if (status === "NotEvaluated") {
    return <span className="badge">Not evaluated</span>;
  }
  return <CoverageBadge status={(status ?? null) as CoverageStatus} />;
}

/** The established before -> after vocabulary: `.diff-line` + `.change-arrow`. */
function StatusDiff({
  before,
  after,
  changed,
}: {
  before: string | null;
  after: string | null;
  changed: boolean;
}) {
  return (
    <div className={`diff-line${changed ? "" : " diff-line-unchanged"}`}>
      <span className="diff-before">
        <StatusCell status={before} />
      </span>
      <span className="change-arrow">&rarr;</span>
      <span className="diff-after">
        <StatusCell status={after} />
      </span>
    </div>
  );
}

function ValueDiff({
  before,
  after,
  format,
}: {
  before: string | number | null;
  after: string | number | null;
  format?: (v: string | number) => string;
}) {
  const fmt = (v: string | number | null) =>
    v === null || v === undefined ? "—" : format ? format(v) : String(v);
  const changed = String(before) !== String(after);
  return (
    <div className={`diff-line${changed ? "" : " diff-line-unchanged"}`}>
      <span className="diff-before num">{fmt(before)}</span>
      {changed && (
        <>
          <span className="change-arrow">&rarr;</span>
          <span className="diff-after num">{fmt(after)}</span>
        </>
      )}
    </div>
  );
}

/**
 * The what-if banner.
 *
 * Loud and unmissable on purpose. A preview figure must never be read as the
 * official coverage verdict — the same decision already taken for the
 * cross-customer sharing panel. `is_what_if` comes from the backend so this does
 * not depend on the frontend inferring anything.
 */
function WhatIfBanner({ impact }: { impact: ScenarioImpact }) {
  if (!impact.is_what_if) return null;
  return (
    <div className="whatif-banner">
      <span className="whatif-tag">What-if only — not the official coverage</span>
      <p>
        Everything below is a projection of what coverage <em>would</em> be if this
        scenario&apos;s overrides were true. Nothing has been saved. The official
        coverage verdict on the Home Dashboard and the well screens is unchanged,
        and stays unchanged until this scenario is applied to the base plan.
      </p>
      {impact.notes.map((n, i) => (
        <p key={i} className="whatif-note">
          {n}
        </p>
      ))}
    </div>
  );
}

function ImpactTiles({ impact }: { impact: ScenarioImpact }) {
  const risk = impact.risk;
  const wellsDelta = impact.covered_wells_after - impact.covered_wells_before;
  const linesDelta = impact.covered_lines_after - impact.covered_lines_before;
  const riskDelta = risk
    ? risk.unrecoverable_quantity_after - risk.unrecoverable_quantity_before
    : 0;

  const tone = (d: number, betterIsUp: boolean) =>
    d === 0
      ? ""
      : (d > 0) === betterIsUp
      ? " impact-tile-better"
      : " impact-tile-worse";

  return (
    <div className="impact-tiles">
      <div className={`impact-tile${tone(wellsDelta, true)}`}>
        <div className="impact-tile-label">Covered wells</div>
        <div className="impact-tile-value">
          {impact.covered_wells_before} &rarr; {impact.covered_wells_after}
        </div>
        <div className="impact-tile-note">
          {impact.changed_well_count} well
          {impact.changed_well_count === 1 ? "" : "s"} would change status
        </div>
      </div>
      <div className={`impact-tile${tone(linesDelta, true)}`}>
        <div className="impact-tile-label">Covered demand lines</div>
        <div className="impact-tile-value">
          {impact.covered_lines_before} &rarr; {impact.covered_lines_after}
        </div>
        <div className="impact-tile-note">
          {impact.changed_line_count} line
          {impact.changed_line_count === 1 ? "" : "s"} would change status
        </div>
      </div>
      {risk && (
        <div className={`impact-tile${tone(riskDelta, false)}`}>
          <div className="impact-tile-label">Unrecoverable demand</div>
          <div className="impact-tile-value">
            {risk.unrecoverable_quantity_before.toLocaleString()} &rarr;{" "}
            {risk.unrecoverable_quantity_after.toLocaleString()}
          </div>
          <div className="impact-tile-note">
            {risk.unrecoverable_lines_before} &rarr;{" "}
            {risk.unrecoverable_lines_after} line(s) a mill order cannot save
          </div>
        </div>
      )}
      <div className="impact-tile">
        <div className="impact-tile-label">MRP rows affected</div>
        <div className="impact-tile-value">
          {impact.mrp_changes.filter((m) => m.kind !== "unchanged").length}
        </div>
        <div className="impact-tile-note">
          of {impact.mrp_changes.length} recommendation row(s)
        </div>
      </div>
    </div>
  );
}

function CoverageImpactTable({ changes }: { changes: LineCoverageChange[] }) {
  if (changes.length === 0) {
    return (
      <div className="card">
        <div className="empty">
          This customer has no demand the coverage engine evaluates.
        </div>
      </div>
    );
  }
  // Changed lines first — that is what the conversation is about.
  const sorted = [...changes].sort(
    (a, b) =>
      Number(b.changed) - Number(a.changed) ||
      a.well_name.localeCompare(b.well_name)
  );
  const worse = (c: LineCoverageChange) =>
    ["Uncovered", "Unrecoverable", "NotEvaluated"].includes(c.status_after) &&
    ["Covered", "CoveredViaSubstitute"].includes(c.status_before);

  return (
    <div className="table-scroll">
      <table className="scenario-table">
        <thead>
          <tr>
            <th>Well</th>
            <th>Product</th>
            <th>Quantity</th>
            <th>ROS</th>
            <th>Coverage: base &rarr; scenario</th>
            <th>Why (scenario)</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((c) => (
            <tr
              key={c.demand_line_id}
              className={
                c.changed ? (worse(c) ? "row-changed-worse" : "row-changed") : ""
              }
            >
              <td>
                <Link to={`/wells/${c.well_id}`}>{c.well_name}</Link>
                {c.changed && !c.directly_overridden && (
                  <span className="knock-on">
                    knock-on effect — this line was not overridden
                  </span>
                )}
              </td>
              <td>{c.product_description ?? c.product_id}</td>
              <td>
                <ValueDiff
                  before={c.quantity_before}
                  after={c.quantity_after}
                  format={(v) => Number(v).toLocaleString()}
                />
              </td>
              <td>
                <ValueDiff
                  before={c.ros_date_before}
                  after={c.ros_date_after}
                  format={(v) => formatDay(String(v))}
                />
              </td>
              <td>
                <StatusDiff
                  before={c.status_before}
                  after={c.status_after}
                  changed={c.changed}
                />
              </td>
              <td className="mrp-reason">
                <ReasonText reason={c.reason_after} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MrpImpactTable({ rows }: { rows: MrpRowChange[] }) {
  if (rows.length === 0) {
    return (
      <div className="card">
        <div className="empty">
          No mill order is recommended either way — nothing to order.
        </div>
      </div>
    );
  }
  return (
    <div className="table-scroll">
      <table className="scenario-table">
        <thead>
          <tr>
            <th>Change</th>
            <th>Product</th>
            <th>Quantity</th>
            <th>Driving ROS</th>
            <th>Recommended order date</th>
            <th>Reason (scenario)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={`${r.product_id}-${r.unrecoverable}`}
              className={r.kind === "unchanged" ? "" : "row-changed"}
            >
              <td>
                <span className={`mrp-kind mrp-kind-${r.kind}`}>{r.kind}</span>
                {r.unrecoverable && (
                  <span className="knock-on">unrecoverable — escalate</span>
                )}
              </td>
              <td>
                <Link to={`/mrp/by-item/${r.product_id}`}>
                  {r.product_description ?? r.product_id}
                </Link>
              </td>
              <td>
                <ValueDiff
                  before={r.quantity_before}
                  after={r.quantity_after}
                  format={(v) => Number(v).toLocaleString()}
                />
              </td>
              <td>
                <ValueDiff
                  before={r.ros_date_before}
                  after={r.ros_date_after}
                  format={(v) => formatDay(String(v))}
                />
              </td>
              <td>
                <ValueDiff
                  before={r.recommended_order_date_before}
                  after={r.recommended_order_date_after}
                  format={(v) => formatDay(String(v))}
                />
              </td>
              <td className="mrp-reason">
                <ReasonText reason={r.reason_after} fallback="no longer needed" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function overrideValue(o: ScenarioOverride) {
  // BOTH columns set is the `number+date` composite -- a whole hypothetical purchase
  // order (`PoArrival.new_order`). Checked FIRST, because the single-column branches
  // below would otherwise print the quantity and silently drop the arrival date, and
  // the pair is the whole content of the row: neither half means anything alone.
  if (o.value_number !== null && o.value_date !== null) {
    return `${o.value_number.toLocaleString()} arriving ${formatDay(o.value_date)}`;
  }
  if (o.value_number !== null) return o.value_number.toLocaleString();
  if (o.value_date !== null) return formatDay(o.value_date);
  return o.value_text ?? "—";
}

/** Resolve target ids to the names a planner actually knows. Fallback to an
 * 8-char id prefix only when the lookup genuinely has no row (a deleted
 * product, a line outside this customer's pool) -- same honesty rule as
 * everywhere else: a name we cannot resolve is shown as an identifier, never
 * guessed. */
function overrideTarget(o: ScenarioOverride, names: TargetNames) {
  const bits: string[] = [];
  if (o.target_demand_line_id)
    bits.push(
      names.lines.get(o.target_demand_line_id) ??
        `line ${o.target_demand_line_id.slice(0, 8)}…`
    );
  if (o.target_well_id)
    bits.push(
      names.wells.get(o.target_well_id) ??
        `well ${o.target_well_id.slice(0, 8)}…`
    );
  if (o.target_product_id)
    bits.push(
      names.products.get(o.target_product_id) ??
        `product ${o.target_product_id.slice(0, 8)}…`
    );
  if (o.target_to_product_id)
    bits.push(
      `→ ${
        names.products.get(o.target_to_product_id) ??
        `substitute ${o.target_to_product_id.slice(0, 8)}…`
      }`
    );
  if (o.target_business_unit_id)
    bits.push(
      names.businessUnits.get(o.target_business_unit_id) ??
        `BU ${o.target_business_unit_id.slice(0, 8)}…`
    );
  return bits.join(" · ") || "—";
}

type TargetNames = {
  lines: Map<string, string>;
  wells: Map<string, string>;
  products: Map<string, string>;
  businessUnits: Map<string, string>;
};

function OverrideTable({
  overrides,
  editable,
  onRemove,
  names,
}: {
  overrides: ScenarioOverride[];
  editable: boolean;
  onRemove: (id: string) => void;
  names: TargetNames;
}) {
  if (overrides.length === 0) {
    return (
      <div className="card">
        <div className="empty">
          No overrides yet — add one below to see its impact.
        </div>
      </div>
    );
  }
  return (
    <div className="table-scroll">
      <table className="scenario-table">
        <thead>
          <tr>
            <th>Family</th>
            <th>Field</th>
            <th>New value</th>
            <th>Target</th>
            <th>Note</th>
            {editable && <th>Actions</th>}
          </tr>
        </thead>
        <tbody>
          {overrides.map((o) => (
            <tr key={o.id}>
              <td>{o.target_kind}</td>
              <td>{o.field_name}</td>
              <td className="override-value">{overrideValue(o)}</td>
              <td className="override-target">{overrideTarget(o, names)}</td>
              <td className="mrp-reason">{o.note ?? "—"}</td>
              {editable && (
                <td>
                  <button className="link-btn" onClick={() => onRemove(o.id)}>
                    Remove
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Add-override form.
 *
 * The kind/field/value-type vocabulary comes from GET /scenarios/override-fields
 * rather than being hardcoded here, so the picker cannot drift from
 * `app.engines.overrides.OVERRIDE_FIELDS`.
 *
 * Products and Business Units are PICKED from the catalogue, not typed as raw
 * UUIDs. The original form took bare id inputs ("the Well Workspace already
 * shows them"), and the product owner hit exactly the failure that argument
 * invites: an Assignment override demands a product id and no screen calls it
 * by that name -- the only place to find one was the By Item URL. A select
 * showing descriptions costs one catalogue fetch and removes the scavenger
 * hunt.
 */
function AddOverrideForm({
  scenarioId,
  vocab,
  lines,
  products,
  businessUnits,
  onAdded,
}: {
  scenarioId: string;
  vocab: OverrideFields | null;
  lines: LineCoverageChange[];
  products: { id: string; description: string | null }[];
  businessUnits: { id: string; name: string }[];
  onAdded: () => void;
}) {
  const kinds = vocab ? Object.keys(vocab.fields) : [];
  const [kind, setKind] = useState<string>("DemandLine");
  const [field, setField] = useState<string>("quantity");
  const [lineId, setLineId] = useState<string>("");
  const [wellId, setWellId] = useState<string>("");
  const [productId, setProductId] = useState<string>("");
  const [buId, setBuId] = useState<string>("");
  const [toProductId, setToProductId] = useState<string>("");
  const [value, setValue] = useState<string>("");
  /** The SECOND value, used only by the `number+date` composite
   *  (`PoArrival.new_order`): `value` holds the quantity and this holds the arrival
   *  date. A hypothetical purchase order is a quantity landing on a date and the row
   *  carries both columns, so the form has to collect both. */
  const [value2, setValue2] = useState<string>("");
  const [note, setNote] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!vocab) return <p>Loading the override vocabulary...</p>;

  const fields = vocab.fields[kind] ?? {};
  const valueType: OverrideValueType = fields[field] ?? "text";
  const enumKey = `${kind}.${field}`;
  const enumValues = vocab.enum_values[enumKey];
  const isSupply = vocab.supply_kinds.includes(kind);
  /** The `number+date` composite -- a whole hypothetical purchase order in one row. */
  const isPair = valueType === "number+date";
  /** A hypothetical new order is scoped to (BU, product) exactly as a real
   *  `InventoryOnOrder` row is, and the backend REQUIRES the Business Unit -- which is
   *  half of what makes the row structurally distinguishable from an `arrival_date`
   *  restatement. So the BU input is shown for it, not only for `Inventory`. */
  const needsBu = kind === "Inventory" || isPair;

  const pickKind = (next: string) => {
    setKind(next);
    const first = Object.keys(vocab.fields[next] ?? {})[0] ?? "";
    setField(first);
    setValue("");
    setValue2("");
  };

  const pickField = (next: string) => {
    setField(next);
    // Cleared on a field change: `arrival_date` and `new_order` sit on the same kind
    // but take different value columns, so a leftover value would be sent into the
    // wrong column and refused.
    setValue("");
    setValue2("");
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.addScenarioOverride(scenarioId, {
        target_kind: kind as ScenarioTargetKind,
        field_name: field,
        target_demand_line_id: lineId || null,
        target_well_id: wellId || null,
        target_product_id: productId || null,
        target_business_unit_id: buId || null,
        target_to_product_id: toProductId || null,
        // The composite sends BOTH columns; every other field sends exactly one and
        // explicitly nulls the rest. `app.engines.overrides.validate` enforces both
        // shapes, so a mistake here is a 400 rather than a wrong answer.
        value_number:
          valueType === "number" || isPair ? Number(value) : null,
        value_date: isPair
          ? new Date(value2).toISOString()
          : valueType === "date"
            ? new Date(value).toISOString()
            : null,
        value_text: valueType === "text" ? value : null,
        note: note || null,
      });
      setValue("");
      setValue2("");
      setNote("");
      onAdded();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="override-form">
      <label>
        Override family
        <select value={kind} onChange={(e) => pickKind(e.target.value)}>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {k}
              {vocab.unmodelled_kinds.includes(k) ? " (not modelled)" : ""}
            </option>
          ))}
        </select>
      </label>
      <label>
        Field
        <select value={field} onChange={(e) => pickField(e.target.value)}>
          {Object.keys(fields).map((f) => (
            <option key={f} value={f}>
              {f}
            </option>
          ))}
        </select>
      </label>

      {kind === "Well" && (
        <label>
          Well
          <select value={wellId} onChange={(e) => setWellId(e.target.value)}>
            <option value="">select…</option>
            {Array.from(
              new Map(lines.map((l) => [l.well_id, l.well_name])).entries()
            ).map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </label>
      )}

      {(kind === "DemandLine" ||
        kind === "Assignment" ||
        kind === "SubstitutionApproval") && (
        <label>
          Demand line
          <select value={lineId} onChange={(e) => setLineId(e.target.value)}>
            <option value="">select…</option>
            {lines.map((l) => (
              <option key={l.demand_line_id} value={l.demand_line_id}>
                {l.well_name} · {l.product_description ?? l.product_id}
              </option>
            ))}
          </select>
        </label>
      )}
      {(kind === "Inventory" || kind === "PoArrival" || kind === "Assignment") && (
        <label>
          Product
          <select
            value={productId}
            onChange={(e) => setProductId(e.target.value)}
          >
            <option value="">select…</option>
            {products.map((p) => (
              <option key={p.id} value={p.id}>
                {p.description ?? p.id}
              </option>
            ))}
          </select>
        </label>
      )}
      {needsBu && (
        <label>
          Business Unit
          <select value={buId} onChange={(e) => setBuId(e.target.value)}>
            <option value="">select…</option>
            {businessUnits.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {kind === "SubstitutionApproval" && (
        <label>
          Substitute product
          <select
            value={toProductId}
            onChange={(e) => setToProductId(e.target.value)}
          >
            <option value="">select…</option>
            {products.map((p) => (
              <option key={p.id} value={p.id}>
                {p.description ?? p.id}
              </option>
            ))}
          </select>
        </label>
      )}

      <label>
        {isPair ? "Order quantity" : "New value"}
        {enumValues ? (
          <select value={value} onChange={(e) => setValue(e.target.value)}>
            <option value="">select…</option>
            {enumValues.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        ) : (
          <input
            type={
              valueType === "number" || isPair
                ? "number"
                : valueType === "date"
                  ? "date"
                  : "text"
            }
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        )}
      </label>
      {/* The composite's SECOND value. Both are required -- a quantity with no
          arrival date could not be projected into any month, and a date with no
          quantity would add nothing -- so `validate` refuses either half alone and
          the button below stays disabled until both are given. */}
      {isPair && (
        <label>
          Expected arrival
          <input
            type="date"
            value={value2}
            onChange={(e) => setValue2(e.target.value)}
          />
        </label>
      )}
      <label>
        Note
        <input value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <button onClick={submit} disabled={busy || !value || (isPair && !value2)}>
        {busy ? "Adding..." : "Add override"}
      </button>
      {/* The what-if note for a HYPOTHETICAL ORDER is its own, and is shown in
          ADDITION to the generic supply note below: the generic one says "Oracle owns
          this row so we cannot write to it", and here there is no row in either
          system, so the honest statement is stronger and different. */}
      {isPair && (
        <div className="whatif-note">
          <strong>
            This asserts a purchase order that does not exist.
          </strong>{" "}
          It is added to the <em>runout projection</em> at the month you give, beside
          whatever real purchase orders the product already has —{" "}
          <strong>no purchase order is created anywhere</strong>. It moves no coverage
          verdict however large it is (coverage is decided from on-hand stock alone),
          and so no MRP recommendation row either. It can <strong>never be applied</strong>:
          applying it would mean placing an order, which is a human last-resort decision
          this platform recommends but does not execute. Both the quantity and the
          expected arrival date are required, and the Business Unit must be this
          customer&apos;s own.
        </div>
      )}
      {isSupply && (
        <div className="whatif-note">
          Supply overrides can be previewed but never applied — on-hand inventory,
          assignments and purchase orders are owned by Oracle and this platform
          holds read-only copies.
        </div>
      )}
      {error && <div className="form-error">{error}</div>}
    </div>
  );
}

/**
 * Apply panel — guarded.
 *
 * Applying WRITES production data, so the consequence is spelled out before it
 * can happen and the button needs a second, explicit confirmation. It is disabled
 * outright when the backend says the scenario is not applicable, and the reason is
 * shown rather than left to be discovered by pressing it.
 */
function ApplyPanel({
  scenario,
  impact,
  onApplied,
}: {
  scenario: ScenarioDetail;
  impact: ScenarioImpact | null;
  onApplied: (result: ScenarioApplyResult) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const blockers = impact?.apply_blockers ?? [];
  const applicable = (impact?.applicable ?? false) && scenario.override_count > 0;

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      onApplied(await api.applyScenario(scenario.id));
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <div className="apply-panel">
      <h2>Apply to base plan</h2>
      {blockers.length > 0 ? (
        <div className="apply-blocked">
          This scenario cannot be applied:
          <ul>
            {blockers.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="apply-warning">
          This writes to <strong>production data</strong>. It is not a preview and
          it is not reversible from this screen.
          <ul>
            <li>
              {impact?.changed_line_count ?? 0} demand line(s) and{" "}
              {impact?.changed_well_count ?? 0} well(s) will change coverage
              status for everyone.
            </li>
            <li>
              A demand revision and an impact record are written for every line
              that moves, so the change appears in the revision history and on the
              Home Dashboard.
            </li>
            <li>
              This scenario becomes an <strong>immutable record</strong> of what
              was agreed: it cannot be edited or applied again.
            </li>
          </ul>
        </div>
      )}

      <div className="apply-actions">
        {!confirming ? (
          <button
            className="btn-danger"
            disabled={!applicable}
            onClick={() => setConfirming(true)}
          >
            Apply to base plan
          </button>
        ) : (
          <>
            <button className="btn-danger" disabled={busy} onClick={run}>
              {busy ? "Applying..." : "Yes — write these changes now"}
            </button>
            <button
              className="btn-plain"
              disabled={busy}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </>
        )}
        {!applicable && blockers.length === 0 && (
          <span className="scenario-sub" style={{ margin: 0 }}>
            Add at least one override first.
          </span>
        )}
      </div>
      {error && <div className="form-error">{error}</div>}
    </div>
  );
}

export default function ScenarioEditor() {
  const { scenarioId } = useParams<{ scenarioId: string }>();
  const [scenario, setScenario] = useState<ScenarioDetail | null>(null);
  const [impact, setImpact] = useState<ScenarioImpact | null>(null);
  const [vocab, setVocab] = useState<OverrideFields | null>(null);
  const [applied, setApplied] = useState<ScenarioApplyResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [impactError, setImpactError] = useState<string | null>(null);
  // Form vs Timeline lives in the URL (?view=timeline), same convention as
  // every other screen's display state — a bookmarked or reloaded editor
  // reopens on the editor the planner was actually using.
  const [searchParams, setSearchParams] = useSearchParams();
  const view: OverrideView =
    searchParams.get("view") === "timeline" ? "timeline" : "form";
  const setView = (next: OverrideView) => {
    const params = new URLSearchParams(searchParams);
    if (next === "timeline") params.set("view", "timeline");
    else params.delete("view");
    setSearchParams(params, { replace: true });
  };
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [busy, setBusy] = useState(false);
  // Catalogue for the override pickers AND for naming existing override
  // targets. Failing fetches degrade to id-prefix display, never break the
  // screen.
  const [products, setProducts] = useState<
    { id: string; description: string | null }[]
  >([]);
  const [businessUnits, setBusinessUnits] = useState<
    { id: string; name: string }[]
  >([]);
  useEffect(() => {
    api
      .getProducts()
      .then((p) =>
        setProducts(p.map((x) => ({ id: x.id, description: x.description })))
      )
      .catch(() => setProducts([]));
    api
      .getBusinessUnits()
      .then((b) => setBusinessUnits(b.map((x) => ({ id: x.id, name: x.name }))))
      .catch(() => setBusinessUnits([]));
  }, []);
  // Stale-response guard: a slow response from an earlier reload (this screen
  // reloads after every override edit) must not overwrite a newer one.
  const reloadSeq = useRef(0);

  const reload = () => {
    if (!scenarioId) return;
    const seq = ++reloadSeq.current;
    setBusy(true);
    api
      .getScenario(scenarioId)
      .then((s) => {
        if (seq !== reloadSeq.current) return;
        setScenario(s);
        setError(null);
        setFetchedAt(new Date());
      })
      // Shown, but an already-loaded scenario is KEPT — a failed refresh after
      // an edit must not blank the overrides the planner is working on.
      .catch((e) => {
        if (seq === reloadSeq.current) setError(String(e));
      })
      .finally(() => {
        if (seq === reloadSeq.current) setBusy(false);
      });
    // The preview is a separate call so a preview failure does not blank the
    // scenario itself — you must still be able to see and fix the overrides.
    api
      .getScenarioPreview(scenarioId)
      .then((i) => {
        if (seq !== reloadSeq.current) return;
        setImpact(i);
        setImpactError(null);
      })
      .catch((e) => {
        if (seq !== reloadSeq.current) return;
        setImpact(null);
        setImpactError(String(e instanceof Error ? e.message : e));
      });
  };

  useEffect(() => {
    reload();
    api.getOverrideFields().then(setVocab).catch(() => setVocab(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scenarioId]);

  const setStatus = async (status: ScenarioStatus) => {
    if (!scenarioId) return;
    try {
      await api.patchScenario(scenarioId, { status });
      reload();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const removeOverride = async (overrideId: string) => {
    if (!scenarioId) return;
    try {
      await api.deleteScenarioOverride(scenarioId, overrideId);
      reload();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  if (!scenario && error)
    return (
      <div>
        <Breadcrumbs trail={[{ label: "Scenarios", to: "/scenarios" }, { label: "Scenario" }]} />
        <p>Failed to load scenario: {error}</p>
      </div>
    );
  if (!scenario)
    return (
      <div>
        <Breadcrumbs trail={[{ label: "Scenarios", to: "/scenarios" }, { label: "Scenario" }]} />
        <p>Loading...</p>
      </div>
    );

  const editable = scenario.status !== "Applied";

  return (
    <div>
      <Breadcrumbs trail={[{ label: "Scenarios", to: "/scenarios" }, { label: scenario.name }]} />
      <div className="top-actions">
        <Link to="/scenarios">&larr; All scenarios</Link>
        <Freshness fetchedAt={fetchedAt} onRefresh={reload} busy={busy} />
      </div>
      {error && (
        <p className="form-error">
          Refresh failed: {error} — showing the last loaded state.
        </p>
      )}

      <div className="scenario-head">
        <div>
          <h1 style={{ marginBottom: 4 }}>
            {scenario.name}{" "}
            <span className={`badge badge-status-${scenario.status}`}>
              {scenario.status}
            </span>
          </h1>
          <p className="scenario-sub">
            {scenario.customer_name}
            {scenario.created_by ? ` · created by ${scenario.created_by}` : ""} ·
            shared with everyone on the project
          </p>
        </div>
        {editable && (
          <div className="status-picker">
            <span>Lifecycle</span>
            <select
              value={scenario.status}
              onChange={(e) => setStatus(e.target.value as ScenarioStatus)}
            >
              {LIFECYCLE.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {scenario.description && <p>{scenario.description}</p>}

      {applied && (
        <div className="applied-banner">
          <p>
            <strong>Applied to the base plan.</strong> The coverage figures below
            are now the official ones.
          </p>
          <p>
            {applied.revised_demand_line_ids.length} demand line(s) revised,{" "}
            {applied.demand_revision_ids.length} revision(s) and{" "}
            {applied.impact_record_ids.length} impact record(s) written.
          </p>
          {applied.notes.map((n, i) => (
            <p key={i}>{n}</p>
          ))}
        </div>
      )}

      {scenario.status === "Applied" && !applied && (
        <div className="applied-banner">
          <p>
            <strong>
              This scenario was applied
              {scenario.applied_at ? ` on ${scenario.applied_at.slice(0, 10)}` : ""}.
            </strong>{" "}
            It is an immutable record of what was agreed and cannot be edited or
            applied again. To revisit the agreement, create a new scenario.
          </p>
        </div>
      )}

      {impactError && (
        <div className="card">
          <div className="empty">Impact could not be computed: {impactError}</div>
        </div>
      )}

      {impact && <WhatIfBanner impact={impact} />}
      {impact && <ImpactTiles impact={impact} />}

      <section className="scenario-section">
        <div className="scenario-view-head">
          <h2>Overrides</h2>
          <div className="view-tabs" role="tablist" aria-label="Override editor">
            <button
              role="tab"
              aria-selected={view === "form"}
              className={`view-tab${view === "form" ? " view-tab-on" : ""}`}
              onClick={() => setView("form")}
            >
              Form view
            </button>
            <button
              role="tab"
              aria-selected={view === "timeline"}
              className={`view-tab${view === "timeline" ? " view-tab-on" : ""}`}
              onClick={() => setView("timeline")}
            >
              Timeline view
            </button>
          </div>
        </div>
        <p className="scenario-section-note">
          Demand (quantity, ROS, status, profile), supply (inventory, PO arrival,
          assignment) and substitution approval.{" "}
          {view === "timeline"
            ? "The timeline edits the same override rows the list below holds — drag a block and it appears in that list."
            : "Switch to Timeline view to drag demand on a time axis instead of typing values."}
        </p>
        <OverrideTable
          overrides={scenario.overrides}
          editable={editable}
          onRemove={removeOverride}
          names={{
            lines: new Map(
              (impact?.line_changes ?? []).map((l) => [
                l.demand_line_id,
                `${l.well_name} · ${l.product_description ?? l.product_id}`,
              ])
            ),
            wells: new Map(
              (impact?.line_changes ?? []).map((l) => [l.well_id, l.well_name])
            ),
            products: new Map(
              products.map((p) => [p.id, p.description ?? p.id])
            ),
            businessUnits: new Map(businessUnits.map((b) => [b.id, b.name])),
          }}
        />
        {view === "form"
          ? editable && (
              <div style={{ marginTop: 12 }}>
                <AddOverrideForm
                  scenarioId={scenario.id}
                  vocab={vocab}
                  lines={impact?.line_changes ?? []}
                  products={products}
                  businessUnits={businessUnits}
                  onAdded={reload}
                />
              </div>
            )
          : impact && (
              <div style={{ marginTop: 12 }}>
                <ScenarioTimeline
                  scenario={scenario}
                  lines={impact.line_changes}
                  editable={editable}
                  onChanged={reload}
                  // The consequence of an on-order drag, so the timeline can show it
                  // where the gesture happened instead of making the planner scroll
                  // to another panel to find out whether the drag did anything.
                  supplyRunoutChanges={impact.supply_runout_changes}
                  mrpChanges={impact.mrp_changes}
                  // The SAME blockers the Apply panel renders, passed rather than
                  // re-derived, so the timeline cannot invent copy that contradicts
                  // the panel that actually refuses the apply.
                  applyBlockers={impact.apply_blockers}
                  // The scenario customer's own Business Unit, needed to scope a
                  // hypothetical new order to (BU, product) as a real purchase-order
                  // row is. Taken from the PREVIEW rather than from `scenario`, which
                  // carries no BU: the preview resolved it the same way every quantity
                  // on this page was resolved, so the timeline cannot name a different
                  // one than the figures were computed under.
                  businessUnitId={impact.business_unit_id}
                />
              </div>
            )}
        {view === "timeline" && !impact && (
          <div className="card">
            <div className="empty">
              The timeline plots the scenario&apos;s effective demand, which comes
              from the preview — and the preview could not be computed, so there is
              nothing trustworthy to plot. Fix the overrides in Form view first.
            </div>
          </div>
        )}
      </section>

      {impact && (
        <>
          <section className="scenario-section">
            <h2>Coverage impact</h2>
            <p className="scenario-section-note">
              Base plan on the left of each arrow, this scenario on the right.
              Highlighted rows change status.
            </p>
            <CoverageImpactTable changes={impact.line_changes} />
          </section>

          <section className="scenario-section">
            <h2>Well rollup</h2>
            <p className="scenario-section-note">
              A well is Covered only when every evaluated line on it is Covered or
              CoveredViaSubstitute.
            </p>
            <div className="table-scroll">
              <table className="scenario-table">
                <thead>
                  <tr>
                    <th>Well</th>
                    <th>Base plan &rarr; scenario</th>
                  </tr>
                </thead>
                <tbody>
                  {impact.well_changes.map((w) => (
                    <tr key={w.well_id} className={w.changed ? "row-changed" : ""}>
                      <td>
                        <Link to={`/wells/${w.well_id}`}>{w.well_name}</Link>
                      </td>
                      <td>
                        <StatusDiff
                          before={w.status_before}
                          after={w.status_after}
                          changed={w.changed}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="scenario-section">
            <h2>MRP impact</h2>
            <p className="scenario-section-note">
              How the mill-order recommendations would move.
            </p>
            <MrpImpactTable rows={impact.mrp_changes} />
          </section>

          <section className="scenario-section">
            <h2>Risk impact</h2>
            <p className="scenario-section-note">
              Unrecoverable demand — ROS that a mill order cannot meet even if
              ordered today.
            </p>
            {impact.risk &&
            impact.risk.became_unrecoverable.length === 0 &&
            impact.risk.no_longer_unrecoverable.length === 0 ? (
              <div className="card">
                <div className="empty">
                  No change in unrecoverable demand (
                  {impact.risk.unrecoverable_lines_before} line(s),{" "}
                  {impact.risk.unrecoverable_quantity_before.toLocaleString()},
                  either way).
                </div>
              </div>
            ) : (
              impact.risk && (
                <div className="card">
                  {impact.risk.no_longer_unrecoverable.length > 0 && (
                    <p>
                      <strong>
                        {impact.risk.no_longer_unrecoverable.length} line(s) rescued
                      </strong>{" "}
                      out of unrecoverable — a mill order could make the date again.
                    </p>
                  )}
                  {impact.risk.became_unrecoverable.length > 0 && (
                    <p style={{ color: "#a11c1c" }}>
                      <strong>
                        {impact.risk.became_unrecoverable.length} line(s) become
                        unrecoverable
                      </strong>{" "}
                      — the ROS would sit inside the lead time, so no mill order can
                      meet it. Escalate: rescope, borrow, or source externally.
                    </p>
                  )}
                  <p className="whatif-note">
                    Unrecoverable quantity{" "}
                    {impact.risk.unrecoverable_quantity_before.toLocaleString()}{" "}
                    <span className="change-arrow">&rarr;</span>{" "}
                    {impact.risk.unrecoverable_quantity_after.toLocaleString()}
                  </p>
                </div>
              )
            )}
          </section>
        </>
      )}

      {editable && (
        <section className="scenario-section">
          <ApplyPanel
            scenario={scenario}
            impact={impact}
            onApplied={(result) => {
              setApplied(result);
              reload();
            }}
          />
        </section>
      )}
    </div>
  );
}
