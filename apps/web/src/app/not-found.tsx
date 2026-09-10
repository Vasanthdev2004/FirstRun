import Link from "next/link";
export default function NotFound() {
  return <section className="empty-state"><h1>Nothing at this address.</h1><p>Check the link, or return to the repositories you can access.</p><Link className="button secondary" href="/">Back to repositories</Link></section>;
}
