import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi } from "../api/client";

/**
 * One line naming the demand scope a supply screen was computed under.
 *
 * MRP and Order Requirements net against exactly the demand the coverage
 * engine evaluates — the platform default set in Administration — and that
 * scope was previously stated nowhere on either screen, so a planner had no
 * way to see WHY a Planned well's demand was absent from the order book
 * (product-owner request, 2026-08-12). The per-request checkboxes on
 * Coverage / Executive / Surplus deliberately do NOT reach these screens;
 * this note says where the lever actually is.
 *
 * Fails silent-but-honest: if the defaults cannot be loaded the note renders
 * nothing rather than guessing a scope.
 */
export default function ScopeNote() {
  const [text, setText] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    adminApi
      .getCoverageScopeDefaults()
      .then((d) => {
        if (!live) return;
        setText(
          `${d.status_filter.join(", ")} wells · ${d.profile_filter.join(
            ", "
          )} profiles`
        );
      })
      .catch(() => {
        if (live) setText(null);
      });
    return () => {
      live = false;
    };
  }, []);
  if (!text) return null;
  return (
    <p className="scope-note">
      Demand scope: <strong>{text}</strong> — the platform default, the same
      scope the coverage engine evaluates. Change it in{" "}
      <Link to="/admin">Administration</Link>; the read-only scope checkboxes
      on Coverage / Executive / Surplus do not affect this screen.
    </p>
  );
}
