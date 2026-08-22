import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  CoverageStatus,
  DemandLineOut,
  errorText,
  WellDemandStatusChangeOut,
  WellDetail,
} from "../api/client";
import Breadcrumbs from "../components/Breadcrumbs";
import CoverageBadge from "../components/CoverageBadge";
import LoadError from "../components/LoadError";
import ReasonText from "../components/ReasonText";
import SharingPanel from "../components/SharingPanel";
import { formatDay } from "./MrpSummary";
import { DEMAND_PROFILES, DEMAND_STATUSES } from "../lib/enums";

/** Line states for which a BU-sharing what-if is worth offering. */
const SHARING_RELEVANT: CoverageStatus[] = ["Uncovered", "Unrecoverable"];

const SUBSTITUTABLE_STATUSES: CoverageStatus[] = [
  "Uncovered",
  "PendingApproval",
  "CoveredViaSubstitute",
];

const STATUS_OPTIONS = [...DEMAND_STATUSES];
const PROFILE_OPTIONS = [...DEMAND_PROFILES];

function toDateInputValue(iso: string) {
  return iso.slice(0, 10);
}

/**
 * Revises quantity / ROS / profile only. Status used to be editable here too,
 * but it is now a well-level property — `POST .../revisions` 400s if `status`
 * is sent, so the field was removed entirely rather than sent silently.
 */
function ReviseRow({
  line,
  onSaved,
}: {
  line: DemandLineOut;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [quantity, setQuantity] = useState(String(line.quantity));
  const [rosDate, setRosDate] = useState(toDateInputValue(line.ros_date));
  const [profile, setProfile] = useState(line.profile);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!editing) {
    return (
      <button className="link-btn" onClick={() => setEditing(true)}>
        Revise
      </button>
    );
  }

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.createRevision(line.id, {
        quantity: Number(quantity),
        ros_date: new Date(rosDate).toISOString(),
        profile,
      });
      setEditing(false);
      onSaved();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="revise-form">
      <label>
        Qty ({line.unit_of_measure})
        <input
          type="number"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
        />
      </label>
      <label>
        ROS
        <input type="date" value={rosDate} onChange={(e) => setRosDate(e.target.value)} />
      </label>
      <label>
        Profile
        <select value={profile} onChange={(e) => setProfile(e.target.value)}>
          {PROFILE_OPTIONS.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </label>
      <button onClick={save} disabled={saving}>
        {saving ? "Saving..." : "Save revision"}
      </button>
      <button className="link-btn" onClick={() => setEditing(false)} disabled={saving}>
        Cancel
      </button>
      {error && <div className="form-error">{error}</div>}
    </div>
  );
}

/**
 * Well-level demand status control. Status is now a property of the WELL, not
 * the line: changing it cascades a revision to EVERY line of the well and can
 * change coverage for OTHER wells of the same customer (status selects whole
 * wells in/out of the shared inventory pool). Warned before submitting, and
 * the before -> after coverage transition is shown after.
 */
