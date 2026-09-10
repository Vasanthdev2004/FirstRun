"use client";
export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <section className="empty-state" role="alert"><h1>This view could not be loaded.</h1><p>The interface encountered an error. Your repository state has not been changed.</p><button className="button secondary" onClick={reset}>Reload view</button></section>;
}
