import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getToken, login } from "../auth";

/**
 * Dev login. Under Entra ID this screen becomes a single "Sign in with
 * Microsoft" redirect — the email/password form is the part that disappears,
 * which is why it stays deliberately minimal.
 */
export default function Login() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Already holding a token (e.g. Back button onto /login): go home. If the
  // token is stale the next API call 401s and lands back here — the single
  // 401 policy in auth.ts, not this component, owns that decision.
  if (getToken()) {
    navigate("/", { replace: true });
    return null;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <span className="sidebar-mark" aria-hidden="true">
          OC
        </span>
        <h1>OCTG Supply Readiness</h1>
        <p className="login-hint">Sign in to continue</p>
        <label>
          Email
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error && (
          <p className="login-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
