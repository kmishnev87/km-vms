"use client";
import { useEffect, useState } from "react";
import { getAuthToken } from "./api";

// No polling: react only to sign-in/session changes in this or another tab.
export function useAuthSession() {
  const [session, setSession] = useState(getAuthToken);
  useEffect(() => {
    const refresh = () => setSession(getAuthToken());
    const onStorage = (event) => {
      if (event.key === null || ["token", "token_expires_at"].includes(event.key)) refresh();
    };
    window.addEventListener("km-vms-auth-changed", refresh);
    window.addEventListener("storage", onStorage);
    refresh();
    return () => {
      window.removeEventListener("km-vms-auth-changed", refresh);
      window.removeEventListener("storage", onStorage);
    };
  }, []);
  return session;
}
