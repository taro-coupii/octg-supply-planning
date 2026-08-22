import { useEffect, useState } from "react";
import ConfirmButton from "../../components/ConfirmButton";
import { ApiError, apiGet, apiSend } from "../../lib/api";
import { UNITS } from "../../lib/enums";
import { errorMessage, type FlatBu, type Product } from "./shared";

type SafetyStock = { id: string; business_unit_id: string; product_id: string; quantity: number; unit: string };

// Cross-cutting rule (parent spec breadcrumb/override rulings): users never see raw
// UUIDs — always resolve to a name, or "—" if the id is unknown.
function name<T extends { id: string; name: string }>(items: T[], id: string): string {
  return items.find((i) => i.id === id)?.name ?? "—";
}

export default function SafetyStocksTab({ businessUnits, products }: { businessUnits: FlatBu[]; products: Product[] }) {
  const [rows, setRows] = useState<SafetyStock[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [buId, setBuId] = useState("");
  const [productId, setProductId] = useState("");
  const [quantity, setQuantity] = useState("");
  const [unit, setUnit] = useState<string>(UNITS[0]);

  const load = () => {
    apiGet<SafetyStock[]>("/admin/safety-stocks")
      .then((r) => {
        setRows(r);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  const upsert = () => {
    if (!buId || !productId || quantity === "") return;
    apiSend("PUT", "/admin/safety-stocks", [
      { business_unit_id: buId, product_id: productId, quantity: Number(quantity), unit },
    ])
      .then(() => {
        setBuId("");
        setProductId("");
        setQuantity("");
        load();
      })
      .catch((e: ApiError | Error) => setError(errorMessage(e)));
  };

  const clear = (row: SafetyStock) => {
    // spec §不変条件2: quantity:null returns the row to unset (deletes it) —
    // never send 0, that would mean an explicit zero.
    apiSend("PUT", "/admin/safety-stocks", [
      { business_unit_id: row.business_unit_id, product_id: row.product_id, quantity: null, unit: row.unit },
    ])
      .then(load)
      .catch((e: ApiError | Error) => setError(errorMessage(e)));
  };

  if (rows === null && !error) return <p>Loading…</p>;

  return (
    <div>
      {error && <p className="inline-error">{error}</p>}
      <p className="hint">A business unit / product pair with no row is unset — shown as “—”, not zero.</p>
      {rows && (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Business unit</th>
              <th>Product</th>
              <th>Quantity</th>
              <th>Unit</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={5}>—</td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{name(businessUnits, r.business_unit_id)}</td>
                <td>{name(products, r.product_id)}</td>
                <td className="num">{r.quantity}</td>
                <td>{r.unit}</td>
                <td>
                  <ConfirmButton label="Clear (unset)" armedLabel="Confirm?" onConfirm={() => clear(r)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
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
        <button type="button" onClick={upsert}>
          Set
        </button>
      </div>
    </div>
  );
}
