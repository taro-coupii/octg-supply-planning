import { ApiError, errorText } from "../api/client";

/**
 * A load failure the planner can act on.
 *
 * Two of the backend's refusals are not bugs and not transient — they are
 * configuration/data-feed problems whose `detail` names the fix:
 *
 *   409 inventory_scope_missing — this customer is not mapped to a Business
 *       Unit, so there is no inventory scope to read. Someone must map it.
 *   424 inventory_row_missing   — no InventoryOnHand row for this (BU, product)
 *       has arrived from Oracle. Someone must fix the feed.
 *
 * The inventory engine raises rather than inventing an on-hand number, which is
 * the right behaviour and the reason these reach the UI at all. Rendering either
 * as "failed to load" would discard both the diagnosis and the named fix, so the
 * server's own prose is shown verbatim and the ids it identified are listed so
 * the request can be handed to the right owner.
 */

const OWNER: Record<string, string> = {
  inventory_scope_missing:
    "This is a customer configuration problem — whoever administers customers " +
    "needs to map this customer to a Business Unit.",
  inventory_row_missing:
    "This is an inventory data-feed problem — whoever owns the Oracle " +
    "inventory feed needs to supply the on-hand row for this Business Unit and " +
    "product.",
};

export default function LoadError({
  what,
  error,
}: {
  /** What could not be loaded, e.g. "By Item analysis". */
  what: string;
  error: unknown;
}) {
  const api = error instanceof ApiError ? error : null;

  if (api && api.isExplained) {
    const owner = api.code ? OWNER[api.code] : undefined;
    return (
      <div className="load-error load-error-explained">
        <div className="load-error-head">
          <span className="badge badge-Uncovered">
            {api.status === 409
              ? "Configuration incomplete"
              : api.status === 424
              ? "Upstream data missing"
              : `Refused (${api.status})`}
          </span>
          <span className="load-error-what">{what} cannot be shown</span>
        </div>
        <p className="load-error-detail">{api.detail}</p>
        {owner && <p className="load-error-owner">{owner}</p>}
        {(api.businessUnitId || api.productId) && (
          <dl className="load-error-ids">
            {api.businessUnitId && (
              <div>
                <dt>Business Unit</dt>
                <dd className="num">{api.businessUnitId}</dd>
              </div>
            )}
            {api.productId && (
              <div>
                <dt>Product</dt>
                <dd className="num">{api.productId}</dd>
              </div>
            )}
          </dl>
        )}
        {api.code && <p className="load-error-code">Code: {api.code}</p>}
      </div>
    );
  }

  return (
    <div className="load-error">
      <div className="load-error-head">
        <span className="load-error-what">Failed to load {what}</span>
      </div>
      <p className="load-error-detail">{errorText(error)}</p>
    </div>
  );
}
