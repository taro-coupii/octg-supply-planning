import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  CustomerSummary,
  DemandLineListResponse,
  DemandLineRow,
  WellSummary,
} from "../api/client";
import ReasonText from "../components/ReasonText";
import { formatDay } from "./MrpSummary";
import { DEMAND_PROFILES, DEMAND_STATUSES } from "../lib/enums";

const STATUSES = [...DEMAND_STATUSES];
const PROFILES = [...DEMAND_PROFILES];
const COVERAGE_STATUSES = [
  "Covered",
  "CoveredViaSubstitute",
  "PendingApproval",
  "Uncovered",
  "Unrecoverable",
];
// Client-side SORTING is deliberately absent here: the list is
// server-paginated, and sorting one page of a larger result set would present
// a locally-sorted fragment as if it were the ordered whole.
const PAGE_SIZE = 25;

/**
 * The scope the coverage engine evaluates. Named here so the "Not evaluated"
 * tooltip can say exactly which lines fall outside it.
 */
const ENGINE_SCOPE_NOTE =
  "Not evaluated — this line's WELL is outside the default scope the " +
  "coverage engine evaluates (well demand status = Confirmed, on the " +
  "Primary or Contingency profile). No coverage verdict exists for it. " +
  "This is not the same as being covered.";

/**
 * Coverage cell for one demand line.
 *
 * Keyed off `evaluated`, NOT off `coverage_status`. An out-of-scope line is
 * served coverage_status = null because the engine has no verdict for it, and a
 * blank cell would read as "fine". It gets an explicit grey "Not evaluated"
 * chip instead. Serving a stale verdict here was a reported critical defect;
 * presenting an absent verdict as innocuous would reintroduce it in the UI.
 */
function LineCoverageCell({ line }: { line: DemandLineRow }) {
  if (!line.evaluated) {
    return (
      <span className="badge badge-not-evaluated" title={ENGINE_SCOPE_NOTE}>
        Not evaluated
      </span>
    );
  }
  if (!line.coverage_status) {
    // In scope but no verdict stored yet: still not a blank.
    return (
      <span
        className="badge badge-not-evaluated"
        title="This line is in scope but the engine has not produced a verdict for it yet."
      >
        No verdict yet
      </span>
    );
  }
  return (
    <span className={`badge badge-${line.coverage_status}`}>
      {line.coverage_status}
    </span>
  );
}

function MultiSelect({
  label,
  options,
  selected,
  onChange,
}: {
  label: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <fieldset className="filter-group">
      <legend>{label}</legend>
      {options.map((o) => (
        <label key={o} className="filter-check">
          <input
            type="checkbox"
            checked={selected.includes(o)}
            onChange={(e) =>
              onChange(
                e.target.checked
                  ? [...selected, o]
                  : selected.filter((v) => v !== o)
              )
            }
          />
          {o}
        </label>
      ))}
    </fieldset>
  );
}

