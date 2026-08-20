import { Fragment } from "react";
import { Link } from "react-router-dom";

type Crumb = { label: string; to?: string };

// Convention (spec §4.7): while an entity name is still loading, pages pass
// label "…" — a raw UUID must never appear in the breadcrumb trail.
export default function Breadcrumbs({ items }: { items: Crumb[] }) {
  return (
    <nav className="breadcrumbs">
      {items.map((c, i) => (
        <Fragment key={i}>
          {i > 0 && " / "}
          {c.to ? <Link to={c.to}>{c.label}</Link> : <span>{c.label}</span>}
        </Fragment>
      ))}
    </nav>
  );
}
