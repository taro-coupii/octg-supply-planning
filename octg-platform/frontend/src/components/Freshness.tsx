/**
 * "Fetched at HH:MM · Refresh" — the platform's minimum honest answer to
 * "am I looking at current data?". Every screen is a one-shot fetch on mount;
 * without this line a screen left open over lunch silently presents the
 * morning's numbers as now's.
 */
export default function Freshness({
  fetchedAt,
  onRefresh,
  busy,
}: {
  fetchedAt: Date | null;
  onRefresh: () => void;
  busy?: boolean;
}) {
  return (
    <span className="freshness">
      {fetchedAt && (
        <span className="freshness-at">
          Fetched{" "}
          {fetchedAt.toLocaleTimeString(undefined, {
            hour: "2-digit",
            minute: "2-digit",
          })}
        </span>
      )}
      <button
        type="button"
        className="freshness-refresh"
        onClick={onRefresh}
        disabled={busy}
      >
        {busy ? "Refreshing…" : "Refresh"}
      </button>
    </span>
  );
}
