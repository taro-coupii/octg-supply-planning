import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { clearToken, getUser } from "../lib/auth";

// The rail is grouped in business-flow order: what you plan against, what you
// judge, what you buy, what you keep. The group labels encode the real shape of
// the work rather than decorating the list.
const NAV_GROUPS: { group: string; items: { to: string; label: string; end?: boolean }[] }[] = [
  {
    group: "Overview",
    items: [
      { to: "/", label: "Home", end: true },
      { to: "/executive", label: "Executive" },
    ],
  },
  {
    group: "Demand",
    items: [
      { to: "/demand", label: "Demand", end: true },
      { to: "/demand/import", label: "Import" },
    ],
  },
  {
    group: "Readiness",
    items: [
      { to: "/coverage", label: "Coverage" },
      { to: "/substitution", label: "Substitution" },
      { to: "/approvals", label: "Approvals" },
      { to: "/analysis/sharing", label: "Sharing" },
    ],
  },
  {
    group: "Supply",
    items: [
      { to: "/mrp", label: "MRP", end: true },
      { to: "/mrp/order-requirements", label: "Order Reqs" },
      { to: "/surplus", label: "Surplus" },
    ],
  },
  {
    group: "What-if",
    items: [{ to: "/scenarios", label: "Scenarios" }],
  },
  {
    group: "Data",
    items: [
      { to: "/catalog", label: "Products" },
      { to: "/inventory/company", label: "Company Inventory" },
      { to: "/inventory/customer-owned", label: "Customer-Owned" },
      { to: "/admin", label: "Administration" },
    ],
  },
];

export default function Layout() {
  const user = getUser();
  const navigate = useNavigate();
  // Mobile (<=640px): the rail collapses to a top bar with a hamburger
  // toggle (spec §5 approach A). Desktop ignores this state entirely (CSS-gated).
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
        {NAV_GROUPS.map((g) => (
          <div key={g.group}>
            <div className="rail-group">{g.group}</div>
            {g.items.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.end}
                className={({ isActive }) => (isActive ? "active" : "")}
                onClick={() => setNavOpen(false)}
              >
                {n.label}
              </NavLink>
            ))}
          </div>
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
