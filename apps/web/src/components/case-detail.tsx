"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError, mutate, useResource } from "@/lib/api";
import type { BaselineView, CaseDetail, CaseSummary, DecisionRequest, DecisionResult, EventPage, EventView, ProofView } from "@/lib/api-types";
import { useSession } from "./workspace";
import { Breadcrumbs, Empty, ErrorNotice, GitHubLink, Hash, humanize, Icon, Loading, navigateTabs, Status, Time } from "./ui";

type Stage = "baseline" | "investigation" | "proof" | "publication";
const stages: { id: Stage; title: string; description: string }[] = [
  { id: "baseline", title: "Baseline", description: "Published setup, clean state" },
  { id: "investigation", title: "Investigation", description: "Bounded recipe repair" },
  { id: "proof", title: "Independent proof", description: "New state, protected verifier" },
  { id: "publication", title: "GitHub handoff", description: "Exact-commit evidence" },
];
const cancellable = new Set<CaseSummary["phase"]>(["queued", "fetching", "baseline_running", "investigating", "needs_input", "proof_running", "repair_ready"]);

export function CasePage({ repositoryId, caseId }: { repositoryId: string; caseId: string }) {
  const path = `/api/repositories/${repositoryId}/cases/${encodeURIComponent(caseId)}`;
  const resource = useResource<CaseDetail>(path, 5_000);
  const [selected, setSelected] = useState<Stage | null>(null);
  const detail = resource.data;
  if (resource.loading) return <Loading label="Loading execution evidence"/>;
  if (!detail) return resource.error ? <ErrorNotice error={resource.error} retry={resource.refresh}/> : null;
  const stage = selected ?? defaultStage(detail);

  return <>
    <Breadcrumbs items={[{ label: "Repository", href: `/repositories/${repositoryId}` }, { label: `Case ${detail.case.id.slice(0, 12)}` }]}/>
    <div className="page-heading case-heading"><div><h1>Setup check</h1><p><Hash value={detail.case.sha} short/><span className="inline-separator">·</span><Time value={detail.case.created_at}/><span className="inline-separator">·</span><span>Version {detail.case.version}</span></p></div><div className="heading-actions"><Status value={detail.case.phase}/><button className="icon-button" aria-label="Refresh case" onClick={resource.refresh}><Icon name="refresh"/></button></div></div>
    {resource.error && <ErrorNotice error={resource.error} retry={resource.refresh}/>}
    {detail.case.phase === "stale" && <div className="notice warning-notice"><Icon name="branch"/><p><strong>This result is stale.</strong> The tested revision is no longer the observed branch head. No passing result carries forward to the new revision.</p></div>}
    {detail.case.cancellation_requested && detail.case.phase !== "cancelled" && <div className="notice warning-notice" role="status"><p><strong>Cancellation requested.</strong> The current bounded execution may still be running. The worker must confirm cleanup before this case is cancelled.</p></div>}
    {detail.message && <p className="case-message">{detail.message}</p>}
    <CaseActions detail={detail} path={path} refresh={resource.refresh} disabled={!!resource.error}/>
    <div className="execution-layout">
      <aside className="execution-rail"><div className="stage-list" role="tablist" aria-label="Execution stage" aria-orientation="vertical" onKeyDown={navigateTabs}>{stages.map((item, index) => <button id={`stage-${item.id}`} role="tab" key={item.id} tabIndex={stage === item.id ? 0 : -1} className={`stage-button ${stage === item.id ? "stage-selected" : ""}`} aria-selected={stage === item.id} aria-controls="stage-panel" onClick={() => setSelected(item.id)}><span className="stage-number">{index + 1}</span><span><strong>{item.title}</strong><small>{item.description}</small><span className="stage-observation">{stageObservation(item.id, detail)}</span></span></button>)}</div><div className="execution-rule"><Icon name="lock" size={16}/><p>The repair agent cannot change the success target or certify its own work.</p></div></aside>
      <section id="stage-panel" role="tabpanel" aria-labelledby={`stage-${stage}`} className="stage-panel" tabIndex={0} key={stage}>
        {stage === "baseline" && <Baseline baseline={detail.baseline}/>}
        {stage === "investigation" && <Investigation detail={detail}/>}
        {stage === "proof" && <Proof proof={detail.proof} exact={false}/>}
        {stage === "publication" && <Publication detail={detail}/>}
      </section>
      <aside className="provenance-rail"><h2>Case identity</h2><dl className="pin-list"><div><dt>Pinned base commit</dt><dd><Hash value={detail.case.sha}/></dd></div><div><dt>Case</dt><dd><code className="hash">{detail.case.id}</code></dd></div><div><dt>Last update</dt><dd><Time value={detail.case.updated_at}/></dd></div></dl><div className="proof-boundary"><Icon name="branch" size={18}/><h3>Repair ≠ healthy main</h3><p>Even a verified repair leaves the default branch unchanged until that branch’s own revision passes.</p><Link className="text-link" href={`/repositories/${repositoryId}`}>View branch health<Icon name="arrow" size={14}/></Link></div></aside>
    </div>
    <Events page={detail.events} path={path}/>
  </>;
}

