import { useEffect, useState } from "react";
import { api, SafetyStockRow } from "../../api/client";
import LoadError from "../../components/LoadError";

/**
 * Safety stock per product. "Not set" is a real state, rendered as such —
 * an empty input, never a 0 the planner didn't type. Saving 0 is allowed and
 * means exactly "the safety stock IS zero"; clearing removes the setting.
 */
export default function SafetyStockPanel() {
  const [rows, setRows] = useState<SafetyStockRow[]>([]);
  const [note, setNote] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [savedFlash, setSavedFlash] = useState<string | null>(null);

  const load = () =>
    api
      .getSafetyStocks()
      .then((r) => {
        setRows(r.rows);
        setNote(r.note);
        setError(null);
        setDrafts({});
      })
      .catch(setError);

  useEffect(() => {
    load();
  }, []);

  async function save(row: SafetyStockRow) {
    const raw = drafts[row.product_id];
    if (raw === undefined) return;
    try {
      if (raw.trim() === "") {
        if (row.quantity !== null) {
          await api.deleteSafetyStock(row.product_id);
        }
      } else {
        const qty = Number(raw);
        if (!Number.isFinite(qty) || qty < 0) return;
        await api.putSafetyStock(row.product_id, qty);
      }
      setSavedFlash(row.product_id);
      setTimeout(() => setSavedFlash(null), 1500);
      await load();
    } catch (e) {
      setError(e);
    }
  }

  if (error != null) return <LoadError what="safety stocks" error={error} />;

  return (
    <section className="card">
      <h3>Safety stock</h3>
      <p className="exec-note-quiet">{note}</p>
      <div className="table-scroll">
        <table className="admin-table">
          <thead>
            <tr>
              <th>Product</th>
              <th>Unit</th>
              <th>Safety stock</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const draft = drafts[r.product_id];
              const shown =
                draft !== undefined ? draft : r.quantity !== null ? String(r.quantity) : "";
              return (
                <tr key={r.product_id}>
                  <td>{r.product_description ?? r.product_id}</td>
                  <td>{r.unit_of_measure}</td>
                  <td className="num">
                    <input
                      type="number"
                      min={0}
                      value={shown}
                      placeholder="not set"
                      aria-label={`Safety stock for ${r.product_description ?? r.product_id}`}
                      onChange={(e) =>
                        setDrafts((d) => ({ ...d, [r.product_id]: e.target.value }))
                      }
                      style={{ width: 110, textAlign: "right" }}
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      disabled={draft === undefined}
                      onClick={() => save(r)}
                    >
                      {savedFlash === r.product_id ? "Saved" : "Save"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
