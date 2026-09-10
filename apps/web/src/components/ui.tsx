import Link from "next/link";
import type { KeyboardEvent, ReactNode } from "react";

export function navigateTabs(event: KeyboardEvent<HTMLElement>) {
  const keys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"];
  if (!keys.includes(event.key)) return;
  const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
  const index = tabs.indexOf(event.target as HTMLButtonElement);
  if (index < 0) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
    : (index + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next]?.focus(); tabs[next]?.click();
}

export function Icon({ name, size = 18 }: { name: "repo" | "arrow" | "branch" | "refresh" | "lock" | "check" | "external" | "terminal"; size?: number }) {
  const paths = {
    repo: <><path d="M5 3h13v17H5a2 2 0 0 1 0-4h13M5 3a2 2 0 0 0-2 2v13"/><path d="M7 7h6M7 10h4"/></>,
    arrow: <><path d="M4 12h15m-6-6 6 6-6 6"/></>,
    branch: <><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M6 7v10m12-10c0 6-12 3-12 8"/></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1"/></>,
    lock: <><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
    external: <><path d="M14 3h7v7m0-7L10 14M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/></>,
    terminal: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

export function humanize(value: string | null | undefined): string {
  if (!value) return "Not checked";
  const labels: Record<string, string> = {
    passed: "Passed", failed: "Failed", timed_out: "Timed out", infrastructure_error: "Infrastructure blocked",
    policy_blocked: "Policy blocked", cleanup_failed: "Cleanup failed", pr_open: "Repair PR open",
    repair_ready: "Repair ready", verified: "Verified", baseline_running: "Running baseline",
    proof_running: "Running proof", needs_input: "Needs input", reconcile_pending: "Reconciling GitHub",
  };
  return labels[value] ?? value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

export function Status({ value, children }: { value?: string | null; children?: ReactNode }) {
  const tone = ["passed", "verified", "repair_ready", "pr_open"].includes(value ?? "") ? "positive"
    : ["failed", "cleanup_failed", "quarantined"].includes(value ?? "") ? "negative"
    : ["needs_input", "stale", "interrupted", "blocked", "infrastructure_error", "timed_out", "policy_blocked"].includes(value ?? "") ? "warning"
    : ["queued", "fetching", "investigating", "baseline_running", "proof_running", "publishing", "reconcile_pending"].includes(value ?? "") ? "active" : "neutral";
  return <span className={`status ${tone}`}><span className="status-dot"/>{children ?? humanize(value)}</span>;
}

export function Hash({ value, short = false }: { value?: string | null; short?: boolean }) {
  return value ? <code className={short ? "hash hash-short" : "hash"} title={value}>{short ? value.replace(/^sha256:/, "").slice(0, 10) : value}</code> : <span className="muted">Not recorded</span>;
}

export function Time({ value }: { value: number | null }) {
  if (!value) return <span>Not observed</span>;
  const date = new Date(value * 1000);
  return <time dateTime={date.toISOString()} title={date.toISOString()}>{new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date)}</time>;
}

export function ErrorNotice({ error, retry }: { error: Error; retry?: () => void }) {
  return <div className="notice error-notice" role="alert"><div><strong>Unable to load the latest state</strong><p>{error.message}</p></div>{retry && <button className="button secondary" onClick={retry}><Icon name="refresh"/>Try again</button>}</div>;
}

export function Loading({ label = "Loading repository state" }: { label?: string }) {
  return <div className="loading-state" role="status"><span className="loading-pulse"/><span>{label}…</span></div>;
}

export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return <div className="empty-state"><Icon name="repo" size={26}/><h2>{title}</h2><div className="muted">{children}</div></div>;
}

export function GitHubLink({ href, children, className = "text-link" }: { href?: string | null; children: ReactNode; className?: string }) {
  if (!href) return null;
  // Browser links never promote an artifact-supplied scheme or arbitrary host.
  try { const url = new URL(href); if (url.protocol !== "https:" || url.hostname !== "github.com" || url.username || url.password) return null; } catch { return null; }
  return <a className={className} href={href} target="_blank" rel="noopener noreferrer">{children}<Icon name="external" size={14}/></a>;
}

export function Breadcrumbs({ items }: { items: { label: string; href?: string }[] }) {
  return <nav className="breadcrumbs" aria-label="Breadcrumb"><Link href="/">Repositories</Link>{items.map((item, index) => <span key={index}><span className="separator" aria-hidden="true">/</span>{item.href ? <Link href={item.href}>{item.label}</Link> : <span aria-current="page">{item.label}</span>}</span>)}</nav>;
}
