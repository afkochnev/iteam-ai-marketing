import type { Metadata } from "next";
import "./styles.css";
import { AuthProvider } from "@/components/auth-provider";
import { AppNavigation } from "@/components/app-navigation";

export const metadata: Metadata = {
  title: "iTeam AI Marketing Department",
  description: "Управляемый AI-отдел маркетинга iTeam",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ru">
      <body><AuthProvider><AppNavigation>{children}</AppNavigation></AuthProvider></body>
    </html>
  );
}
