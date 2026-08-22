import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  CoverageGridResponse,
  CoverageGridRow,
  CustomerSummary,
} from "../api/client";
import LoadError from "../components/LoadError";
import { formatDay } from "./MrpSummary";
import { sortIndicator, useSortable } from "../lib/sort";
import Freshness from "../components/Freshness";

import { DEMAND_PROFILES, DEMAND_STATUSES } from "../lib/enums";

const STATUSES = [...DEMAND_STATUSES];
const PROFILES = [...DEMAND_PROFILES];

/** Well-level rollup vocabulary. Distinct from the per-line CoverageStatus. */
const ROLLUPS = ["Covered", "Uncovered", "Unevaluated"];

/**
 * How long the status/profile toggles sit still before the request goes out.
 *
 * Non-default status/profile filters make the backend recompute coverage for
 * EVERY customer from scratch — the filters change which demand lines compete
 * for the same inventory, so stored rows cannot be reused. Firing one of those
 * per checkbox click would mean a full recompute per click. 450ms is long enough
 * that ticking three boxes in a row is one recompute, short enough that a single
 * deliberate click still feels immediate.
 */
const DEBOUNCE_MS = 450;

const NO_SCOPE_NOTE =
  "No demand in scope — this well has no demand lines inside the filters " +
  "being evaluated, so the coverage engine has no verdict for it. A well with " +
  "no in-scope demand is neither covered nor uncovered.";

/**
 * The well's rollup cell.
 *
 * Keyed off `evaluated` FIRST. `evaluated: false` means in_scope_line_count is
 * 0 — there is nothing to cover — and it is a third state, not a pale green and
 * not a red. Rendering it as either would claim a verdict the engine never
 * produced.
 */
function RollupCell({ row }: { row: CoverageGridRow }) {
  if (!row.evaluated) {
    return (
      <span className="badge badge-no-scope" title={NO_SCOPE_NOTE}>
        No demand in scope
      </span>
    );
  }
  if (!row.coverage_status) {
    return (
      <span
        className="badge badge-not-evaluated"
        title="This well has in-scope demand but the engine has not produced a rollup for it yet."
      >
        No verdict yet
      </span>
    );
  }
  return (
    <span className={`badge badge-${row.coverage_status}`}>
      {row.coverage_status}
    </span>
  );
}

