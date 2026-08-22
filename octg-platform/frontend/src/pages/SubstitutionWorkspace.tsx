import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import ConfirmButton from "../components/ConfirmButton";
import Freshness from "../components/Freshness";
import { ApiError, apiGet, apiSend } from "../lib/api";
import { blockedClass, verdictText } from "../lib/verdict";
import { errorMessage } from "./admin/shared";

type Candidate = {
  substitution_id: string;
  to_product: string;
  technical_ok: boolean;
  customer_rule_allowed: boolean;
  free_qty_by_unit: Record<string, number>;
  hard_assigned_qty: number;
  verdict_if_applied: string;
  blocked_by: "customer" | "well-approval" | "oracle-release" | null;
};

// Row-level state for the two-step "Request approval" flow: a candidate
// with hard_assigned_qty>0, no existing request, and not oracle-blocked
// must show the HARD ALLOCATION warning before the actual submit.
type RowState = { warned: boolean; submitted: boolean; busy: boolean; error: string | null };

function blockedText(blockedBy: Candidate["blocked_by"]): string {
  switch (blockedBy) {
    case "customer":
      return "Blocked — customer rule";
    case "well-approval":
      return "Blocked — well approval pending";
    case "oracle-release":
      return "Blocked — Oracle release required";
    default:
      return "";
  }
}

export default function SubstitutionWorkspace() {
  const [params] = useSearchParams();
  const demandLineId = params.get("demand_line_id") ?? "";

  const [candidates, setCandidates] = useState<Candidate[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [rowState, setRowState] = useState<Record<string, RowState>>({});

  const load = () => {
    if (!demandLineId) {
      setCandidates(null);
      return;
    }
    apiGet<Candidate[]>(`/substitution/candidates?demand_line_id=${demandLineId}`)
      .then((c) => {
        setCandidates(c);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [demandLineId]);

  const getRow = (id: string): RowState => rowState[id] ?? { warned: false, submitted: false, busy: false, error: null };
  const setRow = (id: string, patch: Partial<RowState>) =>
    setRowState((prev) => ({ ...prev, [id]: { ...getRow(id), ...patch } }));

  const requiresHardWarning = (c: Candidate) => c.hard_assigned_qty > 0 && c.blocked_by !== "oracle-release";

  const submitRequest = (c: Candidate) => {
    setRow(c.substitution_id, { busy: true, error: null });
    apiSend("POST", "/substitution-approvals", {
      demand_line_id: demandLineId,
      technical_substitution_id: c.substitution_id,
    })
      .then(() => {
        setRow(c.substitution_id, { busy: false, submitted: true });
      })
      .catch((e: ApiError | Error) => setRow(c.substitution_id, { busy: false, error: errorMessage(e) }));
  };

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Substitution" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Substitution</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>
        {error && <p className="inline-error">{error}</p>}
        {!demandLineId && <p className="hint">Select a demand line (via a well's coverage action) to see candidates.</p>}

        {demandLineId && candidates && (
          <table className="admin-table">
            <thead>
              <tr>
                <th>To product</th>
                <th>Customer rule</th>
                <th>Free qty by unit</th>
                <th>Hard-assigned qty</th>
                <th>Verdict if applied</th>
                <th>Blocked by</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {candidates.length === 0 && (
                <tr>
                  <td colSpan={7}>—</td>
                </tr>
              )}
              {candidates.map((c) => {
                const row = getRow(c.substitution_id);
                return (
                  <tr key={c.substitution_id}>
                    <td>{c.to_product}</td>
                    <td>{c.customer_rule_allowed ? "Allowed" : "Not allowed"}</td>
                    <td className="num">
                      {Object.keys(c.free_qty_by_unit).length === 0
                        ? "—"
                        : Object.entries(c.free_qty_by_unit)
                            .map(([u, q]) => `${q} ${u}`)
                            .join(", ")}
                    </td>
                    <td className="num">{c.hard_assigned_qty}</td>
                    <td>{verdictText(c.verdict_if_applied)}</td>
                    <td>
                      {c.blocked_by ? (
                        <span className={blockedClass(c.blocked_by)}>{blockedText(c.blocked_by)}</span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {row.submitted ? (
                        <span className="hint">Request submitted</span>
                      ) : c.blocked_by ? (
                        <span className="hint">Not eligible</span>
                      ) : (
                        <>
                          {row.error && <p className="inline-error">{row.error}</p>}
                          {requiresHardWarning(c) && !row.warned ? (
                            <button type="button" onClick={() => setRow(c.substitution_id, { warned: true })}>
                              Request approval
                            </button>
                          ) : (
                            <>
                              {requiresHardWarning(c) && row.warned && (
                                <p className="banner-warn">
                                  HARD ALLOCATION INVOLVED — this request does not touch Oracle reservations; it is
                                  backed by free stock only.
                                </p>
                              )}
                              <ConfirmButton
                                label={row.busy ? "Submitting…" : "Request approval"}
                                armedLabel="Confirm request"
                                onConfirm={() => submitRequest(c)}
                              />
                            </>
                          )}
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
