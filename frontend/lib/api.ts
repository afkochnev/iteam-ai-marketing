export type UserRole = "ADMIN" | "MANAGER";
export interface User { id: string; email: string; full_name: string | null; role: UserRole; is_active: boolean; created_at: string; }
interface ApiErrorBody { error?: { message?: string }; }
export type AgentStatus = "ACTIVE" | "INACTIVE";
export interface AgentListItem { id: string; name: string; slug: string; role: string; description: string | null; model: string | null; status: AgentStatus; autonomy_level: number; }
export interface AgentTool { id: string; tool_name: string; is_enabled: boolean; requires_approval: boolean; settings: Record<string, unknown>; }
export interface Agent extends AgentListItem { system_prompt: string; settings: Record<string, unknown>; tools: AgentTool[]; created_at: string; updated_at: string; }
export interface AgentUpdate { description?: string | null; system_prompt?: string; model?: string | null; status?: AgentStatus; autonomy_level?: number; }
export class ApiError extends Error { constructor(message: string, public status: number) { super(message); } }
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init, credentials: "include", headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
    const fallback = response.status === 403 ? "Недостаточно прав для выполнения действия." : "Ошибка запроса.";
    throw new ApiError(body.error?.message ?? fallback, response.status);
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
