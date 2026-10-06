export function PublicationDeliveryNotice({ providerEnabled }: { providerEnabled?: boolean }) {
  return <>
    <p className="notice">{providerEnabled === false
      ? "Автоматическая отправка сейчас отключена"
      : `${providerEnabled === true ? "Provider включён. Это не означает согласование публикации. " : ""}Готовность автоматической отправки не подтверждена: проверьте publishing runtime перед отправкой.`}</p>
    {providerEnabled === false && <p>Provider disabled. Согласование публикации не означает доступность доставки.</p>}
  </>;
}
