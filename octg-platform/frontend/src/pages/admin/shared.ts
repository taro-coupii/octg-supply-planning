// Shared types + helpers for the Administration tabs.

export type BusinessUnitNode = { id: string; name: string; children: BusinessUnitNode[] };
export type FlatBu = { id: string; name: string; parent_id: string | null; depth: number };
export type Product = { id: string; name: string; unit_of_measure: string; weight_kg: number | null };
export type Customer = { id: string; name: string; business_unit_id: string | null };

export function flattenBuTree(nodes: BusinessUnitNode[], parentId: string | null = null, depth = 0): FlatBu[] {
  const out: FlatBu[] = [];
  for (const n of nodes) {
    out.push({ id: n.id, name: n.name, parent_id: parentId, depth });
    out.push(...flattenBuTree(n.children, n.id, depth + 1));
  }
  return out;
}

export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}
