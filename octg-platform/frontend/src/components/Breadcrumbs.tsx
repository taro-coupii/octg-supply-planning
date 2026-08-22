import React from "react";
import { Link } from "react-router-dom";

/**
 * The trail into a detail screen.
 *
 * Four routes on this platform are drill-downs -- a well, an MRP item, a
 * substitution decision, a scenario -- and each is reachable from several
 * places. None of them showed where they sat, so arriving at one from the
 * Executive dashboard looked identical to arriving from the demand list, and
 * the only way back was the browser button.
 *
 * The last crumb is the current page and is deliberately NOT a link: a link
 * that goes where you already are is a dead control.
 */

export type Crumb = { label: string; to?: string };

export default function Breadcrumbs({ trail }: { trail: Crumb[] }) {
  if (trail.length === 0) return null;
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      {trail.map((crumb, i) => {
        const last = i === trail.length - 1;
        return (
          <React.Fragment key={`${crumb.label}-${i}`}>
            {i > 0 && (
              <span className="crumb-sep" aria-hidden="true">
                ›
              </span>
            )}
            {crumb.to && !last ? (
              <Link to={crumb.to}>{crumb.label}</Link>
            ) : (
              <span className="crumb-current" aria-current={last ? "page" : undefined}>
                {crumb.label}
              </span>
            )}
          </React.Fragment>
        );
      })}
    </nav>
  );
}
