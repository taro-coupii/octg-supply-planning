import { useEffect, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import { apiGet } from "../lib/api";
import { DEMAND_PROFILES, DEMAND_STATUSES } from "../lib/enums";
import { writeParam } from "../lib/urlState";
import type { Customer, Product } from "./admin/shared";

type DemandLine = {
  id: string;
  well_id: string;
  well_name: string;
  customer_id: string;
  customer_name: string;
  product_id: string;
  product_name: string;
  quantity: number;
  unit: string;
  ros_date: string;
  profile: string;
  overdue: boolean;
};

type DemandLinesPage = { total: number; page: number; page_size: number; items: DemandLine[] };

const PAGE_SIZE = 25;

// The 7 URL-persisted filters per spec §API /demand/lines.
const FILTER_KEYS = ["customer", "well", "product", "status", "profile", "ros_from", "ros_to"] as const;

export default function DemandList() {
  const [params, setParams] = useSearchParams();
  const customerId = params.get("customer") ?? "";
  const wellId = params.get("well") ?? "";
  const productId = params.get("product") ?? "";
  const status = params.get("status") ?? "";
  const profile = params.get("profile") ?? "";
  const rosFrom = params.get("ros_from") ?? "";
  const rosTo = params.get("ros_to") ?? "";
  const page = Number(params.get("page") ?? "1") || 1;

  const [customers, setCustomers] = useState<Customer[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [data, setData] = useState<DemandLinesPage | null>(null);
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

  const load = () => {
    const qs = new URLSearchParams();
    qs.set("page", String(page));
    qs.set("page_size", String(PAGE_SIZE));
    if (customerId) qs.set("customer_id", customerId);
    if (wellId) qs.set("well_id", wellId);
    if (productId) qs.set("product_id", productId);
    if (status) qs.set("status", status);
    if (profile) qs.set("profile", profile);
    if (rosFrom) qs.set("ros_from", rosFrom);
    if (rosTo) qs.set("ros_to", rosTo);

    apiGet<DemandLinesPage>(`/demand/lines?${qs.toString()}`)
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [customerId, wellId, productId, status, profile, rosFrom, rosTo, page]);

  const setFilter = (key: (typeof FILTER_KEYS)[number] | "page", value: string) => {
    let next = writeParam(params, key, value || null);
    if (key !== "page") next = writeParam(next, "page", null);
    setParams(next);
  };

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Demand" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Demand</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>
        {error && <p className="inline-error">{error}</p>}

        <fieldset>
          <legend>Filters</legend>
          <div className="admin-add-row">
            <select value={customerId} onChange={(e) => setFilter("customer", e.target.value)}>
              <option value="">All customers</option>
              {customers.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            <input
              placeholder="Well ID"
              value={wellId}
              onChange={(e) => setFilter("well", e.target.value)}
              style={{ width: "9rem" }}
            />
            <select value={productId} onChange={(e) => setFilter("product", e.target.value)}>
              <option value="">All products</option>
              {products.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
            <select value={status} onChange={(e) => setFilter("status", e.target.value)}>
              <option value="">All statuses</option>
              {DEMAND_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <select value={profile} onChange={(e) => setFilter("profile", e.target.value)}>
              <option value="">All profiles</option>
              {DEMAND_PROFILES.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
            <label>
              ROS from{" "}
              <input type="date" value={rosFrom} onChange={(e) => setFilter("ros_from", e.target.value)} />
            </label>
            <label>
              ROS to <input type="date" value={rosTo} onChange={(e) => setFilter("ros_to", e.target.value)} />
            </label>
          </div>
        </fieldset>

        {data && (
          <table className="admin-table">
            <thead>
              <tr>
                <th>Well</th>
                <th>Customer</th>
                <th>Product</th>
                <th>Quantity</th>
                <th>ROS Date</th>
                <th>Profile</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.items.length === 0 && (
                <tr>
                  <td colSpan={7}>—</td>
                </tr>
              )}
              {data.items.map((line) => (
                <tr key={line.id}>
                  <td>
                    <Link to={`/wells/${line.well_id}`}>{line.well_name}</Link>
                  </td>
                  <td>{line.customer_name}</td>
                  <td>{line.product_name}</td>
                  <td>
                    {line.quantity} {line.unit}
                  </td>
                  <td>{line.ros_date}</td>
                  <td>{line.profile}</td>
                  <td>{line.overdue && <span className="banner-warn">Overdue</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {data && (
          <div className="upload-row">
            <button type="button" disabled={page <= 1} onClick={() => setFilter("page", String(page - 1))}>
              Previous
            </button>
            <span className="hint">
              Page {data.page} of {totalPages} ({data.total} total)
            </span>
            <button
              type="button"
              disabled={page >= totalPages}
              onClick={() => setFilter("page", String(page + 1))}
            >
              Next
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
