import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  adminApi,
  api,
  CoverageRecomputeFailure,
  CustomerSubstitutionRuleChange,
  CustomerSubstitutionRulesResponse,
  CustomerSummary,
  errorText,
  ProductOut,
  TechnicalSubstitutionChange,
  TechnicalSubstitutionsResponse,
  WellCoverageRollupChange,
} from "../../api/client";
import LoadError from "../../components/LoadError";

/**
 * Substitution master data — layers 1 and 2.
 *
 * WHY THESE TWO SECTIONS ARE EDITABLE
 * -----------------------------------
 * Same argument as the lead-time components. A technical substitution is an
 * ENGINEERING COMPATIBILITY CLAIM and a customer substitution rule is a COMMERCIAL
 * AGREEMENT; Oracle holds neither, both arrived by being typed into a seed script, and
 * a planner asked outright where a substitution item could be registered.
 *
 * TWO THINGS THIS UI MUST NOT IMPLY
 * ---------------------------------
 *   * Registering a technical substitution does not make a substitution usable. It
 *     makes the substitute OFFERED as a candidate. The customer must permit the pair
 *     (layer 2) and each specific demand line needs an Approved approval (layer 3).
 *     Stated in the section note, and the table shows per row how many customer rules
 *     permit it — a row with none is doing nothing yet.
 *   * Layer 2 is a TRUE ALLOW-LIST: a pair with no rule is blocked exactly as an
 *     `allowed: false` rule blocks it. So deleting a permitting rule REVERTS the
 *     substitution to blocked, and the confirmation says so rather than calling it a
 *     removal. This is the single easiest thing here to get wrong.
 *
 * Layer 3 (`WellSubstitutionApproval`) is deliberately absent from this screen. It is
 * not master data — it is one customer's decision about one demand line — and it is
 * already actioned from the Substitution workspace. A settings-screen editor for it
 * would be a second front door to the same decision.
 */

/** A product picker built from the catalogue, showing descriptions rather than uuids. */
function ProductSelect({
  products,
  value,
  onChange,
  placeholder,
  disabled,
}: {
  products: ProductOut[];
  value: string;
  onChange: (id: string) => void;
  placeholder: string;
  disabled?: boolean;
}) {
  return (
    <select
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">{placeholder}</option>
      {products.map((p) => (
        // Description, never the id. A planner cannot check a picker they cannot read,
        // and this API serves the description precisely so the screen need not guess.
        <option key={p.id} value={p.id}>
          {p.description ?? p.id}
        </option>
      ))}
    </select>
  );
}

/**
 * The recompute half of every change report in these two sections.
 *
 * Both endpoints recompute coverage in the same request, because
 * `CoverageResult.status` is PERSISTED — a stored CoveredViaSubstitute verdict resting
 * on a permission that no longer exists would be misinformation, not merely stale. The
 * wells listed here are the server's proof that it happened.
 */
