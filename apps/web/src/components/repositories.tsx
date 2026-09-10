"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, mutate, useResource } from "@/lib/api";
import type { CasePage as CasePageData, CaseSummary, RepositoryDetail, RepositorySummary, StartRunRequest } from "@/lib/api-types";
import { useSession } from "./workspace";
import { Breadcrumbs, Empty, ErrorNotice, Hash, humanize, Icon, Loading, navigateTabs, Status, Time } from "./ui";

export function RepositoryList() {
  const result = useResource<RepositorySummary[]>("/api/repositories", 15_000);
  return <>
    <div className="page-heading"><div><h1>Repositories</h1><p>The declared setup path, checked from a clean start.</p></div><button className="button secondary" onClick={result.refresh}><Icon name="refresh"/>Refresh</button></div>
    {result.error && <ErrorNotice error={result.error} retry={result.refresh}/>}
    {result.loading && <Loading/>}
    {result.data && (result.data.length ? <section className="repo-list" aria-label="Authorized repositories"><div className="list-labels"><span>Repository</span><span>Default branch</span><span>Repair status</span><span/></div>{result.data.map((repo) => <Link className="repo-row" key={repo.repository_id} href={`/repositories/${repo.repository_id}`}><div className="repo-identity"><Icon name="repo" size={21}/><div><h2>{repo.owner}<span className="muted"> / </span>{repo.name}</h2><p>{repo.branch}<span className="inline-separator">·</span><Hash value={repo.current_head} short/></p></div></div><div className="row-state"><span className="mobile-label">Default branch</span><Health repo={repo}/><small>{repo.latest_checked_sha ? <>Checked <Hash value={repo.latest_checked_sha} short/></> : "No recorded verification"}</small></div><div className="row-state"><span className="mobile-label">Repair status</span><Status value={repo.open_case_phase}>{repo.open_case_phase ? humanize(repo.open_case_phase) : "No active repair"}</Status><small>{repo.automatic_prs ? "Authorized PR publication" : "Automatic PRs disabled"}</small></div><Icon name="arrow"/></Link>)}</section> : <Empty title="No repositories available"><p>Your account has no accessible registered repository. Ask the operator to confirm the GitHub App installation and repository permissions.</p></Empty>)}
    <p className="data-note">Repository observations come from stored controller state. A repair result does not change default-branch health.</p>
  </>;
}

export function Health({ repo }: { repo: RepositorySummary }) {
  const current = repo.current_head && repo.latest_checked_sha === repo.current_head;
  if (!repo.latest_checked_sha) return <Status>Not checked</Status>;
  if (!current) return <Status value="stale">New revision not checked</Status>;
  return <Status value={repo.latest_outcome}>{repo.latest_outcome === "passed" ? "Verified at this revision" : repo.latest_outcome === "failed" ? "Broken at this revision" : humanize(repo.latest_outcome)}</Status>;
}

export function RepositoryPage({ repositoryId }: { repositoryId: string }) {
  const resource = useResource<RepositoryDetail>(`/api/repositories/${repositoryId}`, 15_000);
  const cases = useResource<CasePageData>(`/api/repositories/${repositoryId}/cases`, 15_000);
  const [tab, setTab] = useState<"cases" | "contract">("cases");
  const [starting, setStarting] = useState(false);
  const [runError, setRunError] = useState<Error | null>(null);
  const [requestId, setRequestId] = useState<string | null>(null);
  const session = useSession();
  const router = useRouter();
  const detail = resource.data;

  async function startRun() {
    setStarting(true); setRunError(null);
    const id = requestId ?? crypto.randomUUID(); setRequestId(id);
    try {
      const body: StartRunRequest = { request_id: id };
      const started = await mutate<CaseSummary>(`/api/repositories/${repositoryId}/runs`, body, session.csrf_token);
      setRequestId(null);
      router.push(`/repositories/${repositoryId}/cases/${encodeURIComponent(started.id)}`);
    } catch (cause) { setRunError(cause as Error); }
    finally { setStarting(false); }
  }

  if (resource.loading) return <Loading/>;
  if (!detail) return resource.error ? <ErrorNotice error={resource.error} retry={resource.refresh}/> : null;
  const repo = detail.repository;
  return <>
    <Breadcrumbs items={[{ label: `${repo.owner} / ${repo.name}` }]}/>
    <div className="page-heading"><div><h1>{repo.name}</h1><p>{repo.owner}<span className="inline-separator">/</span><Icon name="branch" size={15}/>{repo.branch}</p></div><button className="button primary" disabled={starting || !session.can_write} title={!session.can_write ? "Write permission is required to start a run" : undefined} onClick={startRun}><Icon name={starting ? "refresh" : "terminal"}/>{starting ? "Requesting run…" : "Run setup check"}</button></div>
    {resource.error && <ErrorNotice error={resource.error} retry={resource.refresh}/>}
    {runError && <ErrorNotice error={runError}/>}
    <div className="health-ledger"><section><h2>Default branch</h2><Health repo={repo}/><dl><div><dt>Observed head</dt><dd><Hash value={repo.current_head} short/></dd></div><div><dt>Last checked</dt><dd><Hash value={repo.latest_checked_sha} short/></dd></div></dl></section><section><h2>Repair</h2><Status value={repo.open_case_phase}>{repo.open_case_phase ? humanize(repo.open_case_phase) : "No active repair"}</Status><p>A verified candidate applies only to its tested commit.</p>{repo.open_case_id && <Link className="text-link" href={`/repositories/${repositoryId}/cases/${encodeURIComponent(repo.open_case_id)}`}>Inspect case<Icon name="arrow" size={15}/></Link>}</section><section className="observation-meta"><h2>Observation</h2><Time value={repo.observed_at}/><p>Stored controller state<br/>{repo.automatic_prs ? "PR publication is authorized" : "Automatic PR publication is disabled"}</p></section></div>
    <div className="tabs" role="tablist" aria-label="Repository view" onKeyDown={navigateTabs}><button role="tab" tabIndex={tab === "cases" ? 0 : -1} aria-selected={tab === "cases"} aria-controls="repository-cases" onClick={() => setTab("cases")}>Cases</button><button role="tab" tabIndex={tab === "contract" ? 0 : -1} aria-selected={tab === "contract"} aria-controls="repository-contract" onClick={() => setTab("contract")}>Approved contract<Icon name="lock" size={14}/></button></div>
    {tab === "cases" ? <section id="repository-cases" role="tabpanel"><div className="section-heading"><div><h2>Setup history</h2><p>One case per pinned execution. No sample runs.</p></div><button className="quiet-button" onClick={cases.refresh}><Icon name="refresh" size={15}/>Refresh</button></div>{cases.error && <ErrorNotice error={cases.error} retry={cases.refresh}/>}<CaseRows page={cases.data} loading={cases.loading} repositoryId={repositoryId}/></section> : <section id="repository-contract" role="tabpanel"><Contract detail={detail}/></section>}
  </>;
}

