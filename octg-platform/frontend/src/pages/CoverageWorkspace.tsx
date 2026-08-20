import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import VerdictChip from "../components/VerdictChip";
import { VERDICTS } from "../lib/verdict";
import { ApiError, apiGet, apiSend } from "../lib/api";
import { writeParam } from "../lib/urlState";
import { errorMessage } from "./admin/shared";

type WellRollup = {
  id: string;
  name: string;
  status: string;
  line_count: number;
  verdict_rollup: Record<string, number>;
  worst_verdict: string | null;
};

type CustomerRollup = {
  id: string;
  name: string;
  policy: string;
  wells: WellRollup[];
};

type SkippedCustomer = { name: string; reason: string };

type CoverageGrid = {
  customers: CustomerRollup[];
  skipped_customers: SkippedCustomer[];
  computed_at_min: string | null;
  computed_at_max: string | null;
};

type RecomputeResult = { computed: number; skipped_customers: SkippedCustomer[] };

// NotEvaluated is a distinct rollup bucket (never merged with 0 or Covered).
const ROLLUP_KEYS = [...VERDICTS, "NotEvaluated"] as const;

export default function CoverageWorkspace() {
  const [params, setParams] = useSearchParams();
  const customerFilter = params.get("customer") ?? "";
  const worstVerdictFilter = params.get("worst_verdict") ?? "";
  const search = params.get("q") ?? "";

  const [data, setData] = useState<CoverageGrid | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [busy, setBusy] = useState(false);
  const [recomputeResult, setRecomputeResult] = useState<RecomputeResult | null>(null);

  const load = () => {
    apiGet<CoverageGrid>("/coverage")
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  const setFilter = (key: string, value: string) => {
    setParams(writeParam(params, key, value || null));
  };

  const recompute = () => {
    setBusy(true);
    setRecomputeResult(null);
    apiSend<RecomputeResult>("POST", "/coverage/recompute", {})
      .then((r) => {
        setRecomputeResult(r);
        load();
      })
      .catch((e: ApiError | Error) => setError(errorMessage(e)))
      .finally(() => setBusy(false));
  };

  const q = search.trim().toLowerCase();
  const rows = (data?.customers ?? [])
    .filter((c) => !customerFilter || c.id === customerFilter)
    .map((c) => ({
      customer: c,
      wells: c.wells.filter((w) => {
        if (worstVerdictFilter && (w.worst_verdict ?? "NotEvaluated") !== worstVerdictFilter) return false;
        if (q && !w.name.toLowerCase().includes(q) && !c.name.toLowerCase().includes(q)) return false;
        return true;
      }),
    }))
    .filter((r) => r.wells.length > 0 || (!worstVerdictFilter && !q));

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Coverage" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Coverage</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>
        {error && <p className="inline-error">{error}</p>}

        {data && data.skipped_customers.length > 0 && (
          <p className="banner-warn">
            Skipped during last recompute: {data.skipped_customers.map((s) => `${s.name} (${s.reason})`).join(", ")}
          </p>
        )}
        {recomputeResult && recomputeResult.skipped_customers.length > 0 && (
          <p className="banner-warn">
            Recompute skipped:{" "}
            {recomputeResult.skipped_customers.map((s) => `${s.name} (${s.reason})`).join(", ")}
          </p>
        )}

        <fieldset>
          <legend>Filters</legend>
          <div className="admin-add-row">
            <select value={customerFilter} onChange={(e) => setFilter("customer", e.target.value)}>
              <option value="">All customers</option>
              {(data?.customers ?? []).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            <select value={worstVerdictFilter} onChange={(e) => setFilter("worst_verdict", e.target.value)}>
              <option value="">All verdicts</option>
              {ROLLUP_KEYS.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
            <input
              placeholder="Search well or customer"
              value={search}
              onChange={(e) => setFilter("q", e.target.value)}
            />
            <button type="button" disabled={busy} onClick={recompute}>
              {busy ? "Recomputing…" : "Recompute"}
            </button>
          </div>
        </fieldset>

        <p className="hint">
          Computed at:{" "}
          {data?.computed_at_min && data?.computed_at_max
            ? data.computed_at_min === data.computed_at_max
              ? data.computed_at_min
              : `${data.computed_at_min} – ${data.computed_at_max}`
            : "—"}
        </p>

        <div className="table-scroll">
        <table className="admin-table">
          <thead>
            <tr>
              <th>Customer</th>
              <th>Well</th>
              <th>Status</th>
              <th>Lines</th>
              <th>Rollup</th>
              <th>Worst verdict</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={6}>—</td>
              </tr>
            )}
            {rows.map((r) =>
              r.wells.map((w) => (
                <tr key={w.id}>
                  <td>{r.customer.name}</td>
                  <td>
                    <Link to={`/wells/${w.id}`}>{w.name}</Link>
                  </td>
                  <td>{w.status}</td>
                  <td>{w.line_count}</td>
                  <td>
                    {ROLLUP_KEYS.filter((k) => (w.verdict_rollup[k] ?? 0) > 0)
                      .map((k) => `${k}: ${w.verdict_rollup[k]}`)
                      .join(", ") || "—"}
                  </td>
                  <td>
                    <VerdictChip verdict={w.worst_verdict} />
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  );
}
