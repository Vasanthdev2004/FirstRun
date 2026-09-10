"use client";

import Link from "next/link";
import { createContext, useContext, useState, type ReactNode } from "react";
import { mutate, useResource, type Session } from "@/lib/api";
import { ErrorNotice, Icon, Loading } from "./ui";

const SessionContext = createContext<Session | null>(null);
export function useSession() {
  const session = useContext(SessionContext);
  if (!session) throw new Error("Session boundary is required");
  return session;
}

export function Workspace({ children }: { children: ReactNode }) {
  const { data: session, error, loading, refresh } = useResource<Session>("/api/session", 60_000);
  const [signingOut, setSigningOut] = useState(false);
  const [logoutError, setLogoutError] = useState<Error | null>(null);

  async function logout() {
    setSigningOut(true); setLogoutError(null);
    try { await mutate("/api/auth/logout", {}, session?.csrf_token ?? null); window.location.assign("/"); }
    catch (cause) { setLogoutError(cause as Error); setSigningOut(false); }
  }

  return <div className="workspace">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <aside className="rail">
      <Link className="brand" href="/" aria-label="FirstRun home"><span className="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M5 19V5h14M5 12h10" stroke="currentColor" strokeWidth="2.5"/><path d="m15 16 3 3 4-5" stroke="currentColor" strokeWidth="2"/></svg></span><span>FirstRun</span></Link>
      <nav className="primary-nav" aria-label="Workspace"><Link className="nav-item selected" href="/" aria-current="page"><Icon name="repo"/><span>Repositories</span></Link></nav>
      <div className="rail-bottom"><p>Setup, observed.<br/>Repairs, proven.</p><span className="rail-version">Controlled Node / Linux lane</span></div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><span className="workspace-label">Maintainer workspace</span><div className="account">{session?.authenticated ? <><span className="account-name"><span className="avatar" aria-hidden="true">{session.user?.login.slice(0, 1).toUpperCase()}</span>{session.user?.login}</span><button className="quiet-button" disabled={signingOut} onClick={logout}>{signingOut ? "Signing out…" : "Sign out"}</button></> : <span className="auth-label"><Icon name="lock" size={15}/> {session?.configured ? "Sign-in required" : "Not connected"}</span>}</div></header>
      <main id="main-content" tabIndex={-1} className="main-content">
        {logoutError && <ErrorNotice error={logoutError}/>}
        {loading && <Loading label="Connecting to FirstRun"/>}
        {error && <ErrorNotice error={error} retry={refresh}/>}
        {!loading && !error && session && (session.authenticated ? <SessionContext value={session}>{children}</SessionContext> : <ConnectionState configured={session.configured} refresh={refresh}/>)}
      </main>
      <footer className="workspace-footer"><span>Every result belongs to an exact revision.</span><span>No automatic merges.</span></footer>
    </div>
  </div>;
}

function ConnectionState({ configured, refresh }: { configured: boolean; refresh: () => void }) {
  return <section className="connection-page">
    <div className="page-heading"><div><h1>Your repositories</h1><p>Keep the first-time setup path working.</p></div><span className="connection-state"><span className="status-dot"/>{configured ? "Authentication required" : "Configuration needed"}</span></div>
    <div className="connection-layout">
      <section className="connection-main">
        <div className="connection-glyph"><Icon name={configured ? "lock" : "repo"} size={32}/></div>
        <h2>{configured ? "Sign in to your workspace." : "Connect your first repository."}</h2>
        <p className="connection-copy">{configured ? "Use your GitHub account to view the repositories you can access, inspect setup evidence, and review proposed repairs." : "The interface is ready. A GitHub App installation and owner-approved setup contract are required before FirstRun can show repository state or start a run."}</p>
        <div className="connection-actions">{configured ? <a className="button primary" href="/api/auth/login">Continue with GitHub<Icon name="arrow"/></a> : <button className="button secondary" onClick={refresh}><Icon name="refresh"/>Check connection</button>}</div>
        <p className="connection-note"><Icon name="lock" size={15}/>{configured ? "Repository permissions are checked when you access data or act." : "No repository is connected. No setup run has been started by this interface."}</p>
      </section>
      <aside className="setup-checklist" aria-label={configured ? "What you can inspect" : "Required configuration"}>
        <h3>{configured ? "From failure to review" : "Before the first run"}</h3>
        <ol>
          <li><span className="step-number">1</span><div><strong>{configured ? "Find the failing revision" : "Authorize a GitHub App"}</strong><p>{configured ? "Main-branch health stays separate from a proposed repair." : "Limit the installation to the owner-approved repository. Configure user sign-in on the trusted API."}</p></div></li>
          <li><span className="step-number">2</span><div><strong>{configured ? "Inspect the executed recipe" : "Approve the setup contract"}</strong><p>{configured ? "Read command outcomes and the exact recipe/README changes." : "Pin the target, runtime, verifier, and initial recipe. The README must describe the same steps."}</p></div></li>
          <li><span className="step-number">3</span><div><strong>{configured ? "Review independent proof" : "Connect the isolated worker"}</strong><p>{configured ? "Check fresh-state evidence and exact tested hashes before opening the repair PR." : "Configure the existing worker and provider separately. Repository code never runs in this web process."}</p></div></li>
        </ol>
        {!configured && <p className="setup-docs">Operator instructions: <code>docs/WEB_SETUP.md</code></p>}
      </aside>
    </div>
    <div className="boundary-strip"><Icon name="branch"/><p>A verified repair is evidence for its own commit. Your default branch only becomes healthy after its own revision passes.</p></div>
  </section>;
}
