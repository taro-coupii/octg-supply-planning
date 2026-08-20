import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import { apiGet } from "../lib/api";
import { writeParam } from "../lib/urlState";
import { errorMessage, type Customer, type Product } from "./admin/shared";

type StripPoint = { month: string; closing_balance: number };
type Markers = { order_deadline: string | null; physical_runout: string | null; safety_breach: string | null };
type Requirement = { need_month: string; qty: number; unit: string; ex_mill_month: string; overdue: boolean };
type MorProduct = {
  product: string;
  unit: string;
  business_unit_id: string;
  business_unit_name: string;
  strip: StripPoint[];
  markers: Markers;
  requirements: Requirement[];
  safety_stock: number | null;
  unavailable_reason?: string | null;
};
type Scope = { statuses: string[]; profiles: string[] };
type MorResp = { rows: MorProduct[]; scope: Scope; unavailable_reason?: string | null };

const HORIZONS = [12, 24, 36];

function markerFor(month: string, markers: Markers): "deadline" | "runout" | "safety" | null {
  if (markers.physical_runout === month) return "runout";
  if (markers.safety_breach === month) return "safety";
  if (markers.order_deadline === month) return "deadline";
  return null;
}

export default function MaterialOrderReq() {
  const [params, setParams] = useSearchParams();
  const horizon = Number(params.get("horizon") ?? "12") || 12;
  const customerId = params.get("customer_id") ?? "";

  const [customers, setCustomers] = useState<Customer[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [data, setData] = useState<MorResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  useEffect(() => {
    Promise.all([apiGet<Customer[]>("/customers"), apiGet<Product[]>("/products")])
      .then(([c, p]) => {
        setCustomers(c);
        setProducts(p);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const productName = (id: string): string => products.find((p) => p.id === id)?.name ?? "…";

  const load = () => {
    const qs = new URLSearchParams();
    qs.set("horizon", String(horizon));
    if (customerId) qs.set("customer_id", customerId);
    apiGet<MorResp>(`/mrp/order-requirements?${qs.toString()}`)
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setError(null);
      })
      // Keep previously-loaded data on screen; only surface the error.
      .catch((e) => setError(errorMessage(e)));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [horizon, customerId]);

  const setHorizon = (value: number) => setParams(writeParam(params, "horizon", String(value)));
  const setCustomer = (value: string) => setParams(writeParam(params, "customer_id", value || null));

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Order Reqs" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Material Order Requirements</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>

        {data && (
          <p className="hint">
            Computed under Administration default scope: {data.scope.statuses.join(", ")} /{" "}
            {data.scope.profiles.join(", ")}
          </p>
        )}

        {error && <p className="inline-error">{error}</p>}

        <div className="admin-add-row">
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
          <fieldset>
            <legend>Customer</legend>
            <select value={customerId} onChange={(e) => setCustomer(e.target.value)}>
              <option value="">All customers</option>
              {customers.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </fieldset>
        </div>

        <p className="mor-legend">
          Legend: cell shade reflects closing balance sign only —{" "}
          <span className="mor-cell mor-cell-ok">ok</span> (balance ≥ 0),{" "}
          <span className="mor-cell mor-cell-deficit">deficit</span> (balance &lt; 0). Markers on a month cell —
          ▲ order deadline, ▼ physical runout (red), ▽ safety-stock breach (amber) — safety-stock status is shown
          only by the ▽ marker, never by cell color. When the order deadline falls before the visible strip (the
          order should already have been placed), it cannot land on a cell — it is shown instead as an{" "}
          <span className="overdue-strip-chip">▲ overdue</span> chip in the product header.
        </p>

        {data?.unavailable_reason && <p className="inline-error">{data.unavailable_reason}</p>}

        {data && data.rows.length === 0 && !data.unavailable_reason && <p className="hint">—</p>}

        {data &&
          (() => {
            // BU name is shown only once >1 BU appears among the visible
            // rows — a single-BU view stays uncluttered, a multi-BU view
            // (e.g. "All customers") disambiguates products shared across
            // BUs (product name alone would otherwise look duplicated).
            const showBu = new Set(data.rows.map((r) => r.business_unit_id)).size > 1;
            return data.rows.map((row) => {
              const firstStripMonth = row.strip[0]?.month;
              const deadlineOverdue =
                !!row.markers.order_deadline &&
                !!firstStripMonth &&
                row.markers.order_deadline < firstStripMonth;
              return (
            <div key={`${row.business_unit_id}:${row.product}`} className="card" style={{ margin: "12px 0" }}>
              <h3>
                {productName(row.product)} <span className="hint">({row.unit})</span>
                {showBu && <span className="hint"> — {row.business_unit_name}</span>}
                {deadlineOverdue && <span className="overdue-strip-chip">▲ overdue</span>}
              </h3>
              {row.unavailable_reason ? (
                <p className="hint">{row.unavailable_reason}</p>
              ) : (
                <>
                  <div className="mor-strip">
                    {row.strip.map((pt) => {
                      const marker = markerFor(pt.month, row.markers);
                      const deficit = pt.closing_balance < 0;
                      return (
                        <div
                          key={pt.month}
                          className={`mor-cell ${deficit ? "mor-cell-deficit" : "mor-cell-ok"}`}
                          title={pt.month}
                        >
                          {marker === "deadline" && <span className="mor-marker mor-marker-deadline">▲</span>}
                          {marker === "runout" && <span className="mor-marker mor-marker-runout">▼</span>}
                          {marker === "safety" && <span className="mor-marker mor-marker-safety">▽</span>}
                          <div>{pt.month}</div>
                          <div>{pt.closing_balance}</div>
                        </div>
                      );
                    })}
                  </div>

                  <div className="table-scroll">
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>Need month</th>
                        <th>Quantity</th>
                        <th>Ex-mill month</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {row.requirements.length === 0 && (
                        <tr>
                          <td colSpan={4}>—</td>
                        </tr>
                      )}
                      {row.requirements.map((r) => (
                        <tr key={r.need_month}>
                          <td>{r.need_month}</td>
                          <td>
                            {r.qty} {r.unit}
                          </td>
                          <td>{r.ex_mill_month}</td>
                          <td>{r.overdue && <span className="overdue-chip">Overdue</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  </div>
                </>
              )}
            </div>
              );
            });
          })()}
      </div>
    </div>
  );
}
