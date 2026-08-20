import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Breadcrumbs from "../components/Breadcrumbs";
import Freshness from "../components/Freshness";
import { apiGet } from "../lib/api";
import { writeParam } from "../lib/urlState";
import BusinessUnitsTab from "./admin/BusinessUnitsTab";
import CoverageScopeTab from "./admin/CoverageScopeTab";
import LeadTimesTab from "./admin/LeadTimesTab";
import SafetyStocksTab from "./admin/SafetyStocksTab";
import SubstitutionsTab from "./admin/SubstitutionsTab";
import { flattenBuTree, type BusinessUnitNode, type Customer, type Product } from "./admin/shared";

const TABS = [
  { key: "business-units", label: "BU hierarchy" },
  { key: "lead-times", label: "Lead times" },
  { key: "coverage-scope", label: "Coverage scope" },
  { key: "substitutions", label: "Substitutions" },
  { key: "safety-stocks", label: "Safety stocks" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export default function Administration() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as TabKey | null) ?? TABS[0].key;

  const [products, setProducts] = useState<Product[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [businessUnits, setBusinessUnits] = useState<ReturnType<typeof flattenBuTree>>([]);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  const loadReference = () => {
    Promise.all([
      apiGet<Product[]>("/products"),
      apiGet<Customer[]>("/customers"),
      apiGet<BusinessUnitNode[]>("/business-units"),
    ])
      .then(([p, c, bu]) => {
        setProducts(p);
        setCustomers(c);
        setBusinessUnits(flattenBuTree(bu));
        setFetchedAt(new Date());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(loadReference, []);

  const setTab = (key: TabKey) => setParams(writeParam(params, "tab", key));

  return (
    <div>
      <Breadcrumbs items={[{ label: "Home", to: "/" }, { label: "Administration" }]} />
      <div className="card">
        <div className="page-header">
          <h1>Administration</h1>
          <Freshness fetchedAt={fetchedAt} onRefresh={loadReference} />
        </div>
        {error && <p className="inline-error">{error}</p>}
        <div className="tab-bar">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              className={tab === t.key ? "tab active" : "tab"}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="tab-panel">
          {tab === "business-units" && <BusinessUnitsTab />}
          {tab === "lead-times" && <LeadTimesTab businessUnits={businessUnits} products={products} />}
          {tab === "coverage-scope" && <CoverageScopeTab />}
          {tab === "substitutions" && <SubstitutionsTab products={products} customers={customers} />}
          {tab === "safety-stocks" && <SafetyStocksTab businessUnits={businessUnits} products={products} />}
        </div>
      </div>
    </div>
  );
}
