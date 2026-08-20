import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { clearToken, getUser } from "../lib/auth";

const NAV = [
  { to: "/", label: "Home" },
  { to: "/executive", label: "Executive" },
  { to: "/scenarios", label: "Scenarios" },
  { to: "/catalog", label: "Products" },
  { to: "/admin", label: "Administration" },
  { to: "/inventory/company", label: "Company Inventory" },
  { to: "/inventory/customer-owned", label: "Customer-Owned" },
  { to: "/demand", label: "Demand" },
  { to: "/demand/import", label: "Demand Import" },
  { to: "/coverage", label: "Coverage" },
  { to: "/substitution", label: "Substitution" },
  { to: "/approvals", label: "Approvals" },
  { to: "/analysis/sharing", label: "Sharing" },
  { to: "/mrp", label: "MRP" },
  { to: "/mrp/order-requirements", label: "Order Reqs" },
  { to: "/surplus", label: "Surplus" },
];

export default function Layout() {
  const user = getUser();
  const navigate = useNavigate();
  // Mobile (<=640px): the rail collapses to a top bar with a hamburger
  // toggle (spec §5 方式A). Desktop ignores this state entirely (CSS-gated).
  const [navOpen, setNavOpen] = useState(false);

  const signOut = () => {
    clearToken();
    navigate("/login", { replace: true });
  };

  return (
    <div className="layout">
      <button
        type="button"
        className="rail-toggle"
        aria-label={navOpen ? "Close navigation" : "Open navigation"}
        aria-expanded={navOpen}
        onClick={() => setNavOpen((v) => !v)}
      >
        <span className="rail-toggle-title">OCTG Readiness</span>
        <span className="rail-toggle-icon">{navOpen ? "✕" : "☰"}</span>
      </button>
      <nav className={navOpen ? "rail rail-open" : "rail"}>
        <div className="rail-title">OCTG Readiness</div>
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            className={({ isActive }) => (isActive ? "active" : "")}
            onClick={() => setNavOpen(false)}
          >
            {n.label}
          </NavLink>
        ))}
        <div className="rail-spacer" />
        {user && (
          <div className="rail-user">
            <div className="rail-user-email">{user.email}</div>
            <div className="rail-user-role">{user.role}</div>
            <button type="button" className="rail-signout" onClick={signOut}>
              Sign out
            </button>
          </div>
        )}
      </nav>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
