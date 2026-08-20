import { DEMAND_PROFILES, DEMAND_STATUSES } from "../lib/enums";

// Shared status[]/profile[] scope-override checkboxes (spec §フロントエンド,
// E-2). `touched` starts false and MUST stay false until the user checks or
// unchecks a box — callers use it to decide whether to send status/profile
// query params at all (untouched = default scope, no params sent).
export type ScopeState = {
  touched: boolean;
  statuses: string[];
  profiles: string[];
};

export const DEFAULT_SCOPE_STATE: ScopeState = {
  touched: false,
  statuses: [...DEMAND_STATUSES],
  profiles: [...DEMAND_PROFILES],
};

// Builds the query string for a ScopeChecks value: "" while untouched (send
// no params — default scope), else repeated status=/profile= params.
export function scopeToQuery(state: ScopeState): string {
  if (!state.touched) return "";
  const params = new URLSearchParams();
  for (const s of state.statuses) params.append("status", s);
  for (const p of state.profiles) params.append("profile", p);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

type Props = {
  value: ScopeState;
  onChange: (next: ScopeState) => void;
};

export default function ScopeChecks({ value, onChange }: Props) {
  const toggle = (key: "statuses" | "profiles", item: string) => {
    const set = new Set(value[key]);
    if (set.has(item)) {
      // The last remaining checkbox in a group can't be unchecked (spec).
      if (set.size <= 1) return;
      set.delete(item);
    } else {
      set.add(item);
    }
    onChange({ ...value, touched: true, [key]: Array.from(set) });
  };

  return (
    <div className="scope-checks">
      <fieldset>
        <legend>Statuses</legend>
        {DEMAND_STATUSES.map((s) => (
          <label key={s} className="checkbox-label">
            <input
              type="checkbox"
              checked={value.statuses.includes(s)}
              disabled={value.statuses.length === 1 && value.statuses.includes(s)}
              onChange={() => toggle("statuses", s)}
            />
            {s}
          </label>
        ))}
      </fieldset>
      <fieldset>
        <legend>Profiles</legend>
        {DEMAND_PROFILES.map((p) => (
          <label key={p} className="checkbox-label">
            <input
              type="checkbox"
              checked={value.profiles.includes(p)}
              disabled={value.profiles.length === 1 && value.profiles.includes(p)}
              onChange={() => toggle("profiles", p)}
            />
            {p}
          </label>
        ))}
      </fieldset>
    </div>
  );
}
