export function PublicationDeliveryNotice({ providerEnabled }: { providerEnabled?: boolean }) {
  return <p className="notice">{providerEnabled === false
    ? "Автоматическая отправка сейчас отключена"
    : "Готовность автоматической отправки не подтверждена: проверьте publishing runtime перед отправкой."}</p>;
}
