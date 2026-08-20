import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import { apiGet } from "../lib/api";
import { writeParam } from "../lib/urlState";
import { errorMessage, type Customer, type Product } from "./admin/shared";

type Well = { id: string; customer_id: string; name: string };

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
type DemandLine = {
  id: string;
  customer_id: string;
  well_id: string;
  quantity: number;
  unit: string;
  ros_date: string;
  profile: string;
};
type ProductItem = {
  product: string;
  unit: string;
  opening: Balance;
  runout_months: RunoutMonths;
  months: Month[];
  on_order_undated: number;
  lines: DemandLine[];
};
type Scope = { statuses: string[]; profiles: string[] };
type MrpItemResp = { row: ProductItem; scope: Scope };

const HORIZONS = [12, 24, 36];

export default function MrpByItem() {
  const { id } = useParams<{ id: string }>();
  const [params, setParams] = useSearchParams();
  const horizon = Number(params.get("horizon") ?? "12") || 12;

  const [data, setData] = useState<MrpItemResp | null>(null);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [wells, setWells] = useState<Well[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  useEffect(() => {
    Promise.all([apiGet<Customer[]>("/customers"), apiGet<Well[]>("/wells"), apiGet<Product[]>("/products")])
      .then(([c, w, p]) => {
        setCustomers(c);
        setWells(w);
        setProducts(p);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const load = () => {
    if (!id) return;
    apiGet<MrpItemResp>(`/mrp/items/${id}?horizon=${horizon}`)
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setError(null);
      })
      // Keep previously-loaded data on screen; only surface the error.
      .catch((e) => setError(errorMessage(e)));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [id, horizon]);

  const setHorizon = (value: number) => {
    setParams(writeParam(params, "horizon", String(value)));
  };

  // Convention (spec §4.7): while the entity name is still loading, breadcrumb
  // shows "…" rather than a raw UUID.
  const productLabel = data ? products.find((p) => p.id === data.row.product)?.name ?? "…" : "…";

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "MRP", to: "/mrp" }, { label: productLabel }]} />
      <div className="card">
        <div className="page-header">
          <h1>MRP — {productLabel}</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>

        {data && (
          <p className="hint">
            Computed under Administration default scope: {data.scope.statuses.join(", ")} /{" "}
            {data.scope.profiles.join(", ")}
          </p>
        )}

        {error && <p className="inline-error">{error}</p>}

        <fieldset>
          <legend>Horizon (months)</legend>
          <select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))}>
            {HORIZONS.map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>
        </fieldset>

        {data && (
          <>
            <p>
              Runout — baseline: <strong>{data.row.runout_months.baseline ?? "—"}</strong> &nbsp; with
              recommended: <strong>{data.row.runout_months.with_recommended ?? "—"}</strong> &nbsp; on order:{" "}
              <strong>{data.row.runout_months.on_order ?? "—"}</strong>
            </p>
            <p className="hint">
              On order (undated, not in monthly ledger):{" "}
              {data.row.on_order_undated ? `${data.row.on_order_undated} ${data.row.unit}` : "—"}
            </p>

            <div className="table-scroll">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Month</th>
                  <th>Opening (company)</th>
                  <th>Opening (owned)</th>
                  <th>Receipts booked</th>
                  <th>Receipts recommended</th>
                  <th>Issues</th>
                  <th>Closing (company)</th>
                  <th>Closing (owned)</th>
                </tr>
              </thead>
              <tbody>
                {data.row.months.length === 0 && (
                  <tr>
                    <td colSpan={8}>—</td>
                  </tr>
                )}
                {data.row.months.map((m) => (
                  <tr key={m.month}>
                    <td>{m.month}</td>
                    <td>{m.opening.company}</td>
                    <td>{m.opening.owned}</td>
                    <td className="receipt-booked">{m.receipts_booked}</td>
                    <td className="receipt-recommended">{m.receipts_recommended}</td>
                    <td>{m.issues}</td>
                    <td>{m.closing.company}</td>
                    <td>{m.closing.owned}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>

            <h3>Demand lines in scope</h3>
            {data.row.lines.length === 0 && <p className="hint">—</p>}
            {data.row.lines.length > 0 && (
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Well</th>
                    <th>Customer</th>
                    <th>Quantity</th>
                    <th>ROS date</th>
                    <th>Profile</th>
                  </tr>
                </thead>
                <tbody>
                  {data.row.lines.map((l) => (
                    <tr key={l.id}>
                      <td>
                        <Link to={`/wells/${l.well_id}`}>
                          {wells.find((w) => w.id === l.well_id)?.name ?? "…"}
                        </Link>
                      </td>
                      <td>{customers.find((c) => c.id === l.customer_id)?.name ?? "…"}</td>
                      <td>
                        {l.quantity} {l.unit}
                      </td>
                      <td>{l.ros_date}</td>
                      <td>{l.profile}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </div>
  );
}
