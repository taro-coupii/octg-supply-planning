import { useEffect, useState } from "react";
import { ApiError, apiGet, apiSend } from "../../lib/api";
import { DEMAND_PROFILES, DEMAND_STATUSES } from "../../lib/enums";
import { errorMessage } from "./shared";

type CoverageScope = { statuses: string[]; profiles: string[] };

export default function CoverageScopeTab() {
  const [scope, setScope] = useState<CoverageScope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    apiGet<CoverageScope>("/admin/coverage-scope")
      .then((s) => {
        setScope(s);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const toggle = (key: "statuses" | "profiles", value: string) => {
    setScope((prev) => {
      if (!prev) return prev;
      const set = new Set(prev[key]);
      if (set.has(value)) set.delete(value);
      else set.add(value);
      return { ...prev, [key]: Array.from(set) };
    });
  };

  const save = () => {
    if (!scope) return;
    setSaving(true);
    apiSend<CoverageScope>("PUT", "/admin/coverage-scope", scope)
      .then((s) => {
        setScope(s);
        setError(null);
      })
      .catch((e: ApiError | Error) => setError(errorMessage(e)))
      .finally(() => setSaving(false));
  };

  if (scope === null && !error) return <p>Loading…</p>;

  return (
    <div>
      {error && <p className="inline-error">{error}</p>}
      {scope && (
        <>
          <fieldset>
            <legend>Demand statuses in scope</legend>
            {DEMAND_STATUSES.map((s) => (
              <label key={s} className="checkbox-label">
                <input type="checkbox" checked={scope.statuses.includes(s)} onChange={() => toggle("statuses", s)} />
                {s}
              </label>
            ))}
          </fieldset>
          <fieldset>
            <legend>Demand profiles in scope</legend>
            {DEMAND_PROFILES.map((p) => (
              <label key={p} className="checkbox-label">
                <input type="checkbox" checked={scope.profiles.includes(p)} onChange={() => toggle("profiles", p)} />
                {p}
              </label>
            ))}
          </fieldset>
          <button type="button" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
        </>
      )}
    </div>
  );
}
