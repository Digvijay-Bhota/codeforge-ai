export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner" role={label ? "status" : undefined} aria-label={label}>
      <span className="spinner-ring" aria-hidden="true" />
    </span>
  );
}
