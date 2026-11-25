"use client";

import { useParams } from "next/navigation";
import { StudioWorkspace } from "@/components/studio/workspace";

export default function StudioPage() {
  const { id } = useParams<{ id: string }>();
  return <StudioWorkspace key={id} campaignId={id} />;
}
