// Verdict chip class mapping — 裁定C-1. Red (--bad) is reserved for
// Unrecoverable only; NotEvaluated (no CoverageResult row) is a distinct
// neutral chip, never merged with Covered or counted as 0.
export const VERDICTS = [
  "Covered",
  "CoveredViaSubstitute",
  "PendingApproval",
  "Uncovered",
  "Unrecoverable",
] as const;
export type Verdict = (typeof VERDICTS)[number];

export function verdictClass(verdict: string | null | undefined): string {
  switch (verdict) {
    case "Covered":
      return "verdict-chip verdict-covered";
    case "CoveredViaSubstitute":
      return "verdict-chip verdict-covered-sub";
    case "PendingApproval":
      return "verdict-chip verdict-pending";
    case "Uncovered":
      return "verdict-chip verdict-uncovered";
    case "Unrecoverable":
      return "verdict-chip verdict-unrecoverable";
    default:
      return "verdict-chip verdict-notevaluated";
  }
}

export function verdictLabel(verdict: string | null | undefined): string {
  return verdict ?? "NotEvaluated";
}

export function blockedClass(blockedBy: string | null | undefined): string {
  switch (blockedBy) {
    case "customer":
      return "blocked-badge blocked-customer";
    case "well-approval":
      return "blocked-badge blocked-well-approval";
    case "oracle-release":
      return "blocked-badge blocked-oracle-release";
    default:
      return "";
  }
}

// Screens must never print the raw enum at a person. The verdict is a
// judgement about steel; say it the way a planner would.
const VERDICT_TEXT: Record<string, string> = {
  Covered: "Covered",
  CoveredViaSubstitute: "Covered via substitute",
  PendingApproval: "Pending approval",
  Uncovered: "Uncovered",
  Unrecoverable: "Unrecoverable",
  NotEvaluated: "Not evaluated",
};

export function verdictText(verdict: string | null | undefined): string {
  const key = verdict ?? "NotEvaluated";
  return VERDICT_TEXT[key] ?? key;
}
