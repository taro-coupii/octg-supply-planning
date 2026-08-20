import { Navigate, useLocation } from "react-router-dom";
import { getToken } from "../lib/auth";

// Route guard for Layout routes (spec §フロントエンド): unauthenticated users
// are sent to /login?next=<current path>. api.ts handles the 401-after-login
// case (expired/invalid token discovered mid-session); this guard covers the
// no-token-at-all case before any request is even made.
export default function RequireAuth({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  if (!getToken()) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <>{children}</>;
}
