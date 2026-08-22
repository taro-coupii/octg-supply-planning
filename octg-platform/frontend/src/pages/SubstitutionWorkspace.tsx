import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import ConfirmButton from "../components/ConfirmButton";
import {
  api,
  errorText,
  SubstitutionBlockingLayer,
  SubstitutionCandidate,
} from "../api/client";
import Breadcrumbs from "../components/Breadcrumbs";
import LoadError from "../components/LoadError";

/**
 * `oracle` is its own tone on purpose.
 *
 * "hard-assigned-elsewhere" and "insufficient-inventory" are both inventory
 * blocks, but they send the planner to completely different places:
 *
 *   insufficient-inventory   the steel is NOT THERE  -> the mill (place an order)
 *   hard-assigned-elsewhere  the steel IS THERE, spoken for by another
 *                            customer's demand line -> Oracle (a person removes
 *                            the assignment; this platform cannot release it)
 *
 * Painting them the same red would tell a planner to order steel that is already
 * sitting in the yard. So absent stock keeps the red, and the Oracle-release case
 * gets amber plus its own wording and its own explanatory panel.
 */
type LayerTone = "ok" | "bad" | "wait" | "idle" | "oracle";

function LayerRow({
  n,
  name,
  tone,
  value,
  note,
}: {
  n: number;
  name: string;
  tone: LayerTone;
  value: string;
  note: string;
}) {
  return (
    <div className="layer-row">
      <span className={`layer-dot layer-dot-${tone}`} />
      <span className="layer-name">
        Layer {n} · {name}
      </span>
      <span className={`layer-value layer-value-${tone}`}>{value}</span>
      <span className="layer-note">{note}</span>
    </div>
  );
}

function productLabel(description: string | null, id: string) {
  return description ?? id;
}

// The from side is a property of the LINE, so the API stamps its description on
// every candidate. The id remains only as the last resort for a product row that
// has no description at all -- shortened so it does not dominate the row, but it
// should never be what a planner actually sees.
function shortId(id: string) {
  return `${id.slice(0, 8)}…`;
}

/** Human label for the blocking layer, keyed to where the planner must go. */
const BLOCK_LABEL: Record<Exclude<SubstitutionBlockingLayer, null>, string> = {
  customer: "Customer rule",
  "well-approval": "Well approval",
  "hard-assigned-elsewhere": "Blocked — Oracle release required",
  "insufficient-inventory": "Insufficient inventory",
};

function qty(n: number) {
  return n.toLocaleString();
}

/* ---------------- Inventory layer ---------------- */

/**
 * Layer 4 covers "does the steel exist and can this line have it?" — which is
 * two questions, and the answers have different fixes. See LayerTone above.
 */
function inventoryLayer(c: SubstitutionCandidate): {
  tone: LayerTone;
  value: string;
  note: string;
} {
  if (c.blocking_layer === "hard-assigned-elsewhere") {
    return {
      tone: "oracle",
      value: "⚑ Exists, but assigned elsewhere",
      note:
        `${qty(c.hard_assigned_qty)} exists in this Business Unit but is ` +
        "hard-assigned to another customer's demand line. The steel is here; " +
        "it is not free. A mill order is the wrong answer.",
    };
  }
  if (c.blocking_layer === "insufficient-inventory") {
    return {
      tone: "bad",
      value: "✗ Not enough steel",
      note:
        `Only ${qty(c.available_qty)} free against ${qty(c.required_qty)} ` +
        "required. This quantity genuinely does not exist here — the gap has to " +
        "be sourced.",
    };
  }
  if (c.available_qty >= c.required_qty) {
    return {
      tone: "ok",
      value: "✓ Sufficient",
      note: `${qty(c.available_qty)} free covers the ${qty(
        c.required_qty
      )} required.`,
    };
  }
  return {
    tone: "idle",
    value: "Not the blocking layer",
    note: "An earlier layer blocks first, so inventory has not been decided.",
  };
}

/* ---------------- Over-subscription ---------------- */

/**
 * A PendingApproval no longer reserves the substitute quantity — the platform
 * must not create reservations. That removed the mechanism that used to make
 * scarcity implicit, so scarcity now has to be STATED. This banner is that
 * statement and it is deliberately loud: without it, a planner approving two
 * pending lines against the same 4000 has no way to know one of them must fail.
 *
 * `over_subscription_note` is a complete sentence written for a planner. It is
 * rendered verbatim, with the arithmetic repeated as figures beside it.
 */
