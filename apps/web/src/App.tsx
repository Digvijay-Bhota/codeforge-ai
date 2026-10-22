import { Link, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthContext";
import { CallbackPage } from "./auth/CallbackPage";
import { CALLBACK_PATH, LoginPage } from "./auth/LoginPage";
import { RequireAuth } from "./auth/RequireAuth";
import { AppShell } from "./components/AppShell";
import { EmptyState } from "./components/StateViews";
import { usePageTitle } from "./lib/usePageTitle";
import { OverviewPage } from "./overview/OverviewPage";
import { TaskDetailPage } from "./tasks/TaskDetailPage";
import { TaskListPage } from "./tasks/TaskListPage";

/** Opt in to React Router v7 behaviour now, so the upgrade changes nothing. */
export const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path={CALLBACK_PATH} element={<CallbackPage />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<TaskListPage />} />
        <Route path="overview" element={<OverviewPage />} />
        <Route path="tasks/:taskId" element={<TaskDetailPage />} />
        <Route
          path="*"
          element={<NotFound />}
        />
      </Route>
    </Routes>
  );
}

function NotFound() {
  usePageTitle("Page not found");
  return (
    <EmptyState title="Page not found">
      <Link to="/">Go to tasks</Link>
    </EmptyState>
  );
}

export function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}
