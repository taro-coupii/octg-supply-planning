import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import AppShell from "./components/AppShell";
import Login from "./pages/Login";
import { getToken } from "./auth";
import HomeDashboard from "./pages/HomeDashboard";
import WellWorkspace from "./pages/WellWorkspace";
import SubstitutionWorkspace from "./pages/SubstitutionWorkspace";
import MrpSummary from "./pages/MrpSummary";
import MaterialOrderReq from "./pages/MaterialOrderReq";
import SurplusList from "./pages/SurplusList";
import ApprovalQueue from "./pages/ApprovalQueue";
import NotFound from "./pages/NotFound";
import MrpByItem from "./pages/MrpByItem";
import ScenarioList from "./pages/ScenarioList";
import ScenarioEditor from "./pages/ScenarioEditor";
import DemandList from "./pages/DemandList";
import CoverageWorkspace from "./pages/CoverageWorkspace";
import DemandImport from "./pages/DemandImport";
import ExecutiveDashboard from "./pages/ExecutiveDashboard";
import Administration from "./pages/Administration";
import ProductWorkspace from "./pages/ProductWorkspace";
import SharingAnalysis from "./pages/SharingAnalysis";
import CustomerOwnedInventory from "./pages/CustomerOwnedInventory";
import CompanyInventory from "./pages/CompanyInventory";
import "@fontsource/barlow/400.css";
import "@fontsource/barlow/500.css";
import "@fontsource/barlow/700.css";
import "@fontsource/barlow-condensed/400.css";
import "@fontsource/barlow-condensed/600.css";
import "./index.css";

/**
 * Routing only. The frame -- navigation, active state, page width -- lives in
 * AppShell, so adding a screen here is one Route and one entry in that
 * component's section list rather than an edit to the layout itself.
 */
/**
 * Presence-of-token gate only. Whether the token is VALID is decided by the
 * server on the first API call (any 401 clears the session and redirects —
 * see auth.ts); duplicating that judgement here would be a second
 * implementation of the same decision.
 */
function RequireAuth({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  if (!getToken()) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <>{children}</>;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="*"
          element={
            <RequireAuth>
              <AppShell>
                <Routes>
          <Route path="/" element={<HomeDashboard />} />
          <Route path="/demand" element={<DemandList />} />
          <Route path="/demand/import" element={<DemandImport />} />
          <Route path="/coverage" element={<CoverageWorkspace />} />
          <Route path="/executive" element={<ExecutiveDashboard />} />
          <Route path="/admin" element={<Administration />} />
          <Route path="/products" element={<ProductWorkspace />} />
          <Route path="/analysis/sharing" element={<SharingAnalysis />} />
          <Route
            path="/customer-owned-inventory"
            element={<CustomerOwnedInventory />}
          />
          <Route path="/company-inventory" element={<CompanyInventory />} />
          <Route path="/wells/:wellId" element={<WellWorkspace />} />
          <Route path="/mrp" element={<MrpSummary />} />
          <Route path="/mrp/order-requirements" element={<MaterialOrderReq />} />
          <Route path="/surplus" element={<SurplusList />} />
          <Route path="/approvals" element={<ApprovalQueue />} />
          <Route path="/mrp/by-item/:productId" element={<MrpByItem />} />
          <Route path="/scenarios" element={<ScenarioList />} />
          <Route path="/scenarios/:scenarioId" element={<ScenarioEditor />} />
          <Route
            path="/demand-lines/:demandLineId/substitution"
            element={<SubstitutionWorkspace />}
          />
          {/* Catch-all: a bad URL gets a page saying so, never a blank shell. */}
          <Route path="*" element={<NotFound />} />
                </Routes>
              </AppShell>
            </RequireAuth>
          }
        />
      </Routes>
    </BrowserRouter>
  </React.StrictMode>
);
