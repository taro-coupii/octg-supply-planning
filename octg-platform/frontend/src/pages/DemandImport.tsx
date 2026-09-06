import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  CustomerSummary,
  DemandImportApplyResult,
  DemandImportBatch,
  DemandImportBatchSummary,
  DemandImportDecision,
  DemandImportRow,
  ImportConflictPreview,
  errorText,
} from "../api/client";
import LoadError from "../components/LoadError";
import { formatDay } from "./MrpSummary";

/**
 * Demand Import — Upload -> Staging -> Match -> Review -> Apply.
 *
 * The load-bearing rule of this screen is that the SYSTEM SUGGESTS and the USER
 * DECIDES. `match_type` is the matcher's suggestion and is rendered as advice;
 * `decision` is the planner's answer and is the only thing apply acts on. So:
 *
 *   * uploading stages and mutates nothing, and the screen says so;
 *   * "Accept all suggestions" fills decisions in but never applies;
 *   * Apply is a separate, explicit, guarded action, disabled until at least one
 *     row has been decided;
 *   * an "Error" row offers ONLY Skip, because the API 400s on accepting it;
 *   * a "New" row that nonetheless carries `matched_demand_line_id` still offers
 *     "Accept as revision" — the backend attaches that nearest candidate
 *     precisely so the planner can overrule the suggestion.
 *
 * TWO ADDITIONS, AND BOTH ARE ABOUT THE SAME THING: EDIT REAL VALUES, KNOWINGLY
 * ----------------------------------------------------------------------------
 * 1. DOWNLOAD THE CURRENT BOOK AS A TEMPLATE. Step 1 offers the customer's live
 *    demand as an .xlsx in the exact column contract, so a planner edits real values
 *    instead of retyping the book beside a hint. Re-uploading it unmodified reports
 *    nothing changed.
 *
 * 2. CONFLICT REVIEW AND OVERRIDE APPROVAL. A row can be valid and still disagree
 *    with the live book in a way where the row's OWN diff is not the change that
 *    would happen — it asserts a demand status the well does not have (cascading to
 *    every line of the well, including lines the file never listed), or the line it
 *    revises has been revised by somebody else since staging. Such a row is rendered
 *    AMBER (not red — red means the file is broken and only skippable), shows the
 *    conflict as current -> file, offers a read-only coverage-impact preview, and
 *    requires a SECOND explicit approval before Apply will touch it. Apply is
 *    disabled while any accepted row is unapproved, and the server refuses the whole
 *    batch anyway — the button state is a courtesy, not the guarantee.
 *
 *    A row with NO conflict is completely unaffected: same buttons, same one
 *    decision. A gate that fires on the common case is not a gate.
 */

const MAX_BYTES = 20 * 1024 * 1024;

const DECISION_LABEL: Record<DemandImportDecision, string> = {
  Pending: "Not decided",
  AcceptRevision: "Accept as revision",
  AcceptNew: "Accept as new demand",
  Skip: "Skip",
};

function Step({
  n,
  name,
  state,
}: {
  n: number;
  name: string;
  state: "done" | "current" | "todo";
}) {
  return (
    <li className={`import-step import-step-${state}`}>
      <span className="import-step-n">{n}</span>
      <span>{name}</span>
    </li>
  );
}

/** before -> after for one field, using the shared diff vocabulary. */
function Diff({
  label,
  before,
  after,
}: {
  label: string;
  before: string | number | null;
  after: string | number | null;
}) {
  const b = before === null || before === "" ? null : String(before);
  const a = after === null || after === "" ? null : String(after);
  const unchanged = b === a;
  return (
    <div className={`diff-line${unchanged ? " diff-line-unchanged" : ""}`}>
      <span className="diff-label">{label}</span>
      <span className="diff-before num">{b ?? <em className="diff-none">—</em>}</span>
      <span className="change-arrow">→</span>
      <span className="diff-after num">{a ?? <em className="diff-none">—</em>}</span>
    </div>
  );
}

function RawCells({ row }: { row: DemandImportRow }) {
  const cells: [string, string | null][] = [
    ["well", row.raw_well],
    ["product", row.raw_product],
    ["quantity", row.raw_quantity],
    ["ros_date", row.raw_ros_date],
    ["status", row.raw_status],
    ["profile", row.raw_profile],
  ];
  return (
    <dl className="raw-cells">
      {cells.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v === null || v === "" ? <span className="diff-none">(blank)</span> : v}</dd>
        </div>
      ))}
    </dl>
  );
}

