import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  CustomerSummary,
  ScenarioStatus,
  ScenarioSummary,
} from "../api/client";
import Freshness from "../components/Freshness";

const STATUS_ORDER: ScenarioStatus[] = [
  "Draft",
  "Review",
  "Discussion",
  "Applied",
];

function StatusBadge({ status }: { status: ScenarioStatus }) {
  return <span className={`badge badge-status-${status}`}>{status}</span>;
}

function formatWhen(iso: string) {
  return iso.slice(0, 10);
}

/**
 * The headline coverage delta.
 *
 * Deliberately worded as a what-if ("would move"), never as a fact. It comes
 * from the read-only preview, so it describes something that has not happened —
 * and for an Applied scenario it has ALREADY happened, so the wording changes.
 */
function CoverageDelta({ scenario }: { scenario: ScenarioSummary }) {
  if (scenario.preview_error) {
    return (
      <div className="scenario-delta scenario-delta-none">
        Impact could not be computed: {scenario.preview_error}
      </div>
    );
  }
  if (scenario.override_count === 0) {
    return (
      <div className="scenario-delta scenario-delta-none">
        No overrides yet — nothing to compare.
      </div>
    );
  }
  const wells = scenario.coverage_delta_wells ?? 0;
  const lines = scenario.coverage_delta_lines ?? 0;

  if (scenario.status === "Applied") {
    return (
      <div className="scenario-delta scenario-delta-none">
        Applied — these changes are in the base plan.
      </div>
    );
  }
  if (wells === 0 && lines === 0) {
    return (
      <div className="scenario-delta scenario-delta-none">
        Would change no coverage status.
      </div>
    );
  }
  return (
    <div className="scenario-delta scenario-delta-some">
      Would move {wells} well{wells === 1 ? "" : "s"} and {lines} demand line
      {lines === 1 ? "" : "s"}
    </div>
  );
}

function NewScenarioForm({
  customers,
  onCreated,
}: {
  customers: CustomerSummary[];
  onCreated: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [customerId, setCustomerId] = useState(customers[0]?.id ?? "");
  const [description, setDescription] = useState("");
  const [createdBy, setCreatedBy] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) {
    return (
      <div className="top-actions">
        <button className="btn-plain" onClick={() => setOpen(true)}>
          New scenario
        </button>
      </div>
    );
  }

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.createScenario({
        name,
        customer_id: customerId || customers[0]?.id,
        description: description || null,
        created_by: createdBy || null,
      });
      setOpen(false);
      setName("");
      setDescription("");
      onCreated();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="new-scenario-form">
      <label>
        Name
        <input
          value={name}
          placeholder="What if Hawk-07 slips?"
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label>
        Customer
        <select value={customerId} onChange={(e) => setCustomerId(e.target.value)}>
          {customers.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Description
        <input value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <label>
        Created by
        <input
          value={createdBy}
          placeholder="your name"
          onChange={(e) => setCreatedBy(e.target.value)}
        />
      </label>
      <button onClick={submit} disabled={busy || !name || !customerId}>
        {busy ? "Creating..." : "Create"}
      </button>
      <button className="link-btn" onClick={() => setOpen(false)} disabled={busy}>
        Cancel
      </button>
      {error && <div className="form-error">{error}</div>}
    </div>
  );
}

export default function ScenarioList() {
  const [scenarios, setScenarios] = useState<ScenarioSummary[] | null>(null);
  const [customers, setCustomers] = useState<CustomerSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [busy, setBusy] = useState(false);
  // Stale-response guard, same convention as every other list screen: a slow
  // response from an earlier reload must not overwrite a newer one.
  const reloadSeq = useRef(0);

  const reload = () => {
    const seq = ++reloadSeq.current;
    setBusy(true);
    api
      .getScenarios()
      .then((s) => {
        if (seq !== reloadSeq.current) return;
        setScenarios(s);
        setError(null);
        setFetchedAt(new Date());
      })
      // The error is SHOWN but previously loaded cards are KEPT — a failed
      // refresh must not blank a page that was readable a second ago.
      .catch((e) => {
        if (seq === reloadSeq.current) setError(String(e));
      })
      .finally(() => {
        if (seq === reloadSeq.current) setBusy(false);
      });
  };

  useEffect(() => {
    reload();
    // A failing customer list must not blank the page; it only feeds the form.
    api.getCustomers().then(setCustomers).catch(() => setCustomers([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!scenarios && error) return <p>Failed to load scenarios: {error}</p>;
  if (!scenarios) return <p>Loading...</p>;

  const sorted = [...scenarios].sort(
    (a, b) =>
      STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status) ||
      b.created_at.localeCompare(a.created_at)
  );

  return (
    <div>
      <div className="scenario-head">
        <h1>Scenarios</h1>
        <Freshness fetchedAt={fetchedAt} onRefresh={reload} busy={busy} />
      </div>
      {error && (
        <p className="form-error">
          Refresh failed: {error} — showing the last loaded list.
        </p>
      )}
      <p>
        What if this demand becomes confirmed? What if this ROS moves out 30 days?
        What if mill delivery accelerates? Scenarios exist to support a live
        customer conversation — see the coverage, MRP and risk impact, then apply
        what was agreed.
      </p>
      <p className="scenario-sub">
        <strong>Scenarios are shared.</strong> Everyone with project access sees
        and can review every scenario here; there are no private scenarios.
        &ldquo;Created by&rdquo; is attribution, not permission.
      </p>

      <NewScenarioForm customers={customers} onCreated={reload} />

      {sorted.length === 0 ? (
        <div className="card">
          <div className="empty">
            No scenarios yet. Create one to explore a change with a customer.
          </div>
        </div>
      ) : (
        <div className="scenario-list">
          {sorted.map((s) => (
            <Link
              key={s.id}
              to={`/scenarios/${s.id}`}
              className={`scenario-card scenario-card-${s.status}`}
            >
              <div className="scenario-card-head">
                <span className="scenario-name">{s.name}</span>
                <StatusBadge status={s.status} />
              </div>
              <div className="scenario-meta">
                <span>{s.customer_name}</span>
                <span>
                  {s.override_count} override{s.override_count === 1 ? "" : "s"}
                </span>
                <span>created {formatWhen(s.created_at)}</span>
                {s.created_by_user_name && <span>by {s.created_by_user_name}</span>}
                {s.created_by && <span>on behalf of {s.created_by}</span>}
                {s.applied_at && <span>applied {formatWhen(s.applied_at)}</span>}
              </div>
              {s.description && <div className="scenario-desc">{s.description}</div>}
              <CoverageDelta scenario={s} />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
