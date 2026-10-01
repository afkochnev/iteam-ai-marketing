import Link from "next/link";

export interface BreadcrumbItem {
  label: string;
  href?: string;
}
export function PageBreadcrumbs({ items }: { items: BreadcrumbItem[] }) {
  return (
    <nav className="breadcrumbs" aria-label="Навигационная цепочка">
      <ol>
        {items.map((item, index) => (
          <li key={`${item.label}-${index}`}>
            {index > 0 && <span className="breadcrumb-separator" aria-hidden="true">/</span>}
            {item.href ? <Link href={item.href}>{item.label}</Link> : <span aria-current="page">{item.label}</span>}
          </li>
        ))}
      </ol>
    </nav>
  );
}