function ParsedCells({ row }: { row: DemandImportRow }) {
  return (
    <dl className="raw-cells raw-cells-parsed">
      <div>
        <dt>well</dt>
        <dd>
          {row.well_id ? (
            <Link to={`/wells/${row.well_id}`}>{row.well_name}</Link>
          ) : (
            <span className="diff-none">not resolved</span>
          )}
        </dd>
      </div>
      <div>
        <dt>product</dt>
        <dd>
          {row.product_description ?? (
            <span className="diff-none">not resolved</span>
          )}
        </dd>
      </div>
      <div>
        <dt>quantity</dt>
        <dd className="num">
          {row.quantity === null ? (
            <span className="diff-none">not parsed</span>
          ) : (
            row.quantity.toLocaleString()
          )}
        </dd>
      </div>
      <div>
        <dt>ros_date</dt>
        <dd className="num">
          {row.ros_date ? (
            formatDay(row.ros_date)
          ) : (
            <span className="diff-none">not parsed</span>
          )}
        </dd>
      </div>
      <div>
        <dt>status</dt>
        <dd>{row.status ?? <span className="diff-none">—</span>}</dd>
      </div>
      <div>
        <dt>profile</dt>
        <dd>{row.profile ?? <span className="diff-none">—</span>}</dd>
      </div>
    </dl>
  );
}

/** Which decisions this row may legally take. */
function allowedDecisions(row: DemandImportRow): DemandImportDecision[] {
  // An Error row cannot be accepted in any form — the API rejects it with 400.
  if (row.match_type === "Error") return ["Skip"];
  if (row.match_type === "Revision") return ["AcceptRevision", "Skip"];
  // "New": the suggestion is AcceptNew, but when the matcher attached a nearest
  // candidate the planner may overrule it and take the row as a revision.
  return row.matched_demand_line_id
    ? ["AcceptNew", "AcceptRevision", "Skip"]
    : ["AcceptNew", "Skip"];
}

/** The matcher's own suggestion, as a decision. Null when there isn't one. */
function suggestedDecision(row: DemandImportRow): DemandImportDecision | null {
  if (row.match_type === "Error") return "Skip";
  if (row.match_type === "Revision") return "AcceptRevision";
  return "AcceptNew";
}

const CONFLICT_TITLE: Record<string, string> = {
  WellDemandStatus: "This row moves the WHOLE well",
  ConcurrentRevision: "Somebody else changed this line since the file was staged",
};

/** True when Apply would refuse this row: accepted, conflicting, unapproved. */
function isBlocking(row: DemandImportRow): boolean {
  return (
    row.requires_override_approval &&
    (row.decision === "AcceptRevision" || row.decision === "AcceptNew")
  );
}

/**
 * The conflict panel: what disagrees, the coverage impact on request, and the
 * approval. Rendered instead of nothing — a valid row that would do more than its
 * own diff shows is the one case where the review screen must interrupt.
 */
