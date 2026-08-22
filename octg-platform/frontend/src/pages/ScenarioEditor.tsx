import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import ConfirmButton from "../components/ConfirmButton";
import Freshness from "../components/Freshness";
import { ApiError, apiGet, apiSend } from "../lib/api";
import { DEMAND_STATUSES } from "../lib/enums";
import { verdictText } from "../lib/verdict";
import { errorMessage, type Customer } from "./admin/shared";

type OverrideOut = {
  id: string;
  kind: string;
  target_id: string;
  target_name: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type ScenarioDetail = {
  id: string;
  name: string;
  created_at: string;
  status: string;
  applied_at: string | null;
  overrides: OverrideOut[];
};

const KIND_LABELS: Record<string, string> = {
  quantity: "Quantity",
  ros_date: "ROS date",
  well_status: "Well status",
  po_arrival: "PO arrival",
  hard_release: "Hard release",
  approval_flip: "Approval flip",
};

// spec E-4: apply rejects the whole scenario if any supply-owned override is
// present (po_arrival / hard_release) — the editor pre-warns before Apply.
const SUPPLY_KINDS = new Set(["po_arrival", "hard_release"]);

type DemandLine = {
  id: string;
  well_id: string;
  well_name: string;
  customer_id: string;
  customer_name: string;
  product_id: string;
  product_name: string;
  quantity: number;
  unit: string;
  ros_date: string;
  profile: string;
  overdue: boolean;
};

type Well = { id: string; customer_id: string; name: string; demand_status: string; line_count: number };
type OnOrder = { id: string; business_unit_id: string; product_id: string; quantity: number; unit: string; expected_date: string | null; booking_status: string };
type Assignment = { id: string; business_unit_id: string; product_id: string; customer_id: string; quantity: number; unit: string; reference: string | null };
type Approval = { id: string; status: string; customer_id: string; customer_name: string; well_id: string; well_name: string; to_product_name: string };
type Product = { id: string; name: string };

// spec E-3 preview response shapes (app/engines/scenario.py):
// coverage: {before: {verdict: count}, after: {verdict: count}}
// mrp: {before: {"bu:product": {unit, runout_months: {baseline, with_recommended, on_order}}}, after: same}
type CoveragePreview = { before: Record<string, number>; after: Record<string, number> };
type MrpEntry = { unit: string; runout_months: { baseline: string | null; with_recommended: string | null; on_order: string | null } };
type MrpPreview = { before: Record<string, MrpEntry>; after: Record<string, MrpEntry> };

function payloadSummary(kind: string, payload: Record<string, unknown>): string {
  if (kind === "hard_release") return "(unassign)";
  return Object.entries(payload)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(", ");
}

function monthKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function addMonths(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth() + n, 1);
}

function daysInMonth(year: number, month0: number): number {
  return new Date(year, month0 + 1, 0).getDate();
}

// Clamps `day` to `[year, month0]`'s length and returns an ISO date string.
function clampedIsoDate(year: number, month0: number, day: number): string {
  const clamped = Math.min(day, daysInMonth(year, month0));
  return `${year}-${String(month0 + 1).padStart(2, "0")}-${String(clamped).padStart(2, "0")}`;
}

