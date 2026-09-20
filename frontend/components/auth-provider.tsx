"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { authApi, type User } from "@/lib/api";

interface AuthContextValue { user: User | null; loading: boolean; login: (email: string, password: string) => Promise<void>; logout: () => Promise<void>; }
const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: Readonly<{ children: React.ReactNode }>) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => { authApi.me().then(setUser).catch(() => setUser(null)).finally(() => setLoading(false)); }, []);
  const login = useCallback(async (email: string, password: string) => { const result = await authApi.login(email, password); setUser(result.user); }, []);
  const logout = useCallback(async () => { try { await authApi.logout(); } finally { setUser(null); } }, []);
  const value = useMemo(() => ({ user, loading, login, logout }), [user, loading, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
