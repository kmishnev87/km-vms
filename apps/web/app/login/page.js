"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { saveAuthToken } from "../../lib/api";

const TEXT = {
  title: "\u0412\u0445\u043e\u0434 \u0432 KM VMS",
  subtitle: "\u0410\u0432\u0442\u043e\u0440\u0438\u0437\u0430\u0446\u0438\u044f \u0432 \u0441\u0438\u0441\u0442\u0435\u043c\u0435 \u0432\u0438\u0434\u0435\u043e\u043d\u0430\u0431\u043b\u044e\u0434\u0435\u043d\u0438\u044f",
  username: "\u041b\u043e\u0433\u0438\u043d",
  password: "\u041f\u0430\u0440\u043e\u043b\u044c",
  stay: "\u041e\u0441\u0442\u0430\u0432\u0430\u0442\u044c\u0441\u044f \u0432 \u0441\u0438\u0441\u0442\u0435\u043c\u0435",
  stayHint: "\u0415\u0441\u043b\u0438 \u0432\u043a\u043b\u044e\u0447\u0435\u043d\u043e, \u0441\u0435\u0441\u0441\u0438\u044f \u0436\u0438\u0432\u0451\u0442 \u0434\u043e 24:00 \u0441\u0438\u0441\u0442\u0435\u043c\u043d\u043e\u0433\u043e \u0434\u043d\u044f.",
  submit: "\u0412\u043e\u0439\u0442\u0438",
  busy: "\u0412\u0445\u043e\u0434\u0438\u043c...",
  noToken: "\u0421\u0435\u0440\u0432\u0435\u0440 \u043d\u0435 \u0432\u0435\u0440\u043d\u0443\u043b access_token",
  error: "\u041e\u0448\u0438\u0431\u043a\u0430 \u0432\u0445\u043e\u0434\u0430",
};

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [staySignedIn, setStaySignedIn] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
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

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username,
          password,
          stay_signed_in: staySignedIn,
        }),
      });

      const contentType = response.headers.get("content-type") || "";
      const data = contentType.includes("application/json") ? await response.json() : null;
      if (!response.ok) {
        throw new Error(typeof data?.detail === "string" ? data.detail : `HTTP ${response.status}`);
      }
      if (!data?.access_token) throw new Error(TEXT.noToken);

      saveAuthToken(data.access_token, {
        persistent: staySignedIn,
        expiresAt: data.expires_at,
      });
      localStorage.removeItem("vms_login_redirect");
      sessionStorage.removeItem("vms_login_redirect");
      router.push("/");
      router.refresh();
    } catch (err) {
      setError(err?.message || TEXT.error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="authPage">
      <form onSubmit={handleSubmit} className="authCard">
        <h1 className="authTitle">{TEXT.title}</h1>
        <div className="authSubtitle">{TEXT.subtitle}</div>

        <label className="authLabel">
          <span>{TEXT.username}</span>
          <input className="authInput" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        </label>

        <label className="authLabel">
          <span>{TEXT.password}</span>
          <input className="authInput" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </label>

        <label className="authCheck">
          <input type="checkbox" checked={staySignedIn} onChange={(e) => setStaySignedIn(e.target.checked)} />
          <span>
            <b>{TEXT.stay}</b>
            <small>{TEXT.stayHint}</small>
          </span>
        </label>

        {error ? <div className="authError">{error}</div> : null}

        <button type="submit" disabled={busy} className="authButton">
          {busy ? TEXT.busy : TEXT.submit}
        </button>
      </form>
    </div>
  );
}
