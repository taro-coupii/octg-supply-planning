import Tabs, { TabDef, useTabs } from "../components/Tabs";
import HierarchyPanel from "./admin/HierarchyPanel";
import LeadTimePanel from "./admin/LeadTimePanel";
import CoverageScopePanel from "./admin/CoverageScopePanel";
import SubstitutionPanel from "./admin/SubstitutionPanel";
import SafetyStockPanel from "./admin/SafetyStockPanel";

/**
 * Administration.
 *
 * WHAT IS EDITABLE HERE, AND WHAT DELIBERATELY IS NOT
 * ---------------------------------------------------
 * The screen has two halves and the split is not cosmetic.
 *
 * READ-ONLY — Business Units, customers, allocation policies. `GET /business-units`
 * and `GET /customers` are the only endpoints for them; the platform exposes no
 * mutation, because these records are maintained upstream. Edit controls that could
 * not save would be worse than none: a planner who "changes" a policy and sees
 * nothing happen learns the wrong thing about the platform.
 *
 * EDITABLE — the attribute lead-time components, and the platform-wide coverage
 * scope default. Both are the platform's OWN assumptions. Oracle holds no lead-time
 * table; those months arrived by being typed into a seed script, and the planner who
 * knows the mill queue has lengthened is the person who should be able to say so. The
 * coverage scope was a Python constant, adjustable only by a deploy.
 *
 * The screen's other job is explanation. Two concepts drive coverage everywhere else
 * in the product and this is where someone would come looking for them:
 *
 *   * the Business Unit is the HARD inventory boundary. Stock in another BU is
 *     never offered to this one, whatever its quantity — not by coverage, not by
 *     substitution. Inside a BU it is pooled across every customer below.
 *   * the allocation policy decides HOW that customer's coverage is judged.
 *
 * EVERY EDIT BELOW IS CONSEQUENTIAL, AND THE SERVER SAYS SO
 * ---------------------------------------------------------
 * A lead-time delete can flip products to "not modelled", which retracts the
 * platform's one terminal claim (Unrecoverable). Saving the coverage scope moves every
 * coverage verdict on the platform at once. The backend performs each change and
 * returns a before/after diff rather than refusing or inventing a confirmation step —
 * the same pattern `PUT /wells/{id}/demand-status` and the customer-owned upload
 * follow. So this screen RENDERS that diff after every write. The numbers shown after
 * a save are the server's report of what it did, never this component's guess.
 *
 * TAB SPLIT
 * ---------
 * The four surfaces below are unrelated to one another and each panel fetches its own
 * data on mount — none of it is shared across panels (the Hierarchy panel's Business
 * Units/customers and the Substitution panel's products/customers are each read by
 * exactly one panel), so an inactive tab costs nothing and never blocks the page.
 */

type TabKey = "hierarchy" | "lead-time" | "scope" | "substitutions" | "safety-stock";

const TABS: readonly TabDef<TabKey>[] = [
  { key: "hierarchy", label: "Business Unit → Customer" },
  { key: "lead-time", label: "Lead-time components" },
  { key: "scope", label: "Coverage scope defaults" },
  { key: "substitutions", label: "Substitution master data" },
  { key: "safety-stock", label: "Safety stock" },
];

export default function Administration() {
  const [tab, setTab] = useTabs<TabKey>("tab", TABS);

  return (
    <div>
      <div className="scenario-head">
        <div>
          <h1>Administration</h1>
          <p className="scenario-sub">
            Business Units and their customers (read only), and the platform&apos;s
            own adjustable assumptions: lead-time components, the coverage scope
            default, and the substitution master data (which products may replace
            which, and which customers permit it).
          </p>
        </div>
      </div>

      <Tabs tabs={TABS} active={tab} onSelect={setTab} label="Administration sections" />

      {tab === "hierarchy" && <HierarchyPanel />}
      {tab === "safety-stock" && <SafetyStockPanel />}
      {tab === "lead-time" && <LeadTimePanel />}
      {tab === "scope" && <CoverageScopePanel />}
      {tab === "substitutions" && <SubstitutionPanel />}
    </div>
  );
}
