import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import ConfirmButton from "../components/ConfirmButton";
import Freshness from "../components/Freshness";
import { ApiError, apiGet, apiSend } from "../lib/api";
import { errorMessage } from "./admin/shared";

const STATUSES = ["Pending", "Approved", "Rejected"] as const;
type Status = (typeof STATUSES)[number];

type Approval = {
  id: string;
  customer_name: string;
  well_name: string;
  product_name: string;
  status: Status;
  customer_approved: boolean;
  well_approved: boolean;
  requested_at: string;
  decided_at: string | null;
  note: string | null;
};

type RowState = { customerApproved: boolean; wellApproved: boolean; busy: boolean; error: string | null };

export default function ApprovalQueue() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("status") as Status) || "Pending";

  const [approvals, setApprovals] = useState<Approval[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [rowState, setRowState] = useState<Record<string, RowState>>({});

  const load = () => {
    apiGet<Approval[]>(`/substitution-approvals?status=${tab}`)
      .then((a) => {
        setApprovals(a);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [tab]);

  const setTab = (s: Status) => {
    const next = new URLSearchParams(params);
    next.set("status", s);
    setParams(next);
  };

  const getRow = (id: string): RowState => rowState[id] ?? { customerApproved: false, wellApproved: false, busy: false, error: null };
  const setRow = (id: string, patch: Partial<RowState>) =>
    setRowState((prev) => ({ ...prev, [id]: { ...getRow(id), ...patch } }));

  const decide = (a: Approval) => {
    const row = getRow(a.id);
    setRow(a.id, { busy: true, error: null });
    apiSend("POST", `/substitution-approvals/${a.id}/decide`, {
      customer_approved: row.customerApproved,
      well_approved: row.wellApproved,
    })
      .then(() => {
        setRow(a.id, { busy: false });
        load();
      })
      .catch((e: ApiError | Error) => setRow(a.id, { busy: false, error: errorMessage(e) }));
  };

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Approvals" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Approvals</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>
        {error && <p className="inline-error">{error}</p>}

        <div className="tab-bar">
          {STATUSES.map((s) => (
            <button key={s} type="button" className={`tab ${tab === s ? "active" : ""}`} onClick={() => setTab(s)}>
              {s}
            </button>
          ))}
        </div>

        <div className="tab-panel">
          <table className="admin-table">
            <thead>
              <tr>
                <th>Customer</th>
                <th>Well</th>
                <th>Product</th>
                <th>Requested at</th>
                {tab === "Pending" ? (
                  <>
                    <th>Customer approved</th>
                    <th>Well approved</th>
                    <th></th>
                  </>
                ) : (
                  <th>Decided at</th>
                )}
              </tr>
            </thead>
            <tbody>
              {(!approvals || approvals.length === 0) && (
                <tr>
                  <td colSpan={tab === "Pending" ? 7 : 5}>—</td>
                </tr>
              )}
              {approvals?.map((a) => {
                const row = getRow(a.id);
                return (
                  <tr key={a.id}>
                    <td>{a.customer_name}</td>
                    <td>{a.well_name}</td>
                    <td>{a.product_name}</td>
                    <td className="num">{a.requested_at}</td>
                    {tab === "Pending" ? (
                      <>
                        <td>
                          <label className="checkbox-label">
                            <input
                              type="checkbox"
                              checked={row.customerApproved}
                              onChange={(e) => setRow(a.id, { customerApproved: e.target.checked })}
                            />
                          </label>
                        </td>
                        <td>
                          <label className="checkbox-label">
                            <input
                              type="checkbox"
                              checked={row.wellApproved}
                              onChange={(e) => setRow(a.id, { wellApproved: e.target.checked })}
                            />
                          </label>
                        </td>
                        <td>
                          {row.error && <p className="inline-error">{row.error}</p>}
                          <p className="hint">Any unchecked box decides Rejected.</p>
                          <ConfirmButton
                            label={row.busy ? "Deciding…" : "Decide"}
                            armedLabel="Confirm decision"
                            onConfirm={() => decide(a)}
                          />
                        </td>
                      </>
                    ) : (
                      <td className="num">{a.decided_at ?? "—"}</td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
