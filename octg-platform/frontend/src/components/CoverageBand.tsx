import { VERDICTS } from "../lib/verdict";

// The signature instrument: a well's verdict mix rendered as one segmented
// paint band, the way steel grade is marked on real pipe. Segments are ordered
// best-to-worst and sized by real proportion, so scanning a grid vertically
// shows the yard's state as a stripe pattern. This is data, not decoration.
const SEGMENTS = [...VERDICTS, "NotEvaluated"] as const;

const SEG_CLASS: Record<string, string> = {
  Covered: "cov-seg cov-seg-covered",
  CoveredViaSubstitute: "cov-seg cov-seg-covered-sub",
  PendingApproval: "cov-seg cov-seg-pending",
  Uncovered: "cov-seg cov-seg-uncovered",
  Unrecoverable: "cov-seg cov-seg-unrecoverable",
  NotEvaluated: "cov-seg cov-seg-notevaluated",
};

export default function CoverageBand({ rollup }: { rollup: Record<string, number> }) {
  const present = SEGMENTS.filter((k) => (rollup[k] ?? 0) > 0);
  const total = present.reduce((sum, k) => sum + (rollup[k] ?? 0), 0);
  if (total === 0) {
    return <span className="hint">—</span>;
  }
  const label = present.map((k) => `${k}: ${rollup[k]}`).join(", ");
  return (
    <span className="cov-band" role="img" aria-label={label} title={label}>
      {present.map((k) => (
        <span
          key={k}
          className={SEG_CLASS[k]}
          style={{ width: `${((rollup[k] ?? 0) / total) * 100}%` }}
        />
      ))}
    </span>
  );
}
