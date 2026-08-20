import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import ConfirmButton from "../components/ConfirmButton";
import Freshness from "../components/Freshness";
import { ApiError, apiGet, apiSend } from "../lib/api";
import { UNITS } from "../lib/enums";
import { writeParam } from "../lib/urlState";
import { flattenBuTree, type BusinessUnitNode, type Customer, type FlatBu, type Product } from "./admin/shared";

type OnHand = { id: string; business_unit_id: string; product_id: string; quantity: number; unit: string; source_system: string };
type Assignment = { id: string; business_unit_id: string; product_id: string; customer_id: string; quantity: number; unit: string; reference: string | null };
type OnOrder = { id: string; business_unit_id: string; product_id: string; quantity: number; unit: string; expected_date: string | null; booking_status: string };
type CompanyInventoryData = { on_hand: OnHand[]; assignments: Assignment[]; on_order: OnOrder[] };

// Cross-cutting rule (parent spec breadcrumb/override rulings): users never see raw
// UUIDs — always resolve to a name, or "—" if the id is unknown.
function nameOf<T extends { id: string; name: string }>(items: T[], id: string | null): string {
  if (!id) return "—";
  return items.find((i) => i.id === id)?.name ?? "—";
}

const TABS = [
  { key: "on-hand", label: "On hand" },
  { key: "assignments", label: "Oracle assignments" },
  { key: "on-order", label: "On order" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

export default function CompanyInventory() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as TabKey | null) ?? TABS[0].key;

  const [data, setData] = useState<CompanyInventoryData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  const [businessUnits, setBusinessUnits] = useState<FlatBu[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);

  const [buId, setBuId] = useState("");
  const [productId, setProductId] = useState("");
  const [quantity, setQuantity] = useState("");
  const [unit, setUnit] = useState<string>(UNITS[0]);
  const [formError, setFormError] = useState<string | null>(null);

  const load = () => {
    apiGet<CompanyInventoryData>("/company-inventory")
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  useEffect(() => {
    Promise.all([
      apiGet<BusinessUnitNode[]>("/business-units"),
      apiGet<Product[]>("/products"),
      apiGet<Customer[]>("/customers"),
    ])
      .then(([bu, p, c]) => {
        setBusinessUnits(flattenBuTree(bu));
        setProducts(p);
        setCustomers(c);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const setTab = (key: TabKey) => setParams(writeParam(params, "tab", key));

  const addOnHand = () => {
    if (!buId || !productId || quantity === "") return;
    apiSend<OnHand>("POST", "/company-inventory/on-hand", {
      business_unit_id: buId,
      product_id: productId,
      quantity: Number(quantity),
      unit,
    })
      .then(() => {
        setBuId("");
        setProductId("");
        setQuantity("");
        setFormError(null);
        load();
      })
      .catch((e: ApiError | Error) => setFormError(errorMessage(e)));
  };

  const patchOnHand = (row: OnHand, newQty: number) => {
    apiSend<OnHand>("PATCH", `/company-inventory/on-hand/${row.id}`, { quantity: newQty, unit: row.unit })
      .then(load)
      .catch((e: ApiError | Error) => setFormError(errorMessage(e)));
  };

  const deleteOnHand = (row: OnHand) => {
    apiSend("DELETE", `/company-inventory/on-hand/${row.id}`)
      .then(load)
      .catch((e: ApiError | Error) => setFormError(errorMessage(e)));
  };

  const datedOrders = data ? data.on_order.filter((o) => o.expected_date !== null) : [];
  const undatedOrders = data ? data.on_order.filter((o) => o.expected_date === null) : [];

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Company Inventory" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Company Inventory</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>
        {error && <p className="inline-error">{error}</p>}
        <div className="tab-bar">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              className={tab === t.key ? "tab active" : "tab"}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="tab-panel">
          {tab === "on-hand" && data && (
            <div>
              {formError && <p className="inline-error">{formError}</p>}
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Business unit</th>
                    <th>Product</th>
                    <th>Quantity</th>
                    <th>Unit</th>
                    <th>Source</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {data.on_hand.length === 0 && (
                    <tr>
                      <td colSpan={6}>—</td>
                    </tr>
                  )}
                  {data.on_hand.map((row) => (
                    <tr key={row.id}>
                      <td>{nameOf(businessUnits, row.business_unit_id)}</td>
                      <td>{nameOf(products, row.product_id)}</td>
                      <td>
                        {row.source_system === "oracle" ? (
                          row.quantity
                        ) : (
                          <input
                            type="number"
                            min={0}
                            defaultValue={row.quantity}
                            onBlur={(e) => {
                              const v = Number(e.target.value);
                              if (v !== row.quantity) patchOnHand(row, v);
                            }}
                          />
                        )}
                      </td>
                      <td>{row.unit}</td>
                      <td>{row.source_system}</td>
                      <td>
                        {/* COMPROMISE[C-03R]: oracle-sourced rows are read-only — the server
                            rejects mutations with 409, so the maintenance controls are hidden here. */}
                        {row.source_system !== "oracle" && (
                          <ConfirmButton
                            label="Delete"
                            armedLabel="Confirm?"
                            onConfirm={() => deleteOnHand(row)}
                          />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h3>Add manual on-hand row</h3>
              <div className="admin-add-row">
                <select value={buId} onChange={(e) => setBuId(e.target.value)}>
                  <option value="">Business unit…</option>
                  {businessUnits.map((bu) => (
                    <option key={bu.id} value={bu.id}>
                      {bu.name}
                    </option>
                  ))}
                </select>
                <select value={productId} onChange={(e) => setProductId(e.target.value)}>
                  <option value="">Product…</option>
                  {products.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
                <input
                  type="number"
                  min={0}
                  placeholder="Quantity"
                  value={quantity}
                  onChange={(e) => setQuantity(e.target.value)}
                />
                <select value={unit} onChange={(e) => setUnit(e.target.value)}>
                  {UNITS.map((u) => (
                    <option key={u} value={u}>
                      {u}
                    </option>
                  ))}
                </select>
                <button type="button" onClick={addOnHand}>
                  Add
                </button>
              </div>
            </div>
          )}

          {tab === "assignments" && data && (
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Business unit</th>
                  <th>Product</th>
                  <th>Customer</th>
                  <th>Quantity</th>
                  <th>Unit</th>
                  <th>Reference</th>
                </tr>
              </thead>
              <tbody>
                {data.assignments.length === 0 && (
                  <tr>
                    <td colSpan={6}>—</td>
                  </tr>
                )}
                {data.assignments.map((row) => (
                  <tr key={row.id}>
                    <td>{nameOf(businessUnits, row.business_unit_id)}</td>
                    <td>{nameOf(products, row.product_id)}</td>
                    <td>{nameOf(customers, row.customer_id)}</td>
                    <td>{row.quantity}</td>
                    <td>{row.unit}</td>
                    <td>{row.reference ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {tab === "on-order" && data && (
            <div>
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Business unit</th>
                    <th>Product</th>
                    <th>Quantity</th>
                    <th>Unit</th>
                    <th>Expected date</th>
                    <th>Booking status</th>
                  </tr>
                </thead>
                <tbody>
                  {datedOrders.length === 0 && (
                    <tr>
                      <td colSpan={6}>—</td>
                    </tr>
                  )}
                  {datedOrders.map((row) => (
                    <tr key={row.id}>
                      <td>{nameOf(businessUnits, row.business_unit_id)}</td>
                      <td>{nameOf(products, row.product_id)}</td>
                      <td>{row.quantity}</td>
                      <td>{row.unit}</td>
                      <td>{row.expected_date}</td>
                      <td>{row.booking_status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h3>Date TBD</h3>
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Business unit</th>
                    <th>Product</th>
                    <th>Quantity</th>
                    <th>Unit</th>
                    <th>Booking status</th>
                  </tr>
                </thead>
                <tbody>
                  {undatedOrders.length === 0 && (
                    <tr>
                      <td colSpan={5}>—</td>
                    </tr>
                  )}
                  {undatedOrders.map((row) => (
                    <tr key={row.id}>
                      <td>{nameOf(businessUnits, row.business_unit_id)}</td>
                      <td>{nameOf(products, row.product_id)}</td>
                      <td>{row.quantity}</td>
                      <td>{row.unit}</td>
                      <td>{row.booking_status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
