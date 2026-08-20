import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Freshness from "../components/Freshness";
import { apiGet } from "../lib/api";

type CoverageKpi = {
  rate: number | null;
  covered_count: number;
  evaluated_count: number;
  not_evaluated_count: number;
};

type HomeKpi = {
  customer_count: number;
  well_status_counts: Record<string, number>;
  coverage: CoverageKpi;
  pending_approvals_count: number;
};

type DemandChange = {
  id: string;
  well_id: string;
  well_name: string;
  revision_no: number;
  applied_at: string;
  source: string;
  summary: string;
};

type PendingUnionRow = {
  source: "request" | "verdict";
  label: string;
  id: string;
  well_id: string;
  well_name: string;
  demand_line_id: string;
  link: string;
};

type AttentionWell = {
  well_id: string;
  well_name: string;
  customer_id: string;
  customer_name: string;
  worst_verdict: string;
  line_count: number;
};

type HomeResp = {
  kpi: HomeKpi;
  demand_changes: DemandChange[];
  pending_union: PendingUnionRow[];
  attention_wells: AttentionWell[];
};

function pct(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate * 100)}%`;
}

export default function Home() {
  const [data, setData] = useState<HomeResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  const load = () => {
    apiGet<HomeResp>("/dashboard/home")
      .then((resp) => {
        setData(resp);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  const kpi = data?.kpi;
  const wellCounts = kpi?.well_status_counts ?? {};
  const wellTotal = Object.values(wellCounts).reduce((a, b) => a + b, 0);

  return (
    <div>
      <div className="card">
        <div className="page-header">
          <h1>OCTG Supply Readiness Platform</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>
        {error && <p className="inline-error">{error}</p>}
        {!data && !error && <p>Loading…</p>}

        {kpi && (
          <div className="exec-grid">
            <div className="card">
              <h3>Customers</h3>
              <p style={{ fontSize: 28, margin: 0 }}>{kpi.customer_count}</p>
            </div>
            <div className="card">
              <h3>Wells</h3>
              <p style={{ fontSize: 28, margin: 0 }}>{wellTotal}</p>
              <p className="hint">
                {Object.entries(wellCounts)
                  .map(([status, count]) => `${status}: ${count}`)
                  .join(" · ") || "No wells yet"}
              </p>
            </div>
            <div className="card">
              <h3>Coverage rate</h3>
              <p style={{ fontSize: 28, margin: 0 }}>{pct(kpi.coverage.rate)}</p>
              <p className="hint">
                {kpi.coverage.covered_count} / {kpi.coverage.evaluated_count} evaluated
                {kpi.coverage.not_evaluated_count > 0
                  ? ` · ${kpi.coverage.not_evaluated_count} not evaluated (excluded)`
                  : ""}
              </p>
            </div>
            <div className="card">
              <h3>Pending approvals</h3>
              <p style={{ fontSize: 28, margin: 0 }}>{kpi.pending_approvals_count}</p>
            </div>
          </div>
        )}
      </div>

      {data && (
        <div className="exec-grid">
          <div className="card">
            <div className="page-header">
              <h2>Demand changes</h2>
              <Link to="/demand">See all →</Link>
            </div>
            {data.demand_changes.length === 0 && <p className="hint">No demand revisions yet.</p>}
            <table className="admin-table">
              <tbody>
                {data.demand_changes.map((c) => (
                  <tr key={c.id}>
                    <td>{new Date(c.applied_at).toLocaleString()}</td>
                    <td>
                      <Link to={`/wells/${c.well_id}`}>{c.well_name}</Link>
                    </td>
                    <td>{c.source}</td>
                    <td>{c.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="card">
            <div className="page-header">
              <h2>Pending approvals</h2>
            </div>
            {data.pending_union.length === 0 && <p className="hint">Nothing pending.</p>}
            <table className="admin-table">
              <tbody>
                {data.pending_union.map((row) => (
                  <tr key={`${row.source}-${row.id}`}>
                    <td>
                      <span className="badge">{row.source === "request" ? "Request" : "Verdict"}</span>
                    </td>
                    <td>
                      <Link to={`/wells/${row.well_id}`}>{row.well_name}</Link>
                    </td>
                    <td>{row.label}</td>
                    <td>
                      <Link to={row.link}>Open →</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="card">
            <div className="page-header">
              <h2>Coverage attention</h2>
              <Link to="/coverage">See all →</Link>
            </div>
            {data.attention_wells.length === 0 && <p className="hint">No wells need attention.</p>}
            <table className="admin-table">
              <tbody>
                {data.attention_wells.map((w) => (
                  <tr key={w.well_id}>
                    <td>
                      <Link to={`/wells/${w.well_id}`}>{w.well_name}</Link>
                    </td>
                    <td>{w.customer_name}</td>
                    <td>{w.worst_verdict}</td>
                    <td>{w.line_count} line(s)</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
