import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  CustomerOwnedInventoryOut,
  CustomerOwnedUploadResult,
  CustomerOwnedUploadSummary,
  CustomerSummary,
  errorText,
} from "../api/client";
import LoadError from "../components/LoadError";

/**
 * Customer-Owned Inventory.
 *
 * This is the ONE inventory screen with real write/upload controls. On-hand,
 * assignments and on-order are all read-only Oracle projections; this table is
 * data the platform itself owns because a user uploaded it. Making that
 * asymmetry visible is the point of this screen, not an afterthought — hence
 * the upload panel front and centre instead of tucked into a menu.
 *
 * `has_uploaded` is branched on before anything else:
 *   false                    -> no upload has ever happened. Never render "0".
 *   true, positions = []     -> uploaded, and genuinely owns nothing. A
 *                                measured fact, calmer than the "never
 *                                uploaded" empty state, not the same as it.
 *   true, positions = [...]  -> the table.
 */

export default function CustomerOwnedInventory() {
  const [params, setParams] = useSearchParams();
  const [customers, setCustomers] = useState<CustomerSummary[]>([]);
  const [customerId, setCustomerId] = useState(params.get("customer") ?? "");
  const [data, setData] = useState<CustomerOwnedInventoryOut | null>(null);
  // "When did this data last arrive." Every row of the position above is only as
  // current as the file it came in, so the arrival history is part of reading the
  // figures rather than an audit extra.
  const [history, setHistory] = useState<CustomerOwnedUploadSummary[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);

  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<unknown>(null);
  const [result, setResult] = useState<CustomerOwnedUploadResult | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.getCustomers().then((cs) => {
      setCustomers(cs);
      if (!customerId && cs.length > 0) setCustomerId(cs[0].id);
    }).catch(() => setCustomers([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function selectCustomer(id: string) {
    setCustomerId(id);
    setParams(id ? { customer: id } : {});
  }

  useEffect(() => {
    if (!customerId) return;
    let live = true;
    setLoading(true);
    setResult(null);
    // The history is a secondary read: a failure there must not blank the
    // position itself, so it degrades to an empty list rather than an error.
    api.listCustomerOwnedUploads(customerId).then(
      (h) => live && setHistory(h),
      () => live && setHistory([])
    );
    api
      .getCustomerOwnedInventory(customerId)
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
      })
      .catch((e) => {
        if (!live) return;
        setError(e);
        setData(null);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [customerId]);

  function handleUpload() {
    if (!file || !customerId) return;
    setUploading(true);
    setUploadError(null);
    api
      .uploadCustomerOwnedInventory(customerId, file)
      .then((r) => {
        setResult(r);
        setFile(null);
        if (fileInputRef.current) fileInputRef.current.value = "";
        // Re-fetch the position so the table above reflects the upload
        // immediately — this is a single-step upload, not a staged review.
        api
          .listCustomerOwnedUploads(customerId)
          .then(setHistory)
          .catch(() => undefined);
        return api.getCustomerOwnedInventory(customerId).then(setData);
      })
      .catch((e) => setUploadError(e))
      .finally(() => setUploading(false));
  }

  const contract = result?.column_contract;

  return (
    <div>
      <div className="scenario-head">
        <div>
          <h1>Customer-Owned Inventory</h1>
          <p className="scenario-sub">
            The one inventory table this platform itself owns — uploaded by a
            user, not projected from Oracle. Consumed first for its product,
            ahead of company stock and any Oracle assignment; never offered to
            another customer.
          </p>
        </div>
        <div className="template-row">
          <label className="filter-field">
            <span>Customer</span>
            <select
              value={customerId}
              onChange={(e) => selectCustomer(e.target.value)}
            >
              {customers.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          {/*
            Sits beside the customer picker, and is rendered OUTSIDE the `data &&`
            block on purpose — it depends only on which customer is selected, not on
            there being a position to review. The two empty states are exactly when
            it matters most: a never-uploaded customer has nothing on screen to edit,
            and a customer declaring nothing has nothing either, yet both need a
            correctly-shaped file to type into. Gating it on rows would hide it from
            the users who need it.

            A plain link, so the browser's own download machinery handles the
            filename, the progress and the save dialog — the same single download
            idiom as Demand Import's template button and the MRP export.
          */}
          <a
            className="btn-download"
            aria-disabled={customerId === "" ? "true" : "false"}
            href={
              customerId ? api.customerOwnedTemplateUrl(customerId) : undefined
            }
          >
            Download current position as template
          </a>
        </div>
      </div>
      <p className="scenario-section-note">
        The download is <strong>not a blank sheet</strong> — it is this customer&apos;s
        live declared position as an .xlsx already in the column contract below. Edit
        the cells you mean to change and upload it here; re-uploading it{" "}
        <strong>unchanged restates every position at the same quantity</strong> and
        moves nothing. A customer with no declared position gets a valid header-only
        file to type the first one into, and the workbook&apos;s second sheet says
        which of the two empty cases it is.
      </p>

      {error !== null && (
        <LoadError what="customer-owned inventory" error={error} />
      )}

      {loading && !data && <p>Loading…</p>}

      {data && (
        <>
          <section className="card exec-block coi-upload">
            <h3>Upload</h3>
            <p className="exec-note-quiet">
              Single-step: pick a file, upload, see the result immediately. A
              new upload <strong>replaces</strong> the customer&apos;s declared
              position — there is no staging or review step.
            </p>
            <div className="coi-upload-row">
              <input
                ref={fileInputRef}
                type="file"
                accept=".xlsx"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
              <button
                type="button"
                disabled={!file || uploading}
                onClick={handleUpload}
              >
                {uploading ? "Uploading…" : "Upload"}
              </button>
            </div>
            {uploadError !== null && (
              <p className="load-error-detail">{errorText(uploadError)}</p>
            )}

            <div className="coi-contract">
              <h4>Expected columns</h4>
              {contract ? (
                <table className="exec-status-table">
                  <thead>
                    <tr>
                      <th>Column</th>
                      <th>Required?</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(contract.required ?? []).map((c) => (
                      <tr key={c}>
                        <td className="num">{c}</td>
                        <td>Required</td>
                      </tr>
                    ))}
                    {(contract.optional ?? []).map((c) => (
                      <tr key={c}>
                        <td className="num">{c}</td>
                        <td>Optional</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="exec-note-quiet">
                  Required columns: <code>product</code>, <code>quantity</code>.
                  Optional: <code>customer</code>, <code>as_of_date</code>,{" "}
                  <code>location</code>. (Shown after the first upload attempt
                  from the server&apos;s own contract; this is the documented
                  default in the meantime.)
                </p>
              )}
            </div>
          </section>

          {result && <UploadResultPanel result={result} />}

          <PositionsPanel data={data} />
          <HistoryPanel history={history} />
        </>
      )}
    </div>
  );
}

function UploadResultPanel({ result }: { result: CustomerOwnedUploadResult }) {
  const changedWells = Object.entries(result.coverage_changes).filter(
    ([, [before, after]]) => before !== after
  );
  return (
    <section className="card exec-block coi-result">
      <h3>Upload result</h3>
      <dl className="exec-facts">
        <div>
          <dt>Rows</dt>
          <dd className="num">{result.row_count}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd className="num">{result.created_count}</dd>
        </div>
        <div>
          <dt>Replaced</dt>
          <dd className="num">{result.replaced_count}</dd>
        </div>
        <div>
          <dt>Errors</dt>
          <dd className="num exec-bad">{result.error_count}</dd>
        </div>
      </dl>

      {changedWells.length > 0 && (
        <p className="exec-caveat">
          This upload moved coverage for {changedWells.length} well
          {changedWells.length === 1 ? "" : "s"}:{" "}
          {changedWells.map(([wellId, [before, after]], i) => (
            <span key={wellId}>
              {i > 0 && ", "}
              <Link to={`/wells/${wellId}`}>{wellId}</Link>{" "}
              ({before ?? "—"} → {after ?? "—"})
            </span>
          ))}
        </p>
      )}

      <div className="table-scroll">
        <table className="exec-status-table">
          <thead>
            <tr>
              <th>Row</th>
              <th>Action</th>
              <th>Product</th>
              <th>Quantity</th>
              <th>Previous</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {result.rows.map((r) => (
              <tr key={r.row_number}>
                <td className="num">{r.row_number}</td>
                <td>
                  <span
                    className={`badge ${
                      r.action === "Error"
                        ? "badge-Uncovered"
                        : r.action === "Replaced"
                        ? "badge-PendingApproval"
                        : "badge-Covered"
                    }`}
                  >
                    {r.action}
                  </span>
                </td>
                <td>{r.product_description ?? r.raw_product ?? "—"}</td>
                {/*
                  An ERROR row's quantity is never rendered as a validated
                  figure, even when the server echoes a parsed number back. The
                  observed payload proves both halves of the trap: an
                  unknown-product row carries `quantity: 500` with
                  `unit_of_measure: null`, and a bad-quantity row carries
                  `quantity: null` with `unit_of_measure: "Mtr"`. Printing
                  "500" with an empty unit would put an unlabelled quantity on
                  screen, which is exactly what the units guard exists to
                  prevent — so a refused row shows its RAW cell text and says
                  so instead.
                */}
                <td className="num">
                  {r.action === "Error" ? (
                    <span className="coi-raw">
                      {r.raw_quantity ?? "—"}
                      <span className="coi-raw-note">
                        as written in the file — this row was refused, so the
                        value is not a validated quantity
                      </span>
                    </span>
                  ) : r.quantity === null || !r.unit_of_measure ? (
                    "—"
                  ) : (
                    `${r.quantity.toLocaleString()} ${r.unit_of_measure}`
                  )}
                </td>
                <td className="num">
                  {/* Reported because it is a number the user has just lost
                      sight of. Only meaningful for a replacement. */}
                  {r.action === "Replaced" && r.previous_quantity !== null
                    ? `${r.previous_quantity.toLocaleString()}${
                        r.unit_of_measure ? ` ${r.unit_of_measure}` : ""
                      }`
                    : "—"}
                </td>
                <td className="reason-text">{r.error ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/**
 * When this data arrived.
 *
 * Each upload REPLACED the declared quantity for the products it named, so this
 * is a list of arrivals rather than a set of coexisting declarations — stated in
 * words, because a stack of rows reads like accumulating stock otherwise.
 */
function HistoryPanel({ history }: { history: CustomerOwnedUploadSummary[] }) {
  if (history.length === 0) return null;
  return (
    <section className="card exec-block">
      <h3>When this data arrived</h3>
      <div className="table-scroll">
        <table className="exec-status-table coi-history-table">
          <thead>
            <tr>
              <th>Uploaded</th>
              <th>File</th>
              <th>Sheet</th>
              <th>Rows</th>
              <th>Created</th>
              <th>Replaced</th>
              <th>Errors</th>
            </tr>
          </thead>
          <tbody>
            {/* Server order: newest first. Never re-sorted. */}
            {history.map((h) => (
              <tr key={h.id}>
                <td className="num">
                  {h.uploaded_at ? h.uploaded_at.slice(0, 16).replace("T", " ") : "—"}
                </td>
                <td>{h.filename ?? "—"}</td>
                <td>{h.sheet_name ?? "—"}</td>
                <td className="num">{h.row_count}</td>
                <td className="num">{h.created_count}</td>
                <td className="num">{h.replaced_count}</td>
                <td className={`num${h.error_count > 0 ? " exec-bad" : ""}`}>
                  {h.error_count}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="exec-note-quiet">
        Newest first. Each upload <strong>replaced</strong> the declared quantity
        for the products it named — this is a history of arrivals, not a set of
        declarations that add up.
      </p>
    </section>
  );
}

function PositionsPanel({ data }: { data: CustomerOwnedInventoryOut }) {
  if (!data.has_uploaded) {
    return (
      <section className="card exec-block coi-empty coi-never-uploaded">
        <h3>Declared position</h3>
        <div className="exec-unavailable">
          <span className="exec-unavailable-tag">Never uploaded</span>
          <span className="exec-unavailable-reason">
            No customer-owned inventory has ever been uploaded for{" "}
            <strong>{data.customer_name}</strong>. The platform holds NO data
            about this customer&apos;s owned stock — that is not the same as
            owning none. Use the upload panel above to supply the first
            position.
          </span>
        </div>
        {/* The server's own prose for this exact case. It names the state and
            the endpoint, so a screen that rendered only this would still be
            honest — which is why it is shown rather than paraphrased away. */}
        <p className="exec-caveat">{data.note}</p>
      </section>
    );
  }

  if (data.positions.length === 0) {
    return (
      <section className="card exec-block coi-empty coi-zero">
        <h3>Declared position</h3>
        <p className="empty">
          <strong>{data.customer_name}</strong> has uploaded customer-owned
          inventory and it genuinely declares zero positions — a measured
          fact, not a missing upload.
          {data.last_uploaded_at && (
            <> Last uploaded {data.last_uploaded_at.slice(0, 10)}.</>
          )}
        </p>
        <p className="exec-caveat">{data.note}</p>
      </section>
    );
  }

  return (
    <section className="card exec-block coi-positions">
      <h3>Declared position</h3>
      <p className="exec-note-quiet">{data.note}</p>
      <div className="table-scroll">
        <table className="exec-status-table">
          <thead>
            <tr>
              <th>Product</th>
              <th>Quantity</th>
              <th>Uploaded at</th>
              <th>Source reference</th>
            </tr>
          </thead>
          <tbody>
            {data.positions.map((p) => (
              <tr key={p.product_id}>
                <td>{p.product_description ?? p.product_id}</td>
                <td className="num">
                  {p.quantity.toLocaleString()} {p.unit_of_measure}
                </td>
                {/* uploaded_at is nullable in the schema; a row without an
                    as-of date renders a dash rather than crashing the page. */}
                <td className="num">
                  {p.uploaded_at ? p.uploaded_at.slice(0, 16).replace("T", " ") : "—"}
                </td>
                <td>{p.source_reference ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
