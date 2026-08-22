import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, CustomerSummary } from "../api/client";
import SharingPanel from "../components/SharingPanel";

/**
 * The cross-customer sharing what-if, per customer.
 *
 * The panel itself carries all the projection labelling — see SharingPanel. This
 * page only picks the customer, and says the one thing a planner arriving here
 * cold needs to know before reading any number: the Business Unit is a hard
 * boundary, so a customer can be offered nothing while identical steel sits in
 * quantity in another BU. That is the boundary holding, not a bug.
 */
export default function SharingAnalysis() {
  const [params, setParams] = useSearchParams();
  const customerId = params.get("customer") ?? "";
  const [customers, setCustomers] = useState<CustomerSummary[]>([]);

  useEffect(() => {
    api.getCustomers().then(setCustomers).catch(() => setCustomers([]));
  }, []);

  const selected = customers.find((c) => c.id === customerId) ?? null;

  return (
    <div>
      <div className="scenario-head">
        <div>
          <h1>Business Unit sharing what-if</h1>
          <p className="scenario-sub">
            Could this customer&apos;s uncovered demand be met if inventory were
            shared across customers within the same Business Unit?
          </p>
        </div>
        <label className="filter-field">
          <span>Customer</span>
          <select
            value={customerId}
            onChange={(e) => {
              const v = e.target.value;
              if (v) setParams({ customer: v });
              else setParams({});
            }}
          >
            <option value="">Choose a customer…</option>
            {customers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <p className="filter-note">
        Sharing is evaluated <strong>within one Business Unit only</strong>. Stock
        held in any other Business Unit is never offered, however large the
        quantity — the BU is a hard inventory boundary, so a customer whose BU
        holds no surplus is correctly offered nothing even when an identical
        product sits in bulk elsewhere.
        {selected && (
          <>
            {" "}
            {selected.name} has allocation policy{" "}
            <strong>{selected.allocation_policy}</strong>, which is what its
            official coverage verdict is judged by.
          </>
        )}
      </p>

      {customerId ? (
        <SharingPanel
          customerId={customerId}
          title={`Sharing what-if — ${selected?.name ?? customerId}`}
        />
      ) : (
        <div className="card">
          <div className="empty">
            Choose a customer to run the what-if. Nothing is computed until you
            do, and nothing this analysis reports is ever saved.
          </div>
        </div>
      )}
    </div>
  );
}
