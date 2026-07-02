"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { saveAuthToken } from "../../lib/api";
import { loadCurrentUser } from "../../lib/currentUser";
import { useLocaleText } from "../../lib/i18n";

const LAST_USERNAME_KEY = "km_vms_last_username";

function loadLastUsername() {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(LAST_USERNAME_KEY) || "";
}

function saveLastUsername(value) {
  if (typeof window === "undefined") return;
  const username = String(value || "").trim();
  if (username) window.localStorage.setItem(LAST_USERNAME_KEY, username);
}

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [staySignedIn, setStaySignedIn] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const text = useLocaleText("login");

  useEffect(() => {
    setUsername(loadLastUsername());
    fetch("/api/system/status")
      .then((response) => response.ok ? response.json() : null)
      .then((status) => {
        if (status?.setup_required) router.replace("/setup");
      })
      .catch(() => {});
  }, [router]);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    const normalizedUsername = username.trim();

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: normalizedUsername,
          password,
          stay_signed_in: staySignedIn,
        }),
      });

      const contentType = response.headers.get("content-type") || "";
      const data = contentType.includes("application/json") ? await response.json() : null;
      if (!response.ok) {
        throw new Error(typeof data?.detail === "string" ? data.detail : `HTTP ${response.status}`);
      }
      if (!data?.access_token) throw new Error(text.noToken);

      saveLastUsername(normalizedUsername);
      saveAuthToken(data.access_token, {
        persistent: staySignedIn,
        expiresAt: data.expires_at,
      });
      localStorage.removeItem("vms_login_redirect");
      sessionStorage.removeItem("vms_login_redirect");
      await loadCurrentUser({ force: true });
      router.replace("/");
    } catch (err) {
      setError(err?.message || text.error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="authPage">
      <form onSubmit={handleSubmit} className="authCard">
        <h1 className="authTitle">{text.title}</h1>
        <div className="authSubtitle">{text.subtitle}</div>

        <label className="authLabel">
          <span>{text.username}</span>
          <input className="authInput" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        </label>

        <label className="authLabel">
          <span>{text.password}</span>
          <input className="authInput" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </label>

        <label className="authCheck">
          <input type="checkbox" checked={staySignedIn} onChange={(e) => setStaySignedIn(e.target.checked)} />
          <span>
            <b>{text.stay}</b>
            <small>{text.stayHint}</small>
          </span>
        </label>

        {error ? <div className="authError">{error}</div> : null}

        <button type="submit" disabled={busy} className="authButton">
          {busy ? text.busy : text.submit}
        </button>
      </form>
    </div>
  );
}
