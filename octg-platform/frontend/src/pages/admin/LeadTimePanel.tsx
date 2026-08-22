import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  adminApi,
  errorText,
  LeadTimeComponentChange,
  LeadTimeComponentRow,
  LeadTimeComponentsResponse,
} from "../../api/client";
import LoadError from "../../components/LoadError";

/** What each dimension is keyed off, so the add form is fillable without the source. */
const DIMENSION_KEYED_ON: Record<string, string> = {
  "OD/WT":
    'the canonical "size weight" pair, e.g. "9-5/8 53.5". A product with no weight recorded keys on its size alone.',
  Grade: 'the grade FAMILY (grade_type), e.g. "Carbon" or "13CR" — not the grade itself.',
  Connection: 'the connection, e.g. "VAM 21" or "VAM TOP".',
  Logistics:
    'the grade family, but usually the wildcard — one sailing allowance normally applies to everything.',
};

/**
 * The server's report of what a write did.
 *
 * Rendered after EVERY successful mutation, including ones that changed nothing —
 * "no product's lead time moved" is a result, and hiding it would leave a planner
 * unsure whether the save landed. `note` is the server's prose and is shown verbatim;
 * the lists below it are the same facts in a form a screen can scan.
 */
function LeadTimeChangeReport({
  change,
  onDismiss,
}: {
  change: LeadTimeComponentChange;
  onDismiss: () => void;
}) {
  const retracted = change.product_changes.filter((p) => p.became_unmodelled);
  return (
    <div
      className={`admin-report ${
        retracted.length > 0 ? "admin-report-severe" : ""
      }`}
    >
      <div className="admin-report-head">
        <span className="admin-report-tag">
          Component {change.action}
          {retracted.length > 0 ? " — verdicts retracted" : ""}
        </span>
        <button type="button" className="admin-report-dismiss" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
      <p className="admin-report-note">{change.note}</p>
      {change.product_changes.length > 0 && (
        <table className="admin-report-table">
          <thead>
            <tr>
              <th>Product</th>
              <th>Lead time before</th>
              <th>Lead time after</th>
            </tr>
          </thead>
          <tbody>
            {change.product_changes.map((p) => (
              <tr
                key={p.product_id}
                className={p.became_unmodelled ? "lt-row-missing" : ""}
              >
                <td>{p.product_description ?? p.product_id}</td>
                <td className="num">
                  {p.modelled_before ? (
                    `${p.total_months_before} mo`
                  ) : (
                    <span className="lt-total-unmodelled">not modelled</span>
                  )}
                </td>
                <td className="num">
                  {p.modelled_after ? (
                    `${p.total_months_after} mo`
                  ) : (
                    <span className="lt-total-unmodelled">
                      not modelled (missing{" "}
                      {p.missing_dimensions_after.join(", ")})
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {/* Coverage stores its Uncovered/Unrecoverable choice, so it does NOT
          self-heal on read — the server recomputed it in the same request, and the
          wells whose rollup moved are the visible proof. */}
      {change.well_changes.length > 0 ? (
        <div className="admin-report-wells">
          <strong>Coverage recomputed — {change.well_changes.length} well(s) moved:</strong>
          <ul>
            {change.well_changes.map((w) => (
              <li key={w.well_id}>
                <Link to={`/wells/${w.well_id}`}>{w.well_name}</Link>{" "}
                {w.coverage_before ?? "not evaluated"} &rarr;{" "}
                {w.coverage_after ?? "not evaluated"}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="admin-report-wells">
          Coverage was recomputed for{" "}
          {change.recomputed_customer_ids.length} customer(s); no well&apos;s
          rollup moved.
        </p>
      )}
    </div>
  );
}

/**
 * One editable component row. Inline edit, because a table of 10 rows needs no modal.
 *
 * `editing` is owned by the PARENT, not by this component, and that is load-bearing
 * rather than stylistic. The parent reloads the whole table from the server after every
 * successful write, which yields a new row object with the SAME id — so React keeps this
 * component instance alive and any local `editing` flag would survive the save, leaving
 * the row sitting in edit mode showing inputs after it had already been committed. The
 * parent clears `editingId` when the write succeeds and leaves it set when the write is
 * refused, which is exactly the behaviour wanted in both cases: a refused edit keeps the
 * planner's draft on screen beside the server's explanation.
 */
function ComponentRow({
  row,
  wildcard,
  busy,
  editing,
  onBeginEdit,
  onCancelEdit,
  onSave,
  onDelete,
}: {
  row: LeadTimeComponentRow;
  wildcard: string;
  busy: boolean;
  editing: boolean;
  onBeginEdit: () => void;
  onCancelEdit: () => void;
  onSave: (months: number, label: string | null) => void;
  onDelete: () => void;
}) {
  const [months, setMonths] = useState(String(row.months));
  const [label, setLabel] = useState(row.label ?? "");

  // Re-seed the draft whenever the server's value changes underneath — otherwise a
  // second edit would start from the previous draft rather than from what is stored.
  useEffect(() => {
    setMonths(String(row.months));
    setLabel(row.label ?? "");
  }, [row.months, row.label]);

  const parsed = Number(months);
  const valid = months.trim() !== "" && Number.isFinite(parsed) && parsed >= 0;

  if (!editing) {
    return (
      <tr className={row.shared ? "admin-lt-shared-row" : ""}>
        <td className="lt-dim">
          {row.attribute_value === wildcard ? (
            <>
              <code>{wildcard}</code>
              {/* The SAME convention the lead-time breakdown uses for a
                  wildcard-matched term, so a planner reads one visual language on
                  both screens. */}
              <span
                className="lt-shared"
                title={`This row is the catch-all for ${row.dimension}: it applies to every product that has no more specific row on this dimension.`}
              >
                (shared)
              </span>
            </>
          ) : (
            row.attribute_value
          )}
        </td>
        <td className="num lt-total">{row.months} mo</td>
        <td className="lt-label">{row.label ?? <em>no label</em>}</td>
        <td className="admin-lt-actions">
          <button type="button" onClick={onBeginEdit} disabled={busy}>
            Edit
          </button>
          <button
            type="button"
            className="admin-lt-delete"
            disabled={busy}
            onClick={() => {
              // A confirmation dialog, and it states the actual consequence rather
              // than "are you sure". It is a UI courtesy, NOT the safety mechanism —
              // the server performs the delete and reports the blast radius either
              // way, which is what the report panel above renders.
              const consequence = row.shared
                ? `This is the SHARED (wildcard) row for ${row.dimension}. Deleting it removes that term from every product with no more specific row — usually most of the catalogue.`
                : `This removes the ${row.dimension} term for "${row.attribute_value}".`;
              if (
                window.confirm(
                  `${consequence}\n\nAny product left without a ${row.dimension} row becomes NOT MODELLED: it loses its recommended order date, and it can no longer be judged Unrecoverable — so an existing Unrecoverable verdict will be retracted.\n\nThe server will report exactly which products and wells moved. Proceed?`
                )
              ) {
                onDelete();
              }
            }}
          >
            Delete
          </button>
        </td>
      </tr>
    );
  }

  return (
    <tr className={row.shared ? "admin-lt-shared-row" : ""}>
      <td className="lt-dim">
        {row.attribute_value === wildcard ? <code>{wildcard}</code> : row.attribute_value}
        {/* Stated where somebody would try to change it. The pair is the row's
            identity; re-pointing it at different products is a delete plus a create,
            and the server refuses to do it under a PATCH. */}
        <span className="admin-lt-immutable">
          dimension and attribute value cannot be edited — delete and re-add
        </span>
      </td>
      <td className="num">
        <input
          className="admin-lt-months"
          type="number"
          step="0.5"
          min="0"
          value={months}
          onChange={(e) => setMonths(e.target.value)}
        />
        {!valid && (
          <span className="admin-lt-invalid">
            months must be a number of 0 or more. 0 is valid and means &ldquo;this
            dimension adds nothing&rdquo; — which is not the same as deleting the row.
          </span>
        )}
      </td>
      <td>
        <input
          className="admin-lt-label"
          type="text"
          value={label}
          placeholder="e.g. Ex-mill, Sailing, Threading"
          onChange={(e) => setLabel(e.target.value)}
        />
      </td>
      <td className="admin-lt-actions">
        <button
          type="button"
          disabled={!valid || busy}
          onClick={() => onSave(parsed, label.trim() === "" ? null : label.trim())}
        >
          Save
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setMonths(String(row.months));
            setLabel(row.label ?? "");
            onCancelEdit();
          }}
        >
          Cancel
        </button>
      </td>
    </tr>
  );
}

export default function LeadTimePanel() {
  const [data, setData] = useState<LeadTimeComponentsResponse | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  /** A REFUSAL from the server (409 duplicate, 400 bad dimension). Shown verbatim. */
  const [writeError, setWriteError] = useState<string | null>(null);
  const [change, setChange] = useState<LeadTimeComponentChange | null>(null);
  const [busy, setBusy] = useState(false);
  /**
   * Which row is open for editing, by id. Owned here rather than inside the row so a
   * successful save can CLOSE it — see `ComponentRow`. Only one row edits at a time,
   * which also stops two concurrent edits racing the shared table reload.
   */
  const [editingId, setEditingId] = useState<string | null>(null);

  const [newDimension, setNewDimension] = useState("");
  const [newValue, setNewValue] = useState("");
  const [newMonths, setNewMonths] = useState("");
  const [newLabel, setNewLabel] = useState("");

  function load() {
    return adminApi
      .getLeadTimeComponents()
      .then((d) => {
        setData(d);
        if (newDimension === "" && d.dimensions.length > 0) {
          setNewDimension(d.dimensions[0]);
        }
        setLoadError(null);
      })
      .catch(setLoadError);
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Every mutation funnels through here, so all three share one rule set: clear the
   * previous refusal, render the server's change report, and RELOAD the table from the
   * server rather than patching local state. Reloading matters — the response reports
   * what changed, but `dimensions_with_no_rows` and the sort order are the server's to
   * compute, and a locally spliced array would drift from both.
   */
  function mutate(run: () => Promise<LeadTimeComponentChange>) {
    setBusy(true);
    setWriteError(null);
    run()
      .then((result) => {
        setChange(result);
        // Close the inline editor ONLY on success. A refused edit keeps the row open so
        // the planner's draft is still on screen beside the server's explanation, rather
        // than being discarded along with the reason it was rejected.
        setEditingId(null);
        return load();
      })
      .catch((e) => {
        setChange(null);
        setWriteError(errorText(e));
      })
      .finally(() => setBusy(false));
  }

  if (loadError !== null) {
    return (
      <section className="scenario-section">
        <h2>Lead-time components</h2>
        <LoadError what="the lead-time components" error={loadError} />
      </section>
    );
  }
  if (!data) {
    return (
      <section className="scenario-section">
        <h2>Lead-time components</h2>
        <p>Loading…</p>
      </section>
    );
  }

  const parsedNewMonths = Number(newMonths);
  const canAdd =
    newDimension !== "" &&
    newValue.trim() !== "" &&
    newMonths.trim() !== "" &&
    Number.isFinite(parsedNewMonths) &&
    parsedNewMonths >= 0;

  return (
    <section className="scenario-section">
      <h2>Lead-time components</h2>
      <p className="scenario-section-note">
        A lead time is built from <strong>four attribute terms</strong>, never
        stored per SKU: OD/WT + Grade + Connection + Logistics. Each row below is
        one additive term for one attribute value. A planner can read the
        arithmetic, which is the whole reason the number is trusted.
      </p>
      <p className="scenario-section-note">
        <strong>All four dimensions are required.</strong> If a product has no
        matching row on any one of them its model is <em>incomplete</em>, and the
        platform reports the lead time as <em>not modelled</em> rather than
        summing what it has — a partial sum would be a confidently wrong order
        date. An unmodelled product also gets <em>no</em> Unrecoverable verdict,
        because absent data means &ldquo;cannot judge&rdquo;, not
        &ldquo;hopeless&rdquo;.
      </p>

      {data.incomplete_note && (
        <p className="admin-lt-incomplete">
          <span className="admin-lt-incomplete-tag">
            Model incomplete: {data.dimensions_with_no_rows.join(", ")}
          </span>
          {data.incomplete_note}
        </p>
      )}

      {writeError && (
        <p className="admin-write-error">
          <span className="admin-write-error-tag">Refused — nothing saved</span>
          {writeError}
        </p>
      )}

      {change && (
        <LeadTimeChangeReport change={change} onDismiss={() => setChange(null)} />
      )}

      {/* Grouped by dimension, in the order the breakdown renders them, because the
          four dimensions are added together and a table sorted any other way hides
          that structure. Dimension order comes from the server, not from a literal
          here — a client with its own copy of a closed enum offers options the
          server would reject. */}
      {data.dimensions.map((dimension) => {
        const rows = data.components.filter((c) => c.dimension === dimension);
        return (
          <div key={dimension} className="admin-lt-group">
            <div className="admin-lt-group-head">
              <span className="admin-lt-group-name">{dimension}</span>
              <span className="admin-lt-group-keyed">
                keyed on {DIMENSION_KEYED_ON[dimension] ?? "this product attribute"}
              </span>
            </div>
            {rows.length === 0 ? (
              <div className="empty admin-lt-empty">
                No rows for {dimension}. <strong>Every</strong> product on the
                platform is therefore &ldquo;not modelled&rdquo;, whatever the other
                dimensions say. Add a row — a wildcard (<code>{data.wildcard}</code>)
                row is enough if one allowance applies to everything.
              </div>
            ) : (
              <table className="lt-table admin-lt-table">
                <thead>
                  <tr>
                    <th>Attribute value</th>
                    <th>Months</th>
                    <th>Label (display only)</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <ComponentRow
                      key={row.id}
                      row={row}
                      wildcard={data.wildcard}
                      busy={busy}
                      editing={editingId === row.id}
                      onBeginEdit={() => setEditingId(row.id)}
                      onCancelEdit={() => setEditingId(null)}
                      onSave={(months, label) =>
                        mutate(() =>
                          adminApi.updateLeadTimeComponent(row.id, { months, label })
                        )
                      }
                      onDelete={() =>
                        mutate(() => adminApi.deleteLeadTimeComponent(row.id))
                      }
                    />
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}

      <div className="admin-lt-add">
        <h3>Add a component</h3>
        <p className="admin-lt-add-note">
          Use <code>{data.wildcard}</code> as the attribute value for a row that
          applies to <strong>every</strong> product on its dimension. A specific row
          always beats the wildcard, so a global default plus a handful of exceptions
          is expressible. Only one row may exist per (dimension, attribute value)
          pair — a second would make the breakdown unexplainable, and the server
          refuses it.
        </p>
        <div className="admin-lt-add-grid">
          <label>
            <span>Dimension</span>
            <select
              value={newDimension}
              onChange={(e) => setNewDimension(e.target.value)}
            >
              {data.dimensions.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Attribute value</span>
            <input
              type="text"
              value={newValue}
              placeholder={data.wildcard}
              onChange={(e) => setNewValue(e.target.value)}
            />
          </label>
          <label>
            <span>Months</span>
            <input
              type="number"
              step="0.5"
              min="0"
              value={newMonths}
              onChange={(e) => setNewMonths(e.target.value)}
            />
          </label>
          <label>
            <span>Label (optional)</span>
            <input
              type="text"
              value={newLabel}
              placeholder="e.g. Sailing"
              onChange={(e) => setNewLabel(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="admin-lt-add-button"
            disabled={!canAdd || busy}
            onClick={() =>
              mutate(async () => {
                const result = await adminApi.createLeadTimeComponent({
                  dimension: newDimension,
                  attribute_value: newValue.trim(),
                  months: parsedNewMonths,
                  label: newLabel.trim() === "" ? null : newLabel.trim(),
                });
                setNewValue("");
                setNewMonths("");
                setNewLabel("");
                return result;
              })
            }
          >
            Add component
          </button>
        </div>
        {newDimension !== "" && DIMENSION_KEYED_ON[newDimension] && (
          <p className="admin-lt-add-hint">
            <strong>{newDimension}</strong> is keyed on{" "}
            {DIMENSION_KEYED_ON[newDimension]}
          </p>
        )}
      </div>
    </section>
  );
}
