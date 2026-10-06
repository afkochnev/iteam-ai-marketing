export type UserRole = "ADMIN" | "MANAGER";
export interface User { id: string; email: string; full_name: string | null; role: UserRole; is_active: boolean; created_at: string; }
interface ApiErrorBody { error?: { code?: string; message?: string; details?: { errors?: Array<{ message?: string }> } }; }
export type AgentStatus = "ACTIVE" | "INACTIVE";
export interface AgentListItem { id: string; name: string; slug: string; role: string; description: string | null; model: string | null; status: AgentStatus; autonomy_level: number; }
export interface AgentTool { id: string; tool_name: string; is_enabled: boolean; requires_approval: boolean; settings: Record<string, unknown>; }
export interface Agent extends AgentListItem { system_prompt: string; settings: Record<string, unknown>; tools: AgentTool[]; created_at: string; updated_at: string; }
export interface AgentUpdate { description?: string | null; system_prompt?: string; model?: string | null; status?: AgentStatus; autonomy_level?: number; }
export type CampaignStatus = "DRAFT" | "PLANNING" | "WAITING_APPROVAL" | "ACTIVE" | "PAUSED" | "COMPLETED" | "ARCHIVED";
export interface CampaignInput { name: string; description?: string | null; goal: string; product?: string | null; target_audience?: string | null; offer?: string | null; desired_result?: string | null; start_date?: string | null; end_date?: string | null; }
export interface CampaignListItem { id: string; name: string; goal: string; product: string | null; status: CampaignStatus; start_date: string | null; end_date: string | null; created_at: string; updated_at: string; }
export interface PlannedTask { key: string; task_type: TaskType; title: string; description: string; agent_slug: string; priority: TaskPriority; brief: string; depends_on: string[]; }
export interface CampaignPlan { campaign_summary: string; positioning: string; target_audience: string; main_message: string; content_strategy: string; content_topics: string[]; recommended_article: { title: string; objective: string; angle: string; cta: string }; social_strategy: { channels: string[]; post_count: number; approach: string }; tasks: PlannedTask[]; }
export interface Campaign extends CampaignListItem { description: string | null; target_audience: string | null; offer: string | null; desired_result: string | null; strategy: CampaignPlan | null; strategy_version: number; created_by: string; creator: { id: string; full_name: string | null; email: string }; }
export type CampaignChangeKind = "ADMINISTRATIVE" | "STRATEGIC";
export interface CampaignFieldChange { field: string; label: string; old_value: string | null; new_value: string | null; kind: CampaignChangeKind; }
export interface CampaignChangeImpact { strategy_version: number; strategy_status: string; publication_plan_id: string | null; publication_plan_status: PublicationPlanStatus | null; publication_plan_item_count: number; future_plan_item_count: number; approved_social_post_count: number; scheduled_publication_count: number; published_publication_count: number; scheduled_publications: Array<{ id: string; channel: string; scheduled_at: string | null }>; }
export interface CampaignChangePreview { kind: CampaignChangeKind; changes: CampaignFieldChange[]; impact: CampaignChangeImpact; requires_confirmation: boolean; warning: string | null; guarantees: string[]; }
export interface CampaignChangeState { has_pending_strategic_changes: boolean; changed_fields: string[]; changed_at: string | null; changed_by_user_id: string | null; comment: string | null; baseline_strategy_version: number | null; active_strategy_version: number; plan_requires_review: boolean; affected_article_count: number; affected_social_post_count: number; }
export interface WorkspaceContentReference { id: string; title: string; status: ContentStatus; content_type: ContentType; current_version_id: string | null; current_version_number: number | null; approved_version_id: string | null; approved_version_number: number | null; source_task_id: string; source_task_title: string | null; source_task_status: TaskStatus | null; parent_content_item_id: string | null; publication_plan_item_id: string | null; channel: "TELEGRAM" | "VK" | null; }
export type TaskStatus = "NEW" | "BLOCKED" | "READY" | "IN_PROGRESS" | "WAITING_REVIEW" | "WAITING_APPROVAL" | "APPROVED" | "COMPLETED" | "FAILED" | "CANCELLED";
export type TaskPriority = "LOW" | "NORMAL" | "HIGH" | "URGENT";
export type TaskType = "CAMPAIGN_PLANNING" | "KNOWLEDGE_RESEARCH" | "WRITE_ARTICLE" | "CREATE_SOCIAL_POSTS" | "CONTENT_REVISION" | "MANUAL";
export interface TaskReference { id: string; title: string; status: TaskStatus; }
export type TaskClassification = "actionable" | "superseded" | "historical";
export interface TaskListItem { id: string; campaign_id: string; campaign: { id: string; name: string }; task_type: TaskType; title: string; assigned_agent: { id: string; name: string; slug: string } | null; priority: TaskPriority; status: TaskStatus; deadline: string | null; created_at: string; updated_at: string; classification: TaskClassification; classification_label: string | null; }
export interface Task extends TaskListItem { parent_task_id: string | null; parent_task: TaskReference | null; description: string | null; assigned_agent_id: string | null; input_data: Record<string, unknown>; output_data: Record<string, unknown>; requires_approval: boolean; error_message: string | null; retry_count: number; started_at: string | null; completed_at: string | null; dependencies: TaskReference[]; dependents: TaskReference[]; }
export interface TaskInput { campaign_id: string; parent_task_id?: string | null; task_type: TaskType; title: string; description?: string | null; assigned_agent_id?: string | null; priority: TaskPriority; input_data?: Record<string, unknown>; requires_approval?: boolean; deadline?: string | null; dependency_ids?: string[]; }
export type AgentRunStatus = "QUEUED" | "RUNNING" | "WAITING_APPROVAL" | "COMPLETED" | "FAILED" | "CANCELLED";
export interface AgentRun { id: string; task_id: string; agent_id: string; campaign_id: string; status: AgentRunStatus; model: string; created_at: string; agent?: { id: string; name: string; slug: string }; output_data?: { text?: string } | null; tool_calls?: Array<{ tool_name?: string; name?: string }>; request_count?: number | null; input_tokens?: number | null; output_tokens?: number | null; total_tokens?: number | null; trace_id?: string | null; error_code?: string | null; error_message?: string | null; started_at?: string | null; completed_at?: string | null; }
export type ApprovalStatus = "PENDING" | "APPROVED" | "REJECTED" | "REVISION_REQUESTED";
export interface Approval { id: string; object_type: "CAMPAIGN_STRATEGY" | "CONTENT_ITEM"; object_id: string; subject_version: number; status: ApprovalStatus; reviewed_by_user_id: string | null; comment: string | null; subject_snapshot: CampaignPlan; metadata: Record<string, unknown>; created_at: string; resolved_at: string | null; updated_at: string; }
export type KnowledgeItemStatus = "UPLOADING" | "INDEXING" | "READY" | "FAILED" | "ARCHIVED";
export type KnowledgeSourceStatus = "ACTIVE" | "ERROR" | "INACTIVE";
export type KnowledgeSourceType = "FILE_UPLOAD" | "WEBSITE" | "GOOGLE_DRIVE" | "YOUTUBE";
export interface KnowledgeStore { id: string; provider: "OPENAI"; name: string; external_store_id: string; status: "ACTIVE" | "ERROR" | "INACTIVE"; is_active: boolean; created_at: string; }
export interface KnowledgeSource { id: string; name: string; source_type: KnowledgeSourceType; source_url: string | null; status: KnowledgeSourceStatus; }
export interface KnowledgeItem { id: string; source_id: string; title: string; author: string | null; content_type: string; original_filename: string | null; mime_type: string | null; file_size_bytes: number | null; source_url: string | null; openai_file_id: string | null; vector_store_file_id: string | null; status: KnowledgeItemStatus; metadata: Record<string, unknown>; error_code: string | null; error_message: string | null; created_by: string; created_at: string; updated_at: string; indexed_at: string | null; archived_at: string | null; }
export interface KnowledgeSearchResult { result_key: string; knowledge_item_id: string; source_id: string; source_title: string; filename: string; file_id: string; excerpt: string; score: number | null; metadata: Record<string, unknown>; }
export type KnowledgePackStatus = "READY" | "INSUFFICIENT";
export interface KnowledgePackItem { knowledge_item_id: string; source_title: string; filename: string | null; file_id: string; excerpt: string; relevance_score: number | null; selection_reason: string | null; position: number; result_key: string; }
export interface KnowledgePack { id: string; campaign_id: string; task_id: string; agent_run_id: string; created_by_agent_id: string; strategy_version: number | null; status: KnowledgePackStatus; research_query: string; summary: string; gaps: string[]; metadata: Record<string, unknown>; created_at: string; items: KnowledgePackItem[]; }
export interface KnowledgeSearchResponse { query: string; result_count: number; results: KnowledgeSearchResult[]; }
export type ContentType = "ARTICLE" | "SOCIAL_POST" | "SOCIAL_POST_PACK";
export type ContentStatus = "DRAFT" | "WAITING_REVIEW" | "WAITING_APPROVAL" | "APPROVED" | "REJECTED" | "ARCHIVED";
export interface ContentSource { knowledge_pack_item_id: string; source_title: string; filename: string | null; excerpt: string; relevance_score: number | null; section_key: string; }
export interface ContentVersionSummary { id: string; version_number: number; change_description: string | null; created_by_user_id?: string | null; created_by_agent_id?: string | null; source_agent_run_id?: string | null; created_at: string; }
export interface ContentDerivation { source_content_item_id: string; source_content_item_title: string; source_content_version_id: string; source_version_number: number; section_key: string; }
export interface ContentVersion extends ContentVersionSummary { content: string; structured_content: Record<string, unknown>; sources: ContentSource[]; derivations?: ContentDerivation[]; }
export interface ContentApprovalHistory { id: string; subject_version: number; status: string; comment: string | null; reviewed_by_user_id: string | null; created_at: string; resolved_at: string | null; }
export interface ContentListItem { current_version_id?: string | null; publication_plan_item_id?: string | null; plan_channel?: "TELEGRAM" | "VK" | null; plan_scheduled_at?: string | null; approved_version_id?: string | null; approved_version_number?: number | null; source_content_item_id?: string | null; source_content_item_title?: string | null; id: string; campaign_id: string; content_type: ContentType; title: string; status: ContentStatus; current_version_number: number | null; created_at: string; updated_at: string; parent_content_item_id?: string | null; channel?: "TELEGRAM" | "VK" | null; }
export interface Content extends ContentListItem { source_task_id: string; author_agent_id: string; parent_content_item_id?: string | null; channel?: "TELEGRAM" | "VK" | null; approved_version_id?: string | null; current_version: ContentVersion | null; versions: ContentVersionSummary[]; approval_history?: ContentApprovalHistory[]; }
export interface ContentRevisionProgress { executor_available?: boolean | null; task_id: string; task_status: TaskStatus; agent_run_id: string | null; agent_run_status: AgentRunStatus | null; error_code: string | null; error_message: string | null; base_content_version_id: string; created_content_version_id: string | null; created_at: string; updated_at: string; }
export type PublicationStatus = "DRAFT" | "WAITING_APPROVAL" | "APPROVED" | "SCHEDULED" | "PUBLISHING" | "PUBLISHED" | "FAILED" | "CANCELLED";
export interface PublicationProvenance { content_version_id: string; source_content_version_id: string; section_key: string; }
export interface PublicationReconciliation { id: string; publication_id: string; operator_user_id: string; channel: "TELEGRAM" | "VK"; decision: "CONFIRMED_PUBLISHED" | "CONFIRMED_NOT_PUBLISHED"; external_id: string | null; external_url: string | null; external_published_at: string | null; note: string | null; created_at: string; }
export interface Publication { is_overdue?: boolean; lateness_seconds?: number; publication_plan_item_id?: string | null; id: string; campaign_id: string; content_item_id: string; content_version_id: string; channel: "TELEGRAM" | "VK"; provider_enabled?: boolean; status: PublicationStatus; scheduled_at: string | null; approved_for_publish_at: string | null; approved_for_publish_by: string | null; external_id: string | null; external_url: string | null; published_at: string | null; failure_code: string | null; failure_message: string | null; retry_count: number; retry_allowed?: boolean; reconciliation_required?: boolean; reconciliation_history?: PublicationReconciliation[]; created_at: string; updated_at: string; provenance: PublicationProvenance[]; }
export interface PublicationCalendarItem { is_overdue?: boolean; lateness_seconds?: number; publication_id: string; content_item_id: string; content_version_id: string; title: string; channel: "TELEGRAM" | "VK"; status: PublicationStatus; scheduled_at: string | null; published_at: string | null; external_url: string | null; provider_enabled: boolean; failure_code: string | null; }
export interface PublicationMetricsSnapshot { id: string; publication_id: string; channel: "TELEGRAM" | "VK"; observed_at: string; views: number | null; impressions: number | null; reactions: number | null; likes: number | null; comments: number | null; shares: number | null; clicks: number | null; subscribers: number | null; source: "PROVIDER" | "MANUAL"; provider: string | null; created_at: string; }
export interface PublicationMetrics { publication_id: string; sync_capable: boolean; latest: PublicationMetricsSnapshot | null; history: PublicationMetricsSnapshot[]; }
export interface CampaignPerformance { total_published: number; with_metrics: number; metric_coverage: Record<string, number>; totals: Record<string, number>; publications: Array<{ publication_id: string; content_item_id: string; content_version_id: string; channel: "TELEGRAM" | "VK"; published_at: string | null; metrics: PublicationMetricsSnapshot | null }>; }
export interface Activity { id: string; event_type: string; campaign_id: string | null; task_id: string | null; content_item_id: string | null; approval_id: string | null; metadata: Record<string, unknown>; created_at: string; }
export interface MarketingFeedback { id: string; campaign_id: string; publication_id: string | null; content_item_id: string | null; content_version_id: string | null; source_type: string; category: string; rating: number | null; comment: string | null; observed_at: string | null; created_by_user_id: string | null; created_at: string; }
export interface FeedbackAnalysis { id: string; campaign_id: string; status: "DRAFT" | "ACCEPTED" | "REJECTED" | "FAILED"; strategy_version: number; summary: string; input_snapshot: Record<string, unknown>; findings: Array<Record<string, unknown>>; recommendations: Array<Record<string, unknown>>; experiment_ideas: Array<Record<string, unknown>>; limitations: string[]; agent_run_id: string | null; generated_at: string | null; reviewed_by_user_id: string | null; reviewed_at: string | null; }
export type PublicationPlanStatus = "DRAFT" | "WAITING_APPROVAL" | "APPROVED" | "REJECTED" | "ARCHIVED";
export interface PublicationCollisionWarning { type: "NEAR_EXISTING_PUBLICATION"; publication_id: string; channel: "TELEGRAM" | "VK"; scheduled_at: string; delta_minutes: number; }
export interface PublicationPlanItem { id: string; position: number; scheduled_at: string; channel: "TELEGRAM" | "VK"; source_content_item_id: string; source_content_version_id: string; topic: string; angle: string; purpose: string; format: string; message_brief: string; source_claim_ids: string[] | null; source_support_summary: string | null; status: "PLANNED" | "REMOVED"; near_publication_warnings: PublicationCollisionWarning[]; }
export interface PublicationPlan { id: string; campaign_id: string; status: PublicationPlanStatus; planning_horizon_start: string; planning_horizon_end: string; timezone_policy: string; created_by_user_id: string; generated_by_agent_run_id: string | null; feedback_analysis_id: string | null; approved_at: string | null; approved_by_user_id: string | null; items: PublicationPlanItem[]; }
export interface WorkspacePublicationReference { id: string; content_item_id: string; content_version_id: string; channel: "TELEGRAM" | "VK"; status: PublicationStatus; scheduled_at: string | null; published_at: string | null; publication_plan_item_id: string | null; }
export interface WorkspacePipelineStage { label: string; status: string; href: string | null; action_label: string | null; }
export interface WorkspacePostAction { allowed: boolean; error_code: string | null; reason: string | null; next_action: string | null; existing_task_id: string | null; }
export interface WorkspacePlanItem { id: string; position: number; scheduled_at: string; channel: "TELEGRAM" | "VK"; topic: string; purpose: string; format: string; message_brief: string; source_content_item_id: string; source_content_item_title: string | null; source_content_version_id: string | null; source_version_number: number | null; source_claim_ids: string[] | null; source_support_summary: string | null; status: string; social_posts: WorkspaceContentReference[]; publications: WorkspacePublicationReference[]; pipeline: WorkspacePipelineStage[]; post_action: WorkspacePostAction; }
export interface WorkspacePlan { id: string; status: PublicationPlanStatus; planning_horizon_start: string; planning_horizon_end: string; timezone_policy: string; generated_by_agent_run_id: string | null; item_count: number; items: WorkspacePlanItem[]; }
export interface WorkspaceArticle extends WorkspaceContentReference { campaign_role: string; plan_item_count: number; social_post_count: number; scheduled_publication_count: number; published_count: number; }
export interface WorkspaceKnowledgePack { id: string; status: "READY" | "INSUFFICIENT"; task_id: string; task_title: string | null; agent_run_id: string; created_at: string; summary: string; gaps: string[]; source_count: number; }
export interface WorkspaceKnowledgeState { store_ready: boolean; source_count: number; item_count: number; ready_item_count: number; processing_item_count: number; failed_item_count: number; campaign_packs: WorkspaceKnowledgePack[]; has_current_strategy_pack: boolean; }
export interface WorkspaceTask { id: string; title: string; display_title: string; task_type: TaskType; status: TaskStatus; priority: TaskPriority; created_at: string; updated_at: string; plan_item_id: string | null; error_code: string | null; error_summary: string | null; next_action: string | null; retry_allowed: boolean; }
export interface WorkspaceNextStep { title: string; description: string; href: string; entity_type: string | null; entity_id: string | null; priority?: string; }
export interface WorkspaceFeedbackState { new_feedback_count: number; new_metrics_count: number; }
export interface CampaignDirectorBrief { strategy_status: ApprovalStatus | CampaignStatus; article_count: number; approved_article_count: number; plan_status: PublicationPlanStatus | null; plan_item_count: number; plan_items_with_posts: number; plan_items_without_posts: number; social_post_count: number; posts_waiting_approval: number; scheduled_publication_count: number; published_count: number; failed_task_count: number; blocked_task_count: number; next_step: WorkspaceNextStep; }
export interface CampaignWorkspace { campaign: Campaign; director: CampaignDirectorBrief; knowledge: WorkspaceKnowledgeState; articles: WorkspaceArticle[]; social_posts: WorkspaceContentReference[]; publication_plan: WorkspacePlan | null; other_plans: WorkspacePlan[]; publications: WorkspacePublicationReference[]; attention_tasks: WorkspaceTask[]; feedback: WorkspaceFeedbackState; change_state?: CampaignChangeState; }
export interface SystemStatus { due_publications?: number; overdue_publications?: number; scheduled_publications?: number; publishing_providers_enabled?: { telegram: boolean; vk: boolean }; ready_auto_ai_tasks?: number; stalled_ready_auto_ai_tasks?: number; running_agent_runs?: number; publications?: { failed: number; reconciliation_required: number; provider_disabled: number }; tasks: Record<string, number>; agent_runs: Record<string, number>; stuck_tasks: number; pending_approvals: number; last_activity_at: string | null; }
export class ApiError extends Error { constructor(message: string, public status: number, public code?: string) { super(message); } }
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (typeof init?.body === "string" && !headers.has("Content-Type")) {
    try { JSON.parse(init.body); headers.set("Content-Type", "application/json"); } catch { /* Non-JSON bodies keep their caller headers. */ }
  }
  const response = await fetch(`${API_URL}${path}`, {
    ...init, credentials: "include", headers,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
    const fallback = response.status === 403 ? "Недостаточно прав для выполнения действия." : "Ошибка запроса.";
    const validationMessage = body.error?.details?.errors?.[0]?.message;
    throw new ApiError(validationMessage ?? body.error?.message ?? fallback, response.status, body.error?.code);
  }
  return response.json() as Promise<T>;
}

