import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationDeliveryNotice } from "@/components/publication-delivery-notice";
const status = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ publicationsApi: { deliveryStatus: status } }));
beforeEach(() => vi.clearAllMocks());
describe("Delivery status is separate from scheduling", () => {
  it("does not claim readiness from an enabled flag", () => {
    render(<PublicationDeliveryNotice providerEnabled />);
    expect(screen.queryByText(/Готов к автоматической отправке/)).not.toBeInTheDocument();
    expect(status).not.toHaveBeenCalled();
  });
  it("checks only on request and explains the limited check", async () => {
    status.mockResolvedValue({ state: "CHECK_UNAVAILABLE", message: "Проверка недоступна в полном объёме. План сохранён." });
    render(<PublicationDeliveryNotice providerEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "Проверить сервис отправки" }));
    expect(await screen.findByText(/Проверка недоступна в полном объёме/)).toBeInTheDocument();
    expect(status).toHaveBeenCalledTimes(1);
  });
  it("keeps scheduling successful when the check fails", async () => {
    status.mockRejectedValue(new Error("network"));
    render(<PublicationDeliveryNotice providerEnabled />);
    fireEvent.click(screen.getByRole("button", { name: "Проверить сервис отправки" }));
    expect(await screen.findByText(/Проверка недоступна. План сохранён/)).toBeInTheDocument();
  });
  it("reports disabled delivery separately", () => {
    render(<PublicationDeliveryNotice providerEnabled={false} />);
    expect(screen.getByText(/Автоматическая отправка отключена. План сохранён/)).toBeInTheDocument();
  });
});