function CoverageComparison({ preview }: { preview: CoveragePreview }) {
  const verdicts = Array.from(new Set([...Object.keys(preview.before), ...Object.keys(preview.after)])).sort();
  return (
    <table className="admin-table nested-table">
      <thead>
        <tr>
          <th>Verdict</th>
          <th>Before</th>
          <th>After</th>
          <th>Delta</th>
        </tr>
      </thead>
      <tbody>
        {verdicts.length === 0 && (
          <tr>
            <td colSpan={4}>—</td>
          </tr>
        )}
        {verdicts.map((v) => {
          const before = preview.before[v] ?? 0;
          const after = preview.after[v] ?? 0;
          const delta = after - before;
          return (
            <tr key={v} className={delta !== 0 ? "preview-row-changed" : undefined}>
              <td>{verdictText(v)}</td>
              <td className="num">{before}</td>
              <td className="num">{after}</td>
              <td className={`num${delta !== 0 ? " preview-delta" : ""}`}>
                {delta > 0 ? `+${delta}` : delta}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function MrpComparison({ preview, productName }: { preview: MrpPreview; productName: (id: string) => string }) {
  const keys = Array.from(new Set([...Object.keys(preview.before), ...Object.keys(preview.after)])).sort();
  const runoutLabel = (r: MrpEntry["runout_months"] | undefined) =>
    r ? `baseline ${r.baseline ?? "—"} · w/rec ${r.with_recommended ?? "—"} · on-order ${r.on_order ?? "—"}` : "—";
  return (
    <table className="admin-table nested-table">
      <thead>
        <tr>
          <th>Product</th>
          <th>Before runout</th>
          <th>After runout</th>
        </tr>
      </thead>
      <tbody>
        {keys.length === 0 && (
          <tr>
            <td colSpan={3}>—</td>
          </tr>
        )}
        {keys.map((key) => {
          const [, productId] = key.split(":");
          const before = preview.before[key];
          const after = preview.after[key];
          const changed = JSON.stringify(before?.runout_months) !== JSON.stringify(after?.runout_months);
          return (
            <tr key={key} className={changed ? "preview-row-changed" : undefined}>
              <td>{productName(productId)}</td>
              <td className="num">{runoutLabel(before?.runout_months)}</td>
              <td className={`num${changed ? " preview-delta" : ""}`}>{runoutLabel(after?.runout_months)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export default function ScenarioEditor() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<ScenarioDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  const [customers, setCustomers] = useState<Customer[]>([]);
  const [customerId, setCustomerId] = useState<string>("");
  const [demandLines, setDemandLines] = useState<DemandLine[]>([]);
  const [wells, setWells] = useState<Well[]>([]);
  const [onOrder, setOnOrder] = useState<OnOrder[]>([]);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [products, setProducts] = useState<Product[]>([]);

  const [kind, setKind] = useState<string>("quantity");
  const [targetId, setTargetId] = useState<string>("");
  const [numValue, setNumValue] = useState<string>("");
  const [dateValue, setDateValue] = useState<string>("");
  const [wellStatusValue, setWellStatusValue] = useState<string>(DEMAND_STATUSES[0]);
  const [customerApproved, setCustomerApproved] = useState(false);
  const [wellApproved, setWellApproved] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const [preview, setPreview] = useState<{ coverage?: CoveragePreview; mrp?: MrpPreview } | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [rejected, setRejected] = useState<{ id: string; kind: string; target_id: string }[] | null>(null);

  const [dragLineId, setDragLineId] = useState<string | null>(null);
  const [dragOverMonth, setDragOverMonth] = useState<string | null>(null);

  const load = () => {
    if (!id) return;
    apiGet<ScenarioDetail>(`/scenarios/${id}`)
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e) => setError(errorMessage(e)));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [id]);

  useEffect(() => {
    apiGet<Customer[]>("/customers")
      .then(setCustomers)
      .catch(() => undefined);
    apiGet<Product[]>("/products")
      .then(setProducts)
      .catch(() => undefined);
    apiGet<{ on_hand: unknown[]; assignments: Assignment[]; on_order: OnOrder[] }>("/company-inventory")
      .then((d) => {
        setOnOrder(d.on_order);
        setAssignments(d.assignments);
      })
      .catch(() => undefined);
    apiGet<Approval[]>("/substitution-approvals")
      .then(setApprovals)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!customerId) {
      setDemandLines([]);
      setWells([]);
      return;
    }
    apiGet<{ items: DemandLine[] }>(`/demand/lines?customer_id=${customerId}&page_size=100`)
      .then((d) => setDemandLines(d.items))
      .catch(() => undefined);
    apiGet<Well[]>(`/wells?customer_id=${customerId}`)
      .then(setWells)
      .catch(() => undefined);
  }, [customerId]);

  const isDraft = data?.status === "Draft";

  const productName = (productId: string) => products.find((p) => p.id === productId)?.name ?? productId;

  const resetTargetInputs = () => {
    setTargetId("");
    setNumValue("");
    setDateValue("");
    setWellStatusValue(DEMAND_STATUSES[0]);
    setCustomerApproved(false);
    setWellApproved(false);
  };

  const addOverride = () => {
    if (!id || !targetId) return;
    let payload: Record<string, unknown> = {};
    if (kind === "quantity") payload = { value: Number(numValue) };
    else if (kind === "ros_date") payload = { value: dateValue };
    else if (kind === "well_status") payload = { value: wellStatusValue };
    else if (kind === "po_arrival") payload = { value: dateValue || null };
    else if (kind === "hard_release") payload = {};
    else if (kind === "approval_flip") payload = { customer_approved: customerApproved, well_approved: wellApproved };

    setAdding(true);
    apiSend<OverrideOut>("POST", `/scenarios/${id}/overrides`, { kind, target_id: targetId, payload })
      .then(() => {
        setAddError(null);
        resetTargetInputs();
        load();
      })
      .catch((e) => setAddError(errorMessage(e)))
      .finally(() => setAdding(false));
  };

  const deleteOverride = (overrideId: string) => {
    if (!id) return;
    apiSend("DELETE", `/scenarios/${id}/overrides/${overrideId}`)
      .then(() => load())
      .catch((e) => setError(errorMessage(e)));
  };

  const runPreview = () => {
    if (!id) return;
    apiGet<{ coverage?: CoveragePreview; mrp?: MrpPreview }>(`/scenarios/${id}/preview?sections=coverage,mrp`)
      .then((d) => {
        setPreview(d);
        setPreviewError(null);
      })
      .catch((e) => setPreviewError(errorMessage(e)));
  };

  const apply = () => {
    if (!id) return;
    setApplyError(null);
    setRejected(null);
    apiSend<{ applied_overrides: string[]; rejected: unknown[] }>("POST", `/scenarios/${id}/apply`)
      .then(() => load())
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 422) {
          const detail = (e.body as { detail?: { rejected?: typeof rejected } } | null)?.detail;
          setRejected(detail?.rejected ?? []);
        } else {
          setApplyError(errorMessage(e));
        }
      });
  };

  // Resolves a rejected-override's target_id to the human-readable name the
  // scenario's own override list already carries — a raw UUID must never
  // reach the screen, so unresolved falls back to "—", never the id.
  const rejectedTargetName = (targetId: string): string =>
    (data?.overrides ?? []).find((o) => o.target_id === targetId)?.target_name ?? "—";

  const hasSupplyOverride = (data?.overrides ?? []).some((o) => SUPPLY_KINDS.has(o.kind));

  // --- timeline (spec E-5) -----------------------------------------------

  const months = useMemo(() => {
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), 1);
    return Array.from({ length: 12 }, (_, i) => addMonths(start, i));
  }, []);

  // Pending ros_date overrides, applied on top of the base ros_date for
  // timeline positioning — last-created override for a line wins.
  const rosOverrideByLine = useMemo(() => {
    const map = new Map<string, string>();
    for (const o of data?.overrides ?? []) {
      if (o.kind === "ros_date") map.set(o.target_id, String(o.payload.value));
    }
    return map;
  }, [data]);

  const timelineLines = useMemo(() => {
    return demandLines.map((line) => {
      const effectiveDate = rosOverrideByLine.get(line.id) ?? line.ros_date;
      return { line, effectiveDate };
    });
  }, [demandLines, rosOverrideByLine]);

  const linesByMonth = useMemo(() => {
    const map = new Map<string, typeof timelineLines>();
    for (const entry of timelineLines) {
      const key = entry.effectiveDate.slice(0, 7);
      const arr = map.get(key) ?? [];
      arr.push(entry);
      map.set(key, arr);
    }
    return map;
  }, [timelineLines]);

  const handleDrop = async (month: Date) => {
    setDragOverMonth(null);
    if (!id || !dragLineId || !isDraft) return;
    const lineId = dragLineId;
    setDragLineId(null);
    const entry = timelineLines.find((e) => e.line.id === lineId);
    if (!entry) return;
    const currentDay = Number(entry.effectiveDate.slice(8, 10));
    const newDate = clampedIsoDate(month.getFullYear(), month.getMonth(), currentDay);

    // At most one ros_date override per line: delete any existing one for
    // this target before posting the new one, so drags don't accumulate.
    const existing = (data?.overrides ?? []).filter((o) => o.kind === "ros_date" && o.target_id === lineId);
    try {
      for (const o of existing) {
        await apiSend("DELETE", `/scenarios/${id}/overrides/${o.id}`);
      }
      await apiSend<OverrideOut>("POST", `/scenarios/${id}/overrides`, {
        kind: "ros_date",
        target_id: lineId,
        payload: { value: newDate },
      });
      load();
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  if (!data && !error) return <p>Loading…</p>;

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Scenarios", to: "/scenarios" }, { label: data?.name ?? "…" }]} />
      <div className="card">
        <div className="page-header">
          <h1>{data?.name ?? "Scenario"}</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>
        {error && <p className="inline-error">{error}</p>}
        {data && (
          <p className="hint">
            Status: <span className={`scenario-chip scenario-${data.status.toLowerCase()}`}>{data.status}</span>
            {data.applied_at && <> · Applied {new Date(data.applied_at).toLocaleString()}</>}
          </p>
        )}
        {data && !isDraft && <p className="banner-warn">Applied scenarios are read-only.</p>}
      </div>

      {data && (
        <div className="card">
          <h2>Overrides</h2>
          <table className="admin-table">
            <thead>
              <tr>
                <th>Kind</th>
                <th>Target</th>
                <th>Payload</th>
                <th>Created</th>
                {isDraft && <th />}
              </tr>
            </thead>
            <tbody>
              {data.overrides.length === 0 && (
                <tr>
                  <td colSpan={isDraft ? 5 : 4}>—</td>
                </tr>
              )}
              {data.overrides.map((o) => (
                <tr key={o.id}>
                  <td>{KIND_LABELS[o.kind] ?? o.kind}</td>
                  <td>{o.target_name}</td>
                  <td>{payloadSummary(o.kind, o.payload)}</td>
                  <td>{new Date(o.created_at).toLocaleString()}</td>
                  {isDraft && (
                    <td>
                      <ConfirmButton label="Delete" armedLabel="Confirm delete" onConfirm={() => deleteOverride(o.id)} />
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {isDraft && (
        <div className="card">
          <h2>Add override</h2>
          <div className="admin-add-row">
            <select
              value={kind}
              onChange={(e) => {
                setKind(e.target.value);
                resetTargetInputs();
              }}
            >
              {Object.entries(KIND_LABELS).map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>

            {(kind === "quantity" || kind === "ros_date" || kind === "well_status") && (
              <select value={customerId} onChange={(e) => setCustomerId(e.target.value)}>
                <option value="">Select customer…</option>
                {customers.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            )}

            {(kind === "quantity" || kind === "ros_date") && (
              <select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                <option value="">Select demand line…</option>
                {demandLines.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.well_name} / {l.product_name} (ros {l.ros_date})
                  </option>
                ))}
              </select>
            )}
            {kind === "quantity" && (
              <input type="number" min={0} placeholder="New quantity" value={numValue} onChange={(e) => setNumValue(e.target.value)} />
            )}
            {kind === "ros_date" && <input type="date" value={dateValue} onChange={(e) => setDateValue(e.target.value)} />}

            {kind === "well_status" && (
              <>
                <select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                  <option value="">Select well…</option>
                  {wells.map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.name}
                    </option>
                  ))}
                </select>
                <select value={wellStatusValue} onChange={(e) => setWellStatusValue(e.target.value)}>
                  {DEMAND_STATUSES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </>
            )}

            {kind === "po_arrival" && (
              <>
                <select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                  <option value="">Select PO…</option>
                  {onOrder.map((po) => (
                    <option key={po.id} value={po.id}>
                      {productName(po.product_id)} · {po.expected_date ?? "no date"}
                    </option>
                  ))}
                </select>
                <input type="date" value={dateValue} onChange={(e) => setDateValue(e.target.value)} />
              </>
            )}

            {kind === "hard_release" && (
              <select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                <option value="">Select assignment…</option>
                {assignments.map((a) => (
                  <option key={a.id} value={a.id}>
                    {productName(a.product_id)} · {a.quantity} {a.unit}
                    {a.reference ? ` (${a.reference})` : ""}
                  </option>
                ))}
              </select>
            )}

            {kind === "approval_flip" && (
              <>
                <select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                  <option value="">Select approval…</option>
                  {approvals.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.customer_name} / {a.well_name} / {a.to_product_name} ({a.status})
                    </option>
                  ))}
                </select>
                <label className="checkbox-label">
                  <input type="checkbox" checked={customerApproved} onChange={(e) => setCustomerApproved(e.target.checked)} />
                  Customer approved
                </label>
                <label className="checkbox-label">
                  <input type="checkbox" checked={wellApproved} onChange={(e) => setWellApproved(e.target.checked)} />
                  Well approved
                </label>
              </>
            )}

            <button type="button" onClick={addOverride} disabled={adding || !targetId}>
              {adding ? "Adding…" : "Add override"}
            </button>
          </div>
          {kind === "po_arrival" && <p className="hint">Supply-side override — will be rejected by Apply.</p>}
          {kind === "hard_release" && <p className="hint">Supply-side override — will be rejected by Apply.</p>}
          {addError && <p className="inline-error">{addError}</p>}
        </div>
      )}

      <div className="card">
        <h2>Timeline</h2>
        {!customerId && <p className="hint">Select a customer above to load demand lines onto the timeline.</p>}
        <div className="timeline">
          <div className="timeline-grid">
            {months.map((m) => {
              const key = monthKey(m);
              const label = m.toLocaleDateString(undefined, { month: "short", year: "numeric" });
              const lines = linesByMonth.get(key) ?? [];
              return (
                <div
                  key={key}
                  className={`timeline-month ${dragOverMonth === key ? "timeline-month-dragover" : ""}`}
                  onDragOver={(e) => {
                    if (!isDraft) return;
                    e.preventDefault();
                    setDragOverMonth(key);
                  }}
                  onDragLeave={() => setDragOverMonth((prev) => (prev === key ? null : prev))}
                  onDrop={() => handleDrop(m)}
                >
                  <div className="timeline-month-header">{label}</div>
                  {lines.map(({ line, effectiveDate }) => (
                    <span
                      key={line.id}
                      className="timeline-chip"
                      draggable={isDraft}
                      onDragStart={() => setDragLineId(line.id)}
                      title={`${line.well_name} / ${line.product_name} — ros ${effectiveDate}`}
                    >
                      {line.well_name} / {line.product_name}
                    </span>
                  ))}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Preview</h2>
        <button type="button" onClick={runPreview}>
          Run preview
        </button>
        {previewError && <p className="inline-error">{previewError}</p>}
        {preview && (
          <div className="preview-panel">
            {preview.coverage && (
              <div>
                <h3>Coverage (before → after)</h3>
                <CoverageComparison preview={preview.coverage} />
              </div>
            )}
            {preview.mrp && (
              <div>
                <h3>MRP runouts (before → after)</h3>
                <MrpComparison preview={preview.mrp} productName={productName} />
              </div>
            )}
          </div>
        )}
      </div>

      {isDraft && (
        <div className="card">
          <h2>Apply</h2>
          {hasSupplyOverride && (
            <p className="banner-error">
              This scenario includes supply-side overrides (PO arrival / hard release) — Apply will reject the
              whole scenario (422).
            </p>
          )}
          <ConfirmButton label="Apply scenario" armedLabel="Confirm apply" onConfirm={apply} />
          {applyError && <p className="inline-error">{applyError}</p>}
          {rejected && (
            <div className="inline-error">
              Apply rejected — supply-side overrides:
              <ul>
                {rejected.map((r) => (
                  <li key={r.id}>
                    {KIND_LABELS[r.kind] ?? r.kind} — {rejectedTargetName(r.target_id)}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
