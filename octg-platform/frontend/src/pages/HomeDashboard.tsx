import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import ConfirmButton from "../components/ConfirmButton";
import {
  api,
  CoverageGridResponse,
  HomeDashboardData,
} from "../api/client";
import CoverageBadge from "../components/CoverageBadge";
import Freshness from "../components/Freshness";
import { formatDay } from "./MrpSummary";

/**
 * Home — morning triage, per the Industry design handoff (screen 3a):
 * KPI strip (wells in scope / uncovered / in-scope lines covered / awaiting
 * approval), then three columns: demand changes, uncovered wells, pending
 * approvals as blueprint cards with the EXISTING Approve/Decline operation
 * relocated onto them. Functionality unchanged from the live app — the
 * handoff's own rule.
 */

const UNCOVERED_SHOWN = 7;

function Corners() {
  return (
    <>
      <i className="corner tl" />
      <i className="corner tr" />
      <i className="corner bl" />
      <i className="corner br" />
    </>
  );
}

export default function HomeDashboard() {
  const [data, setData] = useState<HomeDashboardData | null>(null);
  const [grid, setGrid] = useState<CoverageGridResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [decided, setDecided] = useState<Set<string>>(new Set());

  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const load = () => {
    api
      .getHomeDashboard()
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
      })
      .catch((e) => setError(String(e)));
    // The same stored verdicts the Coverage screen reads; a failure here only
    // blanks the KPI strip, never the queues.
    api.getCoverageGrid({}).then(setGrid).catch(() => setGrid(null));
  };
  useEffect(load, []);

  const kpi = useMemo(() => {
    if (!grid) return null;
    const rows = grid.rows;
    const inScope = rows.reduce((s, r) => s + r.in_scope_line_count, 0);
    const covered = rows.reduce((s, r) => s + r.covered_line_count, 0);
    return {
      wells: rows.length,
      uncovered: rows.filter((r) => r.coverage_status === "Uncovered").length,
      covered,
      inScope,
    };
  }, [grid]);

  async function decide(approvalId: string, approved: boolean) {
    // Optimistic removal per the handoff; the queue and KPI refresh after.
    setDecided((d) => new Set(d).add(approvalId));
    try {
      await api.decideSubstitutionApproval(approvalId, approved);
    } finally {
      load();
    }
  }

  if (error) return <p>Failed to load dashboard: {error}</p>;
  if (!data) return <p>Loading...</p>;

  const pending = data.pending_approvals.filter(
    (p) => !(p.approval_id && decided.has(p.approval_id))
  );
  const uncoveredShown = data.uncovered_wells.slice(0, UNCOVERED_SHOWN);

  return (
    <div className="home-industry">
      <header className="home-head">
        <div>
          <h1>Home</h1>
          <p className="home-sub">
            What changed. What&apos;s uncovered. What needs action.
          </p>
        </div>
        <Freshness fetchedAt={fetchedAt} onRefresh={load} />
      </header>

      <div className="home-kpis">
        <div className="home-kpi">
          <span className="home-kpi-label">Wells in scope</span>
          <span className="home-kpi-figure num">{kpi ? kpi.wells : "—"}</span>
        </div>
        <div className="home-kpi">
          <span className="home-kpi-label">Uncovered</span>
          <span className="home-kpi-figure num home-kpi-heavy">
            {kpi ? kpi.uncovered : "—"}
          </span>
        </div>
        <div className="home-kpi">
          <span className="home-kpi-label">In-scope lines covered</span>
          <span className="home-kpi-figure num">
            {kpi ? (
              <>
                {kpi.covered}
                <span className="home-kpi-denominator"> / {kpi.inScope}</span>
              </>
            ) : (
              "—"
            )}
          </span>
        </div>
        <div className="home-kpi">
          <span className="home-kpi-label">Awaiting approval</span>
          <span className="home-kpi-figure num">
            {data.pending_approvals.length}
          </span>
        </div>
      </div>

      <div className="home-cols">
        <section className="home-col">
          <h6 className="home-col-title">Demand changes</h6>
          {data.demand_changes.length === 0 ? (
            <div className="home-empty">No recent demand changes</div>
          ) : (
            <div className="home-list">
              {data.demand_changes.map((c) => (
                // The change names a well; the natural next question is
                // "what does that well look like now" -- so the row goes there.
                <Link
                  key={c.id}
                  to={`/wells/${c.well_id}`}
                  className="home-row home-row-link"
                >
                  <div className="home-row-main">
                    <span className="home-row-name">
                      {c.product_description ?? c.demand_line_id}
                    </span>
                    {(c.well_name || c.customer_name) && (
                      <span className="home-row-sub">
                        {c.well_name}
                        {c.well_name && c.customer_name ? " · " : ""}
                        {c.customer_name}
                      </span>
                    )}
                    <span className="home-row-sub num">
                      Qty {c.quantity_before ?? "—"} → {c.quantity_after ?? "—"}{" "}
                      {c.unit_of_measure ?? ""} · Coverage{" "}
                      {c.coverage_before ?? "—"} → {c.coverage_after ?? "—"}
                    </span>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </section>

        <section className="home-col home-col-wide">
          <h6 className="home-col-title">Uncovered wells</h6>
          {data.uncovered_wells.length === 0 ? (
            <div className="home-empty">All wells covered</div>
          ) : (
            <>
              <div className="home-list">
                {uncoveredShown.map((w) => (
                  <Link key={w.id} to={`/wells/${w.id}`} className="home-row home-row-link">
                    <div className="home-row-main">
                      <span className="home-row-name">
                        {w.name}
                        {w.customer_name && (
                          <span className="home-row-customer">
                            {" "}· {w.customer_name}
                          </span>
                        )}
                      </span>
                      <span className="home-row-sub num">
                        Earliest ROS{" "}
                        {w.earliest_ros_date ? formatDay(w.earliest_ros_date) : "—"}
                        {" · "}first runout{" "}
                        {w.first_runout_date
                          ? formatDay(w.first_runout_date)
                          : "no shortage"}
                      </span>
                    </div>
                    <CoverageBadge status={w.coverage_status} />
                  </Link>
                ))}
              </div>
              <span className="home-col-foot">
                Showing {uncoveredShown.length} of {data.uncovered_wells.length}
                {data.uncovered_wells.length > uncoveredShown.length && (
                  <>
                    {" · "}
                    <Link to="/coverage?rollup=Uncovered">
                      see all on the coverage grid
                    </Link>
                  </>
                )}
              </span>
              <span className="home-col-foot">
                <Link to="/analysis/sharing">
                  Could any be covered by sharing within the Business Unit?
                </Link>{" "}
                Read-only what-if; changes nothing.
              </span>
            </>
          )}
        </section>

        <section className="home-col">
          <h6 className="home-col-title">
            Pending approvals
            {" · "}
            <Link to="/approvals" className="home-col-title-link">
              full queue
            </Link>
          </h6>
          {pending.length === 0 ? (
            <div className="home-empty">No substitutions awaiting approval</div>
          ) : (
            <div className="home-cards">
              {pending.map((p) => (
                <div key={p.demand_line_id} className="blueprint home-approval">
                  <Corners />
                  <div className="home-approval-head">
                    <Link
                      to={`/demand-lines/${p.demand_line_id}/substitution`}
                      className="home-row-name"
                    >
                      {p.well_name}
                      {p.customer_name && (
                        <span className="home-row-customer">
                          {" "}· {p.customer_name}
                        </span>
                      )}
                    </Link>
                    <span className="badge badge-PendingApproval">Pending</span>
                  </div>
                  <p className="home-approval-product num">
                    {p.product_description} · {p.quantity.toLocaleString()}{" "}
                    {p.unit_of_measure} · ROS {formatDay(p.ros_date)}
                    {p.substitute_description && (
                      <>
                        <br />
                        substitute: {p.substitute_description}
                      </>
                    )}
                  </p>
                  {p.approval_id ? (
                    <div className="home-approval-actions">
                      {/* Two-step: a decision is FINAL (the API 409s a
                          re-decision), so one stray click must not commit it. */}
                      <ConfirmButton
                        className="home-btn-secondary"
                        label="Decline"
                        confirmLabel="Confirm decline?"
                        onConfirm={() => decide(p.approval_id!, false)}
                      />
                      <ConfirmButton
                        className="home-btn-primary blueprint"
                        label="Approve"
                        confirmLabel="Confirm approve?"
                        onConfirm={() => decide(p.approval_id!, true)}
                      />
                    </div>
                  ) : (
                    <span className="home-row-sub">
                      Decide on the substitution screen — no open request yet.
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
