import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import { apiDownload, apiGet } from "../lib/api";
import { writeParam } from "../lib/urlState";
import type { Product } from "./admin/shared";
import { errorMessage } from "./admin/shared";

type Balance = { company: number; owned: number };
type Month = {
  month: string;
  opening: Balance;
  receipts_booked: number;
  receipts_recommended: number;
  issues: number;
  closing: Balance;
};
type RunoutMonths = { baseline: string | null; with_recommended: string | null; on_order: string | null };
type ProductSummary = {
  product: string;
  unit: string;
  opening: Balance;
  runout_months: RunoutMonths;
  months: Month[];
  on_order_undated: number;
};
type Scope = { statuses: string[]; profiles: string[] };
type MrpSummaryResp = { rows: ProductSummary[]; scope: Scope };

const HORIZONS = [12, 24, 36];

export default function MrpSummary() {
  const [params, setParams] = useSearchParams();
  const horizon = Number(params.get("horizon") ?? "12") || 12;

  const [products, setProducts] = useState<Product[]>([]);
  const [data, setData] = useState<MrpSummaryResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  useEffect(() => {
    apiGet<Product[]>("/products")
      .then(setProducts)
      .catch((e: Error) => setError(e.message));
  }, []);

  const load = () => {
    apiGet<MrpSummaryResp>(`/mrp/summary?horizon=${horizon}`)
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setError(null);
      })
      // Keep previously-loaded data on screen; only surface the error.
      .catch((e) => setError(errorMessage(e)));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [horizon]);

  const setHorizon = (value: number) => {
    setParams(writeParam(params, "horizon", String(value)));
  };

  const productName = (id: string): string => products.find((p) => p.id === id)?.name ?? "…";

  const [exporting, setExporting] = useState(false);
  const exportXlsx = () => {
    setExporting(true);
    apiDownload(`/mrp/export?horizon=${horizon}`, `mrp-summary-${horizon}mo.xlsx`)
      .catch((e: Error) => setError(e.message))
      .finally(() => setExporting(false));
  };

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "MRP" }]} />
      <div className="card">
        <div className="page-header">
          <h1>MRP Summary</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>

        {data && (
          <p className="hint">
            Computed under Administration default scope: {data.scope.statuses.join(", ")} /{" "}
            {data.scope.profiles.join(", ")}
          </p>
        )}

        {error && <p className="inline-error">{error}</p>}

        <div className="filters">
          <label>
            Horizon (months)
            <select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))}>
              {HORIZONS.map((h) => (
                <option key={h} value={h}>
                  {h}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="confirm-btn" onClick={exportXlsx} disabled={exporting}>
            {exporting ? "Exporting…" : "Export xlsx"}
          </button>
        </div>

        {data && (
          <div className="table-scroll">
          <table className="admin-table">
            <thead>
              <tr>
                <th>Product</th>
                <th>Unit</th>
                <th>Runout — baseline</th>
                <th>Runout — with recommended</th>
                <th>Runout — on order</th>
                <th>On order (undated)</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.length === 0 && (
                <tr>
                  <td colSpan={6}>—</td>
                </tr>
              )}
              {data.rows.map((row) => (
                <tr key={row.product}>
                  <td>
                    <Link to={`/mrp/items/${row.product}`}>{productName(row.product)}</Link>
                  </td>
                  <td>{row.unit}</td>
                  <td className="num">{row.runout_months.baseline ?? "—"}</td>
                  <td className="num">{row.runout_months.with_recommended ?? "—"}</td>
                  <td className="num">{row.runout_months.on_order ?? "—"}</td>
                  <td className="num">{row.on_order_undated ? `${row.on_order_undated} ${row.unit}` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>
    </div>
  );
}
