import type { ReactNode } from "react";
import { errorMessage } from "../api/client";
import { Spinner } from "./Spinner";

export function LoadingState({ label }: { label: string }) {
  return (
    <div className="state" role="status">
      <Spinner />
      <span>{label}</span>
    </div>
  );
}

export function ErrorState({ title, error, onRetry }: { title: string; error: unknown; onRetry?: () => void }) {
  return (
    <div className="state state-error" role="alert">
      <p className="state-title">{title}</p>
      <p className="state-body">{errorMessage(error)}</p>
      {onRetry && (
        <button type="button" className="button button-secondary" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state">
      <p className="state-title">{title}</p>
      {children && <div className="state-body">{children}</div>}
    </div>
  );
}
