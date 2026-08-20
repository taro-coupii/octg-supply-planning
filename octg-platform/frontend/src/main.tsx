import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import RequireAuth from "./components/RequireAuth";
import Administration from "./pages/Administration";
import ApprovalQueue from "./pages/ApprovalQueue";
import CompanyInventory from "./pages/CompanyInventory";
import CoverageWorkspace from "./pages/CoverageWorkspace";
import CustomerOwnedInventory from "./pages/CustomerOwnedInventory";
import DemandImport from "./pages/DemandImport";
import DemandList from "./pages/DemandList";
import ExecutiveDashboard from "./pages/ExecutiveDashboard";
import Home from "./pages/Home";
import MaterialOrderReq from "./pages/MaterialOrderReq";
import MrpByItem from "./pages/MrpByItem";
import Login from "./pages/Login";
import MrpSummary from "./pages/MrpSummary";
import NotFound from "./pages/NotFound";
import ProductWorkspace from "./pages/ProductWorkspace";
import ScenarioEditor from "./pages/ScenarioEditor";
import ScenarioList from "./pages/ScenarioList";
import SharingAnalysis from "./pages/SharingAnalysis";
import SubstitutionWorkspace from "./pages/SubstitutionWorkspace";
import SurplusList from "./pages/SurplusList";
import WellWorkspace from "./pages/WellWorkspace";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route path="/" element={<Home />} />
          <Route path="/executive" element={<ExecutiveDashboard />} />
          <Route path="/catalog" element={<ProductWorkspace />} />
          <Route path="/admin" element={<Administration />} />
          <Route path="/inventory/company" element={<CompanyInventory />} />
          <Route path="/inventory/customer-owned" element={<CustomerOwnedInventory />} />
          <Route path="/demand/import" element={<DemandImport />} />
          <Route path="/demand" element={<DemandList />} />
          <Route path="/wells/:id" element={<WellWorkspace />} />
          <Route path="/coverage" element={<CoverageWorkspace />} />
          <Route path="/substitution" element={<SubstitutionWorkspace />} />
          <Route path="/approvals" element={<ApprovalQueue />} />
          <Route path="/analysis/sharing" element={<SharingAnalysis />} />
          <Route path="/mrp" element={<MrpSummary />} />
          <Route path="/mrp/items/:id" element={<MrpByItem />} />
          <Route path="/mrp/order-requirements" element={<MaterialOrderReq />} />
          <Route path="/surplus" element={<SurplusList />} />
          <Route path="/scenarios" element={<ScenarioList />} />
          <Route path="/scenarios/:id" element={<ScenarioEditor />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </React.StrictMode>
);
