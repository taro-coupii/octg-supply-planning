import { ReactNode } from "react";

export default function WorkQueueCard({
  title,
  children,
  emptyLabel,
}: {
  title: string;
  children: ReactNode[];
  emptyLabel: string;
}) {
  return (
    <div className="card">
      <h3>{title}</h3>
      {children.length === 0 ? <div className="empty">{emptyLabel}</div> : children}
    </div>
  );
}
