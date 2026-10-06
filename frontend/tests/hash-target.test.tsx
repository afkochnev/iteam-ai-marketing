import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useHashTarget } from "../components/use-hash-target";
function Page({ loaded }: { loaded: boolean }) { useHashTarget(loaded); return loaded ? <details><summary>Другие планы</summary><li id="plan-item-42">Пункт 42</li></details> : <p>Loading</p>; }
afterEach(() => { cleanup(); window.history.replaceState(null, "", "/"); vi.useRealTimers(); });
it("reveals and focuses a direct hash target only after async rendering, including collapsed plans", async () => {
  vi.useFakeTimers(); window.history.replaceState(null, "", "/campaigns/1#plan-item-42");
  const scroll = vi.fn(); HTMLElement.prototype.scrollIntoView = scroll;
  const view = render(<Page loaded={false} />); await act(async () => { await vi.runOnlyPendingTimersAsync(); }); expect(scroll).not.toHaveBeenCalled();
  view.rerender(<Page loaded />); await act(async () => { await vi.runOnlyPendingTimersAsync(); });
  const target = document.getElementById("plan-item-42")!; expect(target.closest("details")).toHaveAttribute("open"); expect(document.activeElement).toBe(target); expect(scroll).toHaveBeenCalledWith({ block: "start" });
});
it("handles browser hash/back-forward changes and ignores malformed fragments", async () => {
  vi.useFakeTimers(); render(<Page loaded />); window.history.replaceState(null,"","/#plan-item-42"); act(() => window.dispatchEvent(new HashChangeEvent("hashchange"))); expect(document.activeElement?.id).toBe("plan-item-42");
  window.history.replaceState(null,"","/#%invalid"); expect(() => act(() => window.dispatchEvent(new HashChangeEvent("hashchange")))).not.toThrow();
});
