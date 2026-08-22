import { Link, useLocation } from "react-router-dom";

/**
 * The catch-all route. Before this existed a bad URL rendered an empty
 * AppShell column -- indistinguishable from a broken screen, which is exactly
 * the ambiguity the platform refuses everywhere else.
 */
export default function NotFound() {
  const location = useLocation();
  return (
    <div className="notfound">
      <h2>No screen at this address</h2>
      <p>
        <code>{location.pathname}</code> is not a page in this platform. The
        link that brought you here is stale or mistyped.
      </p>
      <p>
        <Link to="/">Go to the Home dashboard</Link>
      </p>
    </div>
  );
}
