import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, CrossCustomerSharing, SharingUncoveredLine } from "../api/client";
import LoadError from "./LoadError";

/**
 * Cross-customer sharing — a read-only WHAT-IF.
 *
 * It answers a question the official coverage verdict does not ask: could this
 * customer's uncovered demand be met if inventory were shared across customers
 * inside the same Business Unit? Because the official verdict is deliberately
 * customer-scoped, this analysis will routinely CONTRADICT it — a line badged
 * Uncovered here shows as "would be covered".
 *
 * That contradiction is the feature, and it is also the hazard. Rendered in the
 * same visual language as the official badge, it would quietly become a second,
 * more optimistic verdict, and the reason coverage is customer-scoped would be
 * undone in the UI layer. So:
 *
 *   * the whole panel sits inside the established `.whatif-banner` /
 *     `.whatif-tag` vocabulary already used by Scenarios and the Coverage
 *     workspace — one projection language, not a second one;
 *   * the official status is printed next to every projected outcome, so the two
 *     are always read together;
 *   * the projected outcome never uses a `badge-Covered` class. It gets its own
 *     dashed "projection" treatment.
 *
 * Two payload subtleties are handled explicitly:
 *
 *   * `contributions` may be EMPTY while `would_be_covered` is true. That is not
 *     a missing donor list — it means the surplus is BU-level unallocated stock
 *     rather than a transfer from a named customer, and `explanation` says so.
 *     The explanation is rendered instead of a blank table.
 *   * `shared_quantity` is the binding number. The contribution split is an
 *     indicative hint about who to call, and is labelled as such.
 */

function day(iso: string) {
  return iso.slice(0, 10);
}

function LineCard({ line }: { line: SharingUncoveredLine }) {
  return (
    <div
      className={`share-line${
        line.would_be_covered ? " share-line-would" : " share-line-wouldnt"
      }`}
    >
      <div className="share-line-head">
        <span className="share-line-well">
          <Link to={`/wells/${line.well_id}`}>{line.well_name}</Link>
        </span>
        <span className="share-line-product">
          {line.product_description ?? line.product_id}
        </span>
      </div>

      <dl className="share-facts">
        <div>
          <dt>Required</dt>
          <dd className="num">{line.quantity.toLocaleString()}</dd>
        </div>
        <div>
          <dt>ROS</dt>
          <dd className="num">{day(line.ros_date)}</dd>
        </div>
        <div>
          <dt>Official verdict</dt>
          {/* The real, stored, customer-scoped verdict. Printed beside the
              projection so the projection can never be read on its own. */}
          <dd>
            <span className={`badge badge-${line.official_status.replace(/\s.*/, "")}`}>
              {line.official_status}
            </span>
          </dd>
        </div>
        <div>
          <dt>If shared within the BU</dt>
          <dd>
            <span
              className={`share-verdict${
                line.would_be_covered
                  ? " share-verdict-would"
                  : " share-verdict-wouldnt"
              }`}
            >
              {line.would_be_covered
                ? "Would be covered — projection"
                : "Still not coverable — projection"}
            </span>
          </dd>
        </div>
      </dl>

      <div className="share-numbers">
        <span className="share-binding">
          Shareable quantity applied:{" "}
          <strong className="num">
            {line.shared_quantity.toLocaleString()}
          </strong>
        </span>
        {line.shortfall > 0 && (
          <span className="share-shortfall">
            Still short by{" "}
            <strong className="num">{line.shortfall.toLocaleString()}</strong>
          </span>
        )}
      </div>

      {/* Verbatim. It is the sentence that explains an empty donor list, a
          partial share, or a refusal, and paraphrasing it would lose the reason. */}
      <p className="share-explanation">{line.explanation}</p>

      {line.contributions.length > 0 ? (
        <div className="share-donors">
          <div className="share-donors-label">
            Indicative source(s) — who to call
          </div>
          <ul>
            {line.contributions.map((c) => (
              <li key={c.from_customer_id}>
                <span className="share-donor-name">{c.from_customer_name}</span>
                <span className="num">{c.quantity.toLocaleString()}</span>
              </li>
            ))}
          </ul>
          <p className="share-donors-note">
            The binding figure is the shareable quantity above. This split is a
            hint about where the steel is likely to come from, not an allocation
            and not an instruction — nothing here reserves or moves anything.
          </p>
        </div>
      ) : (
        line.would_be_covered && (
          <p className="share-donors-none">
            No named donor customer: the surplus is Business-Unit-level
            unallocated stock, not a transfer from another customer. There is
            nobody to call — see the explanation above.
          </p>
        )
      )}
    </div>
  );
}

