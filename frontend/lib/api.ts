export type UserRole = "ADMIN" | "MANAGER";
export interface User { id: string; email: string; full_name: string | null; role: UserRole; is_active: boolean; created_at: string; }
interface ApiErrorBody { error?: { message?: string; details?: { errors?: Array<{ message?: string }> } }; }
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
export type TaskStatus = "NEW" | "BLOCKED" | "READY" | "IN_PROGRESS" | "WAITING_REVIEW" | "WAITING_APPROVAL" | "APPROVED" | "COMPLETED" | "FAILED" | "CANCELLED";
export type TaskPriority = "LOW" | "NORMAL" | "HIGH" | "URGENT";
export type TaskType = "CAMPAIGN_PLANNING" | "KNOWLEDGE_RESEARCH" | "WRITE_ARTICLE" | "CREATE_SOCIAL_POSTS" | "CONTENT_REVISION" | "MANUAL";
export interface TaskReference { id: string; title: string; status: TaskStatus; }
export interface TaskListItem { id: string; campaign_id: string; campaign: { id: string; name: string }; task_type: TaskType; title: string; assigned_agent: { id: string; name: string; slug: string } | null; priority: TaskPriority; status: TaskStatus; deadline: string | null; created_at: string; updated_at: string; }
export interface Task extends TaskListItem { parent_task_id: string | null; parent_task: TaskReference | null; description: string | null; assigned_agent_id: string | null; input_data: Record<string, unknown>; output_data: Record<string, unknown>; requires_approval: boolean; error_message: string | null; retry_count: number; started_at: string | null; completed_at: string | null; dependencies: TaskReference[]; dependents: TaskReference[]; }
export interface TaskInput { campaign_id: string; parent_task_id?: string | null; task_type: TaskType; title: string; description?: string | null; assigned_agent_id?: string | null; priority: TaskPriority; input_data?: Record<string, unknown>; requires_approval?: boolean; deadline?: string | null; dependency_ids?: string[]; }
export type AgentRunStatus = "QUEUED" | "RUNNING" | "WAITING_APPROVAL" | "COMPLETED" | "FAILED" | "CANCELLED";
export interface AgentRun { id: string; task_id: string; agent_id: string; campaign_id: string; status: AgentRunStatus; model: string; created_at: string; agent?: { id: string; name: string; slug: string }; output_data?: { text?: string } | null; request_count?: number | null; input_tokens?: number | null; output_tokens?: number | null; total_tokens?: number | null; trace_id?: string | null; error_code?: string | null; error_message?: string | null; started_at?: string | null; completed_at?: string | null; }
export type ApprovalStatus = "PENDING" | "APPROVED" | "REJECTED" | "REVISION_REQUESTED";
export interface Approval { id: string; object_type: "CAMPAIGN_STRATEGY" | "CONTENT_ITEM"; object_id: string; subject_version: number; status: ApprovalStatus; reviewed_by_user_id: string | null; comment: string | null; subject_snapshot: CampaignPlan; metadata: Record<string, unknown>; created_at: string; resolved_at: string | null; updated_at: string; }
export class ApiError extends Error { constructor(message: string, public status: number) { super(message); } }
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init, credentials: "include", headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
    const fallback = response.status === 403 ? "Недостаточно прав для выполнения действия." : "Ошибка запроса.";
    const validationMessage = body.error?.details?.errors?.[0]?.message;
    throw new ApiError(validationMessage ?? body.error?.message ?? fallback, response.status);
  }
  return response.json() as Promise<T>;
}

export const authApi = {
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
  create: (payload: CampaignInput) => request<Campaign>("/campaigns", { method: "POST", body: JSON.stringify(payload) }),
  update: (id: string, payload: Partial<CampaignInput>) => request<Campaign>(`/campaigns/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  archive: (id: string) => request<Campaign>(`/campaigns/${id}/archive`, { method: "POST" }),
  generateStrategy: (id: string) => request<{ campaign_id: string; planning_task_id: string; agent_run_id: string; status: CampaignStatus }>(`/campaigns/${id}/generate-strategy`, { method: "POST" }),
  approveStrategy: (id: string, comment?: string) => request<{ campaign: Campaign; approval_id: string; generated_task_ids: string[] }>(`/campaigns/${id}/approve-strategy`, { method: "POST", body: JSON.stringify({ comment: comment || null }) }),
  requestStrategyRevision: (id: string, comment: string) => request(`/campaigns/${id}/request-strategy-revision`, { method: "POST", body: JSON.stringify({ comment }) }),
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
  run: (id: string) => request<AgentRun>(`/tasks/${id}/run`, { method: "POST" }),
  retry: (id: string) => request<AgentRun>(`/tasks/${id}/retry`, { method: "POST" }),
  addDependency: (id: string, depends_on_task_id: string) => request<Task>(`/tasks/${id}/dependencies`, { method: "POST", body: JSON.stringify({ depends_on_task_id }) }),
  removeDependency: (id: string, dependencyId: string) => request<Task>(`/tasks/${id}/dependencies/${dependencyId}`, { method: "DELETE" }),
};

export const agentRunsApi = {
  list: (filters: Record<string, string | undefined> = {}) => { const query = new URLSearchParams(Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1]))); return request<AgentRun[]>(`/agent-runs${query.size ? `?${query}` : ""}`); },
  get: (id: string) => request<AgentRun>(`/agent-runs/${id}`),
};