function ConflictPanel({
  row,
  readOnly,
  busy,
  preview,
  previewError,
  onPreview,
  onApprove,
}: {
  row: DemandImportRow;
  readOnly: boolean;
  busy: boolean;
  preview: ImportConflictPreview | null;
  previewError: string | null;
  onPreview: () => void;
  onApprove: (approved: boolean) => void;
}) {
  const approved = row.override_approved;
  return (
    <div className="import-conflict">
      <div className="import-conflict-head">
        <span
          className={`import-conflict-tag${
            approved ? " import-conflict-tag-approved" : ""
          }`}
        >
          {approved ? "Override approved" : "Conflict"}
        </span>
        <span className="import-conflict-kind">
          {CONFLICT_TITLE[row.conflict_kind ?? ""] ?? "Disagrees with live data"}
        </span>
        {row.conflict_kind === "WellDemandStatus" &&
          row.conflict_cascade_line_count > 0 && (
            <span className="import-cascade">
              {row.conflict_cascade_line_count} line
              {row.conflict_cascade_line_count === 1 ? "" : "s"} revised
            </span>
          )}
      </div>

      <p className="import-conflict-detail">{row.conflict_detail}</p>

      <div className="import-conflict-diff">
        <Diff
          label={`live ${row.conflict_field ?? ""}`}
          before={row.conflict_current_value}
          after={row.conflict_file_value}
        />
        {row.conflict_kind === "ConcurrentRevision" && (
          <Diff
            label="at staging"
            before={
              row.baseline_quantity === null
                ? null
                : row.baseline_quantity.toLocaleString()
            }
            after={
              row.matched_quantity === null
                ? null
                : row.matched_quantity.toLocaleString()
            }
          />
        )}
      </div>

      {!readOnly && (
        <div className="import-conflict-actions">
          <button
            type="button"
            className="btn-plain"
            disabled={busy}
            onClick={onPreview}
          >
            {preview ? "Reload coverage impact" : "Review coverage impact"}
          </button>
          {approved ? (
            <>
              <span className="import-approved-by">
                Approved
                {row.override_approved_by_user_name
                  ? ` by ${row.override_approved_by_user_name}`
                  : ""}
                {row.override_approved_by
                  ? ` on behalf of ${row.override_approved_by}`
                  : ""}
                {row.override_approved_at
                  ? ` on ${formatDay(row.override_approved_at)}`
                  : ""}
                .
              </span>
              <button
                type="button"
                className="link-btn"
                disabled={busy}
                onClick={() => onApprove(false)}
              >
                Withdraw approval
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn-approve"
                disabled={busy}
                onClick={() => onApprove(true)}
              >
                Approve override
              </button>
              <span className="sub-settled">
                Apply refuses this row — and the whole batch — until you do, or
                until you skip it.
              </span>
            </>
          )}
        </div>
      )}

      {previewError && <p className="form-error">{previewError}</p>}

      {preview && (
        <div className="import-preview">
          <h5>What-if only — nothing here has been persisted</h5>
          <div className="impact-tiles">
            <div className="impact-tile">
              <div className="impact-tile-label">Covered lines</div>
              <div className="impact-tile-value num">
                {preview.covered_lines_before}
                <span className="change-arrow">→</span>
                {preview.covered_lines_after}
              </div>
            </div>
            <div className="impact-tile impact-tile-worse">
              <div className="impact-tile-label">Unrecoverable lines</div>
              <div className="impact-tile-value num">
                {preview.unrecoverable_lines_before}
                <span className="change-arrow">→</span>
                {preview.unrecoverable_lines_after}
              </div>
            </div>
            <div className="impact-tile">
              <div className="impact-tile-label">Lines revised</div>
              <div className="impact-tile-value num">
                {preview.cascade_line_count}
              </div>
              <div className="impact-tile-note">
                includes lines this file never listed
              </div>
            </div>
          </div>

          {/* CHANGED lines, plus the line this row names even if its verdict held.
              NOT the whole pool: the response deliberately carries every line the
              two passes had an opinion about (so a client can show the full book),
              but this panel exists to answer one question — "what does approving
              this do?" — and 70 unchanged rows bury the four that answer it. The
              omitted count is stated so the list is not silently partial. */}
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Well</th>
                  <th>Product</th>
                  <th>Quantity</th>
                  <th>Coverage</th>
                  <th>In this file?</th>
                </tr>
              </thead>
              <tbody>
                {preview.line_changes
                  .filter((c) => c.changed || c.named_by_the_row)
                  .map((c) => (
                  <tr key={c.demand_line_id}>
                    <td>{c.well_name}</td>
                    <td>{c.product_description ?? c.product_id}</td>
                    <td className="num">
                      {c.quantity_before.toLocaleString()}
                      {c.quantity_after !== c.quantity_before && (
                        <>
                          <span className="change-arrow">→</span>
                          {c.quantity_after.toLocaleString()}
                        </>
                      )}{" "}
                      <span className="sub-settled">
                        {c.unit_of_measure ?? "unit unknown"}
                      </span>
                    </td>
                    <td>
                      <span className="diff-before">{c.coverage_before}</span>
                      <span className="change-arrow">→</span>
                      <strong>{c.coverage_after}</strong>
                    </td>
                    <td>
                      {c.named_by_the_row ? (
                        "yes"
                      ) : (
                        <span className="import-cascade">
                          not in the file
                        </span>
                      )}
                    </td>
                  </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {(() => {
            const hidden = preview.line_changes.filter(
              (c) => !c.changed && !c.named_by_the_row
            ).length;
            return hidden === 0 ? null : (
              <p className="import-preview-note">
                {hidden} other line{hidden === 1 ? "" : "s"} in this customer's
                pool {hidden === 1 ? "was" : "were"} re-evaluated and{" "}
                {hidden === 1 ? "its" : "their"} coverage verdict did not move.
                They are omitted here to keep the change legible, not because they
                were skipped — the whole pool is recomputed, because inventory is
                shared and a line entering scope competes for the same steel.
              </p>
            );
          })()}

          {preview.notes.map((n, i) => (
            <p className="import-preview-note" key={i}>
              {n}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

export default function DemandImport() {
  const [batches, setBatches] = useState<DemandImportBatchSummary[]>([]);
  const [batch, setBatch] = useState<DemandImportBatch | null>(null);
  const [listError, setListError] = useState<unknown>(null);

  const [file, setFile] = useState<File | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  const [rowBusy, setRowBusy] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);

  const [applying, setApplying] = useState(false);
  const [applyResult, setApplyResult] = useState<DemandImportApplyResult | null>(
    null
  );
  const [applyError, setApplyError] = useState<string | null>(null);

  // ---- Template download (step 1) --------------------------------------
  const [customers, setCustomers] = useState<CustomerSummary[]>([]);
  const [customersError, setCustomersError] = useState<unknown>(null);
  const [templateCustomer, setTemplateCustomer] = useState("");

  // ---- Conflict previews, keyed by row id -----------------------------
  // Held per row rather than one-at-a-time: a batch can carry several conflicts and
  // a planner comparing two of them should not have the first collapse.
  const [previews, setPreviews] = useState<
    Record<string, ImportConflictPreview>
  >({});
  const [previewErrors, setPreviewErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    api
      .getCustomers()
      .then((c) => {
        setCustomers(c);
        setCustomersError(null);
        if (c.length > 0) setTemplateCustomer((cur) => cur || c[0].id);
      })
      .catch(setCustomersError);
  }, []);

  function refreshList() {
    api
      .listDemandImports()
      .then((b) => {
        setBatches(b);
        setListError(null);
      })
      .catch(setListError);
  }

  useEffect(refreshList, []);

  const readOnly = batch?.status === "Applied";

  function chooseFile(f: File | null) {
    setUploadError(null);
    if (!f) {
      setFile(null);
      return;
    }
    if (!f.name.toLowerCase().endsWith(".xlsx")) {
      setFile(null);
      setUploadError(
        `"${f.name}" is not an .xlsx workbook. Only .xlsx is accepted — ` +
          "re-save the file as an Excel workbook and try again."
      );
      return;
    }
    if (f.size > MAX_BYTES) {
      setFile(null);
      setUploadError(
        `"${f.name}" is ${(f.size / 1024 / 1024).toFixed(1)} MB, over the ` +
          "20 MB limit. Split the sheet and upload it in parts."
      );
      return;
    }
    setFile(f);
  }

  function upload() {
    if (!file) return;
    setUploading(true);
    setUploadError(null);
    setApplyResult(null);
    setApplyError(null);
    api
      .uploadDemandImport(file)
      .then((b) => {
        setBatch(b);
        refreshList();
      })
      .catch((e) => setUploadError(String(e instanceof Error ? e.message : e)))
      .finally(() => setUploading(false));
  }

  function openBatch(id: string) {
    setApplyResult(null);
    setApplyError(null);
    setRowError(null);
    // Previews are dropped: they are computed against live data at the moment they
    // were requested, and a stale one beside a reloaded batch would be the exact
    // mistake the server refuses to make by storing a conflict flag.
    setPreviews({});
    setPreviewErrors({});
    api
      .getDemandImport(id)
      .then(setBatch)
      .catch((e) => setListError(e));
  }

  /** Replaces one row in place, after a decision or an approval. */
  function patchRow(updated: DemandImportRow) {
    setBatch((b) =>
      b
        ? { ...b, rows: b.rows.map((r) => (r.id === updated.id ? updated : r)) }
        : b
    );
  }

  async function loadPreview(row: DemandImportRow) {
    if (!batch) return;
    setRowBusy(row.id);
    setPreviewErrors((e) => ({ ...e, [row.id]: "" }));
    try {
      const impact = await api.getDemandImportConflictPreview(batch.id, row.id);
      setPreviews((p) => ({ ...p, [row.id]: impact }));
    } catch (e) {
      setPreviewErrors((errs) => ({ ...errs, [row.id]: errorText(e) }));
    } finally {
      setRowBusy(null);
    }
  }

  /**
   * The SECOND decision. Deliberately not folded into `decide`: accepting a row and
   * approving an override of live data are different acts, and one click doing both
   * would make the approval reachable without being read.
   */
  async function approveOverride(row: DemandImportRow, approved: boolean) {
    if (!batch || readOnly) return;
    setRowBusy(row.id);
    setRowError(null);
    try {
      patchRow(
        await api.setDemandImportOverrideApproval(batch.id, row.id, approved)
      );
    } catch (e) {
      setRowError(errorText(e));
    } finally {
      setRowBusy(null);
    }
  }

  /** Records ONE row's decision. Never applies anything. */
  async function decide(row: DemandImportRow, decision: DemandImportDecision) {
    if (!batch || readOnly) return;
    setRowBusy(row.id);
    setRowError(null);
    try {
      const updated = await api.setDemandImportRowDecision(
        batch.id,
        row.id,
        decision
      );
      setBatch((b) =>
        b
          ? {
              ...b,
              rows: b.rows.map((r) => (r.id === updated.id ? updated : r)),
              pending_count:
                b.rows.filter((r) =>
                  r.id === updated.id
                    ? updated.decision === "Pending"
                    : r.decision === "Pending"
                ).length,
            }
          : b
      );
    } catch (e) {
      setRowError(e instanceof Error ? e.message : String(e));
    } finally {
      setRowBusy(null);
    }
  }

  /**
   * Fills in the matcher's suggestion for every still-undecided row. It records
   * decisions only — it deliberately does NOT call apply, so the planner still
   * has to read the result and press Apply themselves.
   */
  async function acceptAllSuggestions() {
    if (!batch || readOnly) return;
    const pending = batch.rows.filter((r) => r.decision === "Pending");
    for (const row of pending) {
      const s = suggestedDecision(row);
      if (s) await decide(row, s);
    }
  }

  function apply() {
    if (!batch) return;
    setApplying(true);
    setApplyError(null);
    api
      .applyDemandImport(batch.id)
      .then((res) => {
        setApplyResult(res);
        setBatch(res.batch);
        refreshList();
      })
      .catch((e) => setApplyError(e instanceof Error ? e.message : String(e)))
      .finally(() => setApplying(false));
  }

  const decidedCount =
    batch?.rows.filter((r) => r.decision !== "Pending").length ?? 0;
  const pendingCount =
    batch?.rows.filter((r) => r.decision === "Pending").length ?? 0;
  /**
   * Accepted rows whose conflict is still unapproved. Apply is refused by the SERVER
   * while this is non-zero (all-or-nothing, so the clean rows are refused too); the
   * disabled button is a courtesy that saves a round trip, not the guarantee.
   *
   * Derived from the rows rather than read from `batch.unapproved_conflict_count`, so
   * it stays right after an in-place approve or decide without a batch reload — the
   * server's count is the same number computed the same way.
   */
  const blockingCount = batch?.rows.filter(isBlocking).length ?? 0;

  const stepState = (n: number): "done" | "current" | "todo" => {
    const at = !batch ? 1 : readOnly ? 5 : decidedCount > 0 ? 4 : 2;
    return n < at ? "done" : n === at ? "current" : "todo";
  };

  return (
    <div>
      <div className="scenario-head">
        <div>
          <h1>Demand Import</h1>
          <p className="scenario-sub">
            Uploading stages and suggests. It changes no demand. Every row is
            applied only because you decided it should be.
          </p>
        </div>
        <Link className="btn-plain" to="/demand">
          Back to demand
        </Link>
      </div>

      <ol className="import-steps">
        <Step n={1} name="Upload" state={stepState(1)} />
        <Step n={2} name="Staging" state={stepState(2)} />
        <Step n={3} name="Match" state={stepState(3)} />
        <Step n={4} name="Review" state={stepState(4)} />
        <Step n={5} name="Apply" state={stepState(5)} />
      </ol>

      {/* ---- Step 1: Upload ------------------------------------------------ */}
      <section className="scenario-section">
        <h2>1 · Upload a workbook</h2>
        <p className="scenario-section-note">
          An .xlsx file up to 20 MB. Row 1 of the first worksheet is the header;
          header matching ignores case, spaces and underscores.
        </p>
        <div className="override-form">
          <label>
            <span>Workbook (.xlsx)</span>
            <input
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              onChange={(e) => chooseFile(e.target.files?.[0] ?? null)}
            />
          </label>
          <button type="button" disabled={!file || uploading} onClick={upload}>
            {uploading ? "Staging…" : "Upload and stage"}
          </button>
          {uploadError && <p className="form-error">{uploadError}</p>}
        </div>

        {/* Download the CURRENT book, not a blank sheet. Placed BEFORE the column
            contract on purpose: for a planner editing existing demand the file is
            the better starting point, and the contract below is what they need only
            if they are building a sheet from scratch. */}
        <div className="template-card">
          <h3>Start from your current data</h3>
          <p className="scenario-section-note">
            Download a customer's <strong>live demand</strong> as an .xlsx already
            in the column contract below — every existing line, no row cap. Edit the
            cells you mean to change and upload it here. Re-uploading it
            <strong> unchanged reports no changes at all</strong>, so nothing moves
            except what you edited.
          </p>
          {customersError !== null && (
            <LoadError what="the customer list" error={customersError} />
          )}
          <div className="template-row">
            <label>
              <span>Customer</span>
              <select
                value={templateCustomer}
                onChange={(e) => setTemplateCustomer(e.target.value)}
              >
                {customers.length === 0 && <option value="">—</option>}
                {customers.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
            {/* A plain link, so the browser's own download machinery handles the
                filename, the progress and the save dialog. */}
            <a
              className="btn-download"
              aria-disabled={templateCustomer === "" ? "true" : "false"}
              href={
                templateCustomer
                  ? api.demandImportTemplateUrl(templateCustomer)
                  : undefined
              }
            >
              Download current data as template
            </a>
          </div>
          <p className="contract-note">
            The <code>status</code> column carries each row's <em>well's</em> demand
            status, repeated on every row of that well — because that is what the
            column means. Changing it asserts a new status for the whole well and
            cascades a revision to every line of it; the review screen flags that as
            a conflict and asks you to approve it explicitly. Deleting a row does
            <strong> not</strong> delete demand — an import only revises and creates.
            The workbook's second sheet repeats all of this, so it travels with the
            file.
          </p>
        </div>

        <div className="contract-card">
          <h3>Expected columns</h3>
          <p className="contract-line">
            <span className="contract-tag contract-tag-req">Required</span>
            {(batch?.column_contract.required ?? [
              "well",
              "product",
              "quantity",
              "ros_date",
            ]).map((c) => (
              <code key={c} className="contract-col">
                {c}
              </code>
            ))}
          </p>
          <p className="contract-line">
            <span className="contract-tag contract-tag-opt">Optional</span>
            {(batch?.column_contract.optional ?? ["status", "profile"]).map(
              (c) => (
                <code key={c} className="contract-col">
                  {c}
                </code>
              )
            )}
          </p>
          <p className="contract-note">
            ROS dates must be a real Excel date cell, or text as YYYY-MM-DD or
            YYYY/MM/DD. Ambiguous forms such as 03/04/2027 are rejected per row
            rather than guessed. A blank <code>status</code> cell asserts nothing, so
            it inherits <em>the well's current status</em> on a revision and on a new
            line alike — an import with no status column cannot move any well's
            coverage scope. A blank <code>profile</code> inherits the matched line's
            value on a revision and defaults to Primary on a new line.
            {batch?.column_contract === undefined &&
              " (Shown from the documented contract; the server's own contract replaces this once a batch is loaded.)"}
          </p>
        </div>
      </section>

      {/* ---- Previous batches --------------------------------------------- */}
      <section className="scenario-section">
        <h2>Staged batches</h2>
        {listError !== null && (
          <LoadError what="the import batch list" error={listError} />
        )}
        {batches.length === 0 && listError === null && (
          <p className="scenario-section-note">No batches yet.</p>
        )}
        <div className="table-scroll">
          {batches.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>File</th>
                  <th>Status</th>
                  <th>Rows</th>
                  <th>Errors</th>
                  <th>Uploaded</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {batches.map((b) => (
                  <tr key={b.id}>
                    <td>{b.filename ?? b.id}</td>
                    <td>
                      <span className={`badge badge-status-${b.status === "Applied" ? "Applied" : "Draft"}`}>
                        {b.status}
                      </span>
                    </td>
                    <td className="num">{b.row_count}</td>
                    <td className="num">{b.error_count}</td>
                    <td className="num">{formatDay(b.created_at)}</td>
                    <td>
                      <button
                        type="button"
                        className="link-btn"
                        onClick={() => openBatch(b.id)}
                      >
                        {batch?.id === b.id ? "Reload" : "Review"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {/* ---- Steps 2-4: Staging / Match / Review -------------------------- */}
      {batch && (
        <section className="scenario-section">
          <h2>
            2–4 · Staged rows from {batch.filename ?? batch.id}
            {batch.sheet_name && (
              <span className="mrp-count">sheet “{batch.sheet_name}”</span>
            )}
          </h2>

          {readOnly && (
            <div className="applied-banner">
              <p>
                <strong>This batch has been applied</strong> and is read-only.
                Decisions can no longer be changed (the server rejects further
                edits with 409). Re-upload the workbook to stage a fresh batch.
              </p>
              {batch.applied_at && <p>Applied {formatDay(batch.applied_at)}.</p>}
            </div>
          )}

          {!readOnly && (
            <p className="scenario-section-note">
              Nothing here has changed any demand yet. The{" "}
              <strong>suggestion</strong> is the matcher's; the{" "}
              <strong>decision</strong> is yours.
            </p>
          )}

          <div className="impact-tiles">
            <div className="impact-tile">
              <div className="impact-tile-label">Rows</div>
              <div className="impact-tile-value num">{batch.row_count}</div>
            </div>
            <div className="impact-tile">
              <div className="impact-tile-label">Revision suggestions</div>
              <div className="impact-tile-value num">
                {batch.revision_suggestion_count}
              </div>
            </div>
            <div className="impact-tile">
              <div className="impact-tile-label">New suggestions</div>
              <div className="impact-tile-value num">
                {batch.new_suggestion_count}
              </div>
            </div>
            <div className="impact-tile impact-tile-worse">
              <div className="impact-tile-label">Errors</div>
              <div className="impact-tile-value num">{batch.error_count}</div>
              <div className="impact-tile-note">may only be skipped</div>
            </div>
            <div className="impact-tile">
              <div className="impact-tile-label">Undecided</div>
              <div className="impact-tile-value num">{pendingCount}</div>
              <div className="impact-tile-note">apply leaves these alone</div>
            </div>
            {batch.conflict_count > 0 && (
              <div className="impact-tile impact-tile-worse">
                <div className="impact-tile-label">Conflicts with live data</div>
                <div className="impact-tile-value num">
                  {batch.conflict_count}
                </div>
                <div className="impact-tile-note">
                  {blockingCount === 0
                    ? "none blocking apply"
                    : `${blockingCount} awaiting your approval`}
                </div>
              </div>
            )}
          </div>

          {!readOnly && (
            <div className="apply-actions" style={{ marginBottom: 12 }}>
              <button
                type="button"
                className="btn-plain"
                disabled={pendingCount === 0 || rowBusy !== null}
                onClick={acceptAllSuggestions}
              >
                Accept all suggestions ({pendingCount} undecided)
              </button>
              <span className="sub-settled">
                Fills in decisions only. It does not apply anything.
              </span>
            </div>
          )}

          {rowError && <p className="form-error">{rowError}</p>}

          <div className="import-rows">
            {batch.rows.map((row) => {
              const allowed = allowedDecisions(row);
              const suggestion = suggestedDecision(row);
              const isError = row.match_type === "Error";
              const overrideRevision =
                row.match_type === "New" && row.matched_demand_line_id !== null;
              const conflictClass = !row.is_conflict
                ? ""
                : row.override_approved
                ? " import-row-conflict import-row-conflict-approved"
                : " import-row-conflict";
              return (
                <div
                  key={row.id}
                  className={`import-row import-row-${row.match_type.toLowerCase()}${conflictClass}`}
                >
                  <div className="import-row-head">
                    <span className="import-row-n">
                      Excel row {row.row_number}
                    </span>
                    <span
                      className={`badge import-match import-match-${row.match_type.toLowerCase()}`}
                    >
                      Suggestion: {row.match_type}
                    </span>
                    <span
                      className={
                        row.decision === "Pending"
                          ? "badge badge-not-evaluated"
                          : "badge badge-status-Applied"
                      }
                    >
                      {DECISION_LABEL[row.decision]}
                    </span>
                    {row.requires_override_approval && (
                      <span className="badge badge-oracle-block">
                        Needs override approval
                      </span>
                    )}
                    {row.applied && (
                      <span className="badge badge-Covered">Applied</span>
                    )}
                  </div>

                  {isError && (
                    <div className="import-error">
                      <span className="import-error-tag">Row error</span>
                      <span>{row.error}</span>
                    </div>
                  )}

                  {row.is_conflict && (
                    <ConflictPanel
                      row={row}
                      readOnly={readOnly}
                      busy={rowBusy === row.id}
                      preview={previews[row.id] ?? null}
                      previewError={previewErrors[row.id] || null}
                      onPreview={() => loadPreview(row)}
                      onApprove={(approved) => approveOverride(row, approved)}
                    />
                  )}

                  <div className="import-row-grid">
                    <div>
                      <h4>In your file</h4>
                      <RawCells row={row} />
                    </div>
                    <div>
                      <h4>Parsed</h4>
                      <ParsedCells row={row} />
                    </div>
                    <div>
                      <h4>
                        {row.match_type === "Revision"
                          ? "Would change"
                          : overrideRevision
                          ? "Nearest existing line"
                          : "Existing line"}
                      </h4>
                      {row.matched_demand_line_id ? (
                        <>
                          <Diff
                            label="quantity"
                            before={row.matched_quantity}
                            after={row.quantity}
                          />
                          <Diff
                            label="ROS"
                            before={
                              row.matched_ros_date
                                ? formatDay(row.matched_ros_date)
                                : null
                            }
                            after={row.ros_date ? formatDay(row.ros_date) : null}
                          />
                          <Diff
                            label="status"
                            before={row.matched_status}
                            after={row.status}
                          />
                          <Diff
                            label="profile"
                            before={row.matched_profile}
                            after={row.profile}
                          />
                        </>
                      ) : (
                        <p className="sub-settled">
                          {isError
                            ? "No match was attempted."
                            : "No existing demand line — this would be created new."}
                        </p>
                      )}
                    </div>
                  </div>

                  {row.match_reason && (
                    <p className="import-reason">{row.match_reason}</p>
                  )}

                  {overrideRevision && !readOnly && (
                    <p className="import-override-note">
                      The matcher suggests new demand, but it attached the
                      nearest existing line so you can overrule it. If this is
                      really the same demand re-planned, accept it as a revision.
                    </p>
                  )}

                  {row.apply_error && (
                    <p className="form-error">
                      Apply failed for this row: {row.apply_error}
                    </p>
                  )}

                  {!readOnly && (
                    <div className="import-decisions">
                      {allowed.map((d) => (
                        <button
                          key={d}
                          type="button"
                          className={
                            row.decision === d
                              ? "btn-decision btn-decision-on"
                              : "btn-decision"
                          }
                          disabled={rowBusy === row.id}
                          onClick={() => decide(row, d)}
                        >
                          {DECISION_LABEL[d]}
                          {d === suggestion && (
                            <span className="btn-suggested">suggested</span>
                          )}
                        </button>
                      ))}
                      {isError && (
                        <span className="sub-settled">
                          An error row cannot be accepted — fix the spreadsheet
                          and re-upload, or skip it.
                        </span>
                      )}
                      {row.decision !== "Pending" && (
                        <button
                          type="button"
                          className="link-btn"
                          disabled={rowBusy === row.id}
                          onClick={() => decide(row, "Pending")}
                        >
                          Undecide
                        </button>
                      )}
                    </div>
                  )}
                  {readOnly && row.applied_demand_line_id && (
                    <p className="sub-settled">
                      Wrote demand line{" "}
                      <span className="num">{row.applied_demand_line_id}</span>.
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {/* ---- Step 5: Apply ------------------------------------------------ */}
      {batch && !readOnly && (
        <section className="scenario-section">
          <h2>5 · Apply</h2>
          <div className="apply-panel">
            <p className="apply-warning">
              Applying writes real demand. Each accepted row goes through the
              normal revision path, so it creates a demand revision and an impact
              record, and triggers a coverage recompute.
              {pendingCount > 0 && (
                <>
                  {" "}
                  <strong>{pendingCount}</strong> undecided row
                  {pendingCount === 1 ? "" : "s"} will be left completely alone —
                  they are not silently accepted.
                </>
              )}
            </p>
            {decidedCount === 0 && (
              <p className="apply-blocked">
                Nothing to apply yet. Decide at least one row first.
              </p>
            )}
            {blockingCount > 0 && (
              <p className="apply-blocked">
                <strong>
                  {blockingCount} accepted row
                  {blockingCount === 1 ? "" : "s"} conflict
                  {blockingCount === 1 ? "s" : ""} with the live demand book
                </strong>{" "}
                and the override has not been approved. Nothing at all will be
                applied until every one of them is either approved or skipped —
                including the rows that are perfectly clean. That is deliberate: a
                batch cannot be applied twice, so applying the clean half now would
                leave these rows permanently unappliable and you would have to
                re-upload the file to recover a decision you had already made.
              </p>
            )}
            <div className="apply-actions">
              <button
                type="button"
                className="btn-danger"
                disabled={decidedCount === 0 || blockingCount > 0 || applying}
                onClick={apply}
              >
                {applying
                  ? "Applying…"
                  : `Apply ${decidedCount} decided row${
                      decidedCount === 1 ? "" : "s"
                    }`}
              </button>
            </div>
            {applyError && <p className="form-error">{applyError}</p>}
          </div>
        </section>
      )}

      {applyResult && (
        <section className="scenario-section">
          <div className="applied-banner">
            <p>
              <strong>Applied.</strong> {applyResult.revised_count} revised,{" "}
              {applyResult.created_count} created, {applyResult.skipped_count}{" "}
              skipped.
            </p>
            <p>
              <strong>{applyResult.pending_count}</strong> row
              {applyResult.pending_count === 1 ? "" : "s"} were left undecided and
              were not touched. <strong>{applyResult.error_count}</strong> error
              row{applyResult.error_count === 1 ? "" : "s"} in the batch.
            </p>
            <p>
              {applyResult.impact_record_ids.length} impact record
              {applyResult.impact_record_ids.length === 1 ? "" : "s"} written —
              these show on the Home dashboard's Demand Changes card.
            </p>
            {applyResult.failed_row_ids.length > 0 ? (
              <p>
                <strong>
                  {applyResult.failed_row_ids.length} row
                  {applyResult.failed_row_ids.length === 1 ? "" : "s"} failed:
                </strong>{" "}
                {applyResult.failed_row_ids.join(", ")}. Each failing row's own{" "}
                <code>apply_error</code> is shown against it above.
              </p>
            ) : (
              <p>No rows failed.</p>
            )}
            <p>
              {/* The natural next action after landing new demand. */}
              <Link to="/coverage">See the coverage impact now →</Link>
            </p>
          </div>
        </section>
      )}
    </div>
  );
}
