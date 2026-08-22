import { CoverageStatus } from "../api/client";

export default function CoverageBadge({ status }: { status: CoverageStatus }) {
  if (!status) return <span className="badge">No demand</span>;
  return <span className={`badge badge-${status}`}>{status}</span>;
}