function defaultStage(detail: CaseDetail): Stage {
  if (detail.exact_commit_proof || ["pr_open", "publishing", "reconcile_pending"].includes(detail.case.phase)) return "publication";
  if (detail.proof || detail.case.phase === "proof_running") return "proof";
  if (detail.candidate || detail.needs_input || detail.case.phase === "investigating") return "investigation";
  return "baseline";
}

function stageObservation(stage: Stage, detail: CaseDetail) {
  if (stage === "baseline") return detail.baseline ? humanize(detail.baseline.outcome) : detail.case.phase === "baseline_running" ? "Running" : "No evidence yet";
  if (stage === "investigation") return detail.needs_input ? "Needs input" : detail.candidate ? "Candidate recorded" : detail.case.phase === "investigating" ? "Investigating" : "No candidate yet";
  if (stage === "proof") return detail.proof ? detail.proof.verified ? "Candidate verified" : humanize(detail.proof.outcome) : detail.case.phase === "proof_running" ? "Running" : "No evidence yet";
  return detail.repair_pr_url ? "PR recorded" : detail.exact_commit_proof ? "Commit proof recorded" : "No handoff recorded";
}

function CaseActions({ detail, path, refresh, disabled }: { detail: CaseDetail; path: string; refresh: () => void; disabled: boolean }) {
  const session = useSession();
  const router = useRouter();
  const [pending, setPending] = useState<"cancel" | "recheck" | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [intent, setIntent] = useState<DecisionRequest | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const canCancel = cancellable.has(detail.case.phase) && !detail.case.cancellation_requested;
  const canRecheck = detail.needs_input?.supported_actions.includes("recheck") ?? false;

  async function decide(action: "cancel" | "recheck") {
    setPending(action); setError(null);
    // Retry an uncertain POST with the same payload and idempotency key.
    const request: DecisionRequest = intent?.action === action ? intent : { request_id: crypto.randomUUID(), expected_version: detail.case.version, case_sha: detail.case.sha, action };
    setIntent(request);
    try {
      const result = await mutate<DecisionResult>(`${path}/decision`, request, session.csrf_token);
      setIntent(null);
      if (result.replacement_case_id) router.push(`/repositories/${detail.case.repository_id}/cases/${encodeURIComponent(result.replacement_case_id)}`);
      else refresh();
    } catch (cause) {
      setError(cause as Error);
      if (cause instanceof ApiError && cause.status === 409) { setIntent(null); refresh(); }
    } finally { setPending(null); }
  }

  return <>{detail.needs_input && <section className="decision-section"><div><h2>A maintainer decision is needed</h2><p>{detail.needs_input.prompt}</p><p className="muted">{humanize(detail.needs_input.reason_code)}{detail.needs_input.affected_step_id ? ` · Step ${detail.needs_input.affected_step_id}` : ""}</p><p className="decision-guidance">Resolve the required configuration or authority with the operator. Do not paste credentials here. A recheck creates a new case and does not change this case’s pinned target.</p></div>{canRecheck && <div className="decision-controls"><label className="checkbox-label"><input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)}/>The operator has resolved the required input.</label><button className="button primary" disabled={!acknowledged || pending !== null || !session.can_write || disabled} onClick={() => decide("recheck")}>{pending === "recheck" ? "Requesting recheck…" : "Recheck current revision"}<Icon name="arrow" size={16}/></button></div>}</section>}{canCancel && <div className="case-action-row"><span>{!session.can_write ? "Read-only access. Write permission is required to act." : "Cancel requests stop work at the next safe worker boundary."}</span><button className="quiet-button danger-button" disabled={pending !== null || !session.can_write || disabled} onClick={() => decide("cancel")}>{pending === "cancel" ? "Requesting cancellation…" : "Cancel case"}</button></div>}{error && <ErrorNotice error={error} retry={refresh}/>}</>;
}

