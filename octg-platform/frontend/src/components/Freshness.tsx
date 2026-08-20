type Props = { fetchedAt: Date | null; onRefresh: () => void };

export default function Freshness({ fetchedAt, onRefresh }: Props) {
  const time = fetchedAt
    ? fetchedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : "—";
  return (
    <span className="freshness">
      Fetched {time}{" "}
      <button type="button" onClick={onRefresh}>
        Refresh
      </button>
    </span>
  );
}
