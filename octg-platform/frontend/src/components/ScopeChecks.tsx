import { DEMAND_PROFILES, DEMAND_STATUSES } from "../lib/enums";

/**
 * The status/profile scope checkboxes shared by the Executive dashboard and
 * the Surplus list. One implementation, one behaviour: the last box of a
 * group cannot be unticked (the backend refuses an empty filter, and a UI
 * that allowed composing one would only be composing an error).
 */
function CheckGroup({
  label,
  options,
  selected,
  onChange,
}: {
  label: string;
  options: readonly string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const toggle = (value: string) => {
    if (selected.includes(value)) {
      if (selected.length > 1) onChange(selected.filter((v) => v !== value));
    } else {
      onChange([...selected, value]);
    }
  };
  return (
    <div className="exec-scope-checks" aria-label={label}>
      {options.map((o) => (
        <label key={o}>
          <input
            type="checkbox"
            checked={selected.includes(o)}
            onChange={() => toggle(o)}
          />
          {o}
        </label>
      ))}
    </div>
  );
}

export default function ScopeChecks({
  statuses,
  profiles,
  onStatuses,
  onProfiles,
}: {
  statuses: string[];
  profiles: string[];
  onStatuses: (next: string[]) => void;
  onProfiles: (next: string[]) => void;
}) {
  return (
    <>
      <CheckGroup
        label="Well status"
        options={DEMAND_STATUSES}
        selected={statuses}
        onChange={onStatuses}
      />
      <CheckGroup
        label="Profile"
        options={DEMAND_PROFILES}
        selected={profiles}
        onChange={onProfiles}
      />
    </>
  );
}
