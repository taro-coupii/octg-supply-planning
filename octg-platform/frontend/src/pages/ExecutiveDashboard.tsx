import { useEffect, useState, type ReactNode } from "react";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import ScopeChecks, { DEFAULT_SCOPE_STATE, scopeToQuery, type ScopeState } from "../components/ScopeChecks";
import { apiGet } from "../lib/api";
import { errorMessage } from "./admin/shared";
import { verdictText } from "../lib/verdict";

type Block<T> = {
  available: boolean;
  reason?: string | null;
  mt_total: number;
  mt_incomplete: boolean;
} & T;

type DemandTrendBlock = Block<{
  months: { month: string; qty_by_unit: Record<string, number>; mt_total: number; mt_incomplete: boolean }[];
}>;

type CoverageBlock = Block<{
  by_verdict: Record<string, { label: string; count: number; qty_by_unit: Record<string, number> }>;
}>;

type SupplyRiskBlock = Block<{
  items: {
    product_id: string;
    product_name: string;
    bu_id: string;
    unit: string;
    runout_month: string;
    opening_total: number;
  }[];
}>;

type SoftAllocationBlock = Block<{
  by_customer: Record<string, { customer_name: string; qty_by_unit: Record<string, number> }>;
}>;

type InventoryUtilisationBlock = Block<{
  totals_by_unit: Record<string, { allocated: number; surplus: number; obsolete: number }>;
  products: { product_id: string; product_name: string; unit: string; surplus: number; obsolete: number; not_tied: number }[];
}>;

type ExecutiveResp = {
  scope_is_default: boolean;
  status_scope: string[];
  profile_scope: string[];
  warning?: string | null;
  demand_trend: DemandTrendBlock;
  coverage: CoverageBlock;
  supply_risk: SupplyRiskBlock;
  soft_allocation: SoftAllocationBlock;
  inventory_utilisation: InventoryUtilisationBlock;
};

function NativeBreakdown({ qtyByUnit }: { qtyByUnit: Record<string, number> }) {
  const entries = Object.entries(qtyByUnit);
  if (entries.length === 0) return <p className="hint">—</p>;
  return (
    <table className="admin-table nested-table">
      <thead>
        <tr>
          <th>Unit</th>
          <th>Qty</th>
        </tr>
      </thead>
      <tbody>
        {entries.map(([unit, qty]) => (
          <tr key={unit}>
            <td>{unit}</td>
            <td className="num">{qty}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MtHeadline({ mtTotal, mtIncomplete }: { mtTotal: number; mtIncomplete: boolean }) {
  return (
    <div>
      <div className="mt-headline">≈ <span className="num">{mtTotal.toFixed(1)}</span> MT</div>
      {mtIncomplete && <p className="hint">Excludes items without weight</p>}
    </div>
  );
}

function BlockCard({
  title,
  block,
  children,
}: {
  title: string;
  block: { available: boolean; reason?: string | null; mt_total: number; mt_incomplete: boolean };
  children?: ReactNode;
}) {
  return (
    <div className="card exec-block">
      <h2>{title}</h2>
      {!block.available ? (
        <p className="hint">{block.reason ?? "Not available"}</p>
      ) : (
        <>
          <MtHeadline mtTotal={block.mt_total} mtIncomplete={block.mt_incomplete} />
          {children}
        </>
      )}
    </div>
  );
}

export default function ExecutiveDashboard() {
  const [scope, setScope] = useState<ScopeState>(DEFAULT_SCOPE_STATE);
  const [data, setData] = useState<ExecutiveResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  const load = () => {
    apiGet<ExecutiveResp>(`/dashboard/executive${scopeToQuery(scope)}`)
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setError(null);
      })
      // Keep previously-loaded data on screen; only surface the error.
      .catch((e) => setError(errorMessage(e)));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [scope]);

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Executive" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Executive Dashboard</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>

        <ScopeChecks value={scope} onChange={setScope} />

        {error && <p className="inline-error">{error}</p>}

        {data && !data.scope_is_default && (
          <p className="banner-warn">{data.warning ?? "Recomputed read-only — NOT the official stored verdicts"}</p>
        )}
      </div>

      {data && (
        <div className="exec-grid">
          <BlockCard title="Demand trend" block={data.demand_trend}>
            <table className="admin-table nested-table">
              <thead>
                <tr>
                  <th>Month</th>
                  <th>MT</th>
                  <th>Native</th>
                </tr>
              </thead>
              <tbody>
                {data.demand_trend.months.map((m) => (
                  <tr key={m.month}>
                    <td className="num">{m.month}</td>
                    <td className="num">
                      {m.mt_total.toFixed(1)}
                      {m.mt_incomplete && <span title="Excludes items without weight"> *</span>}
                    </td>
                    <td className="num">
                      {Object.entries(m.qty_by_unit)
                        .map(([u, q]) => `${q} ${u}`)
                        .join(", ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </BlockCard>

          <BlockCard title="Coverage" block={data.coverage}>
            <table className="admin-table nested-table">
              <thead>
                <tr>
                  <th>Verdict</th>
                  <th>Count</th>
                  <th>Native</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.coverage.by_verdict).map(([verdict, v]) => (
                  <tr key={verdict}>
                    <td>{v.label || verdictText(verdict)}</td>
                    <td className="num">{v.count}</td>
                    <td className="num">
                      {Object.entries(v.qty_by_unit)
                        .map(([u, q]) => `${q} ${u}`)
                        .join(", ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </BlockCard>

          <BlockCard title="Supply risk" block={data.supply_risk}>
            <table className="admin-table nested-table">
              <thead>
                <tr>
                  <th>Product</th>
                  <th>Runout month</th>
                  <th>Opening</th>
                </tr>
              </thead>
              <tbody>
                {data.supply_risk.items.map((it) => (
                  <tr key={it.product_id}>
                    <td>{it.product_name}</td>
                    <td className="num">{it.runout_month}</td>
                    <td>
                      <span className="num">{it.opening_total}</span> {it.unit}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </BlockCard>

          <BlockCard title="Soft allocation" block={data.soft_allocation}>
            <table className="admin-table nested-table">
              <thead>
                <tr>
                  <th>Customer</th>
                  <th>From-free native</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.soft_allocation.by_customer).map(([cid, c]) => (
                  <tr key={cid}>
                    <td>{c.customer_name}</td>
                    <td className="num">
                      {Object.entries(c.qty_by_unit)
                        .map(([u, q]) => `${q} ${u}`)
                        .join(", ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </BlockCard>

          <BlockCard title="Inventory utilisation" block={data.inventory_utilisation}>
            <NativeBreakdown
              qtyByUnit={Object.fromEntries(
                Object.entries(data.inventory_utilisation.totals_by_unit).map(([u, t]) => [
                  u,
                  t.allocated + t.surplus + t.obsolete,
                ])
              )}
            />
          </BlockCard>
        </div>
      )}
    </div>
  );
}
