import { API_URL, request, requestBlob } from "@/lib/api";

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

export interface StudioGenerationRecovery {
  draft: Omit<StudioIllustrationInput, "caption" | "source_version_id"> & {
    caption: string | null;
    source_version_id: string | null;
  };
  previous: StudioVersion | null;
}

export interface StudioGenerationJob {
  id: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "UNKNOWN";
  asset_id: string | null;
  version_id: string | null;
  error_code: string | null;
  recovery?: StudioGenerationRecovery | null;
}

export interface StudioRevision {
  asset_id: string;
  expected_current_version_id: string;
}

export interface StudioWebsiteDraft {
  seo_title: string;
  seo_description: string;
}

export interface StudioWebsitePage {
  id: string;
  article_version_id: string;
  illustration_version_id: string | null;
  created_at: string;
  published_at: string | null;
}

export interface StudioWebsiteStatus {
  enabled: boolean;
  page: StudioWebsitePage | null;
}

export type StudioWordPressAction = "draft" | "image" | "publish";

export interface StudioWordPressConfirmation {
  target_url: string;
  target_author_id: string;
}

export interface StudioWordPressStatus {
  target_url: string | null;
  target_author_id: string | null;
  target_changed: boolean;
  draft_preview_url?: string | null;
  operations: {
    action: StudioWordPressAction;
    status: "queued" | "running" | "unknown" | "failed" | "succeeded";
    remote_id: string | null;
    remote_url: string | null;
    failure_code: string | null;
    reconciliation_pending: boolean;
    reconciliation_finished_at: string | null;
  }[];
}

const PAGE_SIZE = 50;

function studioPath(campaignId: string, path: string) {
  return `/campaigns/${encodeURIComponent(campaignId)}/studio${path}`;
}

function jsonPost(value: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(value) };
}

function websiteSandboxPath(campaignId: string, versionId: string) {
  return studioPath(campaignId, `/versions/${encodeURIComponent(versionId)}/website-sandbox`);
}

export const studioApi = {
  assets(campaignId: string, offset: number, signal: AbortSignal, kind?: StudioAssetKind) {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (kind) params.set("kind", kind);
    return request<StudioPage<StudioAsset>>(studioPath(campaignId, `/assets?${params}`), { signal });
  },
  versions(campaignId: string, assetId: string, offset: number, signal: AbortSignal, limit = PAGE_SIZE) {
    return request<StudioPage<StudioVersion>>(
      studioPath(campaignId, `/assets/${encodeURIComponent(assetId)}/versions?limit=${limit}&offset=${offset}`),
      { signal },
    );
  },
  version(campaignId: string, versionId: string, signal: AbortSignal) {
    return request<StudioVersion>(studioPath(campaignId, `/versions/${encodeURIComponent(versionId)}`), { signal });
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
  websitePackage(campaignId: string, versionId: string, signal: AbortSignal) {
    return requestBlob(
      studioPath(campaignId, `/versions/${encodeURIComponent(versionId)}/website-package`),
      signal,
      "Не удалось скачать пакет для сайта.",
    );
  },
  wordpressStatus(campaignId: string, versionId: string, signal: AbortSignal) {
    const path = studioPath(campaignId, `/versions/${encodeURIComponent(versionId)}/wordpress`);
    return request<StudioWordPressStatus>(path, { signal });
  },
  wordpressCommand(
    campaignId: string, versionId: string, action: StudioWordPressAction,
    confirmation: StudioWordPressConfirmation, reconcile: boolean,
  ) {
    const suffix = reconcile ? "/reconcile" : "";
    const path = studioPath(campaignId, `/versions/${encodeURIComponent(versionId)}/wordpress/${action}${suffix}`);
    return request<StudioWordPressStatus>(path, jsonPost(confirmation));
  },
  websiteStatus(campaignId: string, versionId: string, signal: AbortSignal) {
    return request<StudioWebsiteStatus>(websiteSandboxPath(campaignId, versionId), { signal });
  },
  createWebsiteDraft(campaignId: string, versionId: string, metadata: StudioWebsiteDraft) {
    return request<StudioWebsitePage>(`${websiteSandboxPath(campaignId, versionId)}/draft`, jsonPost(metadata));
  },
  publishWebsiteDraft(campaignId: string, versionId: string, pageId: string) {
    return request<StudioWebsitePage>(`${websiteSandboxPath(campaignId, versionId)}/${encodeURIComponent(pageId)}/publish`, { method: "POST" });
  },
  websiteDraftUrl(campaignId: string, versionId: string, pageId: string) {
    return `${API_URL}${websiteSandboxPath(campaignId, versionId)}/${encodeURIComponent(pageId)}/preview`;
  },
  websitePublicUrl(pageId: string) {
    return `${API_URL}/website-sandbox/${encodeURIComponent(pageId)}`;
  },
  generate(campaignId: string, input: StudioIllustrationInput, revision?: StudioRevision) {
    return request<StudioGenerationJob>(studioPath(campaignId, "/illustrations/generate"), jsonPost({ ...input, ...revision }));
  },
  generationJob(campaignId: string, jobId: string, signal: AbortSignal) {
    return request<StudioGenerationJob>(studioPath(campaignId, `/illustrations/generation-jobs/${encodeURIComponent(jobId)}`), { signal });
  },
};
