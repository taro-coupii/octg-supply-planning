import { useEffect, useState } from "react";
import ConfirmButton from "../../components/ConfirmButton";
import { ApiError, apiGet, apiSend } from "../../lib/api";
import { errorMessage, type Customer, type Product } from "./shared";

type Substitution = { id: string; from_product_id: string; to_product_id: string };
type CustomerRule = { id: string; customer_id: string; technical_substitution_id: string; allowed: boolean };

// Cross-cutting rule (parent spec breadcrumb/override rulings): users never see raw
// UUIDs — always resolve to a name, or "—" if the id is unknown.
function productName(products: Product[], id: string): string {
  return products.find((p) => p.id === id)?.name ?? "—";
}

export default function SubstitutionsTab({ products, customers }: { products: Product[]; customers: Customer[] }) {
  const [subs, setSubs] = useState<Substitution[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fromId, setFromId] = useState("");
  const [toId, setToId] = useState("");
  const [customerId, setCustomerId] = useState("");
  const [rules, setRules] = useState<CustomerRule[] | null>(null);
  const [rulesError, setRulesError] = useState<string | null>(null);

  const loadSubs = () => {
    apiGet<Substitution[]>("/admin/substitutions")
      .then((s) => {
        setSubs(s);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(loadSubs, []);

  useEffect(() => {
    if (!customerId) {
      setRules(null);
      return;
    }
    apiGet<CustomerRule[]>(`/admin/substitutions/customer-rules?customer_id=${customerId}`)
      .then((r) => {
        setRules(r);
        setRulesError(null);
      })
      .catch((e: Error) => setRulesError(e.message));
  }, [customerId]);

  const create = () => {
    if (!fromId || !toId) return;
    apiSend("POST", "/admin/substitutions", { from_product_id: fromId, to_product_id: toId })
      .then(() => {
        setFromId("");
        setToId("");
        loadSubs();
      })
      .catch((e: ApiError | Error) => setError(errorMessage(e)));
  };

  const remove = (id: string) => {
    apiSend("DELETE", `/admin/substitutions/${id}`)
      .then(loadSubs)
      .catch((e: ApiError | Error) => setError(errorMessage(e)));
  };

  const setAllowed = (subId: string, allowed: boolean) => {
    if (!customerId) return;
    apiSend<CustomerRule[]>(
      "PUT",
      `/admin/substitutions/customer-rules?customer_id=${customerId}`,
      [{ technical_substitution_id: subId, allowed }]
    )
      .then((r) => {
        setRules(r);
        setRulesError(null);
      })
      .catch((e: ApiError | Error) => setRulesError(errorMessage(e)));
  };

  if (subs === null && !error) return <p>Loading…</p>;

  return (
    <div>
      {error && <p className="inline-error">{error}</p>}
      {subs && (
        <table className="admin-table">
          <thead>
            <tr>
              <th>From</th>
              <th>To</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {subs.map((s) => (
              <tr key={s.id}>
                <td>{productName(products, s.from_product_id)}</td>
                <td>{productName(products, s.to_product_id)}</td>
                <td>
                  <ConfirmButton label="Delete" armedLabel="Confirm?" onConfirm={() => remove(s.id)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="admin-add-row">
        <select value={fromId} onChange={(e) => setFromId(e.target.value)}>
          <option value="">From product…</option>
          {products.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select value={toId} onChange={(e) => setToId(e.target.value)}>
          <option value="">To product…</option>
          {products.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <button type="button" onClick={create}>
          Add substitution
        </button>
      </div>

      <h3>Customer rules</h3>
      <select value={customerId} onChange={(e) => setCustomerId(e.target.value)}>
        <option value="">Select customer…</option>
        {customers.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
      {rulesError && <p className="inline-error">{rulesError}</p>}
      {customerId && subs && (
        <table className="admin-table">
          <thead>
            <tr>
              <th>From</th>
              <th>To</th>
              <th>Allowed for this customer</th>
            </tr>
          </thead>
          <tbody>
            {subs.map((s) => {
              const rule = rules?.find((r) => r.technical_substitution_id === s.id);
              return (
                <tr key={s.id}>
                  <td>{productName(products, s.from_product_id)}</td>
                  <td>{productName(products, s.to_product_id)}</td>
                  <td>
                    <input
                      type="checkbox"
                      checked={rule?.allowed ?? false}
                      onChange={(e) => setAllowed(s.id, e.target.checked)}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
