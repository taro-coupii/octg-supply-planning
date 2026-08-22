import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import VerdictChip from "../components/VerdictChip";
import { VERDICTS, verdictText } from "../lib/verdict";
import CoverageBand from "../components/CoverageBand";
import { formatStampRange } from "../lib/datetime";
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
// A row's leading paint band carries its worst verdict. Red is reserved for
// Unrecoverable (steel physically absent); amber covers the states that are
// waiting on a human. The two are never merged (spec §3).
function bandClass(worst: string | null): string {
  switch (worst) {
    case "Unrecoverable":
      return "band-bad";
    case "Uncovered":
    case "PendingApproval":
      return "band-warn";
    case "Covered":
    case "CoveredViaSubstitute":
      return "band-ok";
    default:
      return "band-neutral";
  }
}

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

        <div className="filters">
          <label>
            Customer
            <select value={customerFilter} onChange={(e) => setFilter("customer", e.target.value)}>
              <option value="">All customers</option>
              {(data?.customers ?? []).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Worst verdict
            <select value={worstVerdictFilter} onChange={(e) => setFilter("worst_verdict", e.target.value)}>
              <option value="">All verdicts</option>
              {ROLLUP_KEYS.map((v) => (
                <option key={v} value={v}>
                  {verdictText(v)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Search
            <input
              placeholder="Search well or customer"
              value={search}
              onChange={(e) => setFilter("q", e.target.value)}
            />
          </label>
          <button type="button" disabled={busy} onClick={recompute}>
            {busy ? "Recomputing…" : "Recompute"}
          </button>
        </div>

        <p className="hint">
          Computed at:{" "}
          <span className="num">
            {data?.computed_at_min && data?.computed_at_max
              ? formatStampRange(data.computed_at_min, data.computed_at_max)
              : "—"}
          </span>
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
                <tr key={w.id} className={bandClass(w.worst_verdict)}>
                  <td>{r.customer.name}</td>
                  <td>
                    <Link to={`/wells/${w.id}`}>{w.name}</Link>
                  </td>
                  <td>{w.status}</td>
                  <td className="num">{w.line_count}</td>
                  <td>
                    <CoverageBand rollup={w.verdict_rollup} />
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
