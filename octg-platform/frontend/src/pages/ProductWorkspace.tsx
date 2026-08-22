import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, ProductOut } from "../api/client";
import LoadError from "../components/LoadError";

/**
 * Product Workspace — the catalogue entry point.
 *
 * This is NOT a second By Item view. `/mrp/by-item/:productId` already renders
 * Inventory / Demand / Runout / Recommendation for a product and it is the
 * modern replacement for the workbook's "By Item" sheet. What was missing was a
 * way to REACH it: every other route into By Item starts from demand (the MRP
 * summary, a well, a recommendation), so a product with no demand — or with no
 * inventory row at all — was unreachable.
 *
 * That gap is exactly where the platform's honesty cases live. Two catalogue
 * products cannot be found from any demand-derived list:
 *
 *   * one whose lead time is NOT MODELLED (no component matches its Connection),
 *   * one with no InventoryOnHand row in ANY Business Unit, whose By Item request
 *     therefore returns 424 rather than reporting an on-hand quantity of 0.
 *
 * `GET /products` exists so both are browsable, and this screen is that browser.
 *
 * The payload carries NO quantity, deliberately. A quantity on a catalogue row
 * would be BU-blind — on-hand belongs to a (Business Unit, product) pair — and
 * that is the leak the BU-scoped inventory table exists to close. So this table
 * shows attributes only, and every number comes from the BU-scoped view behind
 * the link.
 */

const DEBOUNCE_MS = 300;

/** Distinct values for a client-side attribute filter, in stable order. */
function distinct(rows: ProductOut[], pick: (p: ProductOut) => string) {
  return Array.from(new Set(rows.map(pick))).sort();
}

export default function ProductWorkspace() {
  // `q` is served by the backend (substring on description); the attribute
  // dropdowns are narrowed here, over the rows the server returned.
  const [qDraft, setQDraft] = useState("");
  const [q, setQ] = useState("");
  const [activeOnly, setActiveOnly] = useState(true);

  const [type, setType] = useState("");
  const [grade, setGrade] = useState("");
  const [connection, setConnection] = useState("");

  const [rows, setRows] = useState<ProductOut[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  // Typing a description fires one request per keystroke otherwise.
  useEffect(() => {
    if (qDraft === q) return;
    const t = window.setTimeout(() => setQ(qDraft), DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [qDraft, q]);

  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .getProducts({ q: q || undefined, active_only: activeOnly })
      .then((d) => {
        if (!live) return;
        setRows(d);
        setError(null);
      })
      .catch((e) => {
        if (!live) return;
        setError(e);
        setRows([]);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [q, activeOnly]);

  const types = useMemo(() => distinct(rows, (p) => p.type), [rows]);
  const grades = useMemo(() => distinct(rows, (p) => p.grade), [rows]);
  const connections = useMemo(
    () => distinct(rows, (p) => p.connection),
    [rows]
  );

  const shown = rows.filter(
    (p) =>
      (!type || p.type === type) &&
      (!grade || p.grade === grade) &&
      (!connection || p.connection === connection)
  );

  const filtered = shown.length !== rows.length;

  return (
    <div>
      <div className="scenario-head">
        <div>
          <h1>Products</h1>
          <p className="scenario-sub">
            The product catalogue. Pick a product to open its By Item view —
            inventory, consuming demand, runout and recommendation.
          </p>
        </div>
        <Link className="btn-plain" to="/mrp">
          MRP summary
        </Link>
      </div>

      <section className="filter-bar">
        <label className="filter-field">
          <span>Description contains</span>
          <input
            type="search"
            value={qDraft}
            placeholder="e.g. 13CR110, Hydril, TBG 4-1/2"
            onChange={(e) => setQDraft(e.target.value)}
          />
        </label>
        <label className="filter-field">
          <span>Type</span>
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">All types</option>
            {types.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span>Grade</span>
          <select value={grade} onChange={(e) => setGrade(e.target.value)}>
            <option value="">All grades</option>
            {grades.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span>Connection</span>
          <select
            value={connection}
            onChange={(e) => setConnection(e.target.value)}
          >
            <option value="">All connections</option>
            {connections.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-check">
          <input
            type="checkbox"
            checked={activeOnly}
            onChange={(e) => setActiveOnly(e.target.checked)}
          />
          Active products only
        </label>
        <button
          type="button"
          className="btn-plain"
          onClick={() => {
            setQDraft("");
            setQ("");
            setType("");
            setGrade("");
            setConnection("");
            setActiveOnly(true);
          }}
        >
          Clear filters
        </button>
      </section>

      <p className="filter-note">
        The description search is done by the server; type, grade and connection
        narrow the rows it returned.
        {qDraft !== q && <strong> Waiting for typing to settle…</strong>}
      </p>

      {/* Stated on the screen, not only in the code: the absence of a quantity
          column here is a design decision, not an omission. */}
      <p className="catalogue-note">
        <strong>No quantity is shown in this list, deliberately.</strong>{" "}
        On-hand stock is a property of a (Business Unit, product) pair, never of
        the catalogue entry — a single number here would be Business-Unit-blind
        and would read as the total available anywhere. Open a product to see its
        BU-scoped inventory position, with the Oracle-owned figures marked as not
        integrated where they are.
      </p>

      {error !== null && <LoadError what="the product catalogue" error={error} />}

      <div className="list-meta">
        {loading ? (
          <span>Loading…</span>
        ) : (
          <span>
            {shown.length} product{shown.length === 1 ? "" : "s"}
            {filtered && <> of {rows.length} returned</>}
            {activeOnly ? " (active only)" : " (including inactive)"}
          </span>
        )}
      </div>

      <div className="table-scroll">
        <table className="demand-table catalogue-table">
          <thead>
            <tr>
              <th>Description</th>
              <th>Type</th>
              <th>Size</th>
              <th>Grade</th>
              <th>Connection</th>
              <th>By Item</th>
            </tr>
          </thead>
          <tbody>
            {shown.length === 0 && !loading && error === null && (
              <tr>
                <td colSpan={6} className="empty">
                  No products match these filters.
                </td>
              </tr>
            )}
            {shown.map((p) => (
              <tr key={p.id}>
                <td>
                  <Link to={`/mrp/by-item/${p.id}`}>
                    {p.description ?? p.id}
                  </Link>
                  {p.description === null && (
                    <span className="catalogue-nodesc">
                      No description on this catalogue row — the id is shown
                      instead of a made-up label.
                    </span>
                  )}
                </td>
                <td>{p.type}</td>
                <td className="num">{p.size}</td>
                <td>{p.grade}</td>
                <td>{p.connection}</td>
                <td>
                  <Link className="link-btn" to={`/mrp/by-item/${p.id}`}>
                    Open
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="catalogue-degrade">
        Opening a product does not always yield a full analysis, and that is
        working as intended. A product with no inventory row in any Business Unit
        cannot have an on-hand quantity reported — the engine refuses to call it
        0 — so By Item returns an explained refusal. It still shows what{" "}
        <em>is</em> knowable: lead time depends only on the product&apos;s
        attributes, so it is displayed alongside the refusal.
      </p>
    </div>
  );
}
