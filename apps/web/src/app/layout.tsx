import type { Metadata } from "next";
import { Workspace } from "@/components/workspace";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "FirstRun · Repositories", template: "%s · FirstRun" },
  description: "Inspect setup failures, bounded repairs, and independently executed proof.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body><Workspace>{children}</Workspace></body></html>;
}
