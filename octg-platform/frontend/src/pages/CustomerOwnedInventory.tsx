import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import { ApiError, apiDownload, apiGet, apiUpload } from "../lib/api";
import { writeParam } from "../lib/urlState";
import type { Customer, Product } from "./admin/shared";

type Position = { id: string; product_id: string; quantity: number; unit: string };
type CustomerOwnedData = { has_uploaded: boolean; uploaded_at: string | null; positions: Position[] };
type RowError = { row: number; errors: string[] };

function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

// Cross-cutting rule (parent spec breadcrumb/override rulings): users never see raw
// UUIDs — always resolve to a name, or "—" if the id is unknown.
function nameOf(items: Product[], id: string | null): string {
  if (!id) return "—";
  return items.find((i) => i.id === id)?.name ?? "—";
}

export default function CustomerOwnedInventory() {
  const [params, setParams] = useSearchParams();
  const customerId = params.get("customer") ?? "";

  const [customers, setCustomers] = useState<Customer[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [data, setData] = useState<CustomerOwnedData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [uploadErrors, setUploadErrors] = useState<RowError[] | string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    Promise.all([apiGet<Customer[]>("/customers"), apiGet<Product[]>("/products")])
      .then(([c, p]) => {
        setCustomers(c);
        setProducts(p);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const load = () => {
    if (!customerId) {
      setData(null);
      return;
    }
    apiGet<CustomerOwnedData>(`/customer-owned-inventory?customer_id=${customerId}`)
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, [customerId]);

  const setCustomer = (id: string) => setParams(writeParam(params, "customer", id || null));

  const upload = () => {
    const file = fileInput.current?.files?.[0];
    if (!file || !customerId) return;
    setUploading(true);
    const form = new FormData();
    form.append("file", file);
    apiUpload<CustomerOwnedData>(`/customer-owned-inventory/upload?customer_id=${customerId}`, form)
      .then((d) => {
        setData(d);
        setFetchedAt(new Date());
        setUploadErrors(null);
        setError(null);
        if (fileInput.current) fileInput.current.value = "";
      })
      .catch((e: ApiError | Error) => {
        const detail = e instanceof ApiError ? (e.body as { detail?: unknown } | null)?.detail : undefined;
        if (Array.isArray(detail)) {
          setUploadErrors(detail as RowError[]);
        } else {
          setUploadErrors(errorMessage(e));
        }
      })
      .finally(() => setUploading(false));
  };

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Customer-Owned Inventory" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Customer-Owned Inventory</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={load} />
        </div>
        {error && <p className="inline-error">{error}</p>}

        <select value={customerId} onChange={(e) => setCustomer(e.target.value)}>
          <option value="">Select customer…</option>
          {customers.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>

        {!customerId && <p className="hint">Choose a customer to see their uploaded positions.</p>}

        {customerId && (
          <div className="tab-panel">
            <div className="upload-row">
              <button
                type="button"
                className="link-btn"
                onClick={() =>
                  apiDownload("/customer-owned-inventory/template", "customer-owned-inventory-template.xlsx").catch((e: Error) =>
                    setError(e.message)
                  )
                }
              >
                Download template
              </button>
              <input type="file" ref={fileInput} accept=".xlsx" />
              <button type="button" onClick={upload} disabled={uploading}>
                {uploading ? "Uploading…" : "Upload"}
              </button>
            </div>

            {uploadErrors && (
              <div className="inline-error">
                {typeof uploadErrors === "string" ? (
                  <p>{uploadErrors}</p>
                ) : (
                  <ul>
                    {uploadErrors.map((re) => (
                      <li key={re.row}>
                        Row {re.row}: {re.errors.join("; ")}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            {data && !data.has_uploaded && (
              <p className="banner-warn">No data uploaded — 0 is not the same as no data.</p>
            )}
            {data && data.has_uploaded && (
              <p className="hint">Last uploaded: {data.uploaded_at}</p>
            )}

            {data && (
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Product</th>
                    <th>Quantity</th>
                    <th>Unit</th>
                  </tr>
                </thead>
                <tbody>
                  {data.positions.length === 0 && (
                    <tr>
                      <td colSpan={3}>—</td>
                    </tr>
                  )}
                  {data.positions.map((p) => (
                    <tr key={p.id}>
                      <td>{nameOf(products, p.product_id)}</td>
                      <td>{p.quantity}</td>
                      <td>{p.unit}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
