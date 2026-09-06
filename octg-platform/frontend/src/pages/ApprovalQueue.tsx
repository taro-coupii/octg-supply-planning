import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApprovalQueueRow } from "../api/client";
import ConfirmButton from "../components/ConfirmButton";
import LoadError from "../components/LoadError";

/**
 * The substitution-approval QUEUE. Before this screen, approvals were only
 * reachable per demand line and via the Home card's handful -- a planner with
 * twenty pending requests had no list. Decisions go through the same endpoint
 * the Home card and the Substitution Workspace use; this is a view, not a
 * second approval mechanism.
 */

const STATUSES = ["Pending", "Approved", "Rejected"] as const;
type QueueStatus = (typeof STATUSES)[number];

function fmtWhen(iso: string | null) {
  return iso ? iso.slice(0, 16).replace("T", " ") : "—";
}

export default function ApprovalQueue() {
  const [status, setStatus] = useState<QueueStatus>("Pending");
  const [rows, setRows] = useState<ApprovalQueueRow[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  // Tab switches must not race: a slow Pending response landing after a fast
  // Approved one would render the wrong tab's rows. Each effect run owns a
  // `live` flag and stale responses are dropped.
  const [reload, setReload] = useState(0);
  const load = useCallback(() => setReload((r) => r + 1), []);
  useEffect(() => {
    let live = true;
    api
      .getSubstitutionApprovals(status)
      .then((r) => {
        if (!live) return;
        setRows(r);
        setError(null);
      })
      .catch((e) => {
        // Keep the rows on screen: a failed refresh is a banner, not a blank.
        if (live) setError(e);
      });
    return () => {
      live = false;
    };
  }, [status, reload]);

  const decide = async (approvalId: string, approved: boolean) => {
    setBusyId(approvalId);
    try {
      await api.decideSubstitutionApproval(approvalId, approved);
      load();
    } catch (e) {
      setError(e);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="mor-page">
      <header className="mor-head">
        <div>
          <h2>Substitution Approvals</h2>
          <p className="mor-summary">
            {rows === null
              ? "Loading…"
              : status === "Pending"
              ? rows.length === 0
                ? "Nothing is waiting for a decision."
                : `${rows.length} request(s) waiting for a customer decision.`
              : `${rows.length} ${status.toLowerCase()} request(s).`}
          </p>
        </div>
        <div className="exec-horizon-picker" role="group" aria-label="Status">
          {STATUSES.map((s) => (
            <button
              key={s}
              type="button"
              className={`exec-horizon-btn${s === status ? " exec-horizon-btn-on" : ""}`}
              aria-pressed={s === status}
              onClick={() => setStatus(s)}
            >
              {s}
            </button>
          ))}
        </div>
      </header>

      {error != null && <LoadError what="the approval queue" error={error} />}

      {rows !== null && (
        // Same rows twice, ONE visible at a time (CSS): the table on desktop,
        // the card list on narrow screens — where the old table clipped the
        // Approve/Decline buttons, i.e. the entire point of this screen,
        // off the right-hand edge. Both act through the same `decide`.
        <div className="table-scroll approval-table-wrap">
          <table className="approval-queue">
            <thead>
              <tr>
                <th>Requested</th>
                <th>Well</th>
                <th>Customer</th>
                <th>Original product</th>
                <th>Substitute</th>
                <th className="num">Quantity</th>
                <th>ROS</th>
                {status !== "Pending" && <th>Decided</th>}
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.approval_id}>
                  <td className="num">{fmtWhen(r.requested_at)}</td>
                  <td>
                    {r.well_id ? (
                      <Link to={`/wells/${r.well_id}`}>{r.well_name}</Link>
                    ) : (
                      r.well_name ?? "—"
                    )}
                  </td>
                  <td>{r.customer_name ?? "—"}</td>
                  <td>{r.from_product_description}</td>
                  <td>{r.to_product_description}</td>
                  <td className="num">
                    {r.quantity !== null
                      ? `${r.quantity.toLocaleString()} ${r.unit_of_measure ?? ""}`
                      : "—"}
                  </td>
                  <td className="num">
                    {r.ros_date ? r.ros_date.slice(0, 10) : "—"}
                  </td>
                  {status !== "Pending" && (
                    <td className="num">
                      {fmtWhen(r.decided_at)}
                      {r.decided_by_user_name ? ` · ${r.decided_by_user_name}` : ""}
                    </td>
                  )}
                  <td>
                    <span className="approval-queue-actions">
                      <Link to={`/demand-lines/${r.demand_line_id}/substitution`}>
                        Details
                      </Link>
                      {r.status === "Pending" && (
                        <>
                          {/* Two-step: a decision is FINAL (409 on
                              re-decision) -- see ConfirmButton. */}
                          <ConfirmButton
                            disabled={busyId === r.approval_id}
                            label="Approve"
                            confirmLabel="Confirm approve?"
                            onConfirm={() => decide(r.approval_id, true)}
                          />
                          <ConfirmButton
                            className="btn-reject"
                            disabled={busyId === r.approval_id}
                            label="Decline"
                            confirmLabel="Confirm decline?"
                            onConfirm={() => decide(r.approval_id, false)}
                          />
                        </>
                      )}
                    </span>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={status === "Pending" ? 8 : 9} className="empty">
                    No {status.toLowerCase()} approvals.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      {rows !== null && (
        <div className="approval-cards">
          {rows.length === 0 && (
            <p className="empty">No {status.toLowerCase()} approvals.</p>
          )}
          {rows.map((r) => (
            <div className="approval-card" key={r.approval_id}>
              <div className="approval-card-head">
                {r.well_id ? (
                  <Link to={`/wells/${r.well_id}`}>{r.well_name}</Link>
                ) : (
                  <span>{r.well_name ?? "—"}</span>
                )}
                <span className="approval-card-when">
                  {fmtWhen(r.requested_at)}
                </span>
              </div>
              <div className="approval-card-body">
                <span>{r.customer_name ?? "—"}</span>
                <span>
                  {r.from_product_description} → {r.to_product_description}
                </span>
                <span>
                  {r.quantity !== null
                    ? `${r.quantity.toLocaleString()} ${r.unit_of_measure ?? ""}`
                    : "—"}
                  {r.ros_date ? ` · ROS ${r.ros_date.slice(0, 10)}` : ""}
                  {status !== "Pending"
                    ? ` · decided ${fmtWhen(r.decided_at)}${
                        r.decided_by_user_name ? ` by ${r.decided_by_user_name}` : ""
                      }`
                    : ""}
                </span>
              </div>
              <div className="approval-card-actions">
                <Link to={`/demand-lines/${r.demand_line_id}/substitution`}>
                  Details
                </Link>
                {r.status === "Pending" && (
                  <>
                    <ConfirmButton
                      disabled={busyId === r.approval_id}
                      label="Approve"
                      confirmLabel="Confirm approve?"
                      onConfirm={() => decide(r.approval_id, true)}
                    />
                    <ConfirmButton
                      className="btn-reject"
                      disabled={busyId === r.approval_id}
                      label="Decline"
                      confirmLabel="Confirm decline?"
                      onConfirm={() => decide(r.approval_id, false)}
                    />
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      <footer className="mor-notes">
        <p>
          Approval grants permission, not steel — it reserves nothing. A
          decision here is the same operation as on the Home card and in the
          Substitution Workspace.
        </p>
      </footer>
    </div>
  );
}
