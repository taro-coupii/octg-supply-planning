import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  adminApi,
  CoverageScopeDefaults,
  CoverageScopeDefaultsChange,
  errorText,
} from "../../api/client";
import LoadError from "../../components/LoadError";

/** Set-equality on two string lists, so "did the user actually change anything". */
function sameSet(a: string[], b: string[]) {
  if (a.length !== b.length) return false;
  const bs = new Set(b);
  return a.every((x) => bs.has(x));
}

export default function CoverageScopePanel() {
  const [data, setData] = useState<CoverageScopeDefaults | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [change, setChange] = useState<CoverageScopeDefaultsChange | null>(null);
  const [busy, setBusy] = useState(false);
  /** The user's draft. Null until loaded, so the checkboxes never render unchecked. */
  const [statuses, setStatuses] = useState<string[] | null>(null);
  const [profiles, setProfiles] = useState<string[] | null>(null);
  /**
   * The user has acknowledged the blast radius. Required before Save is enabled.
   *
   * This is a UI gate only, and honestly labelled as such — the server performs the
   * change and reports what moved regardless of whether anything was ticked. Its
   * purpose is not to protect the data (nothing here is irreversible; the scope can be
   * set straight back) but to make sure the person clicking has read what "every well
   * on the platform" means before they find out.
   */
  const [acknowledged, setAcknowledged] = useState(false);

  function load() {
    return adminApi
      .getCoverageScopeDefaults()
      .then((d) => {
        setData(d);
        setStatuses(d.status_filter);
        setProfiles(d.profile_filter);
        setLoadError(null);
      })
      .catch(setLoadError);
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (loadError !== null) {
    return (
      <section className="scenario-section">
        <h2>Coverage scope defaults</h2>
        <LoadError what="the coverage scope defaults" error={loadError} />
      </section>
    );
  }
  if (!data || statuses === null || profiles === null) {
    return (
      <section className="scenario-section">
        <h2>Coverage scope defaults</h2>
        <p>Loading…</p>
      </section>
    );
  }

  const dirty =
    !sameSet(statuses, data.status_filter) || !sameSet(profiles, data.profile_filter);
  // An empty filter is legal and catastrophic — it puts every well (or every line) out
  // of scope, so every verdict on the platform disappears. The server refuses it with
  // a stated reason; the UI refuses to send it, so the operator is told before the
  // round trip rather than after.
  const emptyFilter = statuses.length === 0 || profiles.length === 0;
  const canSave = dirty && !emptyFilter && acknowledged && !busy;

  function toggle(list: string[], value: string, set: (v: string[]) => void) {
    set(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);
  }

  function save(nextStatuses: string[], nextProfiles: string[]) {
    setBusy(true);
    setWriteError(null);
    adminApi
      .setCoverageScopeDefaults({
        status_filter: nextStatuses,
        profile_filter: nextProfiles,
      })
      .then((result) => {
        setChange(result);
        setAcknowledged(false);
        return load();
      })
      .catch((e) => {
        setChange(null);
        setWriteError(errorText(e));
      })
      .finally(() => setBusy(false));
  }

  return (
    <section className="scenario-section">
      <h2>Coverage scope defaults</h2>
      <p className="scenario-section-note">
        Which demand the coverage engine evaluates at all. The two filters
        deliberately select different things — <strong>status selects wells</strong>{" "}
        (demand status lives on the well, so a status filter takes a well entirely or
        leaves it out entirely) and <strong>profile selects lines</strong>, cutting
        inside a well. That asymmetry is what makes the coverage grid&apos;s well
        count and line count move by explainable amounts.
      </p>

      <p className="admin-scope-warning">
        <span className="admin-scope-warning-tag">
          Changes coverage for every well on the platform
        </span>
        This is <strong>not a display filter</strong>, and it is not per customer.
        These filters decide which demand lines <strong>compete for the same
        steel</strong>, so widening or narrowing them moves verdicts for lines that
        were in scope all along — for every customer in every Business Unit, at once.
        Saving recomputes every customer immediately, so no stored verdict is left
        describing the old scope. Demand outside the scope is{" "}
        <em>not evaluated</em>: a third state, distinct from covered and from
        uncovered.
      </p>

      <div className="admin-scope-provenance">
        {data.persisted ? (
          <>
            <span className="badge admin-scope-badge-set">Adjusted</span> This scope
            was set from this screen
            {data.updated_at ? ` on ${new Date(data.updated_at).toLocaleString()}` : ""}
            .
            {data.matches_shipped_default &&
              " It happens to match the values the platform ships with."}
          </>
        ) : (
          <>
            <span className="badge admin-scope-badge-shipped">Shipped default</span>{" "}
            Nobody has adjusted the scope, so the platform is running on the values it
            ships with. No setting row exists — these are not a stored copy of the
            shipped values, they <em>are</em> the shipped values.
          </>
        )}
      </div>

      {writeError && (
        <p className="admin-write-error">
          <span className="admin-write-error-tag">Refused — nothing saved</span>
          {writeError}
        </p>
      )}

      {change && (
        <div className="admin-report admin-report-severe">
          <div className="admin-report-head">
            <span className="admin-report-tag">
              {change.unchanged
                ? "No change — that scope was already in effect"
                : "Coverage scope saved"}
            </span>
            <button
              type="button"
              className="admin-report-dismiss"
              onClick={() => setChange(null)}
            >
              Dismiss
            </button>
          </div>
          <p className="admin-report-note">{change.note}</p>
          {!change.unchanged && (
            <dl className="exec-facts admin-report-facts">
              <div>
                <dt>Recompute</dt>
                <dd>
                  {change.recomputed_synchronously
                    ? `Completed in this request — ${change.recomputed_customer_ids.length} customer(s), ${change.recomputed_well_count} well(s) visited`
                    : "Deferred"}
                </dd>
              </div>
              <div>
                <dt>Wells whose rollup moved</dt>
                <dd>
                  {change.well_changes.length} of {change.recomputed_well_count}
                </dd>
              </div>
            </dl>
          )}
          {change.well_changes.length > 0 && (
            <div className="admin-report-wells">
              <ul>
                {change.well_changes.map((w) => (
                  <li key={w.well_id}>
                    <Link to={`/wells/${w.well_id}`}>{w.well_name}</Link>{" "}
                    {w.coverage_before ?? "not evaluated"} &rarr;{" "}
                    {w.coverage_after ?? "not evaluated"}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      <div className="admin-scope-editor">
        <fieldset className="admin-scope-field">
          <legend>Demand status in scope — selects whole wells</legend>
          {/* Options come from the server so the UI cannot offer a value the server
              would reject. */}
          {data.available_statuses.map((s) => (
            <label key={s} className="admin-scope-check">
              <input
                type="checkbox"
                checked={statuses.includes(s)}
                disabled={busy}
                onChange={() => toggle(statuses, s, setStatuses)}
              />
              <span>{s}</span>
              {data.shipped_status_filter.includes(s) && (
                <span className="admin-scope-shipped">shipped</span>
              )}
            </label>
          ))}
          {statuses.length === 0 && (
            <p className="admin-scope-empty">
              An empty status filter puts <strong>every well</strong> out of scope, so
              nothing on the platform would be evaluated and the coverage grid would
              empty. Select at least one.
            </p>
          )}
        </fieldset>

        <fieldset className="admin-scope-field">
          <legend>Profile in scope — selects lines within a well</legend>
          {data.available_profiles.map((p) => (
            <label key={p} className="admin-scope-check">
              <input
                type="checkbox"
                checked={profiles.includes(p)}
                disabled={busy}
                onChange={() => toggle(profiles, p, setProfiles)}
              />
              <span>{p}</span>
              {data.shipped_profile_filter.includes(p) && (
                <span className="admin-scope-shipped">shipped</span>
              )}
            </label>
          ))}
          {profiles.length === 0 && (
            <p className="admin-scope-empty">
              An empty profile filter puts <strong>every demand line</strong> out of
              scope. Select at least one.
            </p>
          )}
        </fieldset>
      </div>

      <div className="admin-scope-actions">
        <label className="admin-scope-ack">
          <input
            type="checkbox"
            checked={acknowledged}
            disabled={!dirty || busy}
            onChange={(e) => setAcknowledged(e.target.checked)}
          />
          <span>
            I understand this changes the coverage verdict for{" "}
            <strong>every well on the platform</strong>, not just this screen, and
            that every customer will be recomputed immediately.
          </span>
        </label>
        <div className="admin-scope-buttons">
          <button
            type="button"
            className="admin-scope-save"
            disabled={!canSave}
            onClick={() => save(statuses, profiles)}
          >
            {busy ? "Saving and recomputing…" : "Save coverage scope"}
          </button>
          <button
            type="button"
            disabled={!dirty || busy}
            onClick={() => {
              setStatuses(data.status_filter);
              setProfiles(data.profile_filter);
              setAcknowledged(false);
            }}
          >
            Discard changes
          </button>
          {/* Only offered when it would actually do something, so the control is never
              a no-op that looks like a fix. */}
          {!sameSet(data.status_filter, data.shipped_status_filter) ||
          !sameSet(data.profile_filter, data.shipped_profile_filter) ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setStatuses(data.shipped_status_filter);
                setProfiles(data.shipped_profile_filter);
                setAcknowledged(false);
              }}
            >
              Reset to shipped default ({data.shipped_status_filter.join(", ")} /{" "}
              {data.shipped_profile_filter.join(", ")})
            </button>
          ) : null}
        </div>
        {!dirty && (
          <p className="admin-scope-hint">
            Nothing to save — the checkboxes match the scope currently in effect.
          </p>
        )}
        {dirty && !acknowledged && !emptyFilter && (
          <p className="admin-scope-hint">
            Tick the acknowledgement above to enable Save.
          </p>
        )}
      </div>

      <p className="admin-defaults-note">
        The Coverage workspace can still recompute against other filters ad hoc, but
        that result is an explicitly labelled <em>read-only projection</em> and is
        never stored as the official verdict. This setting is the opposite: it is the
        scope the official verdict is computed under.
      </p>
    </section>
  );
}