function CaseRows({ page, loading, repositoryId }: { page: CasePageData | null; loading: boolean; repositoryId: string }) {
  const [older, setOlder] = useState<CaseSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const nextCursor = older.length ? cursor : page?.next_cursor;
  async function more() {
    if (!nextCursor) return;
    setLoadingMore(true); setError(null);
    try {
      const next = await api<CasePageData>(`/api/repositories/${repositoryId}/cases?cursor=${encodeURIComponent(nextCursor)}`);
      setOlder((current) => [...current, ...next.items]); setCursor(next.next_cursor);
    } catch (cause) { setError(cause as Error); }
    finally { setLoadingMore(false); }
  }
  if (loading) return <Loading label="Loading cases"/>;
  if (!page) return null;
  const items = Array.from(new Map([...older, ...page.items].map((item) => [item.id, item])).values()).sort((a, b) => b.created_at - a.created_at);
  return <>{!items.length ? <Empty title="No setup checks yet"><p>Start a setup check to capture a baseline at the repository’s current revision. The isolated worker must be running to process it.</p></Empty> : <div className="case-list"><div className="case-labels"><span>Case / revision</span><span>State</span><span>Last update</span><span/></div>{items.map((item) => <Link key={item.id} className="case-row" href={`/repositories/${repositoryId}/cases/${encodeURIComponent(item.id)}`}><div><strong className="case-id">{item.id.slice(0, 18)}</strong><div className="case-revision"><Icon name="branch" size={13}/><Hash value={item.sha} short/></div></div><div><Status value={item.phase}/>{item.cancellation_requested && <small className="cancel-note">Cancellation requested</small>}</div><Time value={item.updated_at}/><Icon name="arrow" size={17}/></Link>)}</div>}{error && <ErrorNotice error={error}/>} {nextCursor && <div className="load-more"><button className="button secondary" onClick={more} disabled={loadingMore}>{loadingMore ? "Loading…" : "Load older cases"}</button></div>}</>;
}

function Contract({ detail }: { detail: RepositoryDetail }) {
  return <>
    <div className="section-heading"><div><h2>The approved setup path</h2><p>Approved at <Hash value={detail.approved_sha} short/>. Changes to the target require a new owner approval.</p></div></div>
    <div className="notice"><Icon name="lock"/><p>This view is read-only. The operator records owner-approved contracts through the existing registration workflow; FirstRun does not choose a success target on your behalf.</p></div>
    <div className="contract-grid"><section><h3>Immutable success target</h3><dl className="definition-list"><div><dt>Runtime</dt><dd><code>{detail.target.runtime_image}</code></dd></div><div><dt>Platform</dt><dd>{detail.target.platform}</dd></div><div><dt>Readiness</dt><dd><code>{detail.target.readiness_path}</code> → HTTP {detail.target.readiness_status}</dd></div><div><dt>Functional verifier</dt><dd><code>{detail.target.verifier_id}</code></dd></div><div><dt>Network</dt><dd>{detail.target.network}</dd></div><div><dt>Policy revision</dt><dd>{detail.policy_revision}</dd></div></dl></section><section><h3>Repairable execution recipe</h3><ol className="recipe-steps">{detail.recipe.steps.map((step) => <li key={step.id}><div className="recipe-step-meta"><strong>{step.id}</strong><span>{step.kind === "start" ? "Managed process" : "Foreground"} · {step.timeout_seconds}s</span></div><code>{step.argv.map((arg) => /\s/.test(arg) ? JSON.stringify(arg) : arg).join(" ")}</code><small>Working directory: <code>{step.cwd}</code></small></li>)}</ol></section></div>
    <section className="readme-section"><h3>Published README setup block</h3><p className="muted">Deterministically rendered from the recipe. Divergence is rejected before execution.</p><pre className="code-block">{detail.setup_instructions}</pre></section>
    <details className="digest-details"><summary>View approved input digests</summary><dl className="pin-list">{([['Target', detail.target_digest], ['Recipe', detail.recipe_digest], ['Protected files', detail.protected_digest], ['Verifier', detail.verifier_digest], ['Policy', detail.policy_digest], ['Runtime repository', detail.runtime_digest], ['Runtime image', detail.runtime_image_id]] as const).map(([label, value]) => <div key={label}><dt>{label}</dt><dd><Hash value={value}/></dd></div>)}</dl></details>
  </>;
}
