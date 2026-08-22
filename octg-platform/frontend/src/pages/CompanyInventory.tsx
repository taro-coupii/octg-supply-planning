import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  BusinessUnitOut,
  CompanyAssignmentGroupOut,
  CompanyInventoryPositionOut,
  CompanyInventoryUploadOut,
  CompanyInventoryUploadSummaryOut,
  CompanyInventoryWriteOut,
  CompanyOnHandRowOut,
  CompanyOnOrderRowOut,
  UnitOfMeasure,
  api,
  companyInventoryApi,
  errorText,
} from "../api/client";
import LoadError from "../components/LoadError";
import Tabs, { useTabs } from "../components/Tabs";

/**
 * Company-Owned Inventory maintenance.
 *
 * MVP-COMPROMISE[C-03]: on-hand, on-order and Oracle assignments are all
 * Oracle-owned domains. This screen exists ONLY because the MVP has no Oracle
 * interface — see MVP_COMPROMISES.md C-03 and the `.ci-readonly-banner` below.
 * The three tables here are read-only projections of Oracle, with one
 * emergency-maintenance hole this platform opened because nothing else can
 * write these figures yet. The gate is `editable`/`not_editable_reason` per
 * row, and it retires itself automatically the day a real feed starts
 * stamping its own `source_system` on a row — no code change needed here.
 *
 * On-hand gets the spreadsheet path (template download + upload) because it is
 * naturally one-row-per-product. On-order and assignments deliberately do NOT:
 * on-order is multiple rows per product (one per arrival) and assignments are
 * per demand line, so neither fits a one-row-per-product sheet — inline
 * editing only, and the tab says so rather than leaving the absence unexplained.
 */

function formatQty(quantity: number, unit: UnitOfMeasure): string {
  return `${quantity.toLocaleString()} ${unit}`;
}

function formatSyncedAt(synced_at: string | null): string {
  return synced_at ? synced_at.slice(0, 16).replace("T", " ") : "—";
}

