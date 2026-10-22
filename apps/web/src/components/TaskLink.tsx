import type { ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

export function taskPath(taskId: string): string {
  return `/tasks/${encodeURIComponent(taskId)}`;
}

/** Router state for a task link: the page it was opened from, so "back" returns there. */
export interface FromState {
  from: string;
}

export function useFromState(): FromState {
  const location = useLocation();
  return { from: `${location.pathname}${location.search}` };
}

/** A link to a task page that remembers where it was opened (filters, page and view included). */
export function TaskLink({ taskId, className, children }: { taskId: string; className?: string; children: ReactNode }) {
  const state = useFromState();
  return (
    <Link to={taskPath(taskId)} state={state} className={className}>
      {children}
    </Link>
  );
}

/** Where a task page's back link goes: the page it was opened from, or the task list. */
export function backTarget(state: unknown): { path: string; label: string } {
  const from = (state as Partial<FromState> | null)?.from;
  // Only this app's own paths.
  if (typeof from !== "string" || !from.startsWith("/") || from.startsWith("//")) {
    return { path: "/", label: "All tasks" };
  }
  if (from.startsWith("/overview")) return { path: from, label: "Back to overview" };
  if (from.startsWith("/tasks/")) return { path: from, label: "Back to previous task" };
  return { path: from, label: from === "/" ? "All tasks" : "Back to tasks" };
}
