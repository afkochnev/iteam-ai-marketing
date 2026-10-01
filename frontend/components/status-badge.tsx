export function StatusBadge({ label, status, tone }: { label: string; status?: string; tone?: "success" | "danger" | "neutral" }) {
  const className = tone === "success" ? "approved" : tone === "danger" ? "failed" : tone === "neutral" ? "neutral" : status?.toLowerCase() ?? "neutral";
  return <span className={`status-badge status-${className}`} aria-label={label}>{label}</span>;
}
