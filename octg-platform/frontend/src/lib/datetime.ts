// Timestamps are read, not parsed: show minutes, drop the microseconds and the
// "T" that make an ISO string look like machine output.
export function formatStamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// A min/max pair collapses to one stamp when both land in the same minute.
export function formatStampRange(
  from: string | null | undefined,
  to: string | null | undefined,
): string {
  const a = formatStamp(from);
  const b = formatStamp(to);
  if (a === "—" && b === "—") return "—";
  return a === b ? a : `${a} – ${b}`;
}
