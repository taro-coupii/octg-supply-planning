import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, BusinessUnitOut, SurplusReport, SurplusRow } from "../api/client";
import LoadError from "../components/LoadError";
import Freshness from "../components/Freshness";
import ScopeChecks from "../components/ScopeChecks";
import { DEFAULT_PROFILE_SCOPE, DEFAULT_STATUS_SCOPE } from "../lib/enums";

/**
 * Surplus List — on-hand decomposed into allocated / surplus / obsolete per
 * (BU, product), quantity-only. Same calm-grid language as Order
 * Requirements: one summary sentence, rows with a stacked split bar, detail
 * numbers on the row itself.
 */

function fmtQty(v: number) {
  return v.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

function mt(v: number) {
  return `${v.toLocaleString(undefined, { maximumFractionDigits: 1 })} t`;
}

function StatusChip({ row }: { row: SurplusRow }) {
  if (row.obsolete > 0) {
    return <span className="mor-chip mor-chip-unknown">OBSOLETE</span>;
  }
  if (row.surplus > 0) {
    return <span className="mor-chip mor-chip-due">SURPLUS</span>;
  }
  return <span className="mor-chip mor-chip-ok">FULLY ALLOCATED</span>;
}

function SplitBar({ row }: { row: SurplusRow }) {
  if (row.on_hand <= 0) return null;
  const pct = (v: number) => `${(v / row.on_hand) * 100}%`;
  return (
    <div
      className="surplus-bar"
      role="img"
      aria-label="Allocated vs surplus vs obsolete"
      title={
        `Allocated: ${fmtQty(row.allocated)} ${row.unit_of_measure}\n` +
        `Surplus: ${fmtQty(row.surplus)} ${row.unit_of_measure}\n` +
        `Obsolete: ${fmtQty(row.obsolete)} ${row.unit_of_measure}`
      }
    >
      {row.allocated > 0 && (
        <span className="surplus-seg surplus-seg-allocated" style={{ width: pct(row.allocated) }} />
      )}
      {row.surplus > 0 && (
        <span className="surplus-seg surplus-seg-surplus" style={{ width: pct(row.surplus) }} />
      )}
      {row.obsolete > 0 && (
        <span className="surplus-seg surplus-seg-obsolete" style={{ width: pct(row.obsolete) }} />
      )}
    </div>
  );
}

export default function SurplusList() {
  const [report, setReport] = useState<SurplusReport | null>(null);
  const [bus, setBus] = useState<BusinessUnitOut[]>([]);
  // BU filter lives in the URL so a filtered list is a shareable link.
  const [searchParams, setSearchParams] = useSearchParams();
  const buId = searchParams.get("bu") ?? "";
  const setBuId = (v: string) =>
    setSearchParams(
      (p) => {
        v ? p.set("bu", v) : p.delete("bu");
        return p;
      },
      { replace: true }
    );
  // Demand scope. null = untouched: no params sent, the server's EFFECTIVE
  // default applies, and the checkboxes display the response's scope --
  // an admin-persisted default renders correctly instead of the shipped
  // constants. Non-default = read-only recompute (scope_is_default).
  const [statuses, setStatuses] = useState<string[] | null>(null);
  const [profiles, setProfiles] = useState<string[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api.getBusinessUnits().then(setBus).catch(() => {});
  }, []);

  useEffect(() => {
    let live = true;
    // A scope click triggers a full per-customer recompute server-side, so
    // the toggles are debounced (same rationale and delay as the Coverage
    // workspace); untouched scope fires immediately.
    const touched = statuses !== null || profiles !== null;
    const t = window.setTimeout(
      () => {
        api
          .getSurplusReport(
            buId || undefined,
            statuses ?? undefined,
            profiles ?? undefined
          )
          .then((r) => {
            if (!live) return;
            setReport(r);
            setError(null);
            setFetchedAt(new Date());
          })
          .catch((e) => {
            if (live) setError(e);
          });
      },
      touched ? 450 : 0
    );
    return () => {
      live = false;
      window.clearTimeout(t);
    };
  }, [buId, statuses, profiles, reload]);


  // Sort lives in the URL like the BU filter above (QA 2026-08-14: bookmarks
  // reproduced the filters but lost the sort). No param = server order.
  const sortBy = searchParams.get("sort") ?? "server";
  const setSortBy = (v: string) =>
    setSearchParams(
      (p) => {
        v === "server" ? p.delete("sort") : p.set("sort", v);
        return p;
      },
      { replace: true }
    );
  const idle = report
    ? report.rows.filter((r) => r.surplus > 0 || r.obsolete > 0)
    : [];
  const sortedRows = report
    ? sortBy === "server"
      ? report.rows
      : [...report.rows].sort((a, b) => {
          if (sortBy === "product")
            return (a.product_description ?? a.product_id).localeCompare(
              b.product_description ?? b.product_id
            );
          const key = sortBy as "on_hand" | "surplus" | "obsolete";
          return b[key] - a[key]; // quantities: largest first
        })
    : [];

  return (
    <div className="mor-page">
      <header className="mor-head">
        <div>
          <h2>Surplus List</h2>
          {report && (
            <p className="mor-summary">
              {idle.length === 0 ? (
                "Every on-hand quantity is allocated to demand inside the horizon."
              ) : (
                <>
                  <strong>{idle.length}</strong> of {report.rows.length} positions hold
                  idle steel
                  {report.surplus_tonnes.available &&
                    report.obsolete_tonnes.available && (
                      <>
                        {" "}
                        — surplus <strong>{mt(report.surplus_tonnes.value ?? 0)}</strong>,
                        obsolete{" "}
                        <strong className="mor-neg">
                          {mt(report.obsolete_tonnes.value ?? 0)}
                        </strong>{" "}
                        <span className="exec-iu-converted">(converted to MT)</span>
                      </>
                    )}
                  .
                </>
              )}
            </p>
          )}
        </div>
        <div className="mor-controls">
          <Freshness
            fetchedAt={fetchedAt}
            onRefresh={() => setReload((r) => r + 1)}
          />
          <ScopeChecks
            statuses={statuses ?? report?.status_scope ?? DEFAULT_STATUS_SCOPE}
            profiles={profiles ?? report?.profile_scope ?? DEFAULT_PROFILE_SCOPE}
            onStatuses={setStatuses}
            onProfiles={setProfiles}
          />
          <select aria-label="Business Unit" value={buId} onChange={(e) => setBuId(e.target.value)}>
            <option value="">All business units</option>
            {bus.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
          <select
            aria-label="Sort"
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
          >
            <option value="server">Sort: idle steel first</option>
            <option value="surplus">Sort: surplus (largest)</option>
            <option value="obsolete">Sort: obsolete (largest)</option>
            <option value="on_hand">Sort: on hand (largest)</option>
            <option value="product">Sort: product name</option>
          </select>
        </div>
      </header>

      {report && report.skipped_customers.length > 0 && (
        <p className="exec-scope-projection" role="status">
          {report.skipped_customers.join(", ")} could not be recomputed for
          this scope (no Business Unit or missing inventory rows) — their
          demand keeps the stored default-scope verdicts.
        </p>
      )}

      {report && !report.scope_is_default && (
        <p className="exec-scope-projection" role="status">
          Recomputed read-only for status [{report.status_scope.join(", ")}] /
          profile [{report.profile_scope.join(", ")}] — allocation counts
          demand under this scope, not the official stored verdicts. Set the
          checkboxes back to Confirmed / Primary + Contingency to return to
          them.
        </p>
      )}

      {error != null ? (
        <LoadError what="the surplus report" error={error} />
      ) : null}

      {report && error == null && (
        <>
          <div className="surplus-legend">
            <span><i className="surplus-dot surplus-seg-allocated" /> Allocated</span>
            <span><i className="surplus-dot surplus-seg-surplus" /> Surplus (demand exists, stock exceeds it)</span>
            <span><i className="surplus-dot surplus-seg-obsolete" /> Obsolete (no demand in {report.horizon_months} months)</span>
          </div>
          <div className="mor-axis surplus-axis" aria-hidden="true">
            <span>Product / business unit</span>
            <span>Allocation of on-hand stock</span>
            <span className="mor-axis-figures">On hand / status</span>
          </div>
          <div className="mor-rows">
            {sortedRows.map((r) => (
              <div key={`${r.business_unit_id}-${r.product_id}`} className="mor-row">
                <div className="mor-row-head surplus-row-head">
                  <div className="mor-row-id">
                    <Link to={`/mrp/by-item/${r.product_id}`} className="mor-product">
                      {r.product_description ?? r.product_id}
                    </Link>
                    <Link to="/mrp/order-requirements" className="surplus-mor-link">
                      order requirements
                    </Link>
                    <span className="mor-meta">{r.business_unit_name}</span>
                  </div>
                  <div className="surplus-mid">
                    <SplitBar row={r} />
                    <span className="surplus-figures num">
                      <span className={"surplus-fig" + (r.allocated <= 0 ? " surplus-fig-zero" : "")}>
                        <i className="surplus-dot surplus-seg-allocated" />
                        Allocated {fmtQty(r.allocated)}
                      </span>
                      <span className={"surplus-fig" + (r.surplus <= 0 ? " surplus-fig-zero" : "")}>
                        <i className="surplus-dot surplus-seg-surplus" />
                        Surplus {fmtQty(r.surplus)}
                      </span>
                      <span className={"surplus-fig" + (r.obsolete <= 0 ? " surplus-fig-zero" : "")}>
                        <i className="surplus-dot surplus-seg-obsolete" />
                        Obsolete {fmtQty(r.obsolete)}
                      </span>
                      <span className="mor-unit">{r.unit_of_measure}</span>
                      {r.demand_overdue > 0 && (
                        <span
                          className="surplus-fig surplus-overdue"
                          title={
                            "Part of this product's demand has a ROS month that has " +
                            "already passed. It still counts as demand (lateness " +
                            "does not cancel it) and is labelled here rather than " +
                            "silently blended in."
                          }
                        >
                          incl. {fmtQty(r.demand_overdue)} overdue demand
                        </span>
                      )}
                    </span>
                  </div>
                  <div className="mor-row-figures">
                    <span className="mor-req num">
                      {fmtQty(r.on_hand)} <span className="mor-unit">{r.unit_of_measure}</span>
                    </span>
                    <StatusChip row={r} />
                  </div>
                </div>
              </div>
            ))}
            {report.rows.length === 0 && (
              <p className="empty">No on-hand rows in this scope.</p>
            )}
          </div>
          <footer className="mor-notes">
            <p>{report.note}</p>
            {report.unknown_position_count > 0 && (
              <p>
                {report.unknown_position_count} demanded product(s) have no on-hand
                row anywhere — their position is unknown, not zero, so they cannot
                appear above.
              </p>
            )}
          </footer>
        </>
      )}
    </div>
  );
}
