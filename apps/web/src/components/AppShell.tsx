import { useEffect, useRef } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { Brand } from "./Brand";

export function AppShell() {
  const { session, signOut } = useAuth();
  const user = session?.user;
  const name = user?.github_login ?? user?.display_name ?? "";
  // A task's page belongs to "Tasks".
  const { pathname } = useLocation();
  const inTasks = pathname === "/" || pathname.startsWith("/tasks/");

  // On moving to another page, start keyboard and screen-reader users at its
  // content, as a full page load would.  Filter changes (same path) keep focus.
  const main = useRef<HTMLElement>(null);
  const shownPath = useRef(pathname);
  useEffect(() => {
    if (shownPath.current === pathname) return;
    shownPath.current = pathname;
    main.current?.focus();
  }, [pathname]);

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="topbar-brand" aria-label="CodeForge home">
            <Brand />
          </NavLink>
          <nav className="topbar-nav" aria-label="Main">
            <NavLink to="/overview" className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`}>
              Overview
            </NavLink>
            <Link to="/" className={`nav-link${inTasks ? " is-active" : ""}`} aria-current={inTasks ? "page" : undefined}>
              Tasks
            </Link>
          </nav>
          {user && (
            <div className="topbar-user">
              {user.avatar_url && <img className="avatar" src={user.avatar_url} alt="" width={28} height={28} />}
              <span className="topbar-user-name">{name}</span>
              <button type="button" className="button button-ghost button-small" onClick={() => signOut()}>
                Sign out
              </button>
            </div>
          )}
        </div>
      </header>
      <main className="page" id="main" tabIndex={-1} ref={main}>
        <Outlet />
      </main>
    </div>
  );
}
