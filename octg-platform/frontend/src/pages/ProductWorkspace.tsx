import { useEffect, useState } from "react";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import { apiGet } from "../lib/api";
import { nextSortState, sortRows, type SortDir } from "../lib/sort";
import { flattenBuTree, type BusinessUnitNode, type Customer, type FlatBu, type Product } from "./admin/shared";

type SafetyStock = { id: string; business_unit_id: string; product_id: string; quantity: number; unit: string };
type LeadTime = { id: string; business_unit_id: string | null; product_id: string | null; months: number };
type Substitution = { id: string; from_product_id: string; to_product_id: string };
type CustomerRule = { id: string; customer_id: string; technical_substitution_id: string; allowed: boolean };

type SortKey = "name" | "unit_of_measure" | "weight_kg";

// Cross-cutting rule (parent spec breadcrumb/override rulings): users never see raw
// UUIDs — always resolve to a name, or "—" if the id is unknown.
function nameOf<T extends { id: string; name: string }>(items: T[], id: string | null): string {
  if (!id) return "—";
  return items.find((i) => i.id === id)?.name ?? "—";
}

export default function ProductWorkspace() {
  const [products, setProducts] = useState<Product[] | null>(null);
  const [businessUnits, setBusinessUnits] = useState<FlatBu[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [safetyStocks, setSafetyStocks] = useState<SafetyStock[]>([]);
  const [leadTimes, setLeadTimes] = useState<LeadTime[]>([]);
  const [substitutions, setSubstitutions] = useState<Substitution[]>([]);
  const [customerRules, setCustomerRules] = useState<CustomerRule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDir, setSortDir] = useState<SortDir>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const load = () => {
    Promise.all([
      apiGet<Product[]>("/products"),
      apiGet<BusinessUnitNode[]>("/business-units"),
      apiGet<Customer[]>("/customers"),
      apiGet<SafetyStock[]>("/admin/safety-stocks"),
      apiGet<LeadTime[]>("/admin/lead-times"),
      apiGet<Substitution[]>("/admin/substitutions"),
    ])
      .then(([p, bu, c, ss, lt, subs]) => {
        setProducts(p);
        setBusinessUnits(flattenBuTree(bu));
        setCustomers(c);
        setSafetyStocks(ss);
        setLeadTimes(lt);
        setSubstitutions(subs);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  // Customer substitution rules are fetched per-selected-product's relevant
  // substitutions once a product is selected, aggregated across all customers
  // so the detail panel can show "who allows this" without a customer picker.
  useEffect(() => {
    if (!selectedId) {
      setCustomerRules([]);
      return;
    }
    const relevant = substitutions.filter(
      (s) => s.from_product_id === selectedId || s.to_product_id === selectedId
    );
    if (relevant.length === 0 || customers.length === 0) {
      setCustomerRules([]);
      return;
    }
    Promise.all(
      customers.map((c) =>
        apiGet<CustomerRule[]>(`/admin/substitutions/customer-rules?customer_id=${c.id}`)
      )
    )
      .then((lists) => setCustomerRules(lists.flat()))
      .catch((e: Error) => setError(e.message));
  }, [selectedId, substitutions, customers]);

  const toggleSort = (key: SortKey) => {
    if (key !== sortKey) {
      setSortKey(key);
      setSortDir("asc");
    } else {
      setSortDir(nextSortState(sortDir));
    }
  };

  const ariaSortFor = (key: SortKey): "ascending" | "descending" | "none" => {
    if (sortKey !== key || sortDir === null) return "none";
    return sortDir === "asc" ? "ascending" : "descending";
  };

  const rows = products ? sortRows(products, sortKey, sortDir) : [];
  const selected = products?.find((p) => p.id === selectedId) ?? null;

  const detailSafetyStocks = selected ? safetyStocks.filter((s) => s.product_id === selected.id) : [];
  const detailLeadTimes = selected
    ? leadTimes.filter((l) => l.product_id === selected.id || l.product_id === null)
    : [];
  const substitutesTo = selected ? substitutions.filter((s) => s.from_product_id === selected.id) : [];
  const substitutesFrom = selected ? substitutions.filter((s) => s.to_product_id === selected.id) : [];

  const ruleFor = (subId: string) => customerRules.filter((r) => r.technical_substitution_id === subId);

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Products" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Products</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>
        {error && <p className="inline-error">{error}</p>}
        {products === null && !error && <p>Loading…</p>}
        {products !== null && (
          <div style={{ display: "flex", gap: "1.5rem", alignItems: "flex-start" }}>
            <table className="admin-table" style={{ flex: 1 }}>
              <thead>
                <tr>
                  <th aria-sort={ariaSortFor("name")}>
                    <button type="button" onClick={() => toggleSort("name")}>
                      Name
                    </button>
                  </th>
                  <th aria-sort={ariaSortFor("unit_of_measure")}>
                    <button type="button" onClick={() => toggleSort("unit_of_measure")}>
                      Unit
                    </button>
                  </th>
                  <th aria-sort={ariaSortFor("weight_kg")}>
                    <button type="button" onClick={() => toggleSort("weight_kg")}>
                      Weight (kg)
                    </button>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={3}>—</td>
                  </tr>
                )}
                {rows.map((p) => (
                  <tr
                    key={p.id}
                    onClick={() => setSelectedId(p.id)}
                    className={selectedId === p.id ? "selected" : undefined}
                    style={{ cursor: "pointer" }}
                  >
                    <td>{p.name}</td>
                    <td>{p.unit_of_measure ?? "—"}</td>
                    <td className="num">{p.weight_kg ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {selected && (
              <div className="card" style={{ flex: 1, minWidth: "20rem" }}>
                <h2>{selected.name}</h2>
                <p>
                  Unit: {selected.unit_of_measure ?? "—"} &nbsp; Weight:{" "}
                  <span className="num">{selected.weight_kg ?? "—"}</span> kg
                </p>

                <h3>Safety stocks</h3>
                {detailSafetyStocks.length === 0 && <p>—</p>}
                {detailSafetyStocks.length > 0 && (
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>Business unit</th>
                        <th>Quantity</th>
                        <th>Unit</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detailSafetyStocks.map((s) => (
                        <tr key={s.id}>
                          <td>{nameOf(businessUnits, s.business_unit_id)}</td>
                          <td className="num">{s.quantity}</td>
                          <td>{s.unit}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <p className="hint">A business unit with no row here is unset — shown as “—”, not zero.</p>

                <h3>Lead times</h3>
                {detailLeadTimes.length === 0 && <p>—</p>}
                {detailLeadTimes.length > 0 && (
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>Business unit</th>
                        <th>Months</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detailLeadTimes.map((l) => (
                        <tr key={l.id}>
                          <td>{l.business_unit_id ? nameOf(businessUnits, l.business_unit_id) : "(all)"}</td>
                          <td className="num">{l.months}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                <h3>Technical substitutions</h3>
                {substitutesTo.length === 0 && substitutesFrom.length === 0 && <p>—</p>}
                {(substitutesTo.length > 0 || substitutesFrom.length > 0) && (
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>From</th>
                        <th>To</th>
                        <th>Customer rules</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...substitutesTo, ...substitutesFrom].map((s) => (
                        <tr key={s.id}>
                          <td>{nameOf(products ?? [], s.from_product_id)}</td>
                          <td>{nameOf(products ?? [], s.to_product_id)}</td>
                          <td>
                            {ruleFor(s.id).length === 0 && "—"}
                            {ruleFor(s.id).map((r) => (
                              <div key={r.id}>
                                {nameOf(customers, r.customer_id)}: {r.allowed ? "allowed" : "not allowed"}
                              </div>
                            ))}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