function MultiSelect({
  label,
  options,
  selected,
  onChange,
  note,
}: {
  label: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  note?: string;
}) {
  return (
    <fieldset className="filter-group">
      <legend title={note}>{label}</legend>
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

interface Query {
  customerId: string;
  rollup: string;
  statuses: string[];
  profiles: string[];
}

const EMPTY: Query = { customerId: "", rollup: "", statuses: [], profiles: [] };

/** The filters as URL search params, so a filtered view is a shareable link. */
function queryFromParams(params: URLSearchParams): Query {
  return {
    customerId: params.get("customer") ?? "",
    rollup: params.get("rollup") ?? "",
    statuses: params.getAll("status"),
    profiles: params.getAll("profile"),
  };
}

function paramsFromQuery(q: Query, search: string): URLSearchParams {
  const params = new URLSearchParams();
  if (q.customerId) params.set("customer", q.customerId);
  if (q.rollup) params.set("rollup", q.rollup);
  for (const s of q.statuses) params.append("status", s);
  for (const p of q.profiles) params.append("profile", p);
  if (search) params.set("q", search);
  return params;
}

export default function CoverageWorkspace() {
  const [customers, setCustomers] = useState<CustomerSummary[]>([]);
  const [searchParams, setSearchParams] = useSearchParams();

  // Two layers on purpose. `draft` is what the checkboxes show and updates on
  // every click; `applied` is what has actually been requested and only catches
  // up after the toggles stop moving. The fetch effect depends on `applied`
  // alone, so a burst of clicks produces exactly one recompute.
  const initial = queryFromParams(searchParams);
  const [draft, setDraft] = useState<Query>(initial);
  const [applied, setApplied] = useState<Query>(initial);
  // Instant, client-side text search over the loaded rows (well / customer /
  // planning node). Never debounced -- it hits no server.
  const [search, setSearch] = useState(searchParams.get("q") ?? "");
  // Once the user has touched a status/profile checkbox, stop overwriting the
  // draft from the server's defaults — only the FIRST load should drive them.
  const [userTouchedFilters, setUserTouchedFilters] = useState(false);

  const [data, setData] = useState<CoverageGridResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api.getCustomers().then(setCustomers).catch(() => setCustomers([]));
  }, []);

  const draftKey = JSON.stringify(draft);
  const appliedKey = JSON.stringify(applied);
  const settling = draftKey !== appliedKey;

  useEffect(() => {
    if (draftKey === appliedKey) return;
    const t = window.setTimeout(() => setApplied(draft), DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [draftKey, appliedKey]);

  // Mirror the applied filters (and search text) into the URL, replace-style,
  // so the current view is bookmarkable without flooding the history stack.
  useEffect(() => {
    setSearchParams(paramsFromQuery(applied, search), { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appliedKey, search]);

  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .getCoverageGrid({
        customer_id: applied.customerId || undefined,
        coverage_status: applied.rollup || undefined,
        status: applied.statuses,
        profile: applied.profiles,
      })
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
        setFetchedAt(new Date());
        // Drive the checkboxes from the server's actual defaults rather than
        // restating them locally — on the very first (untouched) load only.
        if (!userTouchedFilters && d.filters.are_default) {
          setDraft((prev) => ({ ...prev, statuses: d.filters.status, profiles: d.filters.profile }));
          setApplied((prev) => ({ ...prev, statuses: d.filters.status, profiles: d.filters.profile }));
        }
      })
      .catch((e) => {
        if (!live) return;
        // Misconfigured customers no longer fail the endpoint — the recompute
        // isolates them per customer and names them in
        // `filters.skipped_customers` (rendered as a banner below). What
        // still lands here is a genuine request failure, so LoadError.
        setError(e);
        setData(null);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [appliedKey, reload]);

  const allRows = data?.rows ?? [];
  const needle = search.trim().toLowerCase();
  const rows = needle
    ? allRows.filter(
        (r) =>
          r.well_name.toLowerCase().includes(needle) ||
          (r.customer_name ?? "").toLowerCase().includes(needle) ||
          (r.planning_node_path ?? "").toLowerCase().includes(needle)
      )
    : allRows;
  const projection = data?.filters.recomputed_read_only === true;

  const { sorted, sort, toggle } = useSortable(rows, {
    well: (r) => r.well_name,
    node: (r) => r.planning_node_path,
    customer: (r) => r.customer_name,
    status: (r) => r.demand_status,
    ros: (r) => r.earliest_ros_date,
    runout: (r) => r.first_runout_date,
    covered: (r) =>
      r.in_scope_line_count === 0
        ? null
        : r.covered_line_count / r.in_scope_line_count,
    rollup: (r) => r.coverage_status,
  }, "sort");

  const counts = {
    Covered: rows.filter((r) => r.evaluated && r.coverage_status === "Covered")
      .length,
    Uncovered: rows.filter(
      (r) => r.evaluated && r.coverage_status === "Uncovered"
    ).length,
    NoScope: rows.filter((r) => !r.evaluated).length,
  };
  const inScopeLines = rows.reduce((a, r) => a + r.in_scope_line_count, 0);
  const coveredLines = rows.reduce((a, r) => a + r.covered_line_count, 0);

  return (
    <div>
      <div className="scenario-head">
        <div>
          <h1>Coverage</h1>
          <p className="scenario-sub">
            Every well's coverage rollup. Filtering is done by the server.
          </p>
        </div>
        <Link className="btn-plain" to="/demand">
          Demand lines
        </Link>
      </div>

      <section className="filter-bar">
        <label className="filter-field">
          <span>Search</span>
          <input
            type="search"
            placeholder="Well, customer, node…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <label className="filter-field">
          <span>Customer</span>
          <select
            value={draft.customerId}
            onChange={(e) =>
              setDraft({ ...draft, customerId: e.target.value })
            }
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
          <span>Rollup</span>
          <select
            value={draft.rollup}
            onChange={(e) => setDraft({ ...draft, rollup: e.target.value })}
          >
            <option value="">All rollups</option>
            {ROLLUPS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        <MultiSelect
          label="Well demand status"
          note="Filters whole WELLS in/out — a well's status is one value for every line of it, so this never splits a well's own lines across the filter."
          options={STATUSES}
          selected={draft.statuses}
          onChange={(statuses) => {
            setUserTouchedFilters(true);
            setDraft({ ...draft, statuses });
          }}
        />
        <MultiSelect
          label="Profile"
          note="Profile still varies per line within a well."
          options={PROFILES}
          selected={draft.profiles}
          onChange={(profiles) => {
            setUserTouchedFilters(true);
            setDraft({ ...draft, profiles });
          }}
        />
        <button
          type="button"
          className="btn-plain"
          onClick={() => {
            setUserTouchedFilters(false);
            setDraft(EMPTY);
            setSearch("");
          }}
        >
          Clear filters
        </button>
      </section>

      <p className="filter-note">
        The demand-status filter selects whole wells, not individual lines
        within them — a well's status is one value for every line of it (see
        Well Workspace). The platform default is <strong>Confirmed only</strong>{" "}
        for status and <strong>Primary + Contingency</strong> for profile.
      </p>

      <p className="filter-note">
        Changing demand status or profile makes the server recompute coverage for
        every customer, so these toggles are debounced by {DEBOUNCE_MS}ms — tick
        several boxes and only one recompute is requested.
        {settling && <strong> Waiting for the toggles to settle…</strong>}
      </p>

      {/* Persistent while the projection is on screen: these numbers are a
          read-only recompute, never the stored verdict. Same visual vocabulary
          as the Scenario what-if banner, deliberately not a second one. */}
      {projection && data && (
        <div className="whatif-banner">
          <span className="whatif-tag">Projection — not saved</span>
          <p>{data.filters.explanation}</p>
          <p className="whatif-note">
            Recomputed for status [{data.filters.status.join(", ") || "—"}] and
            profile [{data.filters.profile.join(", ") || "—"}]. The platform
            default is Confirmed on Primary + Contingency; clear the status and
            profile filters to return to the official stored verdicts.
          </p>
        </div>
      )}

      {!projection && data && (
        <p className="coverage-official">
          {data.filters.explanation}
          {/* C-08: how OLD the answers are, stated instead of implied. */}
          {data.verdicts_computed_to && (
            <span className="coverage-computed-at">
              {" "}Verdicts computed{" "}
              {data.verdicts_computed_from &&
              data.verdicts_computed_from.slice(0, 16) !==
                data.verdicts_computed_to.slice(0, 16)
                ? `between ${data.verdicts_computed_from
                    .slice(0, 16)
                    .replace("T", " ")} and ${data.verdicts_computed_to
                    .slice(0, 16)
                    .replace("T", " ")}`
                : data.verdicts_computed_to.slice(0, 16).replace("T", " ")}
              .
            </span>
          )}
        </p>
      )}

      {data && data.filters.skipped_customers.length > 0 && (
        <p className="exec-scope-projection" role="status">
          {data.filters.skipped_customers.join(", ")} could not be recomputed —
          not mapped to a Business Unit, so no inventory pool exists. Their
          wells keep their previous verdicts. Map them in Administration.
        </p>
      )}

      {error !== null && (
        <LoadError what="the coverage workspace" error={error} />
      )}

      {data && (
        <div className="impact-tiles">
          <div className="impact-tile">
            <div className="impact-tile-label">Wells</div>
            <div className="impact-tile-value num">{data.well_count}</div>
            <div className="impact-tile-note">matching these filters</div>
          </div>
          <div className="impact-tile impact-tile-better">
            <div className="impact-tile-label">Covered</div>
            <div className="impact-tile-value num">{counts.Covered}</div>
            <div className="impact-tile-note">every in-scope line covered</div>
          </div>
          <div className="impact-tile impact-tile-worse">
            <div className="impact-tile-label">Uncovered</div>
            <div className="impact-tile-value num">{counts.Uncovered}</div>
            <div className="impact-tile-note">
              at least one in-scope line not covered
            </div>
          </div>
          <div className="impact-tile impact-tile-noscope">
            <div className="impact-tile-label">No demand in scope</div>
            <div className="impact-tile-value num">{counts.NoScope}</div>
            <div className="impact-tile-note">
              neither covered nor uncovered
            </div>
          </div>
          <div className="impact-tile">
            <div className="impact-tile-label">In-scope lines</div>
            <div className="impact-tile-value num">
              {coveredLines.toLocaleString()} / {inScopeLines.toLocaleString()}
            </div>
            <div className="impact-tile-note">covered / in scope</div>
          </div>
        </div>
      )}

      <div className="list-meta">
        {loading ? <span>Loading…</span> : <span>{rows.length} well rows</span>}
        {"  "}
        <Freshness
          fetchedAt={fetchedAt}
          busy={loading}
          onRefresh={() => setReload((r) => r + 1)}
        />
      </div>

      <div className="table-scroll">
        <table className="demand-table">
          <thead>
            <tr>
              {(
                [
                  ["well", "Well"],
                  ["node", "Planning node"],
                  ["customer", "Customer"],
                  ["status", "Demand status"],
                  ["ros", "Earliest ROS"],
                  ["runout", "First runout"],
                  ["covered", "Lines covered"],
                  ["rollup", "Rollup"],
                ] as const
              ).map(([key, label]) => (
                <th
                  key={key}
                  className="th-sortable"
                  aria-sort={
                    sort.key === key
                      ? sort.direction === "asc"
                        ? "ascending"
                        : "descending"
                      : undefined
                  }
                >
                  <button type="button" onClick={() => toggle(key)}>
                    {label}
                    {sortIndicator(sort, key)}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && !loading && !error && (
              <tr>
                <td colSpan={8} className="empty">
                  No wells match these filters.
                </td>
              </tr>
            )}
            {sorted.map((r) => (
              <tr
                key={r.well_id}
                className={r.evaluated ? undefined : "row-unscoped"}
              >
                <td>
                  <Link to={`/wells/${r.well_id}`}>{r.well_name}</Link>
                </td>
                <td className="node-path">{r.planning_node_path}</td>
                <td>{r.customer_name}</td>
                <td>{r.demand_status}</td>
                <td className="num">
                  {r.earliest_ros_date ? formatDay(r.earliest_ros_date) : "—"}
                </td>
                <td className="num">
                  {r.first_runout_date ? formatDay(r.first_runout_date) : "no shortage"}
                </td>
                <td className="num">
                  {r.in_scope_line_count === 0 ? (
                    <span className="diff-none" title={NO_SCOPE_NOTE}>
                      no lines in scope
                    </span>
                  ) : (
                    <>
                      {r.covered_line_count} / {r.in_scope_line_count}
                    </>
                  )}
                </td>
                <td>
                  <RollupCell row={r} />
                  {!r.evaluated && (
                    <div className="row-demand-status-note">
                      Status is {r.demand_status} — outside the requested filter.
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