function WellStatusControl({
  wellId,
  wellName,
  currentStatus,
  onChanged,
}: {
  wellId: string;
  wellName: string;
  currentStatus: string;
  onChanged: () => void;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<WellDemandStatusChangeOut | null>(null);

  const requestChange = (next: string) => {
    if (next === currentStatus) return;
    setPending(next);
    setError(null);
    setResult(null);
  };

  const confirm = async () => {
    if (!pending) return;
    setSaving(true);
    setError(null);
    try {
      const res = await api.setWellDemandStatus(wellId, { demand_status: pending });
      setResult(res);
      setPending(null);
      onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="well-status-control">
      <div className="well-status-row">
        <span className="well-status-label">Demand status</span>
        <div className="well-status-buttons">
          {STATUS_OPTIONS.map((s) => (
            <button
              key={s}
              type="button"
              className={`well-status-btn${s === currentStatus ? " well-status-btn-active" : ""}`}
              disabled={saving}
              onClick={() => requestChange(s)}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {pending && (
        <div className="well-status-warning" role="alert">
          <p>
            Changing <strong>{wellName}</strong>&apos;s demand status from{" "}
            <strong>{currentStatus}</strong> to <strong>{pending}</strong> revises{" "}
            <strong>every line of this well</strong> and may change coverage for{" "}
            <strong>other wells of the same customer</strong> — status selects
            whole wells in/out of the shared inventory pool.
          </p>
          <div className="well-status-warning-actions">
            <button onClick={confirm} disabled={saving}>
              {saving ? "Applying..." : `Confirm: set to ${pending}`}
            </button>
            <button className="link-btn" onClick={() => setPending(null)} disabled={saving}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <div className="form-error">{error}</div>}

      {result && (
        <div className={`well-status-result${result.unchanged ? " well-status-result-noop" : ""}`}>
          {result.unchanged ? (
            <p>No change made — status was already {result.status_after}.</p>
          ) : (
            <>
              <p>
                Status: <strong>{result.status_before}</strong>{" "}
                <span className="change-arrow">&rarr;</span>{" "}
                <strong>{result.status_after}</strong> ·{" "}
                {result.revised_line_count} line
                {result.revised_line_count === 1 ? "" : "s"} revised
              </p>
              <p className="diff-line">
                Coverage:{" "}
                <span className="diff-before">
                  <CoverageBadge status={result.coverage_before} />
                </span>
                <span className="change-arrow">&rarr;</span>
                <span className="diff-after">
                  <CoverageBadge status={result.coverage_after} />
                </span>
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function WellWorkspace() {
  const { wellId } = useParams<{ wellId: string }>();
  const [well, setWell] = useState<WellDetail | null>(null);
  const [error, setError] = useState<unknown>(null);

  /**
   * The well's owning customer, needed to run the BU-sharing what-if.
   *
   * `GET /wells/{id}` does not carry `customer_id`, so it is resolved from the
   * coverage grid, whose rows do (`CoverageGridRow.customer_id`). Called with no
   * filters, so it returns the official stored verdicts and triggers no
   * recompute. A failure here only means the sharing offer is not shown — it
   * must never blank the well itself.
   */
  const [customerId, setCustomerId] = useState<string | null>(null);
  const [sharingOpen, setSharingOpen] = useState(false);

  const reload = () => {
    if (!wellId) return;
    api.getWell(wellId).then(setWell).catch(setError);
  };

  useEffect(reload, [wellId]);

  useEffect(() => {
    if (!wellId) return;
    setCustomerId(null);
    setSharingOpen(false);
    api
      .getCoverageGrid({})
      .then((grid) => {
        const row = grid.rows.find((r) => r.well_id === wellId);
        setCustomerId(row?.customer_id ?? null);
      })
      .catch(() => setCustomerId(null));
  }, [wellId]);

  if (error)
    return (
      <div>
        <Breadcrumbs trail={[{ label: "Home", to: "/" }, { label: "Well" }]} />
        <LoadError what="this well" error={error} />
      </div>
    );
  if (!well)
    return (
      <div>
        <Breadcrumbs trail={[{ label: "Home", to: "/" }, { label: "Well" }]} />
        <p>Loading...</p>
      </div>
    );

  const hasUncovered = well.demand_lines.some((l) =>
    SHARING_RELEVANT.includes(l.coverage_status)
  );

  return (
    <div>
      <Breadcrumbs trail={[{ label: "Home", to: "/" }, { label: well.name }]} />
      <h1>
        {well.name} <CoverageBadge status={well.coverage_status} />
      </h1>

      <div className="well-dates">
        <span>
          Earliest ROS{" "}
          <strong>
            {well.earliest_ros_date ? formatDay(well.earliest_ros_date) : "—"}
          </strong>
        </span>
        <span>
          First runout{" "}
          <strong>
            {well.first_runout_date ? formatDay(well.first_runout_date) : "no shortage"}
          </strong>
        </span>
      </div>

      <WellStatusControl
        wellId={well.id}
        wellName={well.name}
        currentStatus={well.demand_status}
        onChanged={reload}
      />

      {/*
        The planner discovers the uncovered line here, so the BU-sharing what-if
        is offered here — but behind a click and never mixed into the table
        above. The badge on this page is the official, customer-scoped verdict;
        the what-if contradicts it by design, and letting the two share a surface
        would turn a projection into a second verdict.
      */}
      {hasUncovered && customerId && (
        <div className="share-offer">
          {sharingOpen ? (
            <>
              <button
                type="button"
                className="btn-plain"
                onClick={() => setSharingOpen(false)}
              >
                Hide the sharing what-if
              </button>
              <SharingPanel
                customerId={customerId}
                wellId={wellId}
                title={`If inventory were shared within the Business Unit — ${well.name}`}
              />
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn-plain"
                onClick={() => setSharingOpen(true)}
              >
                Could this be covered by sharing within the Business Unit?
              </button>
              <span className="share-offer-note">
                A read-only projection that deliberately disagrees with the
                official verdict above. Nothing is saved or reserved.
              </span>
            </>
          )}
        </div>
      )}

      {/* Same demand lines twice, ONE visible at a time (CSS): the table on
          desktop, cards on narrow screens — where the table clipped the
          verdict, the reason and the actions off the right-hand edge. */}
      <div className="table-scroll well-lines-table-wrap">
        <table>
          <thead>
            <tr>
              <th>Product</th>
              <th>Qty</th>
              <th>ROS</th>
              {/* Read-only echo of the well's status — see WellStatusControl above for the editable control. */}
              <th title="Read-only. Every line of a well shares one status — change it in the well header above.">
                Status (well-level)
              </th>
              <th>Profile</th>
              <th>Coverage</th>
              <th>Reason</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {well.demand_lines.map((line) => (
              <tr key={line.id}>
                <td>{line.product_description ?? line.product_id}</td>
                <td className="num">
                  {line.quantity.toLocaleString()} {line.unit_of_measure}
                </td>
                <td>{new Date(line.ros_date).toLocaleDateString()}</td>
                <td className="well-status-echo">{line.status}</td>
                <td>{line.profile}</td>
                <td>
                  <CoverageBadge status={line.coverage_status} />
                </td>
                {/*
                  A coverage reason now runs to ~330 characters with the
                  actionable half at the END ("... -- Release the hard assignment
                  in Oracle, then re-sync -- ..."). This was a bare <td>, which
                  the browser squeezes to whatever the other eight columns leave.
                  `.mrp-reason` gives it a floor width and ReasonText breaks it
                  into clauses without ever dropping the tail.
                */}
                <td className="mrp-reason">
                  <ReasonText reason={line.coverage_reason} />
                </td>
                <td>
                  <div className="row-actions">
                    <ReviseRow line={line} onSaved={reload} />
                    {SUBSTITUTABLE_STATUSES.includes(line.coverage_status) && (
                      <Link
                        className="link-btn"
                        to={`/demand-lines/${line.id}/substitution`}
                      >
                        Substitutes
                      </Link>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="well-lines-cards">
        {well.demand_lines.map((line) => (
          <div className="well-line-card" key={line.id}>
            <div className="well-line-card-head">
              <span className="well-line-card-product">
                {line.product_description ?? line.product_id}
              </span>
              <CoverageBadge status={line.coverage_status} />
            </div>
            <div className="well-line-card-facts">
              <span>
                {line.quantity.toLocaleString()} {line.unit_of_measure}
              </span>
              <span>ROS {new Date(line.ros_date).toLocaleDateString()}</span>
              <span>{line.profile}</span>
            </div>
            {line.coverage_reason && (
              <div className="well-line-card-reason">
                <ReasonText reason={line.coverage_reason} />
              </div>
            )}
            <div className="row-actions">
              <ReviseRow line={line} onSaved={reload} />
              {SUBSTITUTABLE_STATUSES.includes(line.coverage_status) && (
                <Link
                  className="link-btn"
                  to={`/demand-lines/${line.id}/substitution`}
                >
                  Substitutes
                </Link>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
