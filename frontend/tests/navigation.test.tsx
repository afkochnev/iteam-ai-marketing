import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppNavigation } from "../components/app-navigation";
import { PageBreadcrumbs } from "../components/page-breadcrumbs";

vi.mock("next/navigation", () => ({ usePathname: () => "/campaigns/campaign-1" }));

describe("global navigation and page context", () => {
  it("keeps the main work areas one click away and marks the active destination", () => {
    render(<AppNavigation><main>Страница</main></AppNavigation>);
    const nav = screen.getByRole("navigation", { name: "Основная навигация" });
    expect(nav).toHaveTextContent("Кампании");
    expect(nav).toHaveTextContent("Контент");
    expect(nav).toHaveTextContent("Задачи");
    expect(nav).toHaveTextContent("Согласования");
    expect(nav).toHaveTextContent("Публикации");
    expect(screen.getByRole("link", { name: "Кампании" })).toHaveAttribute("aria-current", "page");
  });

  it("renders linked breadcrumbs and the current page", () => {
    render(<PageBreadcrumbs items={[{ label: "Кампании", href: "/campaigns" }, { label: "Запуск", href: "/campaigns/1" }, { label: "Задача" }]} />);
    expect(screen.getByRole("navigation", { name: "Навигационная цепочка" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Запуск" })).toHaveAttribute("href", "/campaigns/1");
    expect(screen.getByText("Задача")).toHaveAttribute("aria-current", "page");
  });
});
