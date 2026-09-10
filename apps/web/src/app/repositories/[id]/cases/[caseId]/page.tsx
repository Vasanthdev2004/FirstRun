import { notFound } from "next/navigation";
import { CasePage } from "@/components/case-detail";
export default async function Page({ params }: { params: Promise<{ id: string; caseId: string }> }) {
  const { id, caseId } = await params;
  if (!/^[1-9][0-9]*$/.test(id) || !/^[A-Za-z0-9_-]{1,128}$/.test(caseId)) notFound();
  return <CasePage key={`${id}:${caseId}`} repositoryId={id} caseId={caseId}/>;
}