function Baseline({ baseline }: { baseline: BaselineView | null }) {
  if (!baseline) return <Empty title="No baseline evidence yet"><p>Command output appears after the controller records a baseline result. Waiting is not a passing check.</p></Empty>;
  return <><div className="panel-heading"><div><h2>Published recipe</h2><p>The original setup path at <Hash value={baseline.tested_sha} short/>.</p></div><Status value={baseline.outcome}/></div><div className="acceptance-strip"><div><span>Readiness</span><Status value={baseline.readiness_outcome}/></div><div><span>Functional acceptance</span><Status value={baseline.functional_outcome}/></div><div><span>Cleanup</span><Status value={baseline.cleanup_succeeded ? "passed" : "cleanup_failed"}>{baseline.cleanup_succeeded ? "Confirmed" : "Not confirmed"}</Status></div></div><h3>Executed commands</h3><div className="command-list">{baseline.commands.map((command, index) => <details key={`${command.step_id}-${index}`} className="command-detail" open={command.outcome !== "passed"}><summary><span className="command-title"><Icon name="terminal" size={16}/><code>{command.argv.map((arg) => /\s/.test(arg) ? JSON.stringify(arg) : arg).join(" ")}</code></span><Status value={command.outcome}/></summary><div className="command-content"><div className="command-metadata"><span>Directory: <code>{command.cwd}</code></span><span>Exit: {command.exit_code ?? "Not recorded"}</span><span>{command.kind === "start" ? "Managed process" : "Foreground step"}</span></div>{command.stdout_tail && <><h4>Standard output · retained tail</h4><pre className="log-output">{command.stdout_tail}</pre></>}{command.stderr_tail && <><h4>Standard error · retained tail</h4><pre className="log-output error-output">{command.stderr_tail}</pre></>}{!command.stdout_tail && !command.stderr_tail && <p className="muted">No retained output for this command.</p>}</div></details>)}</div><details className="digest-details"><summary>Baseline input digests</summary><PinList entries={[["Target", baseline.target_digest], ["Recipe", baseline.recipe_digest], ["Verifier", baseline.verifier_digest], ["Runtime image", baseline.runtime_image_id]]}/></details></>;
}

function Investigation({ detail }: { detail: CaseDetail }) {
  return <><div className="panel-heading"><div><h2>Allowed setup changes</h2><p>The recipe is repairable. The success target is not.</p></div></div>{!detail.candidate ? <Empty title={detail.needs_input ? "Repair is waiting on input" : "No candidate recorded"}><p>{detail.needs_input ? "Resolve the focused question above before starting a new check." : "The controller has not recorded an allowed recipe/README patch for this case."}</p></Empty> : <><div className="diff-header"><span>{detail.candidate.paths.length} changed files</span><span>Candidate <Hash value={detail.candidate.patch_digest} short/></span></div><pre className="diff-block" aria-label="Candidate unified diff">{detail.candidate.unified_diff.split("\n").map((line, index) => <span className={line.startsWith("+") && !line.startsWith("+++") ? "diff-added" : line.startsWith("-") && !line.startsWith("---") ? "diff-removed" : line.startsWith("@@") ? "diff-hunk" : ""} key={index}>{line || " "}{"\n"}</span>)}</pre><PinList entries={[["Patch digest", detail.candidate.patch_digest], ["Candidate tree", detail.candidate.candidate_tree_digest]]}/><p className="data-note">A patch alone is not proof. Inspect the independently executed candidate next.</p></>}</>;
}

