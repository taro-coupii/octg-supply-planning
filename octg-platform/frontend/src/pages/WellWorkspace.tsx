import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import ConfirmButton from "../components/ConfirmButton";
import Freshness from "../components/Freshness";
import VerdictChip from "../components/VerdictChip";
import { ApiError, apiGet, apiSend } from "../lib/api";
import { DEMAND_STATUSES } from "../lib/enums";
import type { Customer, Product } from "./admin/shared";
import { errorMessage } from "./admin/shared";

type LineCoverage = {
  verdict: string;
  reason: string;
  action: string | null;
  covered_qty: number;
  computed_at: string;
};

type DemandLineDetail = {
  id: string;
  product_id: string;
  quantity: number;
  unit: string;
  ros_date: string;
  profile: string;
  coverage: LineCoverage | null;
};

type Revision = {
  id: string;
  revision_no: number;
  applied_at: string;
  source: string;
  summary: string;
};

type WellDetail = {
  id: string;
  customer_id: string;
  name: string;
  demand_status: string;
  lines: DemandLineDetail[];
  revisions: Revision[];
};

// Cross-cutting rule: users never see raw UUIDs — resolve to a name, or "—".
function nameOf(items: Product[] | Customer[], id: string): string {
  return items.find((i) => i.id === id)?.name ?? "—";
}

// Actions that route the user to the Substitution Workspace to act on.
function isSubstitutionAction(action: string): boolean {
  return action.toLowerCase().includes("substitution");
}

// §3-8: mixed-unit aggregates render per unit, never summed to a scalar.
function qtyByUnit(lines: DemandLineDetail[]): Record<string, number> {
  const result: Record<string, number> = {};
  for (const l of lines) result[l.unit] = (result[l.unit] ?? 0) + l.quantity;
  return result;
}

export default function WellWorkspace() {
  const { id } = useParams<{ id: string }>();
  const [well, setWell] = useState<WellDetail | null>(null);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [statusChoice, setStatusChoice] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Promise.all([apiGet<Customer[]>("/customers"), apiGet<Product[]>("/products")])
      .then(([c, p]) => {
        setCustomers(c);
        setProducts(p);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const load = () => {
    if (!id) return;
    apiGet<WellDetail>(`/wells/${id}`)
      .then((w) => {
        setWell(w);
        setStatusChoice(w.demand_status);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, [id]);

  const changeStatus = () => {
    if (!id || !statusChoice) return;
    setBusy(true);
    apiSend("POST", `/wells/${id}/status`, { status: statusChoice })
      .then(() => load())
      .catch((e: ApiError | Error) => setError(errorMessage(e)))
      .finally(() => setBusy(false));
  };

  const custName = well ? nameOf(customers, well.customer_id) : "";
  const summary = well ? qtyByUnit(well.lines) : {};

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Wells" }, { label: well ? well.name : "…" }]} />
      <div className="card">
        <div className="page-header">
          <h1>{well ? well.name : "…"}</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>
        {error && <p className="inline-error">{error}</p>}

        {well && (
          <>
            <p className="hint">Customer: {custName}</p>

            <div className="admin-add-row">
              <select value={statusChoice} onChange={(e) => setStatusChoice(e.target.value)}>
                {DEMAND_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
              <ConfirmButton
                label={busy ? "Saving…" : "Change status"}
                armedLabel="Confirm status change"
                onConfirm={changeStatus}
              />
              <span className="hint">Current: {well.demand_status}</span>
            </div>

            <h3>Demand lines</h3>
            {well.lines.length === 0 && <p className="hint">No demand lines.</p>}
            {well.lines.length > 0 && (
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Product</th>
                    <th>Quantity</th>
                    <th>Unit</th>
                    <th>ROS Date</th>
                    <th>Profile</th>
                    <th>Coverage</th>
                    <th>Reason</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {well.lines.map((l) => (
                    <tr key={l.id}>
                      <td>{nameOf(products, l.product_id)}</td>
                      <td>{l.quantity}</td>
                      <td>{l.unit}</td>
                      <td>{l.ros_date}</td>
                      <td>{l.profile}</td>
                      <td>
                        <VerdictChip verdict={l.coverage?.verdict} />
                      </td>
                      <td>{l.coverage?.reason ?? "—"}</td>
                      <td>
                        {l.coverage?.action ? (
                          isSubstitutionAction(l.coverage.action) ? (
                            <Link to={`/substitution?demand_line_id=${l.id}`}>{l.coverage.action}</Link>
                          ) : (
                            l.coverage.action
                          )
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                  <tr>
                    <td>
                      <strong>Total by unit</strong>
                    </td>
                    <td colSpan={7}>
                      {Object.keys(summary).length === 0
                        ? "—"
                        : Object.keys(summary)
                            .map((u) => `${summary[u]} ${u}`)
                            .join(", ")}
                    </td>
                  </tr>
                </tbody>
              </table>
            )}

            <h3>Revision history</h3>
            {well.revisions.length === 0 && <p className="hint">No revisions.</p>}
            {well.revisions.length > 0 && (
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>No</th>
                    <th>Applied at</th>
                    <th>Source</th>
                    <th>Summary</th>
                  </tr>
                </thead>
                <tbody>
                  {well.revisions.map((r) => (
                    <tr key={r.id}>
                      <td>{r.revision_no}</td>
                      <td>{r.applied_at}</td>
                      <td>{r.source}</td>
                      <td>{r.summary}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </div>
  );
}
