"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/profile", label: "Профиль" },
  { href: "/", label: "Главная" },
  { href: "/campaigns", label: "Кампании" },
  { href: "/knowledge", label: "База знаний" },
  { href: "/content", label: "Контент" },
  { href: "/tasks", label: "Задачи" },
  { href: "/approvals", label: "Согласования" },
  { href: "/publications", label: "Публикации" },
];

export function AppNavigation({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  if ((pathname === "/login" || pathname === "/register")) return <>{children}</>;

  return (
    <div className="app-shell">
      <header className="app-topbar">
        <Link className="app-brand" href="/" aria-label="iTeam — главная">
          <span className="brand-mark">i</span>
          <span>iTeam <small>Marketing</small></span>
        </Link>
        <nav className="primary-nav" aria-label="Основная навигация">
          {links.map((item) => {
            const active = item.href === "/"
              ? pathname === "/"
              : pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined}>
                {item.label}
              </Link>
            );
          })}
        </nav>
      </header>
      {children}
    </div>
  );
}
