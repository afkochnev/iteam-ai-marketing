export const AGENT_NAMES: Record<string, string> = {
  marketing_director: "AI-директор по маркетингу",
  knowledge_keeper: "Хранитель знаний",
  writer: "Писатель",
  smm_manager: "SMM-менеджер",
};

export function agentDisplayName(slug: string, fallback: string): string {
  return AGENT_NAMES[slug] ?? fallback;
}
