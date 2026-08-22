import { useEffect, useState } from "react";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import ScopeChecks, { DEFAULT_SCOPE_STATE, scopeToQuery, type ScopeState } from "../components/ScopeChecks";
import { apiGet } from "../lib/api";
import { nextSortState, sortRows, type SortDir } from "../lib/sort";
import { errorMessage } from "./admin/shared";

type SurplusRow = {
  product: string;
  unit: string;
  on_hand: number;
  allocated: number;
  surplus: number;
  obsolete: number;
};
type Scope = { statuses: string[]; profiles: string[] };
type SurplusResp = {
  rows: SurplusRow[];
  totals_by_unit: Record<string, SurplusRow>;
  identity_ok: boolean;
  scope: Scope;
  scope_is_default: boolean;
  warning?: string | null;
};

type SortKey = "product" | "on_hand" | "allocated" | "surplus" | "obsolete";

export default function SurplusList() {
  const [scope, setScope] = useState<ScopeState>(DEFAULT_SCOPE_STATE);
  const [data, setData] = useState<SurplusResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("product");
  const [sortDir, setSortDir] = useState<SortDir>(null);

  const load = () => {
    apiGet<SurplusResp>(`/analysis/surplus${scopeToQuery(scope)}`)
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setError(null);
      })
      // Keep previously-loaded data on screen; only surface the error.
      .catch((e) => setError(errorMessage(e)));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [scope]);

  const toggleSort = (key: SortKey) => {
    if (key !== sortKey) {
      setSortKey(key);
      setSortDir("asc");
    } else {
      setSortDir(nextSortState(sortDir));
    }
  };

  const ariaSortFor = (key: SortKey): "ascending" | "descending" | "none" => {
    if (sortKey !== key || sortDir === null) return "none";
    return sortDir === "asc" ? "ascending" : "descending";
  };

  const rows = data ? sortRows(data.rows, sortKey, sortDir) : [];

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Surplus" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Surplus</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>

        <ScopeChecks value={scope} onChange={setScope} />

        {data && (
          <p className="hint">
            Computed under scope: {data.scope.statuses.join(", ")} / {data.scope.profiles.join(", ")}
          </p>
        )}

        {data && !data.scope_is_default && (
          <p className="banner-warn">{data.warning ?? "Recomputed read-only — NOT the official stored verdicts"}</p>
        )}

        {error && <p className="inline-error">{error}</p>}

        {data && data.identity_ok === false && (
          <p className="banner-error">
            Identity violated — report this. (On hand should always equal allocated + surplus + obsolete.)
          </p>
        )}

        {data && (
          <table className="admin-table">
            <thead>
              <tr>
                <th aria-sort={ariaSortFor("product")}>
                  <button type="button" onClick={() => toggleSort("product")}>
                    Product
                  </button>
                </th>
                <th aria-sort={ariaSortFor("on_hand")}>
                  <button type="button" onClick={() => toggleSort("on_hand")}>
                    On hand
                  </button>
                </th>
                <th>Allocated · Surplus · Obsolete</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr>
                  <td colSpan={3}>—</td>
                </tr>
              )}
              {rows.map((r) => (
                <tr key={r.product}>
                  <td>{r.product}</td>
                  <td>
                    <span className="num">{r.on_hand}</span> {r.unit}
                  </td>
                  <td>
                    <span className="surplus-dot surplus-dot-ok" /> Allocated <span className="num">{r.allocated}</span> ·{" "}
                    <span className="surplus-dot surplus-dot-warn" /> Surplus <span className="num">{r.surplus}</span> ·{" "}
                    <span className="surplus-dot surplus-dot-bad" /> Obsolete <span className="num">{r.obsolete}</span>
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
