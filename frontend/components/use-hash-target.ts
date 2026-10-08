"use client";

import { useEffect, useRef } from "react";

/** Revisit fragment targets after asynchronous content has rendered. */
export function useHashTarget(content: unknown) {
  const previous = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const reveal = () => {
      let id: string;
      try { id = decodeURIComponent(window.location.hash.slice(1)); } catch { return; }
      if (!id) return;
      const target = document.getElementById(id);
      if (!target || previous.current === target) return;
      previous.current = target;
      for (let parent: HTMLElement | null = target; parent; parent = parent.parentElement) {
        if (parent instanceof HTMLDetailsElement) parent.open = true;
      }
      const topbar = document.querySelector(".app-topbar");
      const campaignNavigation = document.querySelector(".campaign-navigation");
      if (topbar) target.style.scrollMarginTop = `${topbar.getBoundingClientRect().height + (campaignNavigation?.getBoundingClientRect().height ?? 0) + 16}px`;
      target.scrollIntoView?.({ block: "start" });
      if (!target.hasAttribute("tabindex")) target.tabIndex = -1;
      target.focus({ preventScroll: true });
    };
    const timer = window.setTimeout(reveal, 0);
    const navigate = () => { previous.current = null; reveal(); };
    window.addEventListener("hashchange", navigate);
    return () => { window.clearTimeout(timer); window.removeEventListener("hashchange", navigate); };
  }, [content]);
}
