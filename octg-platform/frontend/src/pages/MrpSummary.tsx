import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, MrpRecommendation } from "../api/client";
import { LeadTimeCell } from "../components/LeadTime";
import LoadError from "../components/LoadError";
import ReasonText from "../components/ReasonText";
import ScopeNote from "../components/ScopeNote";

// Dates from the MRP engine are plain calendar dates (YYYY-MM-DD) or naive
// datetimes. Slicing avoids the timezone shift `new Date("2026-11-01")` causes.
export function formatDay(value: string) {
  const [y, m, d] = value.slice(0, 10).split("-");
  return `${y}-${m}-${d}`;
}

function todayIso() {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

export function isPastDay(value: string) {
  return value.slice(0, 10) < todayIso();
}

/** The three tiers behind "drawn from stock", shown only when non-zero. */
function DrawBreakdown({ r }: { r: MrpRecommendation }) {
  const parts: string[] = [];
  if (r.drawn_customer_owned > 0)
    parts.push(`customer-owned ${r.drawn_customer_owned.toLocaleString()}`);
  if (r.drawn_company > 0) parts.push(`company ${r.drawn_company.toLocaleString()}`);
  if (r.drawn_substitute > 0)
    parts.push(`substitute ${r.drawn_substitute.toLocaleString()}`);
  if (parts.length === 0) return null;
  return <div className="mrp-draw-breakdown">{parts.join(" · ")}</div>;
}

function RecommendationTable({ rows }: { rows: MrpRecommendation[] }) {
  return (
    <div className="table-scroll">
      <table className="mrp-table">
        <thead>
          <tr>
            <th>Product</th>
            <th>Net shortfall</th>
            <th>Demand</th>
            <th>Drawn from stock</th>
            <th>ROS</th>
            <th>Required Ship Date</th>
            <th>Recommended Order Date</th>
            <th>Lead Time</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.product_id}-${r.unrecoverable}`}>
              <td>
                <Link to={`/mrp/by-item/${r.product_id}`}>
                  {r.product_description ?? r.product_id}
                </Link>
              </td>
              <td className="num">
                <strong>{r.quantity.toLocaleString()}</strong>
                {r.whole_line_ids.length > 0 && (
                  <div className="date-past">
                    {r.whole_line_ids.length} line(s) counted whole — recompute
                    coverage for the net figure
                  </div>
                )}
              </td>
              <td className="num">{r.demand_quantity.toLocaleString()}</td>
              <td className="num">
                {(
                  r.drawn_customer_owned + r.drawn_company + r.drawn_substitute
                ).toLocaleString()}
                <DrawBreakdown r={r} />
              </td>
              <td className="num">{formatDay(r.ros_date)}</td>
              <td className="num">{formatDay(r.required_ship_date)}</td>
              <td className="num">
                {formatDay(r.recommended_order_date)}
                {isPastDay(r.recommended_order_date) && (
                  <div className="date-past">
                    date has passed — ordering now cannot meet ROS
                  </div>
                )}
              </td>
              {/*
                Not `{r.lead_time_months} mo`. That scalar is 0 when the lead
                time is NOT MODELLED, and "0 mo" reads as instant delivery — so
                the least-known product would look like the safest row here.
                LeadTimeCell shows the four-dimension breakdown and refuses to
                print a number the model has not got.
              */}
              <td className="lt-td">
                <LeadTimeCell lead={r.lead_time} fallbackMonths={r.lead_time_months} />
              </td>
              <td className="mrp-reason">
                <ReasonText reason={r.reason} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function MrpSummary() {
  const [rows, setRows] = useState<MrpRecommendation[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api.getMrpSummary().then(setRows).catch(setError);
  }, []);

  if (error) return <LoadError what="MRP summary" error={error} />;
  if (!rows) return <p>Loading...</p>;

  // Two genuinely different actions: place an order vs. escalate. Splitting them
  // keeps an unrecoverable line from reading as something a buyer can just order.
  const orderable = rows.filter((r) => !r.unrecoverable);
  const escalate = rows.filter((r) => r.unrecoverable);

  return (
    <div>
      <div className="page-head">
        <h1>MRP Summary</h1>
        {/*
          A plain link, so the browser's own download machinery handles the
          Content-Disposition filename and the save dialog — the same pattern
          DemandImport.tsx uses for its template download. There is deliberately
          one download pattern in this app rather than a second hand-rolled
          fetch → Blob → object URL → synthetic click.

          No customer argument: this screen has no customer filter (it calls
          api.getMrpSummary() unscoped), so the file must be the same
          system-wide scope the tables below are showing. `api.mrpExportUrl`
          takes an optional customerId for the day this screen grows a filter —
          it is left off here rather than defaulted to some customer, which
          would silently hand the planner a narrower file than the screen.
        */}
        <a className="btn-download" href={api.mrpExportUrl()}>
          Export to Excel
        </a>
      </div>
      <p>
        What should we order? Expand any lead time to see the four attribute
        dimensions it is built from.
      </p>
      <ScopeNote />
      <p className="mrp-export-note">
        <strong>Export to Excel</strong> downloads this list as tab 1 of an .xlsx
        workbook — orderable and unrecoverable rows kept apart, as here — with the
        By Item justification behind every row (demand lines, inventory position,
        runout projection, lead-time breakdown) on the tabs after it.
      </p>
      {rows.some((r) => r.lead_time && !r.lead_time.modelled) && (
        <p className="lt-lead-warning">
          Some products below have a lead time that is <strong>not modelled</strong>.
          That is unknown, not zero — their order dates cannot be trusted and the
          missing attribute dimensions are named in the row.
        </p>
      )}

      <section className="mrp-section">
        <h2 className="mrp-section-title">
          Orderable <span className="mrp-count">{orderable.length}</span>
        </h2>
        <p className="mrp-section-note">
          Lead time still fits. Place the mill order by the recommended order date.
        </p>
        {orderable.length === 0 ? (
          <div className="card">
            <div className="empty">No orderable recommendations.</div>
          </div>
        ) : (
          <RecommendationTable rows={orderable} />
        )}
      </section>

      <section className="mrp-section mrp-section-escalate">
        <h2 className="mrp-section-title">
          Unrecoverable — escalate <span className="mrp-count">{escalate.length}</span>
        </h2>
        <p className="mrp-section-note">
          The recommended order date has already passed: these cannot be met by mill
          order even if ordered today. Rescope ROS, borrow, or source externally.
        </p>
        {escalate.length === 0 ? (
          <div className="card">
            <div className="empty">Nothing unrecoverable.</div>
          </div>
        ) : (
          <RecommendationTable rows={escalate} />
        )}
      </section>
    </div>
  );
}
