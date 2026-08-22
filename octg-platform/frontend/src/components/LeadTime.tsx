import { useState } from "react";
import { LeadTimeBreakdown } from "../api/client";

/**
 * Lead time, rendered as the four-dimension breakdown it actually is.
 *
 * The whole reason a planner trusts this number is that they can see where it
 * came from: OD/WT + Grade + Connection + Logistics. So the per-dimension rows
 * are the substance, not decoration. The total stays visible at a glance and the
 * rows expand under it.
 *
 * The state this file exists to get right is `modelled === false`.
 *
 * When any dimension has no matching component the model cannot state a lead
 * time, and the backend sends `total_months: 0.0` to say so. Two renderings are
 * forbidden here:
 *
 *   * "0 mo"          — claims instant delivery, the opposite of the truth, and
 *                       would make the least-known product look like the safest
 *                       row on the screen.
 *   * `matched_months`— the sum of the dimensions that DID match. It is a
 *                       progress indicator, not a lead time; presenting it as one
 *                       is a confidently wrong date, which is worse than an
 *                       admitted gap.
 *
 * So an unmodelled breakdown shows the words "not modelled", names the missing
 * dimensions, and shows the matched months only under an explicit "matched so
 * far / not a lead time" label.
 */

function months(value: number) {
  return `${value} mo`;
}

/** The total, or an honest refusal. Never a number that isn't one. */
export function LeadTimeTotal({ lead }: { lead: LeadTimeBreakdown }) {
  if (!lead.modelled) {
    return <span className="lt-total lt-total-unmodelled">not modelled</span>;
  }
  return <span className="lt-total">{months(lead.total_months)}</span>;
}

function MissingNote({ lead }: { lead: LeadTimeBreakdown }) {
  return (
    <div className="lt-unmodelled">
      <div className="lt-unmodelled-head">
        Lead time is <strong>not modelled</strong> — it is unknown, not zero.
      </div>
      <div className="lt-missing">
        {lead.missing_dimensions.length > 0 ? (
          <>
            No lead-time component matches this product on:{" "}
            {lead.missing_dimensions.map((d) => (
              <span key={d} className="lt-missing-dim">
                {d}
              </span>
            ))}
          </>
        ) : (
          <>No lead-time component matches this product on any dimension.</>
        )}
      </div>
      {lead.components.length > 0 && (
        <div className="lt-matched-so-far">
          Matched so far: <strong>{months(lead.matched_months)}</strong> — shown
          for information only and deliberately not totalled. This is{" "}
          <strong>not</strong> a lead time, and no order date can be derived from
          it.
        </div>
      )}
      {lead.note && <div className="lt-note">{lead.note}</div>}
    </div>
  );
}

function ComponentRows({ lead }: { lead: LeadTimeBreakdown }) {
  const present = new Set(lead.components.map((c) => c.dimension));
  return (
    <table className="lt-table">
      <thead>
        <tr>
          <th>Dimension</th>
          <th>Matched on</th>
          <th className="num">Months</th>
        </tr>
      </thead>
      <tbody>
        {lead.components.map((c) => (
          <tr key={`${c.dimension}-${c.matched_on}`}>
            <td className="lt-dim">{c.dimension}</td>
            <td>
              <span className="lt-matched">{c.matched_on}</span>
              {c.shared && (
                <span
                  className="lt-shared"
                  title={`This term is the "${c.attribute_value}" catch-all: a shared step that applies to many products rather than one specific to this product's ${c.dimension}.`}
                >
                  (shared)
                </span>
              )}
              <span className="lt-label">{c.label}</span>
            </td>
            <td className="num">{c.months}</td>
          </tr>
        ))}
        {lead.missing_dimensions
          .filter((d) => !present.has(d))
          .map((d) => (
            <tr key={d} className="lt-row-missing">
              <td className="lt-dim">{d}</td>
              <td className="lt-nomatch">no matching component</td>
              <td className="num lt-nomatch">—</td>
            </tr>
          ))}
      </tbody>
      <tfoot>
        <tr>
          <td colSpan={2}>{lead.modelled ? "Total" : "Not modelled"}</td>
          <td className="num">
            {lead.modelled ? months(lead.total_months) : "—"}
          </td>
        </tr>
      </tfoot>
    </table>
  );
}

/**
 * Compact, expandable form for a table cell. The total (or "not modelled") is
 * always visible; the dimension rows are one click away. An unmodelled lead time
 * shows its explanation without needing the click, because that is the case a
 * planner must not be able to skim past.
 */
export function LeadTimeCell({
  lead,
  fallbackMonths,
}: {
  lead: LeadTimeBreakdown | undefined;
  /**
   * `lead_time_months` from an older backend that omits the breakdown. Only used
   * when there is no breakdown at all — and even then a 0 is reported as
   * unknown, never as "0 mo".
   */
  fallbackMonths?: number;
}) {
  const [open, setOpen] = useState(false);

  if (!lead) {
    if (fallbackMonths === undefined || fallbackMonths === 0) {
      return <span className="lt-total lt-total-unmodelled">not modelled</span>;
    }
    return <span className="lt-total">{months(fallbackMonths)}</span>;
  }

  return (
    <div className={`lt-cell${lead.modelled ? "" : " lt-cell-unmodelled"}`}>
      <button
        type="button"
        className="lt-toggle"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <LeadTimeTotal lead={lead} />
        <span className="lt-caret">{open ? "▾" : "▸"}</span>
      </button>
      {!lead.modelled && <MissingNote lead={lead} />}
      {open && <ComponentRows lead={lead} />}
    </div>
  );
}

/** Full-width panel form, for By Item where there is room to show it outright. */
export default function LeadTimePanel({
  lead,
  title = "Lead time",
}: {
  lead: LeadTimeBreakdown;
  title?: string;
}) {
  return (
    <section
      className={`card mrp-block lt-panel${
        lead.modelled ? "" : " lt-panel-unmodelled"
      }`}
    >
      <div className="lt-panel-head">
        <h3>{title}</h3>
        <LeadTimeTotal lead={lead} />
      </div>
      <p className="lt-panel-note">
        Built from four attribute dimensions — OD/WT, Grade, Connection and
        Logistics. A <em>(shared)</em> term is a catch-all step that applies to
        many products rather than one specific to this product.
      </p>
      {!lead.modelled && <MissingNote lead={lead} />}
      <ComponentRows lead={lead} />
    </section>
  );
}