export default function DemandList() {
  const [customers, setCustomers] = useState<CustomerSummary[]>([]);
  const [wells, setWells] = useState<WellSummary[]>([]);

  // Every filter lives in the URL (product-owner decision, 2026-08-12: all
  // seven, not just the headline ones), so any filtered view is bookmarkable
  // and shareable — same replace-style pattern as the Coverage Workspace.
  const [searchParams, setSearchParams] = useSearchParams();
  const [customerId, setCustomerId] = useState(
    searchParams.get("customer") ?? ""
  );
  const [wellId, setWellId] = useState(searchParams.get("well") ?? "");
  const [statuses, setStatuses] = useState<string[]>(
    searchParams.getAll("status")
  );
  const [profiles, setProfiles] = useState<string[]>(
    searchParams.getAll("profile")
  );
  const [coverage, setCoverage] = useState<string[]>(
    searchParams.getAll("coverage")
  );
  const [rosFrom, setRosFrom] = useState(searchParams.get("ros_from") ?? "");
  const [rosTo, setRosTo] = useState(searchParams.get("ros_to") ?? "");
  const [offset, setOffset] = useState(() => {
    const page = Number(searchParams.get("page") ?? "1");
    return Number.isFinite(page) && page > 1 ? (page - 1) * PAGE_SIZE : 0;
  });

  const [data, setData] = useState<DemandLineListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.getCustomers().then(setCustomers).catch(() => setCustomers([]));
    api.getWells().then(setWells).catch(() => setWells([]));
  }, []);

  // Mirror every filter (and the page) into the URL, replace-style, so the
  // current view is bookmarkable without flooding the history stack.
  useEffect(() => {
    const params = new URLSearchParams();
    if (customerId) params.set("customer", customerId);
    if (wellId) params.set("well", wellId);
    for (const s of statuses) params.append("status", s);
    for (const p of profiles) params.append("profile", p);
    for (const c of coverage) params.append("coverage", c);
    if (rosFrom) params.set("ros_from", rosFrom);
    if (rosTo) params.set("ros_to", rosTo);
    if (offset > 0) params.set("page", String(offset / PAGE_SIZE + 1));
    setSearchParams(params, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    customerId,
    wellId,
    statuses.join(","),
    profiles.join(","),
    coverage.join(","),
    rosFrom,
    rosTo,
    offset,
  ]);

  // Filters are server-side, and so is pagination: `total` drives the pager, we
  // never slice a client-side copy of the whole table.
  useEffect(() => {
    let live = true; // stale-response guard: fast page 2 must not lose to slow page 1
    setLoading(true);
    api
      .getDemandLines({
        customer_id: customerId || undefined,
        well_id: wellId || undefined,
        status: statuses,
        profile: profiles,
        coverage_status: coverage,
        ros_from: rosFrom || undefined,
        ros_to: rosTo || undefined,
        limit: PAGE_SIZE,
        offset,
      })
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
      })
      .catch((e) => {
        if (live) setError(String(e));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [
    customerId,
    wellId,
    statuses.join(","),
    profiles.join(","),
    coverage.join(","),
    rosFrom,
    rosTo,
    offset,
  ]);

  // Any filter change resets to page 1; otherwise an offset from a longer
  // result set silently shows an empty page.
  function resetPage<T>(setter: (v: T) => void) {
    return (value: T) => {
      setOffset(0);
      setter(value);
    };
  }

  const total = data?.total ?? 0;
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const notEvaluated = (data?.rows ?? []).filter((r) => !r.evaluated).length;
  const coverageFilterOn = coverage.length > 0;

  return (
    <div>
      <div className="scenario-head">
        <div>
          <h1>Demand</h1>
          <p className="scenario-sub">
            Every demand line across all wells. Filters and paging are applied by
            the server. &quot;Well demand status&quot; filters whole wells, not
            individual lines — every line of a well shares one status.
          </p>
        </div>
        <Link className="btn-plain" to="/demand/import">
          Import from Excel
        </Link>
      </div>

      <section className="filter-bar">
        <label className="filter-field">
          <span>Customer</span>
          <select
            value={customerId}
            onChange={(e) => resetPage(setCustomerId)(e.target.value)}
          >
            <option value="">All customers</option>
            {customers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span>Well</span>
          <select
            value={wellId}
            onChange={(e) => resetPage(setWellId)(e.target.value)}
          >
            <option value="">All wells</option>
            {wells.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span>ROS from</span>
          <input
            type="date"
            value={rosFrom}
            onChange={(e) => resetPage(setRosFrom)(e.target.value)}
          />
        </label>
        <label className="filter-field">
          <span>ROS to</span>
          <input
            type="date"
            value={rosTo}
            onChange={(e) => resetPage(setRosTo)(e.target.value)}
          />
        </label>
        <MultiSelect
          label="Well demand status"
          options={STATUSES}
          selected={statuses}
          onChange={resetPage(setStatuses)}
        />
        <MultiSelect
          label="Profile"
          options={PROFILES}
          selected={profiles}
          onChange={resetPage(setProfiles)}
        />
        <MultiSelect
          label="Coverage"
          options={COVERAGE_STATUSES}
          selected={coverage}
          onChange={resetPage(setCoverage)}
        />
        <button
          type="button"
          className="btn-plain"
          onClick={() => {
            setCustomerId("");
            setWellId("");
            setStatuses([]);
            setProfiles([]);
            setCoverage([]);
            setRosFrom("");
            setRosTo("");
            setOffset(0);
          }}
        >
          Clear filters
        </button>
      </section>

      {coverageFilterOn && (
        <p className="filter-note">
          A coverage filter can only match lines the engine evaluated, so
          out-of-scope lines (wells not Confirmed) are excluded while it is
          set.
        </p>
      )}

      {error && <p className="form-error">Failed to load demand: {error}</p>}

      <div className="list-meta">
        {loading ? (
          <span>Loading…</span>
        ) : (
          <span>
            {total.toLocaleString()} line{total === 1 ? "" : "s"} matched
            {total > 0 && (
              <>
                {" "}
                — showing {offset + 1}–{offset + (data?.returned ?? 0)}
              </>
            )}
            {notEvaluated > 0 && (
              <>
                {" "}
                · <strong>{notEvaluated}</strong> on this page not evaluated
              </>
            )}
          </span>
        )}
      </div>

      <div className="table-scroll">
        <table className="demand-table">
          <thead>
            <tr>
              <th>Well</th>
              <th>Customer</th>
              <th>Planning node</th>
              <th>Product</th>
              <th>Qty</th>
              <th>ROS</th>
              <th>Well demand status</th>
              <th>Profile</th>
              <th>Rev</th>
              <th>Coverage</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {(data?.rows ?? []).length === 0 && !loading && (
              <tr>
                <td colSpan={11} className="empty">
                  No demand lines match these filters.
                </td>
              </tr>
            )}
            {(data?.rows ?? []).map((r) => (
              <tr key={r.id} className={r.evaluated ? undefined : "row-unscoped"}>
                <td>
                  <Link to={`/wells/${r.well_id}`}>{r.well_name}</Link>
                </td>
                <td>{r.customer_name ?? "—"}</td>
                <td className="node-path">{r.planning_node_path}</td>
                <td>
                  {/* Straight to the product's By Item analysis. */}
                  <Link to={`/mrp/by-item/${r.product_id}`}>
                    {r.product_description ?? r.product_id}
                  </Link>
                </td>
                <td className="num">
                  {r.quantity.toLocaleString()} {r.unit_of_measure}
                </td>
                <td className="num">{formatDay(r.ros_date)}</td>
                <td>{r.status}</td>
                <td>{r.profile}</td>
                <td className="num">{r.current_revision_no}</td>
                <td>
                  <LineCoverageCell line={r} />
                </td>
                <td className="mrp-reason">
                  {r.evaluated ? (
                    <ReasonText reason={r.coverage_reason} />
                  ) : (
                    "Outside the engine's default scope: well status Confirmed, Primary/Contingency."
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="pager">
        <button
          type="button"
          className="btn-plain"
          disabled={offset === 0 || loading}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          Previous
        </button>
        <span className="pager-label">
          Page {page} of {pageCount}
        </span>
        <button
          type="button"
          className="btn-plain"
          disabled={offset + PAGE_SIZE >= total || loading}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          Next
        </button>
      </div>
    </div>
  );
}