export default function CompanyInventory() {
  const [params, setParams] = useSearchParams();
  const [businessUnits, setBusinessUnits] = useState<BusinessUnitOut[]>([]);
  const [businessUnitId, setBusinessUnitId] = useState(
    params.get("bu") ?? ""
  );
  const [data, setData] = useState<CompanyInventoryPositionOut | null>(null);
  const [history, setHistory] = useState<CompanyInventoryUploadSummaryOut[]>(
    []
  );
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);

  /**
   * A tab count of ZERO is suppressed rather than rendered.
   *
   * These three tables are projections of an Oracle-owned domain, so an empty
   * one means "the feed has told us nothing", never "the quantity is nought".
   * A badge reading `On order 0` states the second, in the most glanceable spot
   * on the screen, and no amount of explanatory text inside the panel undoes a
   * number a manager has already read. So the badge is simply absent when the
   * projection is empty, and the panel says in words what the absence means.
   *
   * `undefined` (still loading) and `0` (loaded, no rows) collapse to the same
   * rendering here on purpose: neither one is a quantity anybody may act on.
   */
  const badge = (n: number | undefined) => (n ? n : undefined);

  /**
   * ONE definition, used by both `useTabs` (which validates the `?tab=` value)
   * and `<Tabs>` (which renders them). These were two separate array literals
   * that had to be kept in step by hand, and they immediately fell out of step:
   * a change to the counts in one left the other rendering the old figures.
   */
  const TAB_DEFS = [
    { key: "on-hand", label: "On hand", count: badge(data?.on_hand.length) },
    { key: "on-order", label: "On order", count: badge(data?.on_order.length) },
    {
      key: "assignments",
      label: "Oracle assignments",
      count: badge(data?.assignments.length),
    },
  ] as const;

  const [tab, selectTab] = useTabs("tab", TAB_DEFS);

  useEffect(() => {
    api
      .getBusinessUnits()
      .then((bus) => {
        setBusinessUnits(bus);
        if (!businessUnitId && bus.length > 0) setBusinessUnitId(bus[0].id);
      })
      .catch(() => setBusinessUnits([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function selectBusinessUnit(id: string) {
    setBusinessUnitId(id);
    const next = new URLSearchParams(params);
    if (id) next.set("bu", id);
    else next.delete("bu");
    setParams(next, { replace: true });
  }

  function reload() {
    if (!businessUnitId) return;
    setLoading(true);
    companyInventoryApi
      .getPosition(businessUnitId)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => {
        setError(e);
        setData(null);
      })
      .finally(() => setLoading(false));
    companyInventoryApi
      .listUploads(businessUnitId)
      .then(setHistory)
      .catch(() => setHistory([]));
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [businessUnitId]);

  return (
    <div>
      <div className="scenario-head">
        <div>
          <h1>Company Inventory</h1>
          <p className="scenario-sub">
            On-hand, on-order and Oracle assignments for one Business Unit —
            the same three tables every coverage verdict in that Business Unit
            is computed from.
          </p>
        </div>
        <label className="filter-field">
          <span>Business Unit</span>
          <select
            value={businessUnitId}
            onChange={(e) => selectBusinessUnit(e.target.value)}
          >
            {businessUnits.map((bu) => (
              <option key={bu.id} value={bu.id}>
                {bu.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="admin-readonly ci-readonly-banner">
        <span className="admin-readonly-tag">Read-only Oracle projection</span>
        <span>
          These three tables belong to Oracle. This platform has no Oracle
          interface yet (MVP compromise <strong>C-03</strong>), so this screen
          exists only as an emergency manual maintenance hole — it is not a
          feature this platform is meant to keep. Rows already synced from a
          real feed are marked <strong>not editable</strong> below and cannot
          be changed here; only rows this platform itself seeded or a person
          typed in can be edited.
        </span>
      </div>

      {error !== null && (
        <LoadError what="company inventory" error={error} />
      )}

      {loading && !data && <p>Loading…</p>}

      {data && (
        <>
          <Tabs
            label="Company inventory tables"
            active={tab}
            onSelect={selectTab}
            tabs={TAB_DEFS}
          />

          {tab === "on-hand" && (
            <OnHandTab
              businessUnitId={businessUnitId}
              rows={data.on_hand}
              history={history}
              onChanged={reload}
            />
          )}
          {tab === "on-order" && (
            <OnOrderTab rows={data.on_order} onChanged={reload} />
          )}
          {tab === "assignments" && (
            <AssignmentsTab groups={data.assignments} onChanged={reload} />
          )}
        </>
      )}
    </div>
  );
}

/** The server's write-consequence report, reusing `.coi-coverage-changed`. */
function WriteResultPanel({ result }: { result: CompanyInventoryWriteOut }) {
  const changedWells = Object.entries(result.coverage_changes).filter(
    ([, [before, after]]) => before !== after
  );
  return (
    <div className="coi-coverage-changed ci-write-result">
      <h4 className="exec-subhead">Coverage consequences of this write</h4>
      {changedWells.length === 0 ? (
        <p className="coi-coverage-lead">
          No well's coverage verdict changed.
        </p>
      ) : (
        <>
          <p className="coi-coverage-lead">
            This write moved coverage for {changedWells.length} well
            {changedWells.length === 1 ? "" : "s"}:
          </p>
          <ul className="coi-changes">
            {changedWells.map(([wellId, [before, after]]) => (
              <li key={wellId} className="coi-change">
                <span className="coi-change-well">{wellId}</span>
                <span className="coi-change-pair">
                  <span className="coi-change-before">{before ?? "—"}</span>
                  {" → "}
                  <span>{after ?? "—"}</span>
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
      {result.recompute_failures.length > 0 && (
        <p className="exec-caveat">
          {result.recompute_failures.length} customer
          {result.recompute_failures.length === 1 ? "" : "s"} could not be
          recomputed and their stored verdicts now predate this write:{" "}
          {result.recompute_failures.map((f, i) => (
            <span key={f.customer_id}>
              {i > 0 && ", "}
              {f.customer_name} ({f.reason})
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

/** The distinctive treatment for `known === false`: never a "0", never a dash. */
function OnHandQuantityCell({ row }: { row: CompanyOnHandRowOut }) {
  if (!row.known) {
    return (
      <span className="ci-unknown-qty">
        not recorded
        <span className="ci-unknown-qty-note">
          no InventoryOnHand row exists for this product in this Business
          Unit — this is unknown, not zero
        </span>
      </span>
    );
  }
  if (row.quantity === 0) {
    return (
      <span className="ci-zero-qty">
        {formatQty(0, row.unit_of_measure)}
        <span className="ci-zero-qty-note">a measured zero — we hold none</span>
      </span>
    );
  }
  return <span className="num">{formatQty(row.quantity as number, row.unit_of_measure)}</span>;
}

function ProvenanceCell({
  source_system,
  synced_at,
}: {
  source_system: string | null;
  synced_at: string | null;
}) {
  return (
    <span className="ci-provenance">
      <span
        className={
          "coi-source" +
          (source_system === "manual" ? " ci-source-manual" : "")
        }
      >
        {source_system ?? "unknown"}
      </span>
      <span className="ci-synced-at">{formatSyncedAt(synced_at)}</span>
    </span>
  );
}

function EditabilityNote({
  editable,
  reason,
}: {
  editable: boolean;
  reason: string | null;
}) {
  if (editable) return null;
  return <p className="ci-not-editable">{reason ?? "Not editable."}</p>;
}

function OnHandTab({
  businessUnitId,
  rows,
  history,
  onChanged,
}: {
  businessUnitId: string;
  rows: CompanyOnHandRowOut[];
  history: CompanyInventoryUploadSummaryOut[];
  onChanged: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<unknown>(null);
  const [uploadResult, setUploadResult] =
    useState<CompanyInventoryUploadOut | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [editingRow, setEditingRow] = useState<string | null>(null);
  const [draftQty, setDraftQty] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [writeResult, setWriteResult] = useState<CompanyInventoryWriteOut | null>(
    null
  );

  function startEdit(row: CompanyOnHandRowOut) {
    setEditingRow(row.product_id);
    setDraftQty(row.known ? String(row.quantity) : "");
    setSaveError(null);
    setWriteResult(null);
  }

  function cancelEdit() {
    setEditingRow(null);
    setSaveError(null);
  }

  function save(row: CompanyOnHandRowOut) {
    const quantity = Number(draftQty);
    if (!Number.isFinite(quantity)) {
      setSaveError(new Error("Enter a valid number."));
      return;
    }
    setSaving(true);
    setSaveError(null);
    const req = row.row_id
      ? companyInventoryApi.patchOnHand(row.row_id, { quantity })
      : companyInventoryApi.createOnHand({
          business_unit_id: businessUnitId,
          product_id: row.product_id,
          quantity,
        });
    req
      .then((r) => {
        setWriteResult(r);
        setEditingRow(null);
        onChanged();
      })
      .catch((e) => setSaveError(e))
      .finally(() => setSaving(false));
  }

  function handleUpload() {
    if (!file) return;
    setUploading(true);
    setUploadError(null);
    companyInventoryApi
      .uploadOnHand(businessUnitId, file)
      .then((r) => {
        setUploadResult(r);
        setFile(null);
        if (fileInputRef.current) fileInputRef.current.value = "";
        onChanged();
      })
      .catch((e) => setUploadError(e))
      .finally(() => setUploading(false));
  }

  const changedWells = uploadResult
    ? Object.entries(uploadResult.coverage_changes).filter(
        ([, [before, after]]) => before !== after
      )
    : [];

  return (
    <div>
      <section className="card exec-block coi-upload">
        <h3>Template &amp; upload</h3>
        <p className="exec-note-quiet">
          One row per product, matching this Business Unit&apos;s current
          KNOWN on-hand rows. A row absent from the platform&apos;s data comes
          back blank in the template, not as a zero.
        </p>
        <div className="template-row">
          <a
            className="btn-download"
            href={companyInventoryApi.templateUrl(businessUnitId)}
          >
            Download on-hand template
          </a>
        </div>
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
      </section>

      {uploadResult && (
        <section className="card exec-block coi-result">
          <h3>Upload result</h3>
          <dl className="exec-facts">
            <div>
              <dt>Rows</dt>
              <dd className="num">{uploadResult.row_count}</dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd className="num">{uploadResult.created_count}</dd>
            </div>
            <div>
              <dt>Replaced</dt>
              <dd className="num">{uploadResult.replaced_count}</dd>
            </div>
            <div>
              <dt>Errors</dt>
              <dd className="num exec-bad">{uploadResult.error_count}</dd>
            </div>
          </dl>
          {uploadResult.recompute_failures.length > 0 && (
            <p className="exec-caveat">
              {uploadResult.recompute_failures.length} customer
              {uploadResult.recompute_failures.length === 1 ? "" : "s"} could
              not be recomputed:{" "}
              {uploadResult.recompute_failures.map((f, i) => (
                <span key={f.customer_id}>
                  {i > 0 && ", "}
                  {f.customer_name} ({f.reason})
                </span>
              ))}
            </p>
          )}
          {changedWells.length > 0 ? (
            <p className="exec-caveat">
              This upload moved coverage for {changedWells.length} well
              {changedWells.length === 1 ? "" : "s"}:{" "}
              {changedWells.map(([wellId, [before, after]], i) => (
                <span key={wellId}>
                  {i > 0 && ", "}
                  {wellId} ({before ?? "—"} → {after ?? "—"})
                </span>
              ))}
            </p>
          ) : (
            <p className="exec-note-quiet">
              No well&apos;s coverage verdict changed.
            </p>
          )}
          <div className="table-scroll">
            <table className="exec-status-table coi-result-table">
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
                {uploadResult.rows.map((r) => (
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
                    <td className="num">
                      {r.action === "Error" ? (
                        <span className="coi-raw">
                          {r.raw_quantity ?? "—"}
                          <span className="coi-raw-note">
                            as written in the file — refused, not a validated
                            quantity
                          </span>
                        </span>
                      ) : r.quantity === null || !r.unit_of_measure ? (
                        "—"
                      ) : (
                        formatQty(r.quantity, r.unit_of_measure)
                      )}
                    </td>
                    <td className="num">
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
      )}

      {writeResult && <WriteResultPanel result={writeResult} />}

      <section className="card exec-block coi-positions">
        <h3>Position</h3>
        <div className="table-scroll">
          <table className="exec-status-table coi-table">
            <thead>
              <tr>
                <th>Product</th>
                <th>Quantity</th>
                <th>Provenance</th>
                <th>Edit</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.product_id}>
                  <td>
                    {row.product_description ?? row.product_id}
                    <span className="coi-product-id">{row.product_id}</span>
                  </td>
                  <td className="num">
                    <OnHandQuantityCell row={row} />
                  </td>
                  <td>
                    <ProvenanceCell
                      source_system={row.source_system}
                      synced_at={row.synced_at}
                    />
                  </td>
                  <td>
                    {editingRow === row.product_id ? (
                      <div className="ci-edit-form">
                        <input
                          type="number"
                          value={draftQty}
                          onChange={(e) => setDraftQty(e.target.value)}
                          className="ci-edit-input"
                        />
                        <button
                          type="button"
                          disabled={saving}
                          onClick={() => save(row)}
                        >
                          {saving ? "Saving…" : "Save"}
                        </button>
                        <button
                          type="button"
                          className="ci-cancel-btn"
                          disabled={saving}
                          onClick={cancelEdit}
                        >
                          Cancel
                        </button>
                        {saveError !== null && editingRow === row.product_id && (
                          <p className="load-error-detail">
                            {errorText(saveError)}
                          </p>
                        )}
                      </div>
                    ) : row.editable ? (
                      <button type="button" onClick={() => startEdit(row)}>
                        {row.known ? "Edit" : "Set quantity"}
                      </button>
                    ) : (
                      <EditabilityNote
                        editable={row.editable}
                        reason={row.not_editable_reason}
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {history.length > 0 && (
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
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.id}>
                    <td className="num">{formatSyncedAt(h.uploaded_at)}</td>
                    <td>{h.filename ?? "—"}</td>
                    <td>{h.sheet_name ?? "—"}</td>
                    <td className="num">{h.row_count}</td>
                    <td className="num">{h.created_count}</td>
                    <td className="num">{h.replaced_count}</td>
                    <td className={`num${h.error_count > 0 ? " exec-bad" : ""}`}>
                      {h.error_count}
                    </td>
                    <td>
                      <span className="coi-source">{h.source_system}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

function OnOrderTab({
  rows,
  onChanged,
}: {
  rows: CompanyOnOrderRowOut[];
  onChanged: () => void;
}) {
  const [editingRow, setEditingRow] = useState<string | null>(null);
  const [draftQty, setDraftQty] = useState("");
  const [draftDate, setDraftDate] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [writeResult, setWriteResult] = useState<CompanyInventoryWriteOut | null>(
    null
  );

  function startEdit(row: CompanyOnOrderRowOut) {
    setEditingRow(row.row_id);
    setDraftQty(String(row.quantity));
    setDraftDate(row.expected_arrival_date?.slice(0, 10) ?? "");
    setSaveError(null);
    setWriteResult(null);
  }

  function save(row: CompanyOnOrderRowOut) {
    const quantity = Number(draftQty);
    if (!Number.isFinite(quantity)) {
      setSaveError(new Error("Enter a valid number."));
      return;
    }
    setSaving(true);
    setSaveError(null);
    companyInventoryApi
      .patchOnOrder(row.row_id, {
        quantity,
        expected_arrival_date: draftDate ? draftDate : null,
      })
      .then((r) => {
        setWriteResult(r);
        setEditingRow(null);
        onChanged();
      })
      .catch((e) => setSaveError(e))
      .finally(() => setSaving(false));
  }

  return (
    <div>
      <p className="exec-note-quiet ci-no-sheet-note">
        There is deliberately no spreadsheet path for on-order. A product can
        have several on-order rows — one per expected arrival — so it does not
        fit a one-row-per-product template the way on-hand does. Edit rows
        inline below.
      </p>

      {writeResult && <WriteResultPanel result={writeResult} />}

      <section className="card exec-block coi-positions">
        <h3>Purchase order lines</h3>
        {rows.length === 0 ? (
          /* NOT "nothing is on order". `InventoryOnOrder` is a projection of an
             Oracle-owned table, and absence in a projection is SILENCE, not a
             measured zero -- `app/models/inventory_on_order.py` says so in as
             many words, and the platform states "nothing on order" only with an
             explicit `quantity = 0` row. An empty table here means nobody has
             told us, and a planner who reads it as "no steel is coming" has been
             misled by the screen rather than by the data. Amber, because it is a
             gap in the feed that a person upstream has to close. */
          <p className="ci-unknown-qty">
            No purchase-order rows have been recorded for this Business Unit
            <span className="ci-unknown-qty-note">
              This is not the same as &ldquo;nothing is on order&rdquo;. These
              rows are a projection of Oracle&rsquo;s purchase orders, and an
              empty projection means the position is UNKNOWN. A Business Unit
              that genuinely has nothing arriving says so with an explicit
              zero-quantity row.
            </span>
          </p>
        ) : (
          <div className="table-scroll">
            <table className="exec-status-table coi-table">
              <thead>
                <tr>
                  <th>Product</th>
                  <th>Quantity</th>
                  <th>Expected arrival</th>
                  <th>Provenance</th>
                  <th>Edit</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.row_id}>
                    <td>
                      {row.product_description ?? row.product_id}
                      <span className="coi-product-id">{row.product_id}</span>
                    </td>
                    <td className="num">
                      {formatQty(row.quantity, row.unit_of_measure)}
                    </td>
                    <td className="num">
                      {editingRow === row.row_id ? (
                        <input
                          type="date"
                          value={draftDate}
                          onChange={(e) => setDraftDate(e.target.value)}
                        />
                      ) : (
                        row.expected_arrival_date?.slice(0, 10) ?? "undated"
                      )}
                    </td>
                    <td>
                      <ProvenanceCell
                        source_system={row.source_system}
                        synced_at={row.synced_at}
                      />
                      {row.source_reference && (
                        <span className="coi-source-ref">
                          {row.source_reference}
                        </span>
                      )}
                    </td>
                    <td>
                      {editingRow === row.row_id ? (
                        <div className="ci-edit-form">
                          <input
                            type="number"
                            value={draftQty}
                            onChange={(e) => setDraftQty(e.target.value)}
                            className="ci-edit-input"
                          />
                          <button
                            type="button"
                            disabled={saving}
                            onClick={() => save(row)}
                          >
                            {saving ? "Saving…" : "Save"}
                          </button>
                          <button
                            type="button"
                            className="ci-cancel-btn"
                            disabled={saving}
                            onClick={() => setEditingRow(null)}
                          >
                            Cancel
                          </button>
                          {saveError !== null && (
                            <p className="load-error-detail">
                              {errorText(saveError)}
                            </p>
                          )}
                        </div>
                      ) : row.editable ? (
                        <button type="button" onClick={() => startEdit(row)}>
                          Edit
                        </button>
                      ) : (
                        <EditabilityNote
                          editable={row.editable}
                          reason={row.not_editable_reason}
                        />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function AssignmentsTab({
  groups,
  onChanged,
}: {
  groups: CompanyAssignmentGroupOut[];
  onChanged: () => void;
}) {
  const [editingRow, setEditingRow] = useState<string | null>(null);
  const [draftQty, setDraftQty] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [writeResult, setWriteResult] = useState<CompanyInventoryWriteOut | null>(
    null
  );

  function startEdit(rowId: string, quantity: number) {
    setEditingRow(rowId);
    setDraftQty(String(quantity));
    setSaveError(null);
    setWriteResult(null);
  }

  function save(rowId: string) {
    const quantity = Number(draftQty);
    if (!Number.isFinite(quantity)) {
      setSaveError(new Error("Enter a valid number."));
      return;
    }
    setSaving(true);
    setSaveError(null);
    companyInventoryApi
      .patchAssignment(rowId, { quantity })
      .then((r) => {
        setWriteResult(r);
        setEditingRow(null);
        onChanged();
      })
      .catch((e) => setSaveError(e))
      .finally(() => setSaving(false));
  }

  return (
    <div>
      <p className="exec-note-quiet ci-no-sheet-note">
        There is deliberately no spreadsheet path for assignments. Each row is
        per demand line, not per product, so it does not fit a
        one-row-per-product template. Edit lines inline below.
      </p>

      {writeResult && <WriteResultPanel result={writeResult} />}

      {groups.length === 0 ? (
        <section className="card exec-block coi-positions">
          {/* Same rule as the on-order empty state above: absence in an
              Oracle projection is silence. "No assignments" would read as "no
              steel is spoken for", which is a claim this screen has no basis
              to make. */}
          <p className="ci-unknown-qty">
            No Oracle assignment rows have been recorded for this Business Unit
            <span className="ci-unknown-qty-note">
              Unknown, not none. `InventoryAssignment` is a read-only projection
              of Oracle&rsquo;s hard allocations; an empty projection means the
              feed has said nothing, not that no steel is assigned.
            </span>
          </p>
        </section>
      ) : (
        groups.map((group) => (
          <section
            key={group.product_id}
            className="card exec-block coi-positions"
          >
            <h3>
              {group.product_description ?? group.product_id}{" "}
              <span className="coi-product-id">{group.product_id}</span>
            </h3>
            <p className="exec-note-quiet">
              Total assigned:{" "}
              <strong>
                {formatQty(group.total_quantity, group.unit_of_measure)}
              </strong>
            </p>
            <div className="table-scroll">
              <table className="exec-status-table coi-table">
                <thead>
                  <tr>
                    <th>Well</th>
                    <th>Customer</th>
                    <th>Quantity</th>
                    <th>Provenance</th>
                    <th>Edit</th>
                  </tr>
                </thead>
                <tbody>
                  {group.lines.map((line) => (
                    <tr key={line.row_id}>
                      <td>{line.well_name ?? line.well_id}</td>
                      <td>{line.customer_id}</td>
                      <td className="num">
                        {formatQty(line.quantity, group.unit_of_measure)}
                      </td>
                      <td>
                        <ProvenanceCell
                          source_system={line.source_system}
                          synced_at={line.synced_at}
                        />
                      </td>
                      <td>
                        {editingRow === line.row_id ? (
                          <div className="ci-edit-form">
                            <input
                              type="number"
                              value={draftQty}
                              onChange={(e) => setDraftQty(e.target.value)}
                              className="ci-edit-input"
                            />
                            <button
                              type="button"
                              disabled={saving}
                              onClick={() => save(line.row_id)}
                            >
                              {saving ? "Saving…" : "Save"}
                            </button>
                            <button
                              type="button"
                              className="ci-cancel-btn"
                              disabled={saving}
                              onClick={() => setEditingRow(null)}
                            >
                              Cancel
                            </button>
                            {saveError !== null && (
                              <p className="load-error-detail">
                                {errorText(saveError)}
                              </p>
                            )}
                          </div>
                        ) : line.editable ? (
                          <button
                            type="button"
                            onClick={() => startEdit(line.row_id, line.quantity)}
                          >
                            Edit
                          </button>
                        ) : (
                          <EditabilityNote
                            editable={line.editable}
                            reason={line.not_editable_reason}
                          />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ))
      )}
    </div>
  );
}
