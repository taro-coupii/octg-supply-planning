/**
 * Coverage `reason` renderer.
 *
 * Reasons are now long — the SOFT foreign-assignment reason runs to ~330
 * characters — and the actionable half is AT THE END:
 *
 *   "Insufficient FREE on-hand inventory for requested ROS: 6000 of ... is
 *    hard-assigned to another customer's demand line ... -- Release the hard
 *    assignment in Oracle, then re-sync -- this platform cannot create, release
 *    or override a hard reservation ..."
 *
 * So this component never truncates, clamps or ellipsises. Anything that hid the
 * tail would hide precisely the part that tells the planner what to do. Instead
 * the sentence is SPLIT on the separators the backend already uses -- "; " and
 * " -- " -- and each clause is given its own line, so a long reason becomes
 * scannable rather than a wall of text.
 *
 * Two clause kinds get picked out because they are the ones a planner acts on:
 *
 *   * an "OVER-SUBSCRIBED: ..." clause (appended when several PendingApproval
 *     lines chase the same substitute), and
 *   * the trailing action clause, recognised by its imperative opening verb.
 *
 * The full untouched string is also on `title` so a hover or a copy always gets
 * the original text verbatim.
 */

const CLAUSE_SPLIT = /\s--\s|;\s+/g;

/** Clauses that tell the planner what to DO, rather than what is wrong. */
const ACTION_OPENERS = [
  "release",
  "request",
  "obtain",
  "decide",
  "rescope",
  "escalate",
  "remove",
  "map ",
  "re-sync",
  "order",
  "contact",
  "raise",
];

function isActionClause(clause: string) {
  const lower = clause.trim().toLowerCase();
  return ACTION_OPENERS.some((verb) => lower.startsWith(verb));
}

function isOverSubscription(clause: string) {
  return clause.trim().toUpperCase().startsWith("OVER-SUBSCRIBED");
}

export function reasonClauses(reason: string): string[] {
  return reason
    .split(CLAUSE_SPLIT)
    .map((c) => c.trim())
    .filter((c) => c.length > 0);
}

export default function ReasonText({
  reason,
  fallback = "—",
}: {
  reason: string | null;
  /** What to show when there is no reason at all. Never a silent blank. */
  fallback?: string;
}) {
  if (!reason) return <span className="reason-empty">{fallback}</span>;

  const clauses = reasonClauses(reason);

  // A single short clause needs no structure; render it plainly.
  if (clauses.length <= 1) {
    return <span className="reason-text">{reason}</span>;
  }

  return (
    <div className="reason-text" title={reason}>
      {clauses.map((clause, i) => {
        const kind = isOverSubscription(clause)
          ? "reason-clause-oversub"
          : isActionClause(clause)
          ? "reason-clause-action"
          : "reason-clause-detail";
        return (
          <div key={i} className={`reason-clause ${kind}`}>
            {kind === "reason-clause-action" && (
              <span className="reason-action-tag">Action</span>
            )}
            {kind === "reason-clause-oversub" && (
              <span className="reason-oversub-tag">Over-subscribed</span>
            )}
            <span>{clause}</span>
          </div>
        );
      })}
    </div>
  );
}
