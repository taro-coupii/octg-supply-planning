/**
 * The demand-status and profile vocabularies, in ONE place.
 *
 * These mirror the backend enums (`app.models.DemandStatus` /
 * `DemandProfile`). They were previously restated as literals in five
 * separate pages, which made adding a status a five-file change and invited
 * the copies to drift. The DEFAULT_* pairs restate the platform's shipped
 * coverage scope for the screens that seed their checkboxes with it — the
 * authoritative value still comes from the server (`filters.are_default` /
 * `scope_is_default` in the payloads), these are only the initial UI state.
 */

export const DEMAND_STATUSES = ["Planned", "Budgeted", "Confirmed"] as const;
export const DEMAND_PROFILES = ["Primary", "Contingency"] as const;

export const DEFAULT_STATUS_SCOPE: string[] = ["Confirmed"];
export const DEFAULT_PROFILE_SCOPE: string[] = ["Primary", "Contingency"];
