import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, BusinessUnitOut, CustomerSummary } from "../../api/client";
import LoadError from "../../components/LoadError";

/** What each policy actually means for the coverage verdict. */
const POLICY_MEANING: Record<
  string,
  { headline: string; detail: string }
> = {
  soft: {
    headline: "Pooled, earliest-ROS first",
    detail:
      "Unassigned on-hand stock in the Business Unit counts as coverage. Demand lines draw from the pool in ROS order, so an earlier well can consume the steel a later one was counting on. A physical assignment is not required.",
  },
  hard: {
    headline: "Physical assignment only",
    detail:
      "Only inventory physically assigned to a demand line provides coverage. Unassigned pool stock does not, even when it exists in the same Business Unit in ample quantity — which is why a hard-policy customer can read as Uncovered while stock sits on the ground.",
  },
  hybrid: {
    headline: "Assigned first, then pooled",
    detail:
      "Assigned inventory is consumed first; whatever remains uncovered may then draw from the unassigned pool in ROS order. It behaves as hard allocation up to the assigned quantity and as soft allocation beyond it.",
  },
};

function PolicyCard({ policy }: { policy: string }) {
  const meaning = POLICY_MEANING[policy.toLowerCase()];
  return (
    <div className="admin-policy">
      <span className={`badge admin-policy-badge admin-policy-${policy.toLowerCase()}`}>
        {policy}
      </span>
      {meaning ? (
        <span className="admin-policy-headline">{meaning.headline}</span>
      ) : (
        <span className="admin-policy-headline admin-policy-unknown">
          Unrecognised policy — this UI has no description for &ldquo;{policy}
          &rdquo;, so the server value is shown as-is rather than guessed at.
        </span>
      )}
    </div>
  );
}

export default function HierarchyPanel() {
  const [units, setUnits] = useState<BusinessUnitOut[] | null>(null);
  const [customers, setCustomers] = useState<CustomerSummary[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    Promise.all([api.getBusinessUnits(), api.getCustomers()])
      .then(([u, c]) => {
        setUnits(u);
        setCustomers(c);
        setError(null);
      })
      .catch(setError);
  }, []);

  if (error !== null) {
    return (
      <div>
        <LoadError what="the administration data" error={error} />
      </div>
    );
  }
  if (!units || !customers) return <p>Loading…</p>;

  // A customer whose business_unit_id matches no BU, or is null, still has to
  // appear. An unmapped customer is precisely the configuration fault that makes
  // coverage raise 409 inventory_scope_missing elsewhere, so hiding it here
  // would hide the cause of a failure a planner is already looking at.
  const known = new Set(units.map((u) => u.id));
  const orphans = customers.filter(
    (c) => c.business_unit_id === null || !known.has(c.business_unit_id)
  );

  // Policies actually present, so the legend describes this deployment rather
  // than a hard-coded list.
  const policiesPresent = Array.from(
    new Set(customers.map((c) => c.allocation_policy))
  ).sort();

  return (
    <>
      <p className="admin-readonly">
        <span className="admin-readonly-tag">Read only below</span>
        The platform exposes no endpoint that changes a Business Unit, a customer
        or an allocation policy — those records are maintained upstream, so there
        are deliberately no edit controls rather than controls that would fail to
        save. The other tabs on this screen <strong>are</strong> editable: the
        lead-time components, the coverage scope and the two substitution tables are
        this platform&apos;s own assumptions and agreements, not a projection of
        anybody else&apos;s data.
      </p>

      <section className="scenario-section">
        <h2>Business Unit → Customer</h2>
        <p className="scenario-section-note">
          The Business Unit is the <strong>hard inventory boundary</strong>.
          On-hand stock belongs to a (Business Unit, product) pair, and it is
          never offered outside its BU — not by coverage and not by
          substitution, however large the quantity held elsewhere. Inside a BU it
          is pooled across every customer below, earliest need first.
        </p>

        <div className="admin-tree">
          {units.map((u) => {
            const members = customers.filter(
              (c) => c.business_unit_id === u.id
            );
            return (
              <div key={u.id} className="admin-bu">
                <div className="admin-bu-head">
                  <span className="admin-bu-name">{u.name}</span>
                  <span className="admin-bu-count">
                    {members.length} customer{members.length === 1 ? "" : "s"}
                  </span>
                </div>
                <div className="admin-bu-id num">{u.id}</div>
                {members.length === 0 ? (
                  <div className="empty">
                    No customers are mapped to this Business Unit. Its inventory
                    is therefore unreachable by any coverage calculation.
                  </div>
                ) : (
                  <ul className="admin-customers">
                    {members.map((c) => (
                      <li key={c.id} className="admin-customer">
                        <div className="admin-customer-head">
                          <span className="admin-customer-name">{c.name}</span>
                          <PolicyCard policy={c.allocation_policy} />
                        </div>
                        <div className="admin-customer-id num">{c.id}</div>
                        {/* Only links whose target actually reads the
                            `customer` parameter. A link that silently dropped it
                            would land the planner on an all-customer screen
                            looking like a filtered one. */}
                        <div className="admin-customer-links">
                          <Link to={`/executive?customer=${c.id}`}>
                            Executive view
                          </Link>
                          {/* The one inventory tier this platform owns, and the
                              only one with a write control. Linked from here
                              because "who has declared customer-owned stock" is
                              a configuration-shaped question. */}
                          <Link to={`/customer-owned-inventory?customer=${c.id}`}>
                            Customer-owned inventory
                          </Link>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
                {members.length === 1 && (
                  <p className="admin-bu-note">
                    Only one customer sits in this Business Unit, so it competes
                    with nobody for the pool. Any surplus reported for it is its
                    own unconsumed stock.
                  </p>
                )}
              </div>
            );
          })}
        </div>

        {orphans.length > 0 && (
          <div className="admin-orphans">
            <h3>Customers with no Business Unit</h3>
            <p>
              These customers are not mapped to a Business Unit, so there is no
              inventory scope to read for them. Coverage and By Item requests for
              these customers fail with{" "}
              <code>409 inventory_scope_missing</code> — deliberately, rather
              than reporting an invented on-hand quantity.
            </p>
            <ul className="admin-customers">
              {orphans.map((c) => (
                <li key={c.id} className="admin-customer">
                  <div className="admin-customer-head">
                    <span className="admin-customer-name">{c.name}</span>
                    <PolicyCard policy={c.allocation_policy} />
                  </div>
                  <div className="admin-customer-id num">
                    business_unit_id: {c.business_unit_id ?? "null"}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <section className="scenario-section">
        <h2>What an allocation policy decides</h2>
        <p className="scenario-section-note">
          The policy is not a label — it changes the coverage verdict for
          identical inventory and identical demand.
        </p>
        <div className="admin-policy-legend">
          {policiesPresent.map((p) => {
            const meaning = POLICY_MEANING[p.toLowerCase()];
            return (
              <div key={p} className="admin-policy-explain">
                <PolicyCard policy={p} />
                <p>
                  {meaning
                    ? meaning.detail
                    : "This UI has no description for this policy value. It is shown verbatim as served rather than described from a guess."}
                </p>
                <div className="admin-policy-users">
                  {customers
                    .filter((c) => c.allocation_policy === p)
                    .map((c) => c.name)
                    .join(", ")}
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </>
  );
}
