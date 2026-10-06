export const PASSWORD_REQUIREMENTS = "Пароль должен содержать от 12 до 128 символов и не быть пустым.";
export function passwordError(password: string, confirmation: string): string {
  if (!password.trim() || password.length < 12 || password.length > 128) return PASSWORD_REQUIREMENTS;
  if (password !== confirmation) return "Пароли не совпадают.";
  return "";
}