function OverSubscriptionBanner({ c }: { c: SubstitutionCandidate }) {
  const short = c.pending_required_qty - c.available_qty;
  return (
    <div className="oversub" role="alert">
      <div className="oversub-head">
        <span className="oversub-tag">Over-subscribed</span>
        <span className="oversub-headline">
          Approving every pending line cannot succeed
        </span>
      </div>
      <p className="oversub-note">
        {c.over_subscription_note ??
          `${c.pending_line_count} demand lines are awaiting approval for this ` +
            `substitute, together requiring ${qty(c.pending_required_qty)} ` +
            `against ${qty(c.available_qty)} available.`}
      </p>
      <div className="oversub-figures">
        <span>
          Pending lines <strong>{c.pending_line_count}</strong>
        </span>
        <span>
          Together requiring <strong>{qty(c.pending_required_qty)}</strong>
        </span>
        <span>
          Available <strong>{qty(c.available_qty)}</strong>
        </span>
        {short > 0 && (
          <span className="oversub-short">
            Short by <strong>{qty(short)}</strong>
          </span>
        )}
      </div>
      <p className="oversub-why">
        Approval does not reserve steel — this platform never creates a
        reservation. So these lines are all still competing for the same
        quantity. Decide which line gets it, or obtain more.
      </p>
    </div>
  );
}

/* ---------------- Approval-by date ---------------- */

/**
 * Answers "suggest approval by date based on mill supply leadtime — if
 * rejection comes in past this date, we cannot recover the demand by mill
 * supply."
 *
 * The API does not serve a `still_recoverable` flag (see the note on
 * `SubstitutionCandidate` in client.ts — the backend engine computes it
 * internally but never copies it onto the response), so urgency is derived
 * here from whether `approval_by_date` has already passed. When it has, mill
 * fallback is not merely narrowing — it is ALREADY IMPOSSIBLE, and that case
 * gets a visually distinct, more urgent treatment than the ordinary
 * "reject by DATE, still time" case.
 */
function ApprovalByDate({ c }: { c: SubstitutionCandidate }) {
  if (!c.approval_by_date_available) {
    return (
      <div className="approval-by approval-by-unavailable">
        <span className="approval-by-tag">Approval-by date</span>
        <p style={{ margin: "6px 0 0" }}>{c.approval_by_date_reason}</p>
      </div>
    );
  }

  const isPast = c.approval_by_date
    ? c.approval_by_date < new Date().toISOString().slice(0, 10)
    : false;

  return (
    <div className={`approval-by ${isPast ? "approval-by-urgent" : "approval-by-normal"}`}>
      <span className="approval-by-tag">
        {isPast ? "Mill fallback already impossible" : "Approval-by date"}
      </span>
      {c.approval_by_date && (
        <span className="approval-by-date">{formatApprovalDate(c.approval_by_date)}</span>
      )}
      <p style={{ margin: "6px 0 0" }}>{c.approval_by_date_reason}</p>
    </div>
  );
}

// `approval_by_date` is a plain date (no time), so parsing it with `new
// Date(iso)` treats it as UTC midnight and can print the wrong calendar day
// in a timezone behind UTC. Parse the y/m/d components directly instead.
function formatApprovalDate(iso: string) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/* ---------------- Candidate ---------------- */

