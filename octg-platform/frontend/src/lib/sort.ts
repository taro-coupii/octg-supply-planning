import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * Client-side column sorting for table screens.
 *
 * One shared implementation so every screen behaves the same way: click a
 * header to sort ascending, click again for descending, click a third time to
 * return to the SERVER order — which stays the default because the server's
 * order is usually meaningful (earliest-ROS on the coverage grid, urgency on
 * the order-requirements grid) and a screen must never silently discard it.
 *
 * `key` addresses a comparable value per row via the `accessors` map. Null /
 * undefined values always sort LAST regardless of direction, so "no date" can
 * never masquerade as "earliest".
 */

export type SortDirection = "asc" | "desc";

export interface SortState {
  key: string | null;
  direction: SortDirection;
}

type Accessor<Row> = (row: Row) => string | number | null | undefined;

export function useSortable<Row>(
  rows: Row[],
  accessors: Record<string, Accessor<Row>>,
  // When set, the sort state lives in the URL as ?<urlKey>=<column>.<dir> --
  // the same bookmarkability contract the filters already honor (QA
  // 2026-08-14: a bookmarked view reproduced every filter but silently lost
  // the sort). Server order = no param, matching "no param, server default".
  urlKey?: string
) {
  const [searchParams, setSearchParams] = useSearchParams();
  const fromUrl = (): SortState => {
    const raw = urlKey ? searchParams.get(urlKey) : null;
    if (!raw) return { key: null, direction: "asc" };
    const [key, direction] = raw.split(".");
    return {
      key: key && accessors[key] ? key : null,
      direction: direction === "desc" ? "desc" : "asc",
    };
  };
  const [sort, setSort] = useState<SortState>(fromUrl);

  const toggle = (key: string) => {
    setSort((prev) => {
      const next: SortState =
        prev.key !== key
          ? { key, direction: "asc" }
          : prev.direction === "asc"
            ? { key, direction: "desc" }
            : { key: null, direction: "asc" }; // third click: server order
      if (urlKey) {
        setSearchParams(
          (p) => {
            if (next.key) p.set(urlKey, `${next.key}.${next.direction}`);
            else p.delete(urlKey);
            return p;
          },
          { replace: true }
        );
      }
      return next;
    });
  };

  const sorted = useMemo(() => {
    if (sort.key === null) return rows;
    const accessor = accessors[sort.key];
    if (!accessor) return rows;
    const factor = sort.direction === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = accessor(a);
      const vb = accessor(b);
      const aMissing = va === null || va === undefined || va === "";
      const bMissing = vb === null || vb === undefined || vb === "";
      if (aMissing && bMissing) return 0;
      if (aMissing) return 1; // missing always last
      if (bMissing) return -1;
      if (typeof va === "number" && typeof vb === "number") {
        return (va - vb) * factor;
      }
      return String(va).localeCompare(String(vb)) * factor;
    });
    // accessors is a stable literal per screen; rows/sort drive the memo.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, sort]);

  return { sorted, sort, toggle };
}

/** The ▲/▼ indicator for a sortable header. */
export function sortIndicator(sort: SortState, key: string): string {
  if (sort.key !== key) return "";
  return sort.direction === "asc" ? " ▲" : " ▼";
}