function SubstitutionWellChanges({
  wellChanges,
  recomputedCustomerIds,
  wellsExamined,
  failures,
}: {
  wellChanges: WellCoverageRollupChange[];
  recomputedCustomerIds: string[];
  wellsExamined: number;
  failures: CoverageRecomputeFailure[];
}) {
  return (
    <>
      {wellChanges.length === 0 ? (
        <p className="admin-report-wells">
          Coverage was recomputed for {recomputedCustomerIds.length} customer(s) across{" "}
          {wellsExamined} well(s); no well&apos;s rollup moved, but every line-level
          verdict was re-derived.
        </p>
      ) : (
        <div className="admin-report-wells">
          <strong>
            Coverage recomputed — {wellChanges.length} of {wellsExamined} well(s) moved:
          </strong>
          <ul>
            {wellChanges.map((w) => (
              <li key={w.well_id}>
                <Link to={`/wells/${w.well_id}`}>{w.well_name}</Link>{" "}
                {w.coverage_before ?? "not evaluated"} &rarr;{" "}
                {w.coverage_after ?? "not evaluated"}
              </li>
            ))}
          </ul>
        </div>
      )}
      {/* The caveat on everything above. The change WAS made — one Business Unit's
          missing inventory feed must not veto an engineering claim that applies to all
          of them — but these customers' stored verdicts now predate it, and a screen
          showing only the well changes would imply they were unaffected. */}
      {failures.length > 0 && (
        <div className="admin-lt-incomplete">
          <span className="admin-lt-incomplete-tag">
            {failures.length} customer(s) NOT recomputed — verdicts now predate this
            change
          </span>
          <ul>
            {failures.map((f) => (
              <li key={f.customer_id}>
                <strong>{f.customer_name}</strong>: {f.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}

function TechnicalSubstitutionsSection({
  products,
  onChanged,
}: {
  products: ProductOut[];
  /** Layer 1 decides which pairs layer 2 is even consulted for, so a change here can
   *  flip a rule between live and inert. The rules section is told to reload rather
   *  than being left showing a stale `inert_reason`. */
  onChanged: () => void;
}) {
  const [data, setData] = useState<TechnicalSubstitutionsResponse | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  /** A REFUSAL from the server (400 self-substitution, 409 duplicate). Shown verbatim. */
  const [writeError, setWriteError] = useState<string | null>(null);
  const [change, setChange] = useState<TechnicalSubstitutionChange | null>(null);
  const [busy, setBusy] = useState(false);
  const [newFrom, setNewFrom] = useState("");
  const [newTo, setNewTo] = useState("");

  function load() {
    return adminApi
      .getTechnicalSubstitutions()
      .then((d) => {
        setData(d);
        setLoadError(null);
      })
      .catch(setLoadError);
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** One rule set for both mutations: clear the refusal, render the server's report,
   *  and RELOAD from the server rather than splicing local state — `unpermitted_count`
   *  and the row counts are the server's to compute. */
  function mutate(run: () => Promise<TechnicalSubstitutionChange>) {
    setBusy(true);
    setWriteError(null);
    run()
      .then((result) => {
        setChange(result);
        onChanged();
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
        <h2>Technical substitutions</h2>
        <LoadError what="the technical substitutions" error={loadError} />
      </section>
    );
  }
  if (!data) {
    return (
      <section className="scenario-section">
        <h2>Technical substitutions</h2>
        <p>Loading…</p>
      </section>
    );
  }

  const canAdd = newFrom !== "" && newTo !== "" && newFrom !== newTo && !busy;

  return (
    <section className="scenario-section">
      <h2>Technical substitutions</h2>
      <p className="scenario-section-note">
        <strong>Layer 1 of three.</strong> Engineering says the first product may be
        served by the second. Registering a pair here makes the substitute{" "}
        <strong>offered as a candidate</strong> — it does <em>not</em> make it usable.
        The owning customer must also permit the pair (layer 2, below) and the specific
        demand line needs an <em>Approved</em> approval (layer 3, actioned from
        the <Link to="/approvals">approval queue</Link> or a line&apos;s
        Substitution workspace).
      </p>
      <p className="scenario-section-note">
        Pairs are <strong>directional</strong>: registering A&nbsp;&rarr;&nbsp;B does not
        allow B&nbsp;&rarr;&nbsp;A. If both compatibilities are real, register both rows.
      </p>

      {data.unpermitted_count > 0 && (
        <p className="admin-lt-incomplete">
          <span className="admin-lt-incomplete-tag">
            {data.unpermitted_count} pair(s) permitted by nobody
          </span>
          {data.note}
        </p>
      )}

      {writeError && (
        <p className="admin-write-error">
          <span className="admin-write-error-tag">Refused — nothing saved</span>
          {writeError}
        </p>
      )}

      {change && (
        <div className="admin-report">
          <div className="admin-report-head">
            <span className="admin-report-tag">
              Technical substitution {change.action}
              {change.approved_well_approvals_affected > 0
                ? ` — ${change.approved_well_approvals_affected} approved approval(s) affected`
                : ""}
            </span>
            <button
              type="button"
              className="admin-report-dismiss"
              onClick={() => setChange(null)}
            >
              Dismiss
            </button>
          </div>
          <p className="admin-report-note">{change.note}</p>
          <SubstitutionWellChanges
            wellChanges={change.well_changes}
            recomputedCustomerIds={change.recomputed_customer_ids}
            wellsExamined={change.wells_examined}
            failures={change.coverage_recompute_failures}
          />
        </div>
      )}

      {data.substitutions.length === 0 ? (
        <div className="empty admin-lt-empty">
          No technical substitutions are registered, so{" "}
          <strong>no demand line on this platform has any substitute candidate</strong>{" "}
          — every shortage can only be closed by the product itself. Add a pair below.
        </div>
      ) : (
        <table className="lt-table admin-lt-table">
          <thead>
            <tr>
              <th>Substitute this product…</th>
              <th>…with this one</th>
              <th>Customer rules</th>
              <th>Well approvals</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.substitutions.map((row) => (
              <tr
                key={row.id}
                className={
                  row.allowing_customer_rule_count === 0 ? "lt-row-missing" : ""
                }
              >
                <td>{row.from_product_description ?? row.from_product_id}</td>
                <td>{row.to_product_description ?? row.to_product_id}</td>
                <td className="num">
                  {row.allowing_customer_rule_count === 0 ? (
                    <span
                      className="lt-total-unmodelled"
                      title="Layer 2 is a positive allow-list, so with no permitting rule every demand line reports this candidate as blocked at the customer layer."
                    >
                      permitted by none
                      {row.customer_rule_count > 0
                        ? ` (${row.customer_rule_count} veto)`
                        : ""}
                    </span>
                  ) : (
                    `${row.allowing_customer_rule_count} permit of ${row.customer_rule_count}`
                  )}
                </td>
                <td className="num">
                  {row.well_approval_count === 0
                    ? "—"
                    : `${row.approved_well_approval_count} approved of ${row.well_approval_count}`}
                </td>
                <td className="admin-lt-actions">
                  <button
                    type="button"
                    className="admin-lt-delete"
                    disabled={busy}
                    onClick={() => {
                      // States the actual consequence rather than "are you sure". A UI
                      // courtesy, NOT the safety mechanism — the server performs the
                      // delete and reports the blast radius either way.
                      const from =
                        row.from_product_description ?? row.from_product_id;
                      const to = row.to_product_description ?? row.to_product_id;
                      const approvals =
                        row.approved_well_approval_count > 0
                          ? `\n\n${row.approved_well_approval_count} demand line(s) hold an APPROVED customer approval for this exact pair. Those lines lose their substitute coverage. The approval records are kept as history — they are decisions customers actually made — and simply stop having any effect.`
                          : "";
                      if (
                        window.confirm(
                          `This withdraws the engineering claim that "${from}" may be served by "${to}".\n\nThe pair leaves the candidate list for every demand line on "${from}", whatever the customer rules and well approvals say.${approvals}\n\nThe server will report exactly which wells moved. Proceed?`
                        )
                      ) {
                        mutate(() =>
                          adminApi.deleteTechnicalSubstitution(row.id)
                        );
                      }
                    }}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="admin-lt-add">
        <h3>Register a substitution</h3>
        <p className="admin-lt-add-note">
          Pick the product that is <em>short</em> first, then the product that may serve
          it. A product cannot substitute for itself, and each ordered pair may only be
          registered once — the server refuses both with a stated reason.
        </p>
        <div className="admin-lt-add-grid">
          <label>
            <span>Substitute this product</span>
            <ProductSelect
              products={products}
              value={newFrom}
              onChange={setNewFrom}
              placeholder="Select the product in short supply…"
              disabled={busy}
            />
          </label>
          <label>
            <span>…with this one</span>
            <ProductSelect
              products={products}
              value={newTo}
              onChange={setNewTo}
              placeholder="Select the substitute…"
              disabled={busy}
            />
          </label>
          <button
            type="button"
            className="admin-lt-add-button"
            disabled={!canAdd}
            onClick={() =>
              mutate(async () => {
                const result = await adminApi.createTechnicalSubstitution({
                  from_product_id: newFrom,
                  to_product_id: newTo,
                });
                setNewFrom("");
                setNewTo("");
                return result;
              })
            }
          >
            Register substitution
          </button>
        </div>
        {newFrom !== "" && newFrom === newTo && (
          <p className="admin-lt-add-hint">
            The two products are the same. A product substituting for itself would make
            the candidate list offer a demand line its own steel back as an alternative
            to itself — one quantity shown twice. Pick a different substitute.
          </p>
        )}
      </div>
    </section>
  );
}

function CustomerSubstitutionRulesSection({
  products,
  customers,
  reloadKey,
  onChanged,
}: {
  products: ProductOut[];
  customers: CustomerSummary[];
  /** Bumped when layer 1 changes, which can flip a rule between live and inert. */
  reloadKey: number;
  onChanged: () => void;
}) {
  const [data, setData] = useState<CustomerSubstitutionRulesResponse | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [change, setChange] = useState<CustomerSubstitutionRuleChange | null>(null);
  const [busy, setBusy] = useState(false);
  /** "" => every customer. Kept out of the add form's customer, so filtering the table
   *  never silently changes who a new rule would be written for. */
  const [filterCustomer, setFilterCustomer] = useState("");

  const [newCustomer, setNewCustomer] = useState("");
  const [newFrom, setNewFrom] = useState("");
  const [newTo, setNewTo] = useState("");
  const [newAllowed, setNewAllowed] = useState(true);

  function load(customerId: string) {
    return adminApi
      .getCustomerSubstitutionRules(customerId === "" ? undefined : customerId)
      .then((d) => {
        setData(d);
        setLoadError(null);
      })
      .catch(setLoadError);
  }

  useEffect(() => {
    load(filterCustomer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterCustomer, reloadKey]);

  function mutate(run: () => Promise<CustomerSubstitutionRuleChange>) {
    setBusy(true);
    setWriteError(null);
    run()
      .then((result) => {
        setChange(result);
        onChanged();
        return load(filterCustomer);
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
        <h2>Customer substitution rules</h2>
        <LoadError what="the customer substitution rules" error={loadError} />
      </section>
    );
  }

  const canAdd =
    newCustomer !== "" &&
    newFrom !== "" &&
    newTo !== "" &&
    newFrom !== newTo &&
    !busy;

  return (
    <section className="scenario-section">
      <h2>Customer substitution rules</h2>
      <p className="scenario-section-note">
        <strong>Layer 2 of three.</strong> Whether a given customer permits a given
        substitution pair. This is a <strong>positive allow-list</strong>: a pair with{" "}
        <em>no rule at all</em> is blocked exactly as an <em>allowed = No</em> rule
        blocks it. So deleting a permitting rule is not a tidy-up — it{" "}
        <strong>reverts the substitution to blocked</strong>.
      </p>
      <p className="scenario-section-note">
        What <em>allowed = No</em> adds over having no rule is{" "}
        <strong>provenance</strong>: the block becomes a recorded customer decision
        rather than silence. That is why a rule can be <em>toggled</em> as well as
        deleted — a customer changing its mind is normal business, not data cleanup.
      </p>

      {writeError && (
        <p className="admin-write-error">
          <span className="admin-write-error-tag">Refused — nothing saved</span>
          {writeError}
        </p>
      )}

      {change && (
        <div
          className={`admin-report ${
            change.allowed_before === true && change.allowed_after !== true
              ? "admin-report-severe"
              : ""
          }`}
        >
          <div className="admin-report-head">
            <span className="admin-report-tag">
              Rule {change.action}
              {change.allowed_before === true && change.allowed_after !== true
                ? " — substitution reverted to blocked"
                : ""}
            </span>
            <button
              type="button"
              className="admin-report-dismiss"
              onClick={() => setChange(null)}
            >
              Dismiss
            </button>
          </div>
          <p className="admin-report-note">{change.note}</p>
          {change.warning && (
            <p className="admin-lt-incomplete">
              <span className="admin-lt-incomplete-tag">
                Saved, but has no effect yet
              </span>
              {change.warning}
            </p>
          )}
          <SubstitutionWellChanges
            wellChanges={change.well_changes}
            recomputedCustomerIds={change.recomputed_customer_ids}
            wellsExamined={change.wells_examined}
            failures={change.coverage_recompute_failures}
          />
        </div>
      )}

      <div className="admin-lt-add-grid">
        <label>
          <span>Show rules for</span>
          <select
            value={filterCustomer}
            disabled={busy}
            onChange={(e) => setFilterCustomer(e.target.value)}
          >
            <option value="">All customers</option>
            {customers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {!data ? (
        <p>Loading…</p>
      ) : data.rules.length === 0 ? (
        <div className="empty admin-lt-empty">
          {filterCustomer === ""
            ? "No customer has a substitution rule, so every technically-valid substitute on the platform is blocked at the customer layer."
            : "This customer has no substitution rules, so every technically-valid substitute is blocked at the customer layer for them. That is a real state, not missing data — the allow-list is empty."}
        </div>
      ) : (
        <table className="lt-table admin-lt-table">
          <thead>
            <tr>
              <th>Customer</th>
              <th>Substitute this…</th>
              <th>…with this</th>
              <th>Allowed</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.rules.map((row) => (
              <tr
                key={row.id}
                className={row.technical_substitution_exists ? "" : "lt-row-missing"}
              >
                <td>{row.customer_name ?? row.customer_id}</td>
                <td>
                  {row.from_product_description ?? row.from_product_id}
                  {!row.technical_substitution_exists && (
                    <span
                      className="admin-lt-immutable"
                      title={row.inert_reason ?? undefined}
                    >
                      no technical substitution for this pair — this rule has no effect
                    </span>
                  )}
                </td>
                <td>{row.to_product_description ?? row.to_product_id}</td>
                <td>
                  {/* Inline toggle, because flipping a permission is a normal
                      commercial change and a two-screen journey for it would push
                      operators towards delete-and-recreate — which passes through "no
                      rule" and changes the verdict on the way. */}
                  <label className="admin-scope-check">
                    <input
                      type="checkbox"
                      checked={row.allowed}
                      disabled={busy}
                      onChange={() =>
                        mutate(() =>
                          adminApi.setCustomerSubstitutionRuleAllowed(
                            row.id,
                            !row.allowed
                          )
                        )
                      }
                    />
                    <span>{row.allowed ? "Yes" : "No"}</span>
                  </label>
                </td>
                <td className="admin-lt-actions">
                  <button
                    type="button"
                    className="admin-lt-delete"
                    disabled={busy}
                    onClick={() => {
                      const who = row.customer_name ?? row.customer_id;
                      const from =
                        row.from_product_description ?? row.from_product_id;
                      const to = row.to_product_description ?? row.to_product_id;
                      // Two genuinely different consequences, so two different
                      // messages. Calling either one "remove this rule" would be the
                      // allow-list mistake.
                      const consequence = row.allowed
                        ? `This does NOT return the pair to a neutral state. Layer 2 is a positive allow-list, so with no rule "${from}" → "${to}" becomes BLOCKED for ${who}, exactly as an explicit veto would block it. Any demand line covered via this substitute loses that coverage.\n\nTo record a decision instead of reverting to silence, untick "Allowed" rather than deleting.`
                        : `No verdict will change: with no rule the pair is already blocked for ${who}, exactly as this veto blocks it. What is lost is PROVENANCE — the record that ${who} was asked and declined. The block will then read as silence.`;
                      if (
                        window.confirm(
                          `${consequence}\n\nThe server will report exactly which wells moved. Proceed?`
                        )
                      ) {
                        mutate(() =>
                          adminApi.deleteCustomerSubstitutionRule(row.id)
                        );
                      }
                    }}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {data && data.inert_count > 0 && (
        <p className="admin-lt-incomplete">
          <span className="admin-lt-incomplete-tag">
            {data.inert_count} rule(s) have no effect
          </span>
          {data.note}
        </p>
      )}

      <div className="admin-lt-add">
        <h3>Add a rule</h3>
        <p className="admin-lt-add-note">
          A rule may be recorded <strong>before</strong> the technical substitution
          exists — a commercial agreement often lands before engineering signs off, and
          the three layers are independent. The server saves it and tells you it is
          inert until the technical pair is registered. Only one rule may exist per
          (customer, from, to); to change the answer, use the <em>Allowed</em> toggle
          above.
        </p>
        <div className="admin-lt-add-grid">
          <label>
            <span>Customer</span>
            <select
              value={newCustomer}
              disabled={busy}
              onChange={(e) => setNewCustomer(e.target.value)}
            >
              <option value="">Select a customer…</option>
              {customers.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Substitute this product</span>
            <ProductSelect
              products={products}
              value={newFrom}
              onChange={setNewFrom}
              placeholder="Select the product in short supply…"
              disabled={busy}
            />
          </label>
          <label>
            <span>…with this one</span>
            <ProductSelect
              products={products}
              value={newTo}
              onChange={setNewTo}
              placeholder="Select the substitute…"
              disabled={busy}
            />
          </label>
          <label className="admin-scope-check">
            <input
              type="checkbox"
              checked={newAllowed}
              disabled={busy}
              onChange={(e) => setNewAllowed(e.target.checked)}
            />
            <span>
              Allowed — the customer <strong>permits</strong> this substitution. Leave
              unticked to record an explicit <em>veto</em>.
            </span>
          </label>
          <button
            type="button"
            className="admin-lt-add-button"
            disabled={!canAdd}
            onClick={() =>
              mutate(async () => {
                const result = await adminApi.createCustomerSubstitutionRule({
                  customer_id: newCustomer,
                  from_product_id: newFrom,
                  to_product_id: newTo,
                  allowed: newAllowed,
                });
                setNewFrom("");
                setNewTo("");
                return result;
              })
            }
          >
            Add rule
          </button>
        </div>
      </div>
    </section>
  );
}

/**
 * Both substitution sections, sharing one fetch of the product catalogue and the
 * customer list.
 *
 * Shared rather than fetched twice because the two pickers must offer the SAME
 * options: a pair registrable in one section and not the other would be an
 * inconsistency with no cause a user could see.
 */
export default function SubstitutionPanel() {
  const [products, setProducts] = useState<ProductOut[] | null>(null);
  const [customers, setCustomers] = useState<CustomerSummary[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  /** Bumped by a layer-1 change so the layer-2 table re-reads its inert flags. */
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    Promise.all([api.getProducts(), api.getCustomers()])
      .then(([p, c]) => {
        setProducts(p);
        setCustomers(c);
        setError(null);
      })
      .catch(setError);
  }, []);

  if (error !== null) {
    return (
      <section className="scenario-section">
        <h2>Substitution master data</h2>
        <LoadError what="the product catalogue and customer list" error={error} />
      </section>
    );
  }
  if (!products || !customers) {
    return (
      <section className="scenario-section">
        <h2>Substitution master data</h2>
        <p>Loading…</p>
      </section>
    );
  }

  return (
    <>
      <TechnicalSubstitutionsSection
        products={products}
        onChanged={() => setReloadKey((k) => k + 1)}
      />
      <CustomerSubstitutionRulesSection
        products={products}
        customers={customers}
        reloadKey={reloadKey}
        onChanged={() => setReloadKey((k) => k + 1)}
      />
    </>
  );
}
