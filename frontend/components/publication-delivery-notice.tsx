export function PublicationDeliveryNotice({ providerEnabled }: { providerEnabled?: boolean }) {
  return <p className={providerEnabled === false ? "warning" : "subtle"} role="status">{providerEnabled === false
    ? "Автоматическая отправка отключена. План сохранён; перед назначенным временем потребуется включить доставку."
    : providerEnabled === true
      ? "Автоматическая отправка включена. Публикация будет обработана в назначенное время; результат появится здесь."
      : "Состояние сервиса отправки сейчас недоступно. План сохранён; проверьте состояние позже."}</p>;
}