async function uploadRequest<T>(path: string, body: FormData): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, { method: "POST", body, credentials: "include" });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ApiErrorBody;
    throw new ApiError(payload.error?.message ?? "Ошибка загрузки.", response.status, payload.error?.code);
  }
  return response.json() as Promise<T>;
}

export const authApi = {
  capabilities: () => request<{ self_registration_enabled: boolean }>("/auth/capabilities"),
  register: (email: string, password: string, full_name: string) => request<User>("/auth/register", { method: "POST", body: JSON.stringify({ email, password, full_name }) }),
  changePassword: (current_password: string, new_password: string) => request<{ message: string }>("/auth/change-password", { method: "POST", body: JSON.stringify({ current_password, new_password }) }),
  login: (email: string, password: string) => request<{ user: User }>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  me: () => request<User>("/auth/me"),
  logout: () => request<{ message: string }>("/auth/logout", { method: "POST" }),
};

export const agentsApi = {
  list: () => request<AgentListItem[]>("/agents"),
  get: (id: string) => request<Agent>(`/agents/${id}`),
  update: (id: string, payload: AgentUpdate) => request<Agent>(`/agents/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  updateTool: (agentId: string, toolId: string, payload: { is_enabled?: boolean; requires_approval?: boolean }) =>
    request<AgentTool>(`/agents/${agentId}/tools/${toolId}`, { method: "PATCH", body: JSON.stringify(payload) }),
};

export const campaignsApi = {
  list: (status?: CampaignStatus) => request<CampaignListItem[]>(`/campaigns${status ? `?status=${status}` : ""}`),
  get: (id: string) => request<Campaign>(`/campaigns/${id}`),
  workspace: (id: string) => request<CampaignWorkspace>(`/campaigns/${id}/workspace`),
  create: (payload: CampaignInput) => request<Campaign>("/campaigns", { method: "POST", body: JSON.stringify(payload) }),
  update: (id: string, payload: Partial<CampaignInput>) => request<Campaign>(`/campaigns/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  previewChange: (id: string, changes: Partial<CampaignInput>) => request<CampaignChangePreview>(`/campaigns/${id}/changes/preview`, { method: "POST", body: JSON.stringify({ changes }) }),
  applyChange: (id: string, payload: { changes: Partial<CampaignInput>; comment?: string | null; confirmed_impact: boolean; expected_updated_at: string }) => request<{ campaign: Campaign; preview: CampaignChangePreview }>(`/campaigns/${id}/changes`, { method: "POST", body: JSON.stringify(payload) }),
  archive: (id: string) => request<Campaign>(`/campaigns/${id}/archive`, { method: "POST" }),
  generateStrategy: (id: string) => request<{ campaign_id: string; planning_task_id: string; agent_run_id: string; status: CampaignStatus }>(`/campaigns/${id}/generate-strategy`, { method: "POST" }),
  approveStrategy: (id: string, comment?: string) => request<{ campaign: Campaign; approval_id: string; generated_task_ids: string[] }>(`/campaigns/${id}/approve-strategy`, { method: "POST", body: JSON.stringify({ comment: comment || null }) }),
  requestStrategyRevision: (id: string, comment: string) => request(`/campaigns/${id}/request-strategy-revision`, { method: "POST", body: JSON.stringify({ comment }) }),
  prepareStrategyRevision: (id: string) => request<{ campaign_id: string; planning_task_id: string; agent_run_id: string; status: CampaignStatus }>(`/campaigns/${id}/prepare-strategy-revision`, { method: "POST" }),
  rejectStrategy: (id: string, comment: string) => request<Campaign>(`/campaigns/${id}/reject-strategy`, { method: "POST", body: JSON.stringify({ comment }) }),
};

export const approvalsApi = {
  list: (filters: Record<string, string | undefined> = {}) => { const query = new URLSearchParams(Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1]))); return request<Approval[]>(`/approvals${query.size ? `?${query}` : ""}`); },
  get: (id: string) => request<Approval>(`/approvals/${id}`),
};