function Proof({ proof, exact }: { proof: ProofView | null; exact: boolean }) {
  if (!proof) return <Empty title={exact ? "No exact-commit proof recorded" : "No independent proof recorded"}><p>{exact ? "GitHub publication requires a fresh verification of the actual repair commit." : "Proof starts from separate mutable state. Investigation output cannot stand in for it."}</p></Empty>;
  return <><div className="panel-heading"><div><h2>{exact ? "Actual repair commit" : "Independent candidate proof"}</h2><p>{exact ? "Evidence belongs to the exact Git commit tested for publication." : "A separate clean execution, without the repair agent."}</p></div><Status value={proof.verified ? "verified" : proof.outcome}>{proof.verified ? "Verified" : humanize(proof.outcome)}</Status></div><div className="proof-checks"><div><Icon name={proof.fresh_state ? "check" : "lock"}/><span>Independent fresh state</span><strong>{proof.fresh_state ? "Recorded" : "Not confirmed"}</strong></div><div><Icon name={proof.verified ? "check" : "lock"}/><span>Verification predicate</span><strong>{proof.verified ? "Satisfied" : "Not satisfied"}</strong></div><div><Icon name={proof.cleanup_succeeded ? "check" : "lock"}/><span>Owned-resource cleanup</span><strong>{proof.cleanup_succeeded ? "Confirmed" : "Not confirmed"}</strong></div></div><h3>Exact proof inputs</h3><PinList entries={[["Tested commit", proof.tested_sha], ["Git tree", proof.git_tree], ["Content tree", proof.content_tree_digest], ["Candidate patch", proof.candidate_digest], ["Candidate tree", proof.candidate_tree_digest], ["Target", proof.target_digest], ["Executed recipe", proof.recipe_digest], ["README", proof.readme_digest], ["Verifier", proof.verifier_digest], ["Policy", proof.policy_digest], ["Runtime repository", proof.runtime_digest], ["Runtime image", proof.runtime_image_id]]}/></>;
}

function Publication({ detail }: { detail: CaseDetail }) {
  return <><div className="publication-links"><GitHubLink href={detail.repair_pr_url} className="button primary">Open repair PR{detail.repair_pr_number ? ` #${detail.repair_pr_number}` : ""}</GitHubLink><GitHubLink href={detail.check_url}>Open GitHub check</GitHubLink></div><Proof proof={detail.exact_commit_proof} exact/><div className="notice"><Icon name="branch"/><p>FirstRun never auto-merges. A published repair does not certify the default branch; that revision needs its own check after merging.</p></div></>;
}

function PinList({ entries }: { entries: [string, string | null][] }) {
  return <dl className="pin-list">{entries.map(([label, value]) => <div key={label}><dt>{label}</dt><dd><Hash value={value}/></dd></div>)}</dl>;
}

function Events({ page, path }: { page: EventPage; path: string }) {
  const [additional, setAdditional] = useState<EventView[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const next = additional.length ? cursor : page.next_cursor;
  async function loadMore() {
    if (!next) return;
    setBusy(true); setError(null);
    try { const result = await api<EventPage>(`${path}/events?cursor=${next}`); setAdditional((events) => [...events, ...result.items]); setCursor(result.next_cursor); }
    catch (cause) { setError(cause as Error); }
    finally { setBusy(false); }
  }
  const items = Array.from(new Map([...page.items, ...additional].map((event) => [event.event_id, event])).values());
  return <section className="events-section"><div className="section-heading"><div><h2>Controller timeline</h2><p>Recorded events, not model reasoning.</p></div><span className="muted">{items.length} loaded</span></div>{items.length ? <ol className="event-list">{items.map((event) => <li key={event.event_id}><Time value={event.created_at}/><span className="event-dot"/><div><strong>{humanize(event.phase)}</strong><p>{event.summary}</p></div></li>)}</ol> : <p className="muted">No events have been recorded.</p>}{error && <ErrorNotice error={error}/>} {next && <button className="button secondary" disabled={busy} onClick={loadMore}>{busy ? "Loading…" : "Load more events"}</button>}</section>;
}
