import { notFound } from "next/navigation";
import { RepositoryPage } from "@/components/repositories";
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^[1-9][0-9]*$/.test(id)) notFound();
  return <RepositoryPage key={id} repositoryId={id}/>;
}
