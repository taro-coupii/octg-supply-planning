import { verdictClass, verdictLabel, verdictText } from "../lib/verdict";

export default function VerdictChip({ verdict }: { verdict: string | null | undefined }) {
  return (
    <span className={verdictClass(verdict)} title={verdictLabel(verdict)}>
      {verdictText(verdict)}
    </span>
  );
}
