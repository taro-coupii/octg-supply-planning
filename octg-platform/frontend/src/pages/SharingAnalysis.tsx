import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import VerdictChip from "../components/VerdictChip";
import { ApiError, apiGet } from "../lib/api";
import { writeParam } from "../lib/urlState";
import { errorMessage } from "./admin/shared";

type Customer = { id: string; name: string };

type Line = {
  id: string;
  well_id: string;
  product_id: string;
  unit: string;
  quantity: number;
};

type Peer = {
  customer_id: string;
  customer_name: string;
  releasable_qty_by_unit: number;
  would_cover: boolean;
};

type SharingEntry = {
  line: Line;
  official_verdict: string;
  peers: Peer[];
};

export default function SharingAnalysis() {
  const [params, setParams] = useSearchParams();
  const customerId = params.get("customer_id") ?? "";

  const [customers, setCustomers] = useState<Customer[]>([]);
  const [entries, setEntries] = useState<SharingEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  useEffect(() => {
    apiGet<Customer[]>("/customers")
      .then((c) => setCustomers(c))
      .catch((e: Error) => setError(e.message));
  }, []);

  const load = () => {
    if (!customerId) {
      setEntries(null);
      return;
    }
    apiGet<SharingEntry[]>(`/analysis/sharing?customer_id=${customerId}`)
      .then((d) => {
        setEntries(d);
        setFetchedAt(new Date());
        setError(null);
      })
      // Keep previously-loaded data on screen; only surface the error.
      .catch((e: ApiError | Error) => setError(errorMessage(e)));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [customerId]);

  const setCustomer = (value: string) => {
    setParams(writeParam(params, "customer_id", value || null));
  };

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Sharing" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Sharing Analysis</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>

        <p className="banner-warn">
          Releasable stock shown here is currently covering the donor's own demand. Releasing it would create a
          shortfall for the donor. Read-only what-if — official verdicts unchanged.
        </p>

        {error && <p className="inline-error">{error}</p>}

        <div className="filters">
          <label>
            Customer
            <select value={customerId} onChange={(e) => setCustomer(e.target.value)}>
              <option value="">Select a customer…</option>
              {customers.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {!customerId && <p className="hint">Select a customer to see their uncovered/unrecoverable lines.</p>}

        {customerId && entries && (
          <table className="admin-table">
            <thead>
              <tr>
                <th>Line</th>
                <th>Official verdict</th>
                <th>Peers (releasable / would cover)</th>
              </tr>
            </thead>
            <tbody>
              {entries.length === 0 && (
                <tr>
                  <td colSpan={3}>No uncovered/unrecoverable lines for this customer.</td>
                </tr>
              )}
              {entries.map((entry) => (
                <tr key={entry.line.id}>
                  <td>
                    {entry.line.product_id} — <span className="num">{entry.line.quantity}</span> {entry.line.unit}
                  </td>
                  <td>
                    <VerdictChip verdict={entry.official_verdict} />
                  </td>
                  <td>
                    {entry.peers.length === 0 ? (
                      "—"
                    ) : (
                      <table className="admin-table nested-table">
                        <thead>
                          <tr>
                            <th>Customer</th>
                            <th>Releasable qty</th>
                            <th>Would cover</th>
                          </tr>
                        </thead>
                        <tbody>
                          {entry.peers.map((p) => (
                            <tr key={p.customer_id}>
                              <td>{p.customer_name}</td>
                              <td>
                                <span className="num">{p.releasable_qty_by_unit}</span> {entry.line.unit}
                              </td>
                              <td>{p.would_cover ? "✓" : "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
