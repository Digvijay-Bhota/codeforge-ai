import { statusInfo, type Tone } from "../lib/status";

export function Badge({ tone, children }: { tone: Tone; children: string }) {
  return (
    <span className={`badge badge-${tone}`}>
      <span className="badge-dot" aria-hidden="true" />
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const { label, tone } = statusInfo(status);
  return <Badge tone={tone}>{label}</Badge>;
}
