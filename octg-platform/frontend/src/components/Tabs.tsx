import { useSearchParams } from "react-router-dom";

/**
 * A segmented control whose selection lives in the URL.
 *
 * The URL is the point. Administration holds four unrelated settings surfaces
 * and every one of them is something a planner gets SENT to -- "the lead-time
 * term for that dimension is missing", "check the coverage scope default". If
 * the selected panel were component state, none of those could be linked to,
 * and the page would always open on whichever panel happened to be first.
 *
 * An unknown or absent value falls back to the first tab rather than rendering
 * nothing, so a stale or hand-edited link degrades to the page's front door
 * instead of to a blank screen.
 */

export type TabDef<K extends string> = {
  key: K;
  label: string;
  /** Optional figure shown beside the label, e.g. a row count. */
  count?: number;
};

export function useTabs<K extends string>(
  param: string,
  tabs: readonly TabDef<K>[]
): [K, (key: K) => void] {
  const [params, setParams] = useSearchParams();
  const raw = params.get(param);
  const active = tabs.some((t) => t.key === raw) ? (raw as K) : tabs[0].key;

  const select = (key: K) => {
    const next = new URLSearchParams(params);
    next.set(param, key);
    // `replace` so flicking between tabs does not fill the back button with
    // steps that all look like the same page.
    setParams(next, { replace: true });
  };

  return [active, select];
}

export default function Tabs<K extends string>({
  tabs,
  active,
  onSelect,
  label,
}: {
  tabs: readonly TabDef<K>[];
  active: K;
  onSelect: (key: K) => void;
  label: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((tab) => (
        <button
          key={tab.key}
          type="button"
          role="tab"
          aria-selected={tab.key === active}
          className={"tab" + (tab.key === active ? " tab-on" : "")}
          onClick={() => onSelect(tab.key)}
        >
          {tab.label}
          {tab.count !== undefined && (
            <span className="tab-count">{tab.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}
