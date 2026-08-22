import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import { apiGet, apiSend } from "../lib/api";
import { errorMessage } from "./admin/shared";

type ScenarioRow = {
  id: string;
  name: string;
  created_at: string;
  status: string;
  applied_at: string | null;
};

export default function ScenarioList() {
  const [rows, setRows] = useState<ScenarioRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);

  const load = () => {
    apiGet<ScenarioRow[]>("/scenarios")
      .then((d) => {
        setRows(d);
        setFetchedAt(new Date());
        setError(null);
      })
      // Keep previously-loaded data on screen; only surface the error.
      .catch((e) => setError(errorMessage(e)));
  };

  useEffect(load, []);

  const create = () => {
    if (!name.trim()) return;
    setCreating(true);
    apiSend<ScenarioRow>("POST", "/scenarios", { name: name.trim() })
      .then(() => {
        setName("");
        setError(null);
        load();
      })
      .catch((e) => setError(errorMessage(e)))
      .finally(() => setCreating(false));
  };

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Scenarios" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Scenarios</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>

        {error && <p className="inline-error">{error}</p>}

        <div className="admin-add-row">
          <input
            type="text"
            placeholder="New scenario name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button type="button" onClick={create} disabled={creating || !name.trim()}>
            {creating ? "Creating…" : "Create scenario"}
          </button>
        </div>

        {rows && (
          <table className="admin-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th>Created</th>
                <th>Applied</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr>
                  <td colSpan={4}>—</td>
                </tr>
              )}
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>
                    <Link to={`/scenarios/${r.id}`}>{r.name}</Link>
                  </td>
                  <td>
                    <span className={`scenario-chip scenario-${r.status.toLowerCase()}`}>{r.status}</span>
                  </td>
                  <td className="num">{new Date(r.created_at).toLocaleString()}</td>
                  <td className="num">{r.applied_at ? new Date(r.applied_at).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
