import { useEffect, useState } from "react";
import { ApiError, apiGet, apiSend } from "../../lib/api";
import { errorMessage, type FlatBu, type Product } from "./shared";

type LeadTime = { id?: string; business_unit_id: string | null; product_id: string | null; months: number };

export default function LeadTimesTab({ businessUnits, products }: { businessUnits: FlatBu[]; products: Product[] }) {
  const [rows, setRows] = useState<LeadTime[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = () => {
    apiGet<LeadTime[]>("/admin/lead-times")
      .then((r) => {
        setRows(r);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  const update = (idx: number, patch: Partial<LeadTime>) => {
    setRows((prev) => (prev ? prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)) : prev));
  };

  const addRow = () => {
    setRows((prev) => [...(prev ?? []), { business_unit_id: null, product_id: null, months: 1 }]);
  };

  const removeRow = (idx: number) => {
    setRows((prev) => (prev ? prev.filter((_, i) => i !== idx) : prev));
  };

  const save = () => {
    if (!rows) return;
    setSaving(true);
    apiSend<LeadTime[]>(
      "PUT",
      "/admin/lead-times",
      rows.map((r) => ({ business_unit_id: r.business_unit_id, product_id: r.product_id, months: r.months }))
    )
      .then((r) => {
        setRows(r);
        setError(null);
      })
      .catch((e: ApiError | Error) => setError(errorMessage(e)))
      .finally(() => setSaving(false));
  };

  if (rows === null && !error) return <p>Loading…</p>;

  return (
    <div>
      {error && <p className="inline-error">{error}</p>}
      <p className="hint">
        Blank BU / product = applies to all. Most specific row wins (BU+product &gt; product &gt; BU &gt; default).
      </p>
      {rows && (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Business unit</th>
              <th>Product</th>
              <th>Months</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, idx) => (
              <tr key={r.id ?? `new-${idx}`}>
                <td>
                  <select
                    value={r.business_unit_id ?? ""}
                    onChange={(e) => update(idx, { business_unit_id: e.target.value || null })}
                  >
                    <option value="">(all)</option>
                    {businessUnits.map((bu) => (
                      <option key={bu.id} value={bu.id}>
                        {bu.name}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <select
                    value={r.product_id ?? ""}
                    onChange={(e) => update(idx, { product_id: e.target.value || null })}
                  >
                    <option value="">(all)</option>
                    {products.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <input
                    type="number"
                    min={1}
                    value={r.months}
                    onChange={(e) => update(idx, { months: Number(e.target.value) })}
                  />
                </td>
                <td>
                  <button type="button" onClick={() => removeRow(idx)}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="admin-add-row">
        <button type="button" onClick={addRow}>
          Add row
        </button>
        <button type="button" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save all"}
        </button>
      </div>
    </div>
  );
}