export const tasksApi = {
  list: (filters: Record<string, string | undefined> = {}) => { const query = new URLSearchParams(Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1]))); return request<TaskListItem[]>(`/tasks${query.size ? `?${query}` : ""}`); },
  get: (id: string) => request<Task>(`/tasks/${id}`),
  create: (payload: TaskInput) => request<Task>("/tasks", { method: "POST", body: JSON.stringify(payload) }),
  update: (id: string, payload: Partial<Omit<TaskInput, "campaign_id" | "task_type" | "dependency_ids">>) => request<Task>(`/tasks/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  start: (id: string) => request<Task>(`/tasks/${id}/start`, { method: "POST" }),
  complete: (id: string, output_data: Record<string, unknown> = {}) => request<Task>(`/tasks/${id}/complete`, { method: "POST", body: JSON.stringify({ output_data }) }),
  cancel: (id: string) => request<Task>(`/tasks/${id}/cancel`, { method: "POST" }),
  run: (id: string, feedback_analysis_id?: string) => request<AgentRun>(`/tasks/${id}/run`, feedback_analysis_id ? { method: "POST", body: JSON.stringify({ feedback_analysis_id }) } : { method: "POST" }),
  retry: (id: string) => request<AgentRun>(`/tasks/${id}/retry`, { method: "POST" }),
  addDependency: (id: string, depends_on_task_id: string) => request<Task>(`/tasks/${id}/dependencies`, { method: "POST", body: JSON.stringify({ depends_on_task_id }) }),
  removeDependency: (id: string, dependencyId: string) => request<Task>(`/tasks/${id}/dependencies/${dependencyId}`, { method: "DELETE" }),
};

export const agentRunsApi = {
  list: (filters: Record<string, string | undefined> = {}) => { const query = new URLSearchParams(Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1]))); return request<AgentRun[]>(`/agent-runs${query.size ? `?${query}` : ""}`); },
  get: (id: string) => request<AgentRun>(`/agent-runs/${id}`),
};

export const knowledgeApi = {
  getStore: () => request<KnowledgeStore | null>("/knowledge/store"),
  initializeStore: () => request<KnowledgeStore>("/knowledge/store/initialize", { method: "POST" }),
  listSources: () => request<KnowledgeSource[]>("/knowledge/sources"),
  listItems: () => request<KnowledgeItem[]>("/knowledge/items"),
  upload: (file: File, title: string, author: string) => { const body = new FormData(); body.append("file", file); if (title) body.append("title", title); if (author) body.append("author", author); return uploadRequest<KnowledgeItem>("/knowledge/upload", body); },
  retry: (id: string) => request<KnowledgeItem>(`/knowledge/items/${id}/retry`, { method: "POST" }),
  archive: (id: string) => request<KnowledgeItem>(`/knowledge/items/${id}/archive`, { method: "POST" }),
  search: (query: string, max_results = 10) => request<KnowledgeSearchResponse>("/knowledge/search", { method: "POST", body: JSON.stringify({ query, max_results }) }),
};

export const knowledgePacksApi = {
  list: (filters: Record<string, string | undefined> = {}) => { const query = new URLSearchParams(Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1]))); return request<KnowledgePack[]>(`/knowledge-packs${query.size ? `?${query}` : ""}`); },
  get: (id: string) => request<KnowledgePack>(`/knowledge-packs/${id}`),
};
export const contentApi = {
  list: (filters: Record<string, string | undefined> = {}) => { const query = new URLSearchParams(Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1]))); return request<ContentListItem[]>(`/content${query.size ? `?${query}` : ""}`); },
  get: (id: string) => request<Content>(`/content/${id}`),
  versions: (id: string) => request<ContentVersionSummary[]>(`/content/${id}/versions`),
  approve: (id: string, comment?: string) => request<Content>(`/content/${id}/approve`, { method: "POST", body: JSON.stringify({ comment: comment || null }) }),
  reject: (id: string, comment: string) => request<Content>(`/content/${id}/reject`, { method: "POST", body: JSON.stringify({ comment }) }),
  requestRevision: (id: string, comment: string) => request<Content>(`/content/${id}/request-revision`, { method: "POST", body: JSON.stringify({ comment }) }),
  revisionProgress: (id: string) => request<ContentRevisionProgress | null>(`/content/${id}/revision-status`),
  manualEdit: (id: string, payload: { content: string; expected_current_version_id: string; change_description?: string | null }) => request<Content>(`/content/${id}/manual-edit`, { method: "POST", body: JSON.stringify(payload) }),
};
export const publicationsApi = {
  scheduleContent: (contentId: string) => request<Publication>(`/publications/plan-content/${contentId}/schedule`, { method: "POST" }),
  listCampaign: (campaignId: string) => request<Publication[]>(`/publications/campaign/${campaignId}`),
  calendar: (campaignId: string, from: string, to: string) => request<PublicationCalendarItem[]>(`/publications/campaign/${campaignId}/calendar?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`),
  create: (payload: { content_item_id: string; content_version_id: string; channel: "TELEGRAM" | "VK" }) => request<Publication>("/publications", { method: "POST", body: JSON.stringify(payload) }),
  approve: (id: string) => request<Publication>(`/publications/${id}/approve`, { method: "POST" }),
  schedule: (id: string, scheduled_at: string) => request<Publication>(`/publications/${id}/schedule`, { method: "POST", body: JSON.stringify({ scheduled_at }) }),
  cancel: (id: string) => request<Publication>(`/publications/${id}/cancel`, { method: "POST" }),
  replaceScheduledVersion: (id: string) => request<Publication>(`/publications/${id}/replace-scheduled-version`, { method: "POST" }),
  publishNow: (id: string) => request<Publication>(`/publications/${id}/publish-now`, { method: "POST" }),
  retry: (id: string) => request<Publication>(`/publications/${id}/retry`, { method: "POST" }),
  reconcilePublished: (id: string, payload: { external_id: string; external_url?: string; published_at?: string; note?: string }) => request<Publication>(`/publications/${id}/reconcile/published`, { method: "POST", body: JSON.stringify(payload) }),
  reconcileNotPublished: (id: string, note?: string) => request<Publication>(`/publications/${id}/reconcile/not-published`, { method: "POST", body: JSON.stringify({ note: note || null }) }),
  recoverStuck: (id: string) => request<Publication>(`/publications/${id}/recover-stuck`, { method: "POST" }),
  metrics: (id: string) => request<PublicationMetrics>(`/publications/${id}/metrics`),
  addManualMetrics: (id: string, payload: Record<string, unknown>) => request<PublicationMetricsSnapshot>(`/publications/${id}/metrics`, { method: "POST", body: JSON.stringify(payload) }),
  syncMetrics: (id: string) => request<PublicationMetrics>(`/publications/${id}/metrics/sync`, { method: "POST" }),
};
export const metricsApi = {
  campaign: (campaignId: string, from: string, to: string, channel?: string) => request<CampaignPerformance>(`/campaigns/${campaignId}/performance?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}${channel ? `&channel=${channel}` : ""}`),
};
export const feedbackApi = {
  list: (campaignId: string) => request<MarketingFeedback[]>(`/campaigns/${campaignId}/feedback`),
  create: (campaignId: string, payload: Record<string, unknown>) => request<MarketingFeedback>(`/campaigns/${campaignId}/feedback`, { method: "POST", body: JSON.stringify(payload) }),
  analyses: (campaignId: string) => request<FeedbackAnalysis[]>(`/campaigns/${campaignId}/feedback-analysis`),
  generate: (campaignId: string) => request<FeedbackAnalysis>(`/campaigns/${campaignId}/feedback-analysis`, { method: "POST" }),
  accept: (id: string) => request<FeedbackAnalysis>(`/feedback-analysis/${id}/accept`, { method: "POST" }),
  reject: (id: string) => request<FeedbackAnalysis>(`/feedback-analysis/${id}/reject`, { method: "POST" }),
};
export const publicationPlansApi = {
  list: (campaignId: string) => request<PublicationPlan[]>(`/campaigns/${campaignId}/publication-plans`),
  create: (campaignId: string, payload: Record<string, unknown>) => request<PublicationPlan>(`/campaigns/${campaignId}/publication-plans`, { method: "POST", body: JSON.stringify(payload) }),
  generate: (campaignId: string, payload: Record<string, unknown>) => request<PublicationPlan>(`/campaigns/${campaignId}/publication-plans/generate`, { method: "POST", body: JSON.stringify(payload) }),
  submit: (id: string) => request<PublicationPlan>(`/publication-plans/${id}/submit`, { method: "POST" }),
  approve: (id: string) => request<PublicationPlan>(`/publication-plans/${id}/approve`, { method: "POST" }),
  reject: (id: string) => request<PublicationPlan>(`/publication-plans/${id}/reject`, { method: "POST" }),
  revise: (id: string) => request<PublicationPlan>(`/publication-plans/${id}/revise`, { method: "POST" }),
  addItem: (id: string, payload: Record<string, unknown>) => request<PublicationPlan>(`/publication-plans/${id}/items`, { method: "POST", body: JSON.stringify(payload) }),
  updateItem: (id: string, itemId: string, payload: Record<string, unknown>) => request<PublicationPlan>(`/publication-plans/${id}/items/${itemId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  removeItem: (id: string, itemId: string) => request<PublicationPlan>(`/publication-plans/${id}/items/${itemId}`, { method: "DELETE" }),
  reorder: (id: string, itemIds: string[]) => request<PublicationPlan>(`/publication-plans/${id}/reorder`, { method: "POST", body: JSON.stringify({ item_ids: itemIds }) }),
};
export const activitiesApi = { list: (campaignId?: string) => request<Activity[]>(`/activities${campaignId ? `?campaign_id=${campaignId}` : ""}`) };
export const systemApi = { status: () => request<SystemStatus>("/system/status") };
