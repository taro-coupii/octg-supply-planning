export type SortDir = "asc" | "desc" | null;

export function nextSortState(current: SortDir): SortDir {
  if (current === null) return "asc";
  if (current === "asc") return "desc";
  return null;
}

function compare(a: unknown, b: unknown): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b));
}

export function sortRows<T>(rows: T[], key: keyof T, dir: SortDir): T[] {
  if (dir === null) return rows.slice();
  const nonNull = rows.filter((r) => r[key] != null);
  const nulls = rows.filter((r) => r[key] == null);
  nonNull.sort((x, y) => compare(x[key], y[key]));
  if (dir === "desc") nonNull.reverse();
  return [...nonNull, ...nulls];
}