function CandidateCard({
  candidate,
  onChanged,
}: {
  candidate: SubstitutionCandidate;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const status = candidate.approval_status;

  const wellTone: LayerTone =
    status === "Approved"
      ? "ok"
      : status === "Rejected"
      ? "bad"
      : status === "Pending"
      ? "wait"
      : "idle";

  const inv = inventoryLayer(candidate);
  const oracleBlocked = candidate.blocking_layer === "hard-assigned-elsewhere";

  // The head badge distinguishes the three outcomes, not two: usable, blocked by
  // something this platform can move, and blocked by an Oracle assignment that
  // only a person can move.
  const headBadge = candidate.usable
    ? { cls: "badge-Covered", text: "Usable" }
    : oracleBlocked
    ? { cls: "badge-oracle-block", text: "Blocked — Oracle release required" }
    : { cls: "badge-Uncovered", text: "Blocked" };

  return (
    <div className={`sub-card${oracleBlocked ? " sub-card-oracle" : ""}`}>
      <div className="sub-card-head">
        <div className="sub-pair">
          <span className="sub-from" title={candidate.from_product_id}>
            {candidate.from_product_description ??
              shortId(candidate.from_product_id)}
          </span>
          <span className="change-arrow">&rarr;</span>
          <span className="sub-to">
            {productLabel(candidate.product.description, candidate.to_product_id)}
          </span>
        </div>
        <span className={`badge ${headBadge.cls}`}>{headBadge.text}</span>
      </div>

      {candidate.over_subscribed && <OverSubscriptionBanner c={candidate} />}

      {/*
        Rendered whenever the LINE is PendingApproval (the backend's
        `applicable` gate keys off the line's coverage verdict, not off
        whether an approval row exists yet — see approval_by_date_reason for
        the "not applicable" wording when it truly does not apply).
      */}
      <ApprovalByDate c={candidate} />

      <div className="layer-chain">
        <LayerRow
          n={1}
          name="Technical"
          tone="ok"
          value="✓ Cleared"
          note="Engineering allows this pair — candidates only appear when technically valid."
        />
        <LayerRow
          n={2}
          name="Customer"
          tone={candidate.customer_allowed ? "ok" : "bad"}
          value={candidate.customer_allowed ? "✓ Allowed" : "✗ Not allowed"}
          note={
            candidate.customer_allowed
              ? "Customer rule permits this substitution pair."
              : "No customer rule permits this pair (silence is not permission)."
          }
        />
        <LayerRow
          n={3}
          name="Well approval"
          tone={wellTone}
          value={status ?? "Not yet requested"}
          note="Approval applies to this specific demand line only. Approval grants permission, not steel — it reserves nothing."
        />
        <LayerRow
          n={4}
          name="Inventory"
          tone={inv.tone}
          value={inv.value}
          note={inv.note}
        />
      </div>

      {/*
        Available and hard-assigned sit side by side deliberately. "Available 0"
        on its own reads as "there is none", which is false when 5000 is sitting
        in the yard against someone else's line.
      */}
      <div className="sub-qty">
        <span>
          Free / available <strong>{qty(candidate.available_qty)}</strong>
        </span>
        {candidate.hard_assigned_qty > 0 && (
          <>
            <span className="change-arrow">+</span>
            <span
              className="qty-assigned"
              title="Physically present in this Business Unit but hard-assigned to another customer's demand line, so unavailable to this line."
            >
              Hard-assigned elsewhere{" "}
              <strong>{qty(candidate.hard_assigned_qty)}</strong>
            </span>
          </>
        )}
        <span className="change-arrow">/</span>
        <span>
          Required <strong>{qty(candidate.required_qty)}</strong>
        </span>
        {oracleBlocked ? (
          <span className="qty-oracle">
            the steel exists — it is not free
          </span>
        ) : candidate.available_qty >= candidate.required_qty ? (
          <span className="qty-ok">sufficient inventory</span>
        ) : (
          <span className="qty-short">insufficient inventory</span>
        )}
      </div>

      {candidate.blocking_layer && (
        <div
          className={`sub-blocking${
            oracleBlocked ? " sub-blocking-oracle" : ""
          }`}
        >
          <span className="sub-blocking-label">Blocked at</span>{" "}
          {BLOCK_LABEL[candidate.blocking_layer]}
          <span className="sub-blocking-key"> ({candidate.blocking_layer})</span>
        </div>
      )}

      {/*
        The action comes from the server for all four layers. Nothing here
        composes its own — a UI-authored action string is how the screen and the
        engine end up disagreeing.
      */}
      {candidate.recommended_action && (
        <div
          className={`sub-action${oracleBlocked ? " sub-action-oracle" : ""}`}
        >
          <span className="sub-action-label">Recommended action</span>
          <span className="sub-action-text">{candidate.recommended_action}</span>
        </div>
      )}

      {/*
        Submission alert: the request is still allowed (priority stays
        customer > well-approval > oracle-release), but a planner filing a
        proposal against a product that carries a hard assignment elsewhere
        must know BEFORE submitting — approval grants permission, not steel,
        and the reserved parcel will not be freed by this request.
      */}
      {status === null && !oracleBlocked && candidate.hard_assigned_qty > 0 && (
        <div className="sub-hard-warning" role="alert">
          <span className="sub-hard-warning-label">Hard allocation involved</span>
          {qty(candidate.hard_assigned_qty)} of this substitute is hard-assigned
          to another customer&apos;s demand line in Oracle. This request does not
          touch that reservation — only the free {qty(candidate.available_qty)}{" "}
          backs it, and if that moves first the approval lands without steel.
        </div>
      )}

      <div className="sub-actions">
        {/*
          No "Request approval" button while an Oracle assignment is the block:
          approval would not make the steel free, and offering it invites a
          planner to work the wrong queue.
        */}
        {status === null && !oracleBlocked && (
          <button
            disabled={busy}
            onClick={() =>
              run(() =>
                api.requestSubstitutionApproval(candidate.demand_line_id, {
                  from_product_id: candidate.from_product_id,
                  to_product_id: candidate.to_product_id,
                })
              )
            }
          >
            {busy
              ? "Requesting..."
              : candidate.recommended_action ?? "Request approval"}
          </button>
        )}
        {status === null && oracleBlocked && (
          <span className="sub-settled">
            Nothing to request here — the assignment has to be removed in Oracle
            first, then re-synced.
          </span>
        )}
        {status === "Pending" && candidate.approval_id && (
          <>
            {/* Two-step: the decision is FINAL (409 on re-decision). */}
            <ConfirmButton
              disabled={busy}
              label={busy ? "Working..." : "Approve"}
              confirmLabel="Confirm approve?"
              onConfirm={() =>
                run(() => api.decideSubstitutionApproval(candidate.approval_id!, true))
              }
            />
            <ConfirmButton
              className="btn-reject"
              disabled={busy}
              label="Reject"
              confirmLabel="Confirm reject?"
              onConfirm={() =>
                run(() => api.decideSubstitutionApproval(candidate.approval_id!, false))
              }
            />
          </>
        )}
        {(status === "Approved" || status === "Rejected") && (
          <span className="sub-settled">Approval {status.toLowerCase()} — no action needed.</span>
        )}
      </div>

      {error && <div className="form-error">{error}</div>}
    </div>
  );
}

export default function SubstitutionWorkspace() {
  const { demandLineId } = useParams<{ demandLineId: string }>();
  const [candidates, setCandidates] = useState<SubstitutionCandidate[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  const reload = () => {
    if (!demandLineId) return;
    api
      .getSubstitutionCandidates(demandLineId)
      .then((c) => {
        setCandidates(c);
        setError(null);
      })
      .catch((e) => setError(e));
  };

  useEffect(reload, [demandLineId]);

  const subTrail = [
    { label: "Demand lines", to: "/demand" },
    { label: "Substitution" },
  ];

  if (error)
    return (
      <div>
        <Breadcrumbs trail={subTrail} />
        <LoadError what="substitution candidates" error={error} />
      </div>
    );
  if (!candidates)
    return (
      <div>
        <Breadcrumbs trail={subTrail} />
        <p>Loading...</p>
      </div>
    );

  const oversubscribed = candidates.filter((c) => c.over_subscribed);

  return (
    <div>
      <Breadcrumbs trail={subTrail} />
      <h1>Substitution Workspace</h1>
      <p>
        A substitute is only usable when all three approval layers clear{" "}
        <em>and</em> the steel is both present and free. Those last two are not
        the same thing.
      </p>

      {oversubscribed.length > 0 && (
        <p className="sub-oversub-lead">
          One or more substitutes below are over-subscribed: more pending demand
          is chasing them than exists. Approval never reserves steel, so these
          lines are still competing.
        </p>
      )}

      {candidates.length === 0 ? (
        <div className="card">
          <div className="empty">No technically-valid substitutes for this demand line.</div>
        </div>
      ) : (
        <div className="sub-list">
          {candidates.map((c) => (
            <CandidateCard key={c.to_product_id} candidate={c} onChanged={reload} />
          ))}
        </div>
      )}
    </div>
  );
}