export default function SharingPanel({
  customerId,
  wellId,
  title = "Cross-customer sharing what-if",
}: {
  customerId: string;
  /** When set, only this well's lines are shown. */
  wellId?: string;
  title?: string;
}) {
  const [data, setData] = useState<CrossCustomerSharing | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    setData(null);
    setError(null);
    setLoading(true);
    api
      .getCrossCustomerSharing(customerId)
      .then((d) => {
        if (live) setData(d);
      })
      .catch((e) => {
        if (live) setError(e);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [customerId]);

  if (error !== null) {
    return <LoadError what="the cross-customer sharing what-if" error={error} />;
  }
  if (loading || !data) return <p>Loading the sharing what-if…</p>;

  const lines = wellId
    ? data.uncovered_lines.filter((l) => l.well_id === wellId)
    : data.uncovered_lines;

  const wouldCover = lines.filter((l) => l.would_be_covered).length;

  return (
    <section className="whatif-banner share-panel">
      <span className="whatif-tag">Projection — not saved</span>
      <h3 className="share-title">{title}</h3>

      <p>
        This asks whether <strong>{data.customer_name}</strong>&apos;s uncovered
        demand could be met if inventory were shared across customers inside{" "}
        <strong>{data.business_unit_name ?? "this Business Unit"}</strong>.{" "}
        <strong>
          It deliberately disagrees with the official coverage badge.
        </strong>{" "}
        The official verdict is customer-scoped and is unchanged by anything
        below — nothing here has been saved, reserved, assigned or moved.
      </p>

      {data.donor_customer_ids.length === 0 && (
        <p className="whatif-note">
          No other customer in this Business Unit holds shareable stock, so any
          surplus reported is this customer&apos;s own unconsumed inventory.
        </p>
      )}

      <div className="share-tiles">
        <div className="share-tile">
          <div className="share-tile-label">Uncovered lines examined</div>
          <div className="share-tile-value num">{lines.length}</div>
        </div>
        <div className="share-tile share-tile-would">
          <div className="share-tile-label">Would be covered by sharing</div>
          <div className="share-tile-value num">{wouldCover}</div>
          <div className="share-tile-note">projection only</div>
        </div>
        <div className="share-tile share-tile-wouldnt">
          <div className="share-tile-label">Still not coverable</div>
          <div className="share-tile-value num">{lines.length - wouldCover}</div>
        </div>
      </div>

      {lines.length === 0 ? (
        <div className="empty">
          {wellId
            ? "This well has no uncovered demand lines in the sharing analysis, so there is nothing to project for it."
            : "This customer has no uncovered demand lines, so there is nothing to project."}
        </div>
      ) : (
        <div className="share-lines">
          {lines.map((l) => (
            <LineCard key={l.demand_line_id} line={l} />
          ))}
        </div>
      )}

      {!wellId && data.product_surplus.length > 0 && (
        <div className="share-surplus">
          <div className="share-donors-label">
            Shareable surplus in {data.business_unit_name ?? "this Business Unit"}
          </div>
          <div className="table-scroll">
            <table className="lt-table">
              <thead>
                <tr>
                  <th>Product</th>
                  <th className="num">BU on hand</th>
                  <th className="num">Committed in BU</th>
                  <th className="num">Shareable</th>
                </tr>
              </thead>
              <tbody>
                {data.product_surplus.map((s) => (
                  <tr key={s.product_id}>
                    <td>
                      <Link to={`/mrp/by-item/${s.product_id}`}>
                        {s.product_description ?? s.product_id}
                      </Link>
                    </td>
                    <td className="num">{s.bu_on_hand.toLocaleString()}</td>
                    <td className="num">{s.committed_in_bu.toLocaleString()}</td>
                    <td
                      className={`num${
                        s.shareable > 0 ? " share-surplus-some" : " share-surplus-none"
                      }`}
                    >
                      {s.shareable.toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="share-donors-note">
            Committed stock is not offered — moving it would simply uncover the
            customer it is already covering. Only the shareable column is genuine
            surplus.
          </p>
        </div>
      )}

      {data.notes.map((n, i) => (
        <p key={i} className="whatif-note">
          {n}
        </p>
      ))}
    </section>
  );
}
