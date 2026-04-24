"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          username,
          password,
        }),
      });

      const contentType = response.headers.get("content-type") || "";
      let data = null;

      if (contentType.includes("application/json")) {
        data = await response.json();
      } else {
        const text = await response.text();
        throw new Error(text || `HTTP ${response.status}`);
      }

      if (!response.ok) {
        const detail =
          typeof data?.detail === "string"
            ? data.detail
            : data?.detail
              ? JSON.stringify(data.detail)
              : `HTTP ${response.status}`;
        throw new Error(detail);
      }

      if (!data?.access_token) {
        throw new Error("Сервер не вернул access_token");
      }

      localStorage.setItem("token", data.access_token);
      router.push("/live");
      router.refresh();
    } catch (err) {
      setError(err?.message || "Ошибка входа");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#f3f6fb",
        padding: 24,
      }}
    >
      <form
        onSubmit={handleSubmit}
        style={{
          width: "100%",
          maxWidth: 560,
          background: "#ffffff",
          borderRadius: 28,
          padding: 32,
          boxShadow: "0 10px 30px rgba(15, 23, 42, 0.06)",
        }}
      >
        <h1
          style={{
            margin: "0 0 24px 0",
            fontSize: 40,
            lineHeight: 1.1,
            color: "#0f172a",
            fontWeight: 800,
          }}
        >
          Вход в TNAS VMS
        </h1>

        <div
          style={{
            marginBottom: 24,
            color: "#64748b",
            fontSize: 18,
          }}
        >
          Авторизация в системе видеонаблюдения
        </div>

        <div style={{ marginBottom: 18 }}>
          <label
            style={{
              display: "block",
              marginBottom: 8,
              color: "#334155",
              fontSize: 16,
            }}
          >
            Логин
          </label>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            style={{
              width: "100%",
              height: 54,
              borderRadius: 16,
              border: "1px solid #cbd5e1",
              padding: "0 18px",
              fontSize: 18,
              outline: "none",
            }}
          />
        </div>

        <div style={{ marginBottom: 18 }}>
          <label
            style={{
              display: "block",
              marginBottom: 8,
              color: "#334155",
              fontSize: 16,
            }}
          >
            Пароль
          </label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            style={{
              width: "100%",
              height: 54,
              borderRadius: 16,
              border: "1px solid #cbd5e1",
              padding: "0 18px",
              fontSize: 18,
              outline: "none",
            }}
          />
        </div>

        {error ? (
          <div
            style={{
              marginBottom: 18,
              background: "#fee2e2",
              color: "#b91c1c",
              borderRadius: 14,
              padding: "12px 14px",
              fontSize: 14,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {error}
          </div>
        ) : null}

        <button
          type="submit"
          disabled={busy}
          style={{
            width: "100%",
            height: 54,
            borderRadius: 16,
            border: "none",
            background: "#0f172a",
            color: "#ffffff",
            fontSize: 18,
            fontWeight: 700,
            cursor: "pointer",
          }}
        >
          {busy ? "Входим..." : "Войти"}
        </button>
      </form>
    </div>
  );
}
