import { useEffect, useRef, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import ConfirmButton from "../components/ConfirmButton";
import Freshness from "../components/Freshness";
import { ApiError, apiDownload, apiGet, apiSend, apiUpload } from "../lib/api";
import { writeParam } from "../lib/urlState";
import { errorMessage, type Customer } from "./admin/shared";

type Conflict = {
  well_name: string;
  existing_line_count: number;
  staged_line_count: number;
  existing_qty_by_unit: Record<string, number>;
  staged_qty_by_unit: Record<string, number>;
};

type ImportRecord = {
  id: string;
  customer_id: string;
  filename: string;
  status: string;
  conflicts: Conflict[];
};

type ApplyResult = { applied_wells: string[]; created_wells: string[] };

type RowError = { row: number; errors: string[] };


function nameOf(items: Customer[], id: string): string {
  return items.find((i) => i.id === id)?.name ?? "—";
}

// Cross-cutting rule (spec §3-8): mixed-unit aggregates render as a dict per
// unit, never a summed scalar — so this renders one line per unit.
function QtyByUnit({ qty }: { qty: Record<string, number> }) {
  const units = Object.keys(qty);
  if (units.length === 0) return <>—</>;
  return (
    <>
      {units.map((u) => (
        <div key={u}>
          {qty[u]} {u}
        </div>
      ))}
    </>
  );
}

export default function DemandImport() {
  const [params, setParams] = useSearchParams();
  const customerId = params.get("customer") ?? "";

  const [customers, setCustomers] = useState<Customer[]>([]);
  const [imports, setImports] = useState<ImportRecord[]>([]);
  const [current, setCurrent] = useState<ImportRecord | null>(null);
  const [applyResult, setApplyResult] = useState<ApplyResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploadErrors, setUploadErrors] = useState<RowError[] | string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    apiGet<Customer[]>("/customers")
      .then(setCustomers)
      .catch((e: Error) => setError(e.message));
  }, []);

  const loadImports = () => {
    apiGet<ImportRecord[]>("/demand/imports")
      .then((list) => {
        setImports(customerId ? list.filter((i) => i.customer_id === customerId) : list);
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(loadImports, [customerId]);

  const setCustomer = (id: string) => {
    setParams(writeParam(params, "customer", id || null));
    setCurrent(null);
    setApplyResult(null);
  };

  const upload = () => {
    const file = fileInput.current?.files?.[0];
    if (!file || !customerId) return;
    setUploading(true);
    const form = new FormData();
    form.append("file", file);
    apiUpload<ImportRecord>(`/demand/imports?customer_id=${customerId}`, form)
      .then((imp) => {
        setCurrent(imp);
        setApplyResult(null);
        setUploadErrors(null);
        setError(null);
        loadImports();
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

  const apply = () => {
    if (!current) return;
    setBusy(true);
    apiSend<ApplyResult>("POST", `/demand/imports/${current.id}/apply`)
      .then((res) => {
        setApplyResult(res);
        setCurrent(null);
        setError(null);
        loadImports();
      })
      .catch((e: ApiError | Error) => setError(errorMessage(e)))
      .finally(() => setBusy(false));
  };

  const discard = () => {
    if (!current) return;
    setBusy(true);
    apiSend<ImportRecord>("POST", `/demand/imports/${current.id}/discard`)
      .then(() => {
        setCurrent(null);
        setApplyResult(null);
        setError(null);
        loadImports();
      })
      .catch((e: ApiError | Error) => setError(errorMessage(e)))
      .finally(() => setBusy(false));
  };

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Demand Import" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Demand Import</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={loadImports} />
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

        {!customerId && <p className="hint">Choose a customer to import demand for.</p>}

        {customerId && (
          <div className="tab-panel">
            <div className="upload-row">
              <button
                type="button"
                className="link-btn"
                onClick={() => apiDownload("/demand/template", "demand-template.xlsx").catch((e: Error) => setError(e.message))}
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

            {applyResult && (
              <p className="banner-warn">
                Applied: {applyResult.applied_wells.join(", ") || "—"}
                {applyResult.created_wells.length > 0 &&
                  ` (new wells: ${applyResult.created_wells.join(", ")})`}
                . <Link to="/demand">View Demand List</Link>
              </p>
            )}

            {current && (
              <div>
                <h3>Conflicts for {current.filename}</h3>
                {current.conflicts.length === 0 && (
                  <p className="hint">No conflicting wells — every staged well is new or has no existing lines.</p>
                )}
                {current.conflicts.length > 0 && (
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>Well</th>
                        <th>Existing lines</th>
                        <th>Staged lines</th>
                        <th>Existing qty</th>
                        <th>Staged qty</th>
                      </tr>
                    </thead>
                    <tbody>
                      {current.conflicts.map((c) => (
                        <tr key={c.well_name}>
                          <td>{c.well_name}</td>
                          <td>{c.existing_line_count}</td>
                          <td>{c.staged_line_count}</td>
                          <td>
                            <QtyByUnit qty={c.existing_qty_by_unit} />
                          </td>
                          <td>
                            <QtyByUnit qty={c.staged_qty_by_unit} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <div className="upload-row">
                  <ConfirmButton
                    label={busy ? "Applying…" : "Apply"}
                    armedLabel="Confirm apply"
                    onConfirm={apply}
                  />
                  <button type="button" onClick={discard} disabled={busy}>
                    Discard
                  </button>
                </div>
              </div>
            )}

            <h3>Past imports</h3>
            {imports.length === 0 && <p className="hint">No imports yet.</p>}
            {imports.length > 0 && (
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Filename</th>
                    <th>Customer</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {imports.map((i) => (
                    <tr key={i.id}>
                      <td>{i.filename}</td>
                      <td>{nameOf(customers, i.customer_id)}</td>
                      <td>
                        <span className={i.status === "applied" ? "hint" : i.status === "discarded" ? "hint" : "banner-warn"}>
                          {i.status}
                        </span>
                      </td>
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
