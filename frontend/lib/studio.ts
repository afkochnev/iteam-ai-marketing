import { request, requestBlob } from "@/lib/api";

export type StudioAssetKind = "ARTICLE" | "ILLUSTRATION";

export interface StudioMedia {
  sha256: string;
  width: number;
  height: number;
  mime_type: string;
  size_bytes: number;
}

export interface StudioArticleInput {
  title: string;
  body: string;
  source_version_id?: string;
  illustration_version_id?: string;
}

export interface StudioIllustrationInput {
  title: string;
  brief: string;
  brand_profile: string;
  alt_text: string;
  usage_rights: string;
  caption?: string;
  source_version_id?: string;
}

interface StudioIllustrationPayload extends Omit<StudioIllustrationInput, "source_version_id" | "caption"> {
  caption: string | null;
  original: StudioMedia;
  variants: Array<StudioMedia & { name: string }>;
  provenance: {
    source: "UPLOAD" | "GENERATED";
    model?: string | null;
    prompt?: string | null;
    provider_request_id?: string | null;
    generation_size?: string | null;
  };
}

interface StudioVersionFields {
  id: string;
  asset_id: string;
  version_number: number;
  supersedes_version_id: string | null;
  source_version_id: string | null;
  illustration_version_id: string | null;
  snapshot_hash: string;
  created_by: string;
  created_at: string;
  approval: { approved_at: string; approved_by: string } | null;
}

export type StudioVersion = StudioVersionFields & (
  | { kind: "ARTICLE"; payload: { title: string; body: string } }
  | { kind: "ILLUSTRATION"; payload: StudioIllustrationPayload }
);

export interface StudioAsset {
  id: string;
  kind: StudioAssetKind;
  latest_version: StudioVersion;
}

export interface StudioPage<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface StudioGenerationJob {
  id: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "UNKNOWN";
  asset_id: string | null;
  version_id: string | null;
  error_code: string | null;
}

export interface StudioRevision {
  asset_id: string;
  expected_current_version_id: string;
}

const PAGE_SIZE = 50;

function studioPath(campaignId: string, path: string) {
  return `/campaigns/${encodeURIComponent(campaignId)}/studio${path}`;
}

function jsonPost(value: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(value) };
}

export const studioApi = {
  assets(campaignId: string, offset: number, signal: AbortSignal, kind?: StudioAssetKind) {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (kind) params.set("kind", kind);
    return request<StudioPage<StudioAsset>>(studioPath(campaignId, `/assets?${params}`), { signal });
  },
  versions(campaignId: string, assetId: string, offset: number, signal: AbortSignal) {
    return request<StudioPage<StudioVersion>>(
      studioPath(campaignId, `/assets/${encodeURIComponent(assetId)}/versions?limit=${PAGE_SIZE}&offset=${offset}`),
      { signal },
    );
  },
  saveArticle(campaignId: string, input: StudioArticleInput, revision?: StudioRevision) {
    const path = revision ? `/assets/articles/${encodeURIComponent(revision.asset_id)}/versions` : "/assets/articles";
    const payload = revision ? { ...input, expected_current_version_id: revision.expected_current_version_id } : input;
    return request<StudioVersion>(studioPath(campaignId, path), jsonPost(payload));
  },
  uploadIllustration(campaignId: string, input: StudioIllustrationInput, file: File | undefined, revision?: StudioRevision) {
    const path = revision ? `/assets/illustrations/${encodeURIComponent(revision.asset_id)}/versions` : "/assets/illustrations";
    const form = new FormData();
    if (file) form.set("file", file);
    form.set("title", input.title);
    form.set("brief", input.brief);
    form.set("brand_profile", input.brand_profile);
    form.set("alt_text", input.alt_text);
    form.set("usage_rights", input.usage_rights);
    if (input.caption) form.set("caption", input.caption);
    if (input.source_version_id) form.set("source_version_id", input.source_version_id);
    if (revision) form.set("expected_current_version_id", revision.expected_current_version_id);
    return request<StudioVersion>(studioPath(campaignId, path), { method: "POST", body: form });
  },
  approve(campaignId: string, versionId: string) {
    return request<StudioVersion>(studioPath(campaignId, `/versions/${encodeURIComponent(versionId)}/approve`), { method: "POST" });
  },
  media(campaignId: string, versionId: string, name: string, signal: AbortSignal) {
    return requestBlob(studioPath(campaignId, `/versions/${encodeURIComponent(versionId)}/media/${encodeURIComponent(name)}`), signal);
  },
  generate(campaignId: string, input: StudioIllustrationInput, revision?: StudioRevision) {
    return request<StudioGenerationJob>(studioPath(campaignId, "/illustrations/generate"), jsonPost({ ...input, ...revision }));
  },
  generationJob(campaignId: string, jobId: string, signal: AbortSignal) {
    return request<StudioGenerationJob>(studioPath(campaignId, `/illustrations/generation-jobs/${encodeURIComponent(jobId)}`), { signal });
  },
};
