import { request } from "./api";
export interface Conversation { id: string; campaign_id: string; created_by_user_id: string; title: string | null; archived_at: string | null; created_at: string; updated_at: string; }
export interface ChatReference { entity_type: "campaign" | "task" | "content" | "publication_plan" | "publication" | "knowledge_pack"; entity_id: string; label: string; href: string; }
export interface ChatMessage { id: string; role: "USER" | "ASSISTANT" | "SYSTEM_EVENT"; status: "PENDING" | "COMPLETED" | "FAILED"; content: string; references: ChatReference[]; limitations: string[]; error_message: string | null; created_at: string; }
export const directorChatApi = {
  list: (id: string) => request<Conversation[]>(`/campaigns/${id}/marketing-conversations`),
  create: (id: string) => request<Conversation>(`/campaigns/${id}/marketing-conversations`, { method: "POST", body: "{}" }),
  messages: (id: string) => request<ChatMessage[]>(`/marketing-conversations/${id}/messages`),
  send: (id: string, content: string, client_message_id: string) => request<{ user_message: ChatMessage; assistant_message: ChatMessage }>(`/marketing-conversations/${id}/messages`, { method: "POST", body: JSON.stringify({ content, client_message_id }) }),
  retry: (id: string, message: string) => request<ChatMessage>(`/marketing-conversations/${id}/messages/${message}/retry`, { method: "POST" }),
  archive: (id: string) => request<Conversation>(`/marketing-conversations/${id}/archive`, { method: "POST" }),
};
// Accept only the exact local route built by the backend for a typed UUID reference.
export function verifiedHref(reference: ChatReference, campaignId: string): string | null {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(reference.entity_id)) return null;
  const routes: Record<ChatReference["entity_type"], string> = {
    campaign: `/campaigns/${reference.entity_id}`, task: `/tasks/${reference.entity_id}`,
    content: `/content/${reference.entity_id}`, publication_plan: `/campaigns/${campaignId}#publication-plan`,
    publication: "/publications", knowledge_pack: `/knowledge?campaign_id=${campaignId}#knowledge-packs-heading`,
  };
  return routes[reference.entity_type] === reference.href ? reference.href : null;
}
