import { verdictClass, verdictLabel } from "../lib/verdict";

export default function VerdictChip({ verdict }: { verdict: string | null | undefined }) {
  return <span className={verdictClass(verdict)}>{verdictLabel(verdict)}</span>;
}
