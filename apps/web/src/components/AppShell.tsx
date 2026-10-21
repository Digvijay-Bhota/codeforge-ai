import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { Brand } from "./Brand";

export function AppShell() {
  const { session, signOut } = useAuth();
  const user = session?.user;
  const name = user?.github_login ?? user?.display_name ?? "";

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
            <NavLink to="/" className={({ isActive }) => `nav-link${isActive ? " is-active" : ""}`}>
              Tasks
            </NavLink>
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
      <main className="page" id="main" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
