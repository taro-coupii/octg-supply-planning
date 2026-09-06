import { useEffect, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { getUser, logout } from "../auth";

/**
 * The application frame: a vertical navigation rail plus the page column.
 *
 * This replaces the dark header bar that carried eleven links in a single row.
 * Two things were wrong with that and both are structural rather than
 * cosmetic:
 *
 *   1. It used `Link`, so nothing was ever marked as current. A planner who
 *      followed a link from the Home dashboard into the Coverage workspace got
 *      no confirmation of where they had landed.
 *   2. Eleven peers do not fit on a horizontal axis at a readable size, and the
 *      next screen (authentication is still to come, and with it a user menu)
 *      would have had to squeeze the other eleven to fit.
 *
 * A rail fixes both and makes the group labels -- which already existed and
 * already carried the right idea -- actually legible as groups.
 */

type NavItem = { to: string; label: string; end?: boolean };
type NavSection = { label: string; items: NavItem[] };

/**
 * The grouping is unchanged from the header it replaces: screens are filed by
 * the question they answer, not by the subsystem that serves them, so a new
 * screen lands in an existing group rather than lengthening a flat list.
 */
const SECTIONS: NavSection[] = [
  {
    label: "Readiness",
    items: [
      { to: "/", label: "Home", end: true },
      { to: "/coverage", label: "Coverage" },
      { to: "/approvals", label: "Approvals" },
      { to: "/executive", label: "Executive" },
    ],
  },
  {
    label: "Demand",
    items: [
      { to: "/demand", label: "Demand lines", end: true },
      { to: "/demand/import", label: "Import" },
    ],
  },
  {
    label: "Supply",
    items: [
      { to: "/mrp", label: "MRP", end: true },
      { to: "/mrp/order-requirements", label: "Order requirements" },
      { to: "/surplus", label: "Surplus" },
      { to: "/products", label: "Products" },
      { to: "/customer-owned-inventory", label: "Customer-owned inventory" },
      { to: "/company-inventory", label: "Company inventory" },
      { to: "/scenarios", label: "Scenarios" },
    ],
  },
  {
    label: "Setup",
    items: [{ to: "/admin", label: "Administration" }],
  },
];

/**
 * Reading width is a property of the SCREEN, not of the application.
 *
 * The old shell capped every page at 1100px while the MRP table declared a
 * 980px minimum and the scenario timeline is far wider than any window -- so
 * the densest screens on the platform were permanently horizontally scrolled
 * inside a column narrower than their content. Tables get a wider column and
 * the timeline gets the whole window.
 */
function widthClassFor(pathname: string): string {
  if (pathname.startsWith("/scenarios/")) return "app-main-full";
  const wide = [
    "/mrp",
    "/demand",
    "/coverage",
    "/products",
    "/customer-owned-inventory",
    "/company-inventory",
    "/admin",
  ];
  if (wide.some((p) => pathname === p || pathname.startsWith(p + "/"))) {
    return "app-main-wide";
  }
  return "";
}

/**
 * Session block pinned to the rail's foot — the slot the header bar never had
 * room for. The identity shown is the login-time copy (display only; every
 * access decision is the server's — see auth.ts), so there is no fetch here.
 * A planner sees their Business Unit name because that IS their data scope;
 * an admin sees "All business units" for the same reason.
 */
function UserMenu() {
  const user = getUser();
  if (!user) return null;
  return (
    <div className="sidebar-user">
      <div className="sidebar-user-identity">
        <span className="sidebar-user-name">{user.display_name}</span>
        <span className="sidebar-user-scope">
          {user.role === "admin"
            ? "All business units"
            : user.business_unit_name ?? "No business unit"}
        </span>
      </div>
      <button type="button" className="sidebar-user-signout" onClick={logout}>
        Sign out
      </button>
    </div>
  );
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation();
  const [open, setOpen] = useState(false);

  // Following a link on a narrow window must close the rail, otherwise the
  // page you navigated to is hidden behind the thing you navigated with.
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  return (
    <div className="app-shell">
      <button
        type="button"
        className="sidebar-toggle"
        aria-label={open ? "Close navigation" : "Open navigation"}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "✕" : "☰"}
      </button>

      {open && (
        <button
          type="button"
          className="sidebar-scrim"
          aria-label="Close navigation"
          onClick={() => setOpen(false)}
        />
      )}

      <aside className={"sidebar" + (open ? " sidebar-open" : "")}>
        <Link to="/" className="sidebar-brand">
          <span className="sidebar-mark" aria-hidden="true">
            OC
          </span>
          <span className="sidebar-title">
            OCTG
            <span>Supply Readiness</span>
          </span>
        </Link>

        <nav className="sidebar-nav" aria-label="Primary">
          {SECTIONS.map((section) => (
            <div className="nav-group" key={section.label}>
              <span className="nav-group-label">{section.label}</span>
              <div className="nav-group-links">
                {section.items.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) =>
                      "nav-link" + (isActive ? " nav-link-active" : "")
                    }
                  >
                    {item.label}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        {/* Static file from public/ (served like the SPA's own JS/CSS, i.e.
            OUTSIDE auth). Deliberate: it contains only demo-data screenshots
            and operating instructions, and a help page that 401s before you
            can log in helps nobody. */}
        <a
          className="sidebar-manual-link"
          href="/manual.html"
          target="_blank"
          rel="noopener"
        >
          📘 Manual
        </a>

        <UserMenu />
      </aside>

      <main className={"app-main " + widthClassFor(pathname)}>{children}</main>
    </div>
  );
}
