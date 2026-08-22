import { authFetch, withToken } from "../auth";
const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export type CoverageStatus =
  | "Covered"
  | "CoveredViaSubstitute"
  | "PendingApproval"
  | "Uncovered"
  | "Unrecoverable"
  | null;

export interface WellSummary {
  id: string;
  name: string;
  /** Owning customer via the planning node; null when the well is unmapped. */
  customer_id?: string | null;
  customer_name?: string | null;
  /** Well-level status (Planned/Budgeted/Confirmed). One value for the whole well. */
  demand_status: string;
  coverage_status: CoverageStatus;
  /** Earliest ROS among this well's demand lines. Server-sorts by this — never re-sort client-side. */
  earliest_ros_date: string | null;
  /** Earliest ROS this well's coverage did NOT satisfy. Null means "no shortage", not missing data. */
  first_runout_date: string | null;
}

export interface DemandLineOut {
  id: string;
  product_id: string;
  product_description: string | null;
  quantity: number;
  unit_of_measure: UnitOfMeasure;
  ros_date: string;
  /** Read-only echo of the well's demand_status — repeated on every line, identical within a well. */
  status: string;
  profile: string;
  current_revision_no: number;
  coverage_status: CoverageStatus;
  coverage_reason: string | null;
}

export interface WellDetail extends WellSummary {
  customer_id: string;
  customer_name: string;
  planning_node_path: string;
  demand_lines: DemandLineOut[];
}

/** "Mtr" | "PC" | "MT" */
export type UnitOfMeasure = "Mtr" | "PC" | "MT";

/** Response of `PUT /wells/{well_id}/demand-status`. */
export interface WellDemandStatusChangeOut {
  well_id: string;
  well_name: string;
  status_before: string;
  status_after: string;
  coverage_before: CoverageStatus;
  coverage_after: CoverageStatus;
  demand_line_ids: string[];
  revision_ids: string[];
  impact_record_ids: string[];
  revised_line_count: number;
  /** True => the requested status was already in effect; nothing was revised. */
  unchanged: boolean;
}

export interface WellDemandStatusInput {
  demand_status: string;
}

export interface ImpactRecord {
  id: string;
  demand_line_id: string;
  well_id: string;
  well_name?: string | null;
  customer_name?: string | null;
  product_description: string | null;
  unit_of_measure: UnitOfMeasure | null;
  quantity_before: number | null;
  quantity_after: number | null;
  ros_date_before: string | null;
  ros_date_after: string | null;
  status_before: string | null;
  status_after: string | null;
  coverage_before: string | null;
  coverage_after: string | null;
  created_at: string;
}

export interface PendingApprovalSummary {
  well_id: string;
  well_name: string;
  customer_name?: string | null;
  demand_line_id: string;
  product_description: string;
  quantity: number;
  unit_of_measure: UnitOfMeasure;
  ros_date: string;
  /** Open WellSubstitutionApproval id, when one exists — powers the card's
   * Approve/Decline (the existing decide operation, relocated). */
  approval_id?: string | null;
  substitute_description?: string | null;
}

export interface HomeDashboardData {
  demand_changes: ImpactRecord[];
  uncovered_wells: WellSummary[];
  pending_approvals: PendingApprovalSummary[];
}

export type SubstitutionApprovalStatus = "Approved" | "Pending" | "Rejected";

/**
 * Which layer stops this substitute being usable.
 *
 * "hard-assigned-elsewhere" is NOT a flavour of "insufficient-inventory" and the
 * UI must not render them alike. The steel physically exists in this Business
 * Unit; it is hard-assigned to another customer's demand line. This platform
 * cannot release it — the fix is a person editing the assignment in Oracle. By
 * contrast "insufficient-inventory" means the steel genuinely is not there, and
 * the fix is the mill. Two different destinations, two different treatments.
 */
export type SubstitutionBlockingLayer =
  | "customer"
  | "well-approval"
  | "hard-assigned-elsewhere"
  | "insufficient-inventory"
  | null;

/**
 * The backend's documented resolution order (its `BLOCK_PRIORITY`), most
 * blocking first. Mirrored here so the UI can sort/reason about layers without
 * inventing an order of its own.
 */
export const BLOCK_PRIORITY: Exclude<SubstitutionBlockingLayer, null>[] = [
  "customer",
  "well-approval",
  "hard-assigned-elsewhere",
  "insufficient-inventory",
];

// Catalogue only -- no quantity. On-hand is a property of (Business Unit,
// product), never of the catalogue entry. Where a quantity is needed, use the
// BU-scoped figure the endpoint provides (e.g. SubstitutionCandidate.available_qty).
export interface ProductOut {
  id: string;
  type: string;
  size: string;
  grade: string;
  connection: string;
  description: string | null;
  unit_of_measure: UnitOfMeasure;
}

export interface SubstitutionCandidate {
  demand_line_id: string;
  from_product_id: string;
  from_product_description: string | null;
  to_product_id: string;
  product: ProductOut;
  customer_allowed: boolean;
  approval_status: SubstitutionApprovalStatus | null;
  approval_id: string | null;
  /** FREE on-hand in this Business Unit — what this line could actually take. */
  available_qty: number;
  required_qty: number;
  usable: boolean;
  blocking_layer: SubstitutionBlockingLayer;

  /**
   * Quantity of the substitute that exists in this BU but is hard-assigned to
   * another demand line. Show it NEXT TO available_qty: "0 available" alone
   * reads as "no steel", which is false and sends the planner to the mill
   * instead of to Oracle.
   */
  hard_assigned_qty: number;

  /**
   * The server's action text for whichever layer is blocking. Always prefer this
   * over any string the UI holds — the backend supplies it for all four layers
   * precisely so the two can never disagree.
   */
  recommended_action: string | null;

  // ---- Over-subscription -------------------------------------------------
  //
  // A PendingApproval does NOT reserve the substitute quantity (this platform
  // must not create reservations). So several lines can be pending against the
  // same steel, and scarcity is no longer implicit in a reservation — it has to
  // be SAID. `over_subscription_note` is that sentence; when `over_subscribed`
  // is true it must be rendered prominently, otherwise a planner approving both
  // lines has no way to know one of them must fail.
  /** How many demand lines are awaiting approval on this substitute. */
  pending_line_count: number;
  /** Their combined requirement, which may exceed available_qty. */
  pending_required_qty: number;
  over_subscribed: boolean;
  /** Full sentence, already written for a planner. Render verbatim. */
  over_subscription_note: string | null;

  // ---- Approval-by date ---------------------------------------------------
  //
  // Answers "if rejection comes in past this date, we cannot recover the
  // demand by mill supply." `available=false` => render `approval_by_date_reason`
  // instead of a guessed date. There is deliberately no `still_recoverable`
  // field on this payload (see app.engines.substitution.SubstitutionApprovalByDate,
  // which computes it internally but app.api.substitution does not copy it onto
  // the response schema) — the UI derives urgency from whether `approval_by_date`
  // has already passed. Reported as a backend gap, not silently patched server-side.
  approval_by_date_available: boolean;
  approval_by_date: string | null;
  approval_by_date_reason: string;
}

export interface SubstitutionApproval {
  id: string;
  demand_line_id: string;
  from_product_id: string;
  to_product_id: string;
  status: SubstitutionApprovalStatus;
  requested_at: string;
  decided_at: string | null;
}

/** One row of GET /substitution-approvals (the approval queue). */
export interface ApprovalQueueRow {
  approval_id: string;
  demand_line_id: string;
  status: string;
  requested_at: string;
  decided_at: string | null;
  well_id: string | null;
  well_name: string | null;
  customer_name: string | null;
  from_product_description: string;
  to_product_description: string;
  quantity: number | null;
  unit_of_measure: string | null;
  ros_date: string | null;
}

export interface SubstitutionApprovalInput {
  from_product_id: string;
  to_product_id: string;
}

// ---- MRP ----

// ---------------------------------------------------------------------------
// Lead time
//
// Lead time is a SUM OVER FOUR ATTRIBUTE DIMENSIONS, not a scalar stored on the
// product. The breakdown is the point: transparency is the stated reason a
// planner trusts the number at all, so the per-dimension rows are the feature
// and the total is the summary of them.
//
// The trap is `modelled === false`. It means at least one dimension had no
// matching component, so the model CANNOT state a lead time. In that case the
// backend deliberately sends `total_months: 0.0`. Rendering that as "0 mo" would
// claim instant delivery — the exact opposite of the truth — and would make an
// unmodelled product look like the safest row on the screen. `matched_months`
// is the sum of the dimensions that DID match; it exists only to show how far
// the model got and is NOT a lead time. Never substitute it for the total.
// ---------------------------------------------------------------------------

/** "OD/WT" | "Grade" | "Connection" | "Logistics" — served as free text. */
export interface LeadTimeComponent {
  dimension: string;
  /** The component row's own key. "*" is the catch-all / shared term. */
  attribute_value: string;
  /** What this product's attribute actually was when matched. */
  matched_on: string;
  months: number;
  /** True => matched the "*" catch-all, i.e. a term shared by many products. */
  shared: boolean;
  label: string;
  component_id?: string;
}

export interface LeadTimeBreakdown {
  product_id: string;
  components: LeadTimeComponent[];
  /** 0.0 whenever `modelled` is false. Never render it as a lead time then. */
  total_months: number;
  modelled: boolean;
  /** Dimensions with no matching component. Non-empty => modelled is false. */
  missing_dimensions: string[];
  /** Diagnostic only — how far the model got. NOT a lead time. */
  matched_months: number;
  transit_months?: number;
  /** The engine's own explanation when not modelled. Empty when modelled. */
  note: string;
}

/** One MRP Layer 1 row: a product that needs procurement action. */
export interface MrpRecommendation {
  product_id: string;
  product_description: string | null;
  quantity: number;
  unit_of_measure: UnitOfMeasure;
  ros_date: string;
  required_ship_date: string;
  recommended_order_date: string;
  /**
   * Scalar view of `lead_time.total_months`, kept for compatibility. It is 0
   * when the lead time is NOT MODELLED, so do not render it on its own — use
   * `lead_time` and honour `lead_time.modelled`.
   */
  lead_time_months: number;
  /** The explainable breakdown behind `lead_time_months`. */
  lead_time?: LeadTimeBreakdown;
  /** true => cannot be met by mill order even if ordered today; escalate. */
  unrecoverable: boolean;
  reason: string;
  demand_line_ids: string[];
}

export interface ByItemDemandLine {
  demand_line_id: string;
  well_id: string;
  well_name: string;
  customer_name?: string | null;
  profile: string;
  quantity: number;
  unit_of_measure: UnitOfMeasure;
  ros_date: string;
  coverage_status: CoverageStatus;
  coverage_reason: string | null;
  product_id?: string;
  product_description?: string | null;
}

/**
 * `assigned` and `on_order` are Oracle-owned and NOT yet integrated: they come
 * back as 0 with oracle_integrated=false. The UI must never render those as a
 * measured zero — see InventorySection in MrpByItem.
 */
export interface InventoryPosition {
  on_hand: number;
  assigned: number;
  on_order: number | null;
  oracle_integrated: boolean;
  /** e.g. "unavailable" — why `assigned` is not a measured figure. */
  assigned_source?: string;
  on_order_source?: string;
  on_order_earliest_arrival?: string | null;
  on_order_latest_arrival?: string | null;
  on_order_undated?: number;
  on_order_poed?: number;
  on_order_booked?: number;
  on_order_unstated?: number;
  customer_owned?: number;
  customer_owned_source?: string;
  unit_of_measure: UnitOfMeasure;
}

export interface RunoutPoint {
  month: string;
  opening_balance: number;
  demand: number;
  closing_balance: number;
  /** primary + contingency === demand */
  demand_primary: number;
  demand_contingency: number;
  /** Cust/Owned balance split; customer-owned depletes first. */
  closing_customer_owned: number;
  closing_company: number;
  /** Opening split, before this month's demand draws. */
  opening_customer_owned: number;
  opening_company: number;
  /** Incoming split by certainty: real POs vs the engine's suggestion. */
  incoming_on_order: number;
  incoming_recommended: number;
  unit_of_measure: UnitOfMeasure;
}

export interface ByItemAnalysis {
  product_id: string;
  product_description: string | null;
  unit_of_measure: UnitOfMeasure;
  inventory: InventoryPosition;
  demand_lines: ByItemDemandLine[];
  runout: RunoutPoint[];
  runout_month: string | null;
  /**
   * Hypothetical second series: "what does the runout curve look like if the
   * recommended order is placed and arrives." Distinct from `runout` — never
   * merge the two into one curve. `null` month means the order closes the gap
   * entirely within the projected horizon (balance never goes negative).
   */
  runout_with_recommended_order?: RunoutPoint[];
  runout_month_with_recommended_order?: string | null;
  /**
   * THE MONTHLY LEDGER: opening (ownership split) + real on-order arrivals +
   * the suggested order's arrival (separate columns — promise vs suggestion)
   * − demand = ending (ownership split).
   */
  ledger?: RunoutPoint[];
  ledger_runout_month?: string | null;
  /** Undated on-order quantity that could not be placed in any month. */
  ledger_undated_on_order?: number;
  /** The single most-urgent recommendation, for overlaying on the timeline. */
  recommendation: MrpRecommendation | null;
  /**
   * Full list (a product can have both a recoverable and an unrecoverable
   * line). Optional because a backend older than this field will omit it.
   */
  recommendations?: MrpRecommendation[];
  /**
   * The product's lead-time breakdown, present whether or not an order is
   * recommended — it is a property of the product, not of a recommendation.
   */
  lead_time?: LeadTimeBreakdown;
}

/**
 * Status is deliberately absent: it is a property of the WELL, not the line
 * (see `PUT /wells/{id}/demand-status`). Sending `status` here now 400s.
 */
export interface DemandRevisionInput {
  quantity: number;
  ros_date: string;
  profile: string;
}

// ---------------------------------------------------------------------------
// Errors
//
// Two of the backend's refusals are configuration/data-feed problems with a
// NAMED FIX, and their `detail` is the whole message:
//
//   409 inventory_scope_missing — the customer is not mapped to a Business Unit.
//       Fixed by whoever administers customers.
//   424 inventory_row_missing   — no InventoryOnHand row for this (BU, product)
//       has arrived. Fixed by whoever owns the inventory feed.
//
// The engine refuses to invent an on-hand number rather than guess, so these are
// not crashes. Collapsing either into "failed to load" throws away both the
// diagnosis and the fix, so `ApiError` carries the parsed body and every screen
// that can trigger them renders `detail` verbatim.
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  status: number;
  /** Stable machine-readable discriminator, e.g. "inventory_row_missing". */
  code: string | null;
  /** The server's prose. Already written for a planner — show it verbatim. */
  detail: string | null;
  businessUnitId: string | null;
  productId: string | null;

  /**
   * Rows an operation refused to write because they conflict with live data and
   * the override is not approved. Only ever set on the demand-import apply 409.
   */
  blockedRowIds: string[];

  constructor(
    status: number,
    body: {
      error?: string;
      detail?: string | Record<string, unknown>;
      business_unit_id?: string | null;
      product_id?: string | null;
    } | null,
    path: string
  ) {
    // FastAPI's `HTTPException(detail=...)` accepts a dict as well as a string, and
    // the demand-import conflict refusal uses one so a client can highlight the
    // offending rows instead of parsing prose. Unwrap it, so the planner-facing
    // message inside is not thrown away in favour of "failed: 409" — the whole
    // reason this class keeps `detail` verbatim.
    const raw = body?.detail;
    const nested =
      raw !== null && typeof raw === "object"
        ? (raw as { error?: unknown; detail?: unknown; blocked_row_ids?: unknown })
        : null;
    const detail =
      typeof raw === "string"
        ? raw
        : typeof nested?.detail === "string"
        ? nested.detail
        : null;
    super(detail ?? `${path} failed: ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code =
      typeof body?.error === "string"
        ? body.error
        : typeof nested?.error === "string"
        ? nested.error
        : null;
    this.detail = detail;
    this.businessUnitId = body?.business_unit_id ?? null;
    this.productId = body?.product_id ?? null;
    this.blockedRowIds = Array.isArray(nested?.blocked_row_ids)
      ? (nested!.blocked_row_ids as string[])
      : [];
  }

  /** True => an explained configuration/data problem, not a generic failure. */
  get isExplained() {
    return this.detail !== null;
  }
}

async function apiError(res: Response, path: string): Promise<ApiError> {
  let body: Record<string, unknown> | null = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  return new ApiError(res.status, body as never, path);
}

/** Message for display, without the "Error: " that String(e) prepends. */
export function errorText(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`);
  if (!res.ok) throw await apiError(res, path);
  return res.json();
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await apiError(res, path);
  return res.json();
}


// ---------------------------------------------------------------------------
// Material Order Requirements (GET /mrp/order-requirements)
// ---------------------------------------------------------------------------

/** One (product, month) cell; quantities in the row's own unit. */
export interface MorCell {
  month: string;
  demand: number;
  arrivals: number;
  /** Month-end balance before any new order; negative = cumulative unmet demand. */
  projected_balance: number;
  /** The shortfall that FIRST bites this month. Row cells sum to the total. */
  order_requirement: number;
  /** Month the covering order must leave (need month − lead time). Null when
   * nothing is required or the lead time is not modelled. */
  order_by_month: string | null;
}

/** `available=false` = position UNKNOWN: render `reason`, never figures. */
export interface MorRow {
  product_id: string;
  product_description: string | null;
  unit_of_measure: string;
  available: boolean;
  /** null = not set (distinct from an explicit 0). Requirements trigger below this level. */
  safety_stock: number | null;
  position: InventoryPosition | null;
  lead_time_months: number | null;
  lead_time_modelled: boolean;
  lead_time_note: string;
  total_demand: number;
  /** Overdue portion of total_demand (ROS month passed) — folded into the
   * grid's first month, labelled here rather than silently blended. */
  total_overdue_demand: number;
  total_order_requirement: number;
  order_flag: boolean;
  first_order_by: string | null;
  already_late: boolean;
  cells: MorCell[];
  reason: string | null;
}

export interface MorGrid {
  months: string[];
  horizon_months: number;
  generated_for_customer_id: string | null;
  rows: MorRow[];
  unavailable_count: number;
  notes: string[];
}


// ---------------------------------------------------------------------------
// Surplus List (GET /analysis/surplus)
// ---------------------------------------------------------------------------

/** One (BU, product): allocated + surplus + obsolete === on_hand exactly. */
export interface SurplusRow {
  business_unit_id: string;
  business_unit_name: string | null;
  product_id: string;
  product_description: string | null;
  unit_of_measure: string;
  on_hand: number;
  allocated: number;
  surplus: number;
  obsolete: number;
  demand_in_window: number;
  /** Overdue portion of demand_in_window (ROS month passed) — counted, labelled. */
  demand_overdue: number;
  customer_owned: number;
}

export interface SurplusReport {
  horizon_months: number;
  generated_at: string;
  rows: SurplusRow[];
  allocated_tonnes: Figure;
  surplus_tonnes: Figure;
  obsolete_tonnes: Figure;
  unknown_position_count: number;
  note: string;
  /** The demand scope the allocation was computed under. When
   *  `scope_is_default` is false the figures came from a read-only recompute,
   *  not the stored official verdicts. */
  status_scope: string[];
  profile_scope: string[];
  scope_is_default: boolean;
  /** Customers a scoped recompute skipped (no BU / missing on-hand row). */
  skipped_customers: string[];
}


// ---------------------------------------------------------------------------
// Safety stock administration
// ---------------------------------------------------------------------------

/** `quantity` null = NOT SET (a real state, distinct from an explicit 0). */
export interface SafetyStockRow {
  product_id: string;
  product_description: string | null;
  unit_of_measure: string;
  quantity: number | null;
  note: string | null;
}

export interface SafetyStockList {
  rows: SafetyStockRow[];
  note: string;
}

export const api = {
  getHomeDashboard: () => getJSON<HomeDashboardData>("/dashboard/home"),
  getWells: () => getJSON<WellSummary[]>("/wells"),
  getOrderRequirements: (customerId?: string, horizonMonths?: number) =>
    getJSON<MorGrid>(
      "/mrp/order-requirements" +
        search([
          ["customer_id", customerId],
          ["horizon_months", horizonMonths],
        ])
    ),
  getMrpSummary: (customerId?: string) =>
    getJSON<MrpRecommendation[]>(
      customerId ? `/mrp/summary?customer_id=${encodeURIComponent(customerId)}` : "/mrp/summary"
    ),
  /**
   * URL of the MRP as an .xlsx: summary on tab 1, the By Item justification on
   * tabs 2 onward.
   *
   * A URL rather than a fetch, for the same reasons as
   * `demandImportTemplateUrl` — the browser's own download machinery handles the
   * Content-Disposition filename, the progress indicator and the save dialog,
   * and fetch → Blob → object URL → synthetic click would reimplement all three
   * worse. There is deliberately ONE download pattern in this app.
   *
   * `customerId` is optional, mirroring `getMrpSummary` and the server: MRP is a
   * system-wide procurement view by default, so the file must not be narrower
   * than the screen it exports.
   */
  mrpExportUrl: (customerId?: string) =>
    withToken(
      `${API_BASE}/mrp/export` +
        (customerId ? `?customer_id=${encodeURIComponent(customerId)}` : "")
    ),
  getMrpByItem: (productId: string) =>
    getJSON<ByItemAnalysis>(`/mrp/by-item/${productId}`),
  /**
   * Lead time on its own. By Item legitimately fails (409/424) when a product
   * has no inventory row in any Business Unit, but the lead time is knowable
   * regardless — it depends only on the product's attributes. So when By Item
   * cannot be loaded, fall back to this rather than showing nothing.
   */
  getLeadTime: (productId: string) =>
    getJSON<LeadTimeBreakdown>(`/mrp/lead-time/${productId}`),
  getWell: (id: string) => getJSON<WellDetail>(`/wells/${id}`),
  /**
   * Revises demand status for EVERY line of the well at once — status is a
   * well-level property. May change coverage for other wells of the same
   * customer (status selects whole wells in/out of the inventory pool).
   */
  setWellDemandStatus: (wellId: string, body: WellDemandStatusInput) =>
    putJSON<WellDemandStatusChangeOut>(`/wells/${wellId}/demand-status`, body),
  createRevision: (demandLineId: string, body: DemandRevisionInput) =>
    postJSON<ImpactRecord>(`/demand-lines/${demandLineId}/revisions`, body),
  getSubstitutionCandidates: (demandLineId: string) =>
    getJSON<SubstitutionCandidate[]>(
      `/demand-lines/${demandLineId}/substitution-candidates`
    ),
  requestSubstitutionApproval: (
    demandLineId: string,
    body: SubstitutionApprovalInput
  ) =>
    postJSON<SubstitutionApproval>(
      `/demand-lines/${demandLineId}/substitution-approvals`,
      body
    ),
  decideSubstitutionApproval: (approvalId: string, approved: boolean) =>
    postJSON<SubstitutionApproval>(
      `/substitution-approvals/${approvalId}/decision`,
      { approved }
    ),
  /** The approval queue: every well-substitution approval, newest first. */
  getSubstitutionApprovals: (status?: string) =>
    getJSON<ApprovalQueueRow[]>(
      "/substitution-approvals" + search([["status", status]])
    ),

  // ---- Scenario Planning ----
  // getScenarioPreview is a READ-ONLY what-if. Its numbers must never be
  // rendered as the official coverage verdict — see ScenarioImpact.is_what_if.
  getOverrideFields: () => getJSON<OverrideFields>("/scenarios/override-fields"),
  getScenarios: (customerId?: string) =>
    getJSON<ScenarioSummary[]>(
      customerId
        ? `/scenarios?customer_id=${encodeURIComponent(customerId)}`
        : "/scenarios"
    ),
  createScenario: (body: ScenarioInput) =>
    postJSON<ScenarioDetail>("/scenarios", body),
  getScenario: (id: string) => getJSON<ScenarioDetail>(`/scenarios/${id}`),
  patchScenario: (id: string, body: ScenarioPatch) =>
    patchJSON<ScenarioDetail>(`/scenarios/${id}`, body),
  addScenarioOverride: (id: string, body: ScenarioOverrideInput) =>
    postDetail<ScenarioOverride>(`/scenarios/${id}/overrides`, body),
  deleteScenarioOverride: (id: string, overrideId: string) =>
    deleteJSON(`/scenarios/${id}/overrides/${overrideId}`),
  getScenarioPreview: (id: string) =>
    getJSON<ScenarioImpact>(`/scenarios/${id}/preview`),
  applyScenario: (id: string) =>
    postDetail<ScenarioApplyResult>(`/scenarios/${id}/apply`, {}),

  getCustomers: () => getJSON<CustomerSummary[]>("/customers"),
  getCustomer: (id: string) => getJSON<CustomerSummary>(`/customers/${id}`),
  getBusinessUnits: () => getJSON<BusinessUnitOut[]>("/business-units"),
  getSafetyStocks: () => getJSON<SafetyStockList>("/admin/safety-stocks"),
  putSafetyStock: (productId: string, quantity: number, note?: string) =>
    putJSON<SafetyStockRow>(`/admin/safety-stocks/${encodeURIComponent(productId)}`, {
      quantity,
      note,
    }),
  deleteSafetyStock: (productId: string) =>
    deleteJSON(`/admin/safety-stocks/${encodeURIComponent(productId)}`),
  getSurplusReport: (businessUnitId?: string, status?: string[], profile?: string[]) =>
    getJSON<SurplusReport>(
      "/analysis/surplus" +
        search([
          ["business_unit_id", businessUnitId],
          ...(status ?? []).map((s): [string, string] => ["status", s]),
          ...(profile ?? []).map((p): [string, string] => ["profile", p]),
        ])
    ),
  /** Create a Business Unit. A duplicate name is a 409 (matched case-insensitively). */
  createBusinessUnit: (name: string) =>
    postDetail<BusinessUnitCreated>("/business-units", { name }),
  /**
   * Change a customer's Business Unit and/or allocation policy, in ONE request so the
   * server recomputes once. Refusals to render verbatim: 400 (bad policy value or
   * unknown BU id), 409 `unmap_refused`, 424 `remap_unresolvable` — the last one names
   * the products whose (BU, product) inventory rows the destination is missing, and
   * the remap is rolled back.
   */
  updateCustomerConfiguration: (id: string, body: CustomerConfigInput) =>
    patchJSON<CustomerConfigChange>(
      `/customers/${encodeURIComponent(id)}`,
      body
    ),

  // ---- Product catalogue ----
  // No quantity in the payload, deliberately: on-hand belongs to (BU, product),
  // never to a catalogue row. See ProductOut.
  getProducts: (params: ProductQuery = {}) =>
    getJSON<ProductOut[]>(
      `/products${search([
        ["q", params.q],
        // Only send active_only when overriding the server default (true).
        ["active_only", params.active_only === false ? "false" : undefined],
      ])}`
    ),

  // ---- Executive dashboard ----
  getExecutiveDashboard: (params: ExecutiveQuery = {}) =>
    getJSON<ExecutiveDashboard>(
      `/dashboard/executive${search([
        ["customer_id", params.customer_id],
        ["business_unit_id", params.business_unit_id],
        ["allocation_horizon_months", params.allocation_horizon_months],
        [
          "inventory_utilisation_horizon_months",
          params.inventory_utilisation_horizon_months,
        ],
        ...(params.status ?? []).map((s): [string, string] => ["status", s]),
        ...(params.profile ?? []).map((p): [string, string] => ["profile", p]),
      ])}`
    ),

  // ---- Customer-owned inventory ----
  // Unlike on-hand/assignments/on-order (all read-only Oracle projections),
  // this is data the platform itself owns via upload. `has_uploaded` is the
  // whole point: it distinguishes "never uploaded" from "uploaded, owns
  // nothing" — see CustomerOwnedInventoryOut.
  getCustomerOwnedInventory: (customerId: string) =>
    getJSON<CustomerOwnedInventoryOut>(
      `/customer-owned-inventory/${encodeURIComponent(customerId)}`
    ),
  listCustomerOwnedUploads: (customerId: string) =>
    getJSON<CustomerOwnedUploadSummary[]>(
      `/customer-owned-inventory/${encodeURIComponent(customerId)}/uploads`
    ),
  uploadCustomerOwnedInventory: (customerId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return postForm<CustomerOwnedUploadResult>(
      `/customer-owned-inventory/${encodeURIComponent(customerId)}/uploads`,
      form
    );
  },
  /**
   * The customer's CURRENT declared position as an .xlsx, already in the upload
   * contract. A URL, not a fetch — same single download idiom as
   * `demandImportTemplateUrl` and `mrpExportUrl`: the browser's own machinery
   * handles the Content-Disposition filename, the progress and the save dialog.
   *
   * Not a blank sheet. Re-uploading it unchanged restates every position at the
   * same quantity and moves nothing. Valid for a customer with NO declared
   * position too — a header-only workbook, which is the case the link is most
   * useful in, so never gate it on there being rows to show.
   */
  customerOwnedTemplateUrl: (customerId: string) =>
    withToken(
      `${API_BASE}/customer-owned-inventory/${encodeURIComponent(
        customerId
      )}/template`
    ),

  // ---- Cross-customer sharing (read-only what-if) ----
  // Deliberately DISAGREES with the official customer-scoped coverage verdict.
  // Never render its output as the official badge — see CrossCustomerSharing.
  getCrossCustomerSharing: (customerId: string) =>
    getJSON<CrossCustomerSharing>(
      `/analysis/cross-customer-sharing?customer_id=${encodeURIComponent(
        customerId
      )}`
    ),

  // ---- Demand List / Coverage Workspace / Demand Import ----
  getDemandLines: (params: DemandLineQuery) =>
    getJSON<DemandLineListResponse>(`/demand-lines${demandLineQuery(params)}`),
  getCoverageGrid: (params: CoverageGridQuery) =>
    getJSON<CoverageGridResponse>(`/coverage${coverageGridQuery(params)}`),
  listDemandImports: () =>
    getJSON<DemandImportBatchSummary[]>("/demand-imports"),
  uploadDemandImport: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return postForm<DemandImportBatch>("/demand-imports", form);
  },
  getDemandImport: (id: string) =>
    getJSON<DemandImportBatch>(`/demand-imports/${id}`),
  /**
   * URL of the CURRENT demand book as an .xlsx, in the import column contract.
   *
   * A URL rather than a fetch: the browser's own download machinery handles the
   * Content-Disposition filename, the progress indicator and the save dialog, and
   * doing it by hand (fetch -> Blob -> object URL -> synthetic click) would
   * reimplement all three worse. `customer_id` is required by the server — there is
   * deliberately no all-customers export.
   */
  demandImportTemplateUrl: (customerId: string, wellId?: string) =>
    withToken(
      `${API_BASE}/demand-imports/template?customer_id=${encodeURIComponent(
        customerId
      )}` + (wellId ? `&well_id=${encodeURIComponent(wellId)}` : "")
    ),
  /**
   * Coverage impact of approving ONE conflicting row. READ-ONLY — asking for it
   * approves nothing. Never render a figure from it as the coverage verdict.
   */
  getDemandImportConflictPreview: (batchId: string, rowId: string) =>
    getJSON<ImportConflictPreview>(
      `/demand-imports/${batchId}/rows/${rowId}/conflict-preview`
    ),
  /**
   * The SECOND decision: approve (or withdraw approval for) overriding a row's
   * conflict with the live book. Distinct from accept/skip on purpose.
   */
  setDemandImportOverrideApproval: (
    batchId: string,
    rowId: string,
    approved: boolean,
    approvedBy?: string
  ) =>
    postDetail<DemandImportRow>(
      `/demand-imports/${batchId}/rows/${rowId}/override-approval`,
      { approved, approved_by: approvedBy ?? null }
    ),
  setDemandImportRowDecision: (
    batchId: string,
    rowId: string,
    decision: DemandImportDecision
  ) =>
    patchJSON<DemandImportRow>(
      `/demand-imports/${batchId}/rows/${rowId}`,
      { decision }
    ),
  applyDemandImport: (id: string) =>
    postDetail<DemandImportApplyResult>(`/demand-imports/${id}/apply`, {}),
};

// ---------------------------------------------------------------------------
// Scenario Planning
//
// Two kinds of type live below and the difference is the important part:
//
//   Scenario / ScenarioOverride  — PERSISTED, shared, editable rows.
//   ScenarioImpact               — a READ-ONLY WHAT-IF. Nothing in it has been
//                                  persisted. `is_what_if` is sent by the
//                                  backend so the UI can label it without
//                                  inferring anything, exactly as the
//                                  cross-customer sharing panel is labelled. A
//                                  figure from it must never be shown as the
//                                  official coverage verdict.
//
// Appended as its own block, with its own hoisted fetch helpers, so this file
// stays easy to merge.
// ---------------------------------------------------------------------------

export type ScenarioStatus = "Draft" | "Review" | "Discussion" | "Applied";

export type ScenarioTargetKind =
  | "DemandLine"
  | "Inventory"
  | "PoArrival"
  | "Assignment"
  | "SubstitutionApproval";

/** "number" | "date" | "text" — which value_* field the override uses. */
/** Which value column(s) a (kind, field) pair uses.
 *
 * `"number+date"` is the ONE composite, used by `PoArrival.new_order`: a hypothetical
 * purchase order is a QUANTITY LANDING ON A DATE, so the row carries BOTH
 * `value_number` and `value_date` and neither half is meaningful alone. It is also
 * what makes such a row impossible to confuse with a `PoArrival.arrival_date`
 * restatement of a REAL purchase order, which takes the date column only. A form
 * rendering this type must collect both values and send both. */
export type OverrideValueType = "number" | "date" | "text" | "number+date";

/**
 * The override vocabulary, served by the backend so the editor's field pickers
 * cannot drift from `app.engines.overrides.OVERRIDE_FIELDS`.
 */
export interface OverrideFields {
  fields: Record<string, Record<string, OverrideValueType>>;
  enum_values: Record<string, string[]>;
  /** Previewable but never applicable — Oracle-owned, or unmodelled. */
  supply_kinds: string[];
  /** The preview cannot model these at all. */
  unmodelled_kinds: string[];
}

export interface ScenarioOverride {
  id: string;
  scenario_id: string;
  target_kind: ScenarioTargetKind;
  field_name: string;
  target_demand_line_id: string | null;
  target_well_id: string | null;
  target_product_id: string | null;
  target_business_unit_id: string | null;
  target_from_product_id: string | null;
  target_to_product_id: string | null;
  value_number: number | null;
  value_date: string | null;
  value_text: string | null;
  note: string | null;
  created_at: string;
}

export interface ScenarioOverrideInput {
  target_kind: ScenarioTargetKind;
  field_name: string;
  target_demand_line_id?: string | null;
  // WELL/demand_status overrides require this -- without it, a well-status
  // override cannot be created from this client at all.
  target_well_id?: string | null;
  target_product_id?: string | null;
  target_business_unit_id?: string | null;
  target_from_product_id?: string | null;
  target_to_product_id?: string | null;
  value_number?: number | null;
  value_date?: string | null;
  value_text?: string | null;
  note?: string | null;
}

export interface ScenarioSummary {
  id: string;
  name: string;
  description: string | null;
  customer_id: string;
  customer_name: string;
  status: ScenarioStatus;
  created_by: string | null;
  created_at: string;
  updated_at: string | null;
  applied_at: string | null;
  override_count: number;
  /** Headline what-if delta: wells this scenario would move. */
  coverage_delta_wells: number | null;
  coverage_delta_lines: number | null;
  /** Set when this row's preview failed. One bad row must not blank the list. */
  preview_error: string | null;
}

export interface ScenarioDetail extends ScenarioSummary {
  overrides: ScenarioOverride[];
}

export interface ScenarioInput {
  name: string;
  customer_id: string;
  description?: string | null;
  created_by?: string | null;
}

export interface ScenarioPatch {
  name?: string;
  description?: string;
  status?: ScenarioStatus;
}

/**
 * `status_before` / `status_after` may be the literal "NotEvaluated" — a real
 * outcome (the line is outside the coverage engine's filters), not a blank.
 */
export interface LineCoverageChange {
  demand_line_id: string;
  well_id: string;
  well_name: string;
  product_id: string;
  product_description: string | null;
  status_before: string;
  status_after: string;
  reason_before: string | null;
  reason_after: string | null;
  quantity_before: number;
  quantity_after: number;
  ros_date_before: string;
  ros_date_after: string;
  changed: boolean;
  /** False for a line that moved as a knock-on effect of someone else's change. */
  directly_overridden: boolean;
}

export interface WellCoverageChange {
  well_id: string;
  well_name: string;
  status_before: string | null;
  status_after: string | null;
  changed: boolean;
}

export interface MrpRowChange {
  kind: "added" | "removed" | "changed" | "unchanged";
  product_id: string;
  product_description: string | null;
  unrecoverable: boolean;
  quantity_before: number | null;
  quantity_after: number | null;
  ros_date_before: string | null;
  ros_date_after: string | null;
  recommended_order_date_before: string | null;
  recommended_order_date_after: string | null;
  reason_after: string | null;
}

/**
 * What a PoArrival.arrival_date override does to one product's runout curve.
 *
 * BOTH runout months come from an ON-ORDER-AWARE projection — one at the arrival
 * dates Oracle promised, one at the overridden dates — so the difference is
 * attributable to the shift alone. NEITHER equals `ByItemAnalysis.runout_month`,
 * which is on-hand-only by design. Do not label these as the By Item runout.
 *
 * NO COVERAGE VERDICT MOVES BECAUSE OF THIS, and a UI must not imply one did:
 * coverage is decided from on-hand stock alone and incoming supply is read beside
 * that verdict, never inside it.
 */
export interface SupplyRunoutChange {
  product_id: string;
  product_description: string | null;
  unit_of_measure: string | null;
  /** Null when every purchase-order row for this product is undated, in which
   *  case there was no schedule position to shift and `shift_days` is 0. */
  arrival_before: string | null;
  arrival_after: string | null;
  shift_days: number;
  runout_month_before: string | null;
  runout_month_after: string | null;
  /** False is a real answer, not a failure — a shift inside one month, or a
   *  product with enough stock never to run out, both leave the month alone.
   *  Render "no change to the runout month", never "the override was ignored". */
  runout_month_changed: boolean;
  on_order_dated_quantity: number;
  on_order_undated_quantity: number;
  runout_before: RunoutPoint[];
  runout_after: RunoutPoint[];

  /** HYPOTHETICAL purchase orders — ones that DO NOT EXIST — asserted by
   *  `PoArrival.new_order` overrides. `[quantity, expected_arrival]` pairs, earliest
   *  first. `runout_after` includes them; `runout_before` does not, which is what
   *  makes the two curves a readable diff. Empty on a pure arrival-date shift. */
  hypothetical_orders: [number, string][];
  /** The invented total. DELIBERATELY NOT part of `on_order_dated_quantity`, which
   *  stays a sum of real Oracle-projected rows a planner can go and verify. Never
   *  add the two into one "on order" figure. */
  hypothetical_quantity: number;
  /** What MRP already recommends ordering for this product. Null when it
   *  recommends nothing — there is no gap to size against. */
  mrp_recommended_quantity: number | null;
  /** "The order you drew is big enough to cover that recommendation." NOT "the MRP
   *  row went away" — it did not and cannot: MRP rows derive from coverage verdicts
   *  and coverage is on-hand-only. Render as a sizing check, never a resolved gap. */
  hypothetical_covers_recommendation: boolean;
}

export interface RiskImpact {
  unrecoverable_lines_before: number;
  unrecoverable_lines_after: number;
  unrecoverable_quantity_before: number;
  unrecoverable_quantity_after: number;
  became_unrecoverable: string[];
  no_longer_unrecoverable: string[];
}

export interface ScenarioImpact {
  scenario_id: string;
  scenario_name: string;
  scenario_status: string;
  customer_id: string;
  customer_name: string;
  business_unit_id: string | null;
  business_unit_name: string | null;
  override_count: number;
  /** Recorded but NOT taken into account because no engine reads the thing they
   *  describe. Always 0 today — PoArrival was the only such kind and is now
   *  modelled (see `supply_runout_changes`). Kept so the next kind that outruns
   *  the engines is reported loudly rather than silently ignored. */
  unmodelled_override_count: number;
  line_changes: LineCoverageChange[];
  well_changes: WellCoverageChange[];
  mrp_changes: MrpRowChange[];
  /** Runout effect of PoArrival overrides — empty unless the scenario restates an
   *  arrival date. This is the ONLY place such an override shows up: it moves no
   *  coverage verdict and no MRP recommendation row. */
  supply_runout_changes: SupplyRunoutChange[];
  risk: RiskImpact | null;
  covered_lines_before: number;
  covered_lines_after: number;
  covered_wells_before: number;
  covered_wells_after: number;
  changed_line_count: number;
  changed_well_count: number;
  /** Whether apply would succeed. False => `apply_blockers` says why. */
  applicable: boolean;
  apply_blockers: string[];
  /** Always true. Present so the UI can say so without inferring it. */
  is_what_if: boolean;
  notes: string[];
}

export interface ScenarioApplyResult {
  scenario_id: string;
  scenario_name: string;
  applied_at: string;
  revised_demand_line_ids: string[];
  demand_revision_ids: string[];
  impact_record_ids: string[];
  decided_approval_ids: string[];
  line_status_after: [string, string][];
  well_status_after: [string, string | null][];
  notes: string[];
}

export interface CustomerSummary {
  id: string;
  name: string;
  business_unit_id: string | null;
  allocation_policy: string;
}

/**
 * The backend's refusals carry a `detail` that a planner needs to READ — "this
 * scenario contains supply overrides that Oracle owns" is the whole message, and
 * a bare "409" would throw it away. Function declarations (not consts) so the
 * hoisting is unambiguous when used from the `api` object above.
 */
async function detailedError(res: Response, path: string): Promise<Error> {
  // Delegates to `apiError` so these paths raise the same ApiError as the GETs
  // and keep `status` / `error` / the inventory ids, not just the prose.
  return apiError(res, path);
}

async function putJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await detailedError(res, path);
  return res.json();
}

async function patchJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await detailedError(res, path);
  return res.json();
}

async function deleteJSON(path: string): Promise<void> {
  const res = await authFetch(`${API_BASE}${path}`, { method: "DELETE" });
  if (!res.ok) throw await detailedError(res, path);
}

async function postDetail<T>(path: string, body: unknown): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await detailedError(res, path);
  return res.json();
}

/** Multipart upload. No Content-Type header — the browser sets the boundary. */
async function postForm<T>(path: string, form: FormData): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, { method: "POST", body: form });
  if (!res.ok) throw await detailedError(res, path);
  return res.json();
}

// ---------------------------------------------------------------------------
// Demand List  (GET /demand-lines)
//
// `evaluated` is the field that matters. When it is false the line sits outside
// the coverage engine's status/profile scope, so `coverage_status` is null: the
// engine HAS NO verdict for it. That is a real, nameable state ("Not
// evaluated"), never a blank cell — serving a stale verdict here was a reported
// critical defect and rendering an absent verdict as innocuous would reintroduce
// it in the UI layer. Never key off `coverage_status == null` alone.
// ---------------------------------------------------------------------------

export interface DemandLineRow {
  id: string;
  well_id: string;
  well_name: string;
  planning_node_id: string;
  /** Ready-made breadcrumb, e.g. "Project Alpha / Pad A". */
  planning_node_path: string;
  customer_id: string;
  customer_name?: string | null;
  product_id: string;
  product_description: string | null;
  quantity: number;
  unit_of_measure: UnitOfMeasure;
  ros_date: string;
  /** The WELL's demand status, repeated on every line. Not editable per-line. */
  status: string;
  profile: string;
  current_revision_no: number;
  /** False => outside the engine's scope, no verdict exists. */
  evaluated: boolean;
  coverage_status: CoverageStatus;
  coverage_reason: string | null;
}

export interface DemandLineListResponse {
  total: number;
  limit: number;
  offset: number;
  returned: number;
  rows: DemandLineRow[];
}

export interface DemandLineQuery {
  customer_id?: string;
  well_id?: string;
  product_id?: string;
  status?: string[];
  profile?: string[];
  coverage_status?: string[];
  ros_from?: string;
  ros_to?: string;
  limit?: number;
  offset?: number;
}

function search(pairs: [string, string | number | undefined][]): string {
  const qs = new URLSearchParams();
  for (const [key, value] of pairs) {
    if (value === undefined || value === "") continue;
    qs.append(key, String(value));
  }
  const s = qs.toString();
  return s ? `?${s}` : "";
}

function demandLineQuery(p: DemandLineQuery): string {
  const pairs: [string, string | number | undefined][] = [
    ["customer_id", p.customer_id],
    ["well_id", p.well_id],
    ["product_id", p.product_id],
    ["ros_from", p.ros_from],
    ["ros_to", p.ros_to],
    ["limit", p.limit],
    ["offset", p.offset],
  ];
  for (const v of p.status ?? []) pairs.push(["status", v]);
  for (const v of p.profile ?? []) pairs.push(["profile", v]);
  for (const v of p.coverage_status ?? []) pairs.push(["coverage_status", v]);
  return search(pairs);
}

// ---------------------------------------------------------------------------
// Coverage Workspace  (GET /coverage)
//
// Two states the UI must not blur:
//   * `filters.recomputed_read_only` — the caller passed non-default
//     status/profile filters, so these numbers are a read-only recompute, NOT
//     the official stored verdict. `filters.explanation` must be shown verbatim.
//     The recompute is expensive, so toggling the filters is debounced.
//   * a row with `evaluated: false` — the well has no in-scope demand at all.
//     Neither covered nor uncovered; a third state, not a red or green badge.
// ---------------------------------------------------------------------------

/** The well-level rollup vocabulary, distinct from per-line CoverageStatus. */
export type CoverageRollup = "Covered" | "Uncovered" | "Unevaluated";

export interface CoverageGridFilters {
  status: string[];
  profile: string[];
  /** True => these are the platform defaults and the rows are the stored verdict. */
  are_default: boolean;
  /** True => read-only projection; nothing was saved. */
  recomputed_read_only: boolean;
  /** Show verbatim. Do not paraphrase. */
  explanation: string;
  /** Customers skipped by a recompute (no Business Unit mapped). */
  skipped_customers: string[];
}

export interface CoverageGridRow {
  well_id: string;
  well_name: string;
  planning_node_id: string;
  planning_node_path: string;
  customer_id: string;
  customer_name: string;
  /** The well's demand status. Filtering by status now excludes/includes whole wells. */
  demand_status: string;
  in_scope_line_count: number;
  covered_line_count: number;
  coverage_status: string | null;
  evaluated: boolean;
  earliest_ros_date: string | null;
  first_runout_date: string | null;
}

export interface CoverageGridResponse {
  filters: CoverageGridFilters;
  well_count: number;
  rows: CoverageGridRow[];
  /** Oldest / newest computed_at across the verdicts shown (C-08). */
  verdicts_computed_from: string | null;
  verdicts_computed_to: string | null;
}

export interface CoverageGridQuery {
  customer_id?: string;
  coverage_status?: string;
  status?: string[];
  profile?: string[];
}

function coverageGridQuery(p: CoverageGridQuery): string {
  const pairs: [string, string | number | undefined][] = [
    ["customer_id", p.customer_id],
    ["coverage_status", p.coverage_status],
  ];
  for (const v of p.status ?? []) pairs.push(["status", v]);
  for (const v of p.profile ?? []) pairs.push(["profile", v]);
  return search(pairs);
}

// ---------------------------------------------------------------------------
// Demand Import  (Upload -> Staging -> Match -> Review -> Apply)
//
// Upload STAGES and SUGGESTS; it mutates no demand. `match_type` is the
// matcher's suggestion, `decision` is the user's answer, and only the latter
// drives apply. Nothing is ever auto-applied.
// ---------------------------------------------------------------------------

export type DemandImportBatchStatus = "Staged" | "Applied";

/** The matcher's SUGGESTION. "Error" rows may only be skipped (API 400s). */
export type DemandImportMatchType = "Revision" | "New" | "Error";

/** The USER's decision. "Pending" until they make one. */
export type DemandImportDecision =
  | "Pending"
  | "AcceptRevision"
  | "AcceptNew"
  | "Skip";

export interface DemandImportRow {
  id: string;
  /** 1-based spreadsheet row number, so the user can find it in Excel. */
  row_number: number;
  raw_well: string | null;
  raw_product: string | null;
  raw_quantity: string | null;
  raw_ros_date: string | null;
  raw_status: string | null;
  raw_profile: string | null;
  well_id: string | null;
  well_name: string | null;
  product_id: string | null;
  product_description: string | null;
  quantity: number | null;
  ros_date: string | null;
  status: string | null;
  profile: string | null;
  match_type: DemandImportMatchType;
  /**
   * May be non-null even when match_type is "New": the backend attaches the
   * nearest candidate precisely so the planner can overrule the suggestion and
   * accept the row as a revision instead.
   */
  matched_demand_line_id: string | null;
  matched_quantity: number | null;
  matched_ros_date: string | null;
  matched_status: string | null;
  matched_profile: string | null;
  match_reason: string | null;
  error: string | null;
  decision: DemandImportDecision;

  /**
   * CONFLICT — this row disagrees with the LIVE book. Computed fresh by the server
   * on every read, never a stored flag: live data keeps moving after staging.
   *
   * A conflict is NOT an error. `match_type === "Error"` means the FILE is wrong and
   * the row may only be skipped. A conflict row is perfectly valid; it merely
   * disagrees with what it would land on, in one of the two ways where the row's own
   * diff is NOT the change that would happen. An ordinary revision carrying a
   * different quantity is deliberately not a conflict.
   */
  is_conflict: boolean;
  conflict_kind: "WellDemandStatus" | "ConcurrentRevision" | null;
  conflict_field: string | null;
  conflict_current_value: string | null;
  conflict_file_value: string | null;
  conflict_detail: string | null;
  conflict_cascade_line_count: number;
  /** True when Apply would refuse this row as things stand. Gate on THIS, not on
   * `is_conflict` — which stays true after the override is approved. */
  requires_override_approval: boolean;

  override_approved: boolean;
  override_approved_at: string | null;
  /** Attribution only — no server logic branches on it. */
  override_approved_by: string | null;

  /** What was live when the row was staged. baseline -> current -> file is three
   * values; the two-column diff only has room for two. */
  baseline_revision_no: number | null;
  baseline_quantity: number | null;
  baseline_ros_date: string | null;
  /** The matched line's revision number RIGHT NOW. Differs from the baseline
   * exactly when somebody has revised it since staging. */
  current_revision_no: number | null;

  applied: boolean;
  applied_demand_line_id: string | null;
  apply_error: string | null;
}

/** One line, before -> after, in a conflict preview. NOTHING here is persisted. */
export interface ImportConflictLineChange {
  demand_line_id: string;
  well_id: string;
  well_name: string;
  product_id: string;
  product_description: string | null;
  unit_of_measure: string | null;
  coverage_before: string;
  coverage_after: string;
  reason_before: string | null;
  reason_after: string | null;
  quantity_before: number;
  quantity_after: number;
  ros_date_before: string;
  ros_date_after: string;
  changed: boolean;
  /** False marks a line dragged along by the well-status cascade — the lines the
   * spreadsheet never mentioned, which the row's own diff cannot show. */
  named_by_the_row: boolean;
}

export interface ImportConflictWellChange {
  well_id: string;
  well_name: string;
  coverage_before: string | null;
  coverage_after: string | null;
  changed: boolean;
}

/**
 * READ-ONLY what-if: coverage impact of approving ONE conflicting import row.
 *
 * `is_what_if` is always true and must be rendered as such. No number in here is
 * the coverage verdict — the same rule the scenario preview and the cross-customer
 * sharing panel follow.
 */
export interface ImportConflictPreview {
  batch_id: string;
  row_id: string;
  row_number: number;
  conflict_kind: string;
  conflict_field: string;
  conflict_current_value: string;
  conflict_file_value: string;
  conflict_detail: string;
  customer_id: string;
  customer_name: string;
  well_id: string | null;
  well_name: string | null;
  revised_line_ids: string[];
  cascade_line_count: number;
  line_changes: ImportConflictLineChange[];
  well_changes: ImportConflictWellChange[];
  covered_lines_before: number;
  covered_lines_after: number;
  unrecoverable_lines_before: number;
  unrecoverable_lines_after: number;
  changed_line_count: number;
  changed_well_count: number;
  is_what_if: boolean;
  notes: string[];
}

export interface DemandImportColumnContract {
  required: string[];
  optional: string[];
}

export interface DemandImportBatch {
  id: string;
  filename: string | null;
  sheet_name: string | null;
  status: DemandImportBatchStatus;
  row_count: number;
  error_count: number;
  /** Rows the user has not decided. Apply leaves these alone. */
  pending_count: number;
  revision_suggestion_count: number;
  new_suggestion_count: number;
  /** Rows that disagree with the live book. */
  conflict_count: number;
  /** Of those, how many are ACCEPTED and still unapproved — i.e. how many are
   * blocking Apply right now. A conflicting row left Pending or Skipped blocks
   * nothing, so it is not counted here. */
  unapproved_conflict_count: number;
  created_at: string;
  applied_at: string | null;
  column_contract: DemandImportColumnContract;
  rows: DemandImportRow[];
}

export interface DemandImportBatchSummary {
  id: string;
  filename: string | null;
  status: DemandImportBatchStatus;
  row_count: number;
  error_count: number;
  created_at: string;
  applied_at: string | null;
}

export interface DemandImportApplyResult {
  batch_id: string;
  status: DemandImportBatchStatus;
  revised_count: number;
  created_count: number;
  skipped_count: number;
  /** Rows still undecided — deliberately untouched, not silently accepted. */
  pending_count: number;
  error_count: number;
  failed_row_ids: string[];
  impact_record_ids: string[];
  batch: DemandImportBatch;
}

// ---------------------------------------------------------------------------
// Administration  (GET /business-units, GET /customers, GET /customers/{id})
//
// All three are READ-ONLY. There is no mutation endpoint for a business unit,
// a customer, or an allocation policy, so the Administration screen is a
// read-only view: an editable-looking control there would be a lie.
//
// `allocation_policy` is the rule by which that customer's coverage is judged,
// and the Business Unit is the hard inventory boundary that is never crossed —
// not even by the cross-customer sharing what-if.
// ---------------------------------------------------------------------------

export interface BusinessUnitOut {
  id: string;
  name: string;
}

/**
 * What `POST /business-units` returns.
 *
 * The note is the point of the body rather than decoration: a brand-new BU has no
 * customers AND no `InventoryOnHand` rows, so it looks configured and is inert. The
 * interesting failure arrives one click later — mapping a customer into it is the
 * remap `PATCH /customers/{id}` refuses and rolls back.
 */
export interface BusinessUnitCreated {
  business_unit: BusinessUnitOut;
  /** Always 0 on creation. Served so the field means the same thing everywhere. */
  inventory_on_hand_row_count: number;
  note: string;
}

/**
 * Change a customer's Business Unit and/or its allocation policy. ONE request.
 *
 * Both fields feed the SAME coverage pass, so a combined change is recomputed once.
 * Sending only one field leaves the other alone; `business_unit_id: null` is an
 * explicit request to UN-map, which the server refuses with a 409 (an unmapped
 * customer has no inventory pool at all and nothing anybody could load would fix it).
 */
export interface CustomerConfigInput {
  business_unit_id?: string | null;
  /** "soft" | "hard" | "hybrid". A bad value is a 400 naming all three, not a 422. */
  allocation_policy?: string;
}

/** What a customer-configuration PATCH actually did. */
export interface CustomerConfigChange {
  customer: CustomerSummary;
  /** True => the values sent were already in force. Nothing written, nothing recomputed. */
  unchanged: boolean;

  business_unit_changed: boolean;
  business_unit_id_before: string | null;
  business_unit_id_after: string | null;
  business_unit_name_before: string | null;
  business_unit_name_after: string | null;

  allocation_policy_changed: boolean;
  allocation_policy_before: string;
  allocation_policy_after: string;

  /** Denominator for `well_changes` — how many of this customer's wells were visited. */
  wells_examined: number;
  well_changes: WellCoverageRollupChange[];
  /** 1 for a real change INCLUDING one that moved both fields; 0 for a no-op. */
  recomputes_performed: number;

  /** False => this customer's coverage could not be resolved before the change either. */
  coverage_resolvable_before: boolean;
  /**
   * Non-null => coverage STILL cannot be resolved, so the stored verdicts were rolled
   * back and now predate this change. Only ever set for a policy-only change; a BU
   * change that ends unresolvable is refused outright. A real caveat on everything else.
   */
  unresolved_reason: string | null;

  note: string;
}

/** "soft" | "hard" | "hybrid" — served as free text, so keep it widened. */
export type AllocationPolicy = string;

export interface ProductQuery {
  /** Case-insensitive substring filter on the description. */
  q?: string;
  /** Server default is true. Only sent when explicitly set to false. */
  active_only?: boolean;
}

// ---------------------------------------------------------------------------
// Executive dashboard  (GET /dashboard/executive)
//
// This payload goes to management, and its whole design is that every block
// carries an `available` flag plus a `reason`. When `available` is false the
// figure is `null` — the backend refuses to substitute a zero for an unknown.
//
// The UI's obligation is the mirror image: render the `reason` where the number
// would have gone. A 0, a dash, or an empty chart on a management screen turns
// "we could not compute this" into "this is fine", which is the single worst
// failure mode in this application.
//
// One distinction the types have to keep visible: a genuine prior total of 0 is
// reported as `{available: true, value: 0}` — a real measurement — while the
// PERCENTAGE change from zero comes back `{available: false}` because it is
// mathematically undefined. "0" and "unavailable" are different renderings.
// ---------------------------------------------------------------------------

/** A figure that may not exist. `value` is null exactly when `available` is false. */
export interface Figure {
  available: boolean;
  value: number | null;
  /** Prose written for a manager. Render verbatim in place of the number. */
  reason: string | null;
}

export interface DemandTrendHorizon {
  months: number;
  window_start: string;
  window_end: string;
  /** Always a real measurement — the current book is always countable. */
  current_total: number;
  current_line_count: number;
  unit_of_measure?: UnitOfMeasure | null;
  quantities_by_unit?: QuantityByUnit[];
  /**
   * Reconstructed from DemandRevision history, so it is UNAVAILABLE for rows
   * created before that history existed.
   */
  previous: Figure;
  previous_as_of: string;
  /** Undefined (not zero) when `previous.value` is 0. */
  change_pct: Figure;
  /** Display-ready copy explaining what the comparison means. Show it. */
  definition: string;
  /** MT headline: display-layer conversion (C-05), floor semantics. */
  current_tonnes: Figure;
  previous_tonnes: Figure;
  /** Exists even for mixed-unit horizons — tonnes are one unit. */
  change_pct_tonnes: Figure;
}

export interface DemandTrend {
  horizons: DemandTrendHorizon[];
  notes: string[];
}

/** One (unit, quantity) breakdown entry. Present wherever a total can span units. */
export interface QuantityByUnit {
  unit_of_measure: UnitOfMeasure;
  quantity: number;
}

/** Resolved scope the backend computed for this request — render this, never reconstruct it. */
export interface ExecutiveScope {
  business_unit_id: string | null;
  business_unit_name: string | null;
  customer_id: string | null;
  customer_name: string | null;
  label: string;
  description: string;
  customer_ids: string[];
}

export interface CoverageByStatus {
  status: string;
  quantity: number;
  line_count: number;
  well_count: number;
  counts_as_covered: boolean;
  unit_of_measure: UnitOfMeasure | null;
  quantities_by_unit: QuantityByUnit[];
  /** MT headline: display-layer conversion (C-05), floor semantics. */
  tonnes: Figure;
}

/**
 * Coverage is now primarily QUANTITY-based. `coverage_pct` is the quantity
 * ratio and is the headline; `well_coverage_pct` and the four well counts
 * survive as a REFERENCE row, demoted from the headline they used to be — a
 * well short by 50 and a well short by 40,000 count alike by well and nothing
 * alike by quantity.
 */
export interface ExecutiveCoverage {
  available: boolean;
  coverage_pct: number | null;
  covered_quantity: number | null;
  total_quantity: number | null;
  unit_of_measure: UnitOfMeasure | null;
  covered_quantities_by_unit: QuantityByUnit[];
  total_quantities_by_unit: QuantityByUnit[];
  by_status: CoverageByStatus[];
  well_coverage_pct: number | null;
  covered_well_count: number | null;
  uncovered_well_count: number | null;
  evaluated_well_count: number | null;
  unevaluated_well_count: number | null;
  in_scope_line_count: number | null;
  reason: string | null;
  /** MT headline: display-layer conversion (C-05), floor semantics. */
  covered_tonnes: Figure;
  total_tonnes: Figure;
  coverage_pct_tonnes: Figure;
}

export interface ExecutiveSupplyRisk {
  available: boolean;
  /** Demand not coverable even if ordered today. */
  unrecoverable_quantity: number | null;
  unrecoverable_line_count: number | null;
  affected_well_count: number | null;
  unrecoverable_pct_of_in_scope_demand: number | null;
  unit_of_measure: UnitOfMeasure | null;
  quantities_by_unit: QuantityByUnit[];
  reason: string | null;
  /** The on-order caveat. Must be visible, not a tooltip. */
  note: string | null;
  /** MT headline: display-layer conversion (C-05), floor semantics. */
  unrecoverable_tonnes: Figure;
}

/**
 * This platform's OWN soft-allocation coverage — NOT Oracle's hard
 * allocation. Replaces the old `ExecutiveAllocation` / `allocation` block
 * entirely. Channels partition demand in draw-priority order; render all
 * five, in the order the backend sends them.
 */
export interface SoftAllocationChannel {
  key: string;
  label: string;
  quantity: number;
  line_count: number;
  pct: number;
  unit_of_measure: UnitOfMeasure | null;
  quantities_by_unit: QuantityByUnit[];
  /** MT headline: display-layer conversion (C-05), floor semantics. */
  tonnes: Figure;
}

export interface SoftAllocationCoverage {
  horizon_months: number;
  available: boolean;
  total_quantity: number | null;
  total_line_count: number | null;
  channels: SoftAllocationChannel[];
  unit_of_measure: UnitOfMeasure | null;
  quantities_by_unit: QuantityByUnit[];
  /** Provenance for the assignment-drawing channels. */
  assignment_source: string | null;
  unresolved_customer_ids: string[];
  reason: string | null;
  /** The anti-confusion sentence — "this is NOT Oracle's hard allocation". Render verbatim. */
  note: string | null;
  /** MT headline: display-layer conversion (C-05), floor semantics. */
  total_tonnes: Figure;
}

export interface FirstRunoutWell {
  well_id: string;
  well_name: string;
  customer_id: string;
  customer_name: string;
  business_unit_id: string;
  first_runout_date: string;
  shortfall_quantity: number;
  shortfall_line_count: number;
  unit_of_measure: UnitOfMeasure | null;
  quantities_by_unit: QuantityByUnit[];
  /** MT headline: display-layer conversion (C-05), floor semantics. */
  shortfall_tonnes: Figure;
}

export interface FirstRunout {
  available: boolean;
  wells: FirstRunoutWell[];
  total_well_count: number | null;
  returned_well_count: number | null;
  cap: number | null;
  omitted_well_count: number | null;
  truncated: boolean;
  earliest_first_runout_date: string | null;
  reason: string | null;
  note: string | null;
}

export interface IncomingSupplyHorizon {
  months: number;
  window_end: string;
  quantity: number;
  row_count: number;
  unit_of_measure: UnitOfMeasure | null;
  quantities_by_unit: QuantityByUnit[];
  /** MT headline: display-layer conversion (C-05), floor semantics. */
  tonnes: Figure;
}

/** On-order data. Follow `available`/`reason` honesty exactly — never render a bare 0. */
export interface IncomingSupply {
  available: boolean;
  quantity: number | null;
  unit_of_measure: UnitOfMeasure | null;
  quantities_by_unit: QuantityByUnit[];
  row_count: number | null;
  product_count: number | null;
  products_with_no_data: number | null;
  products_with_nothing_on_order: number | null;
  earliest_expected_arrival: string | null;
  latest_expected_arrival: string | null;
  undated_quantity: number | null;
  by_arrival_horizon: IncomingSupplyHorizon[];
  /** MT headline: display-layer conversion (C-05), floor semantics. */
  tonnes: Figure;
  undated_tonnes: Figure;
  /** "synthetic" | "oracle" (or similar) — provenance of the on-order rows. */
  source: string | null;
  oracle_integrated: boolean;
  reason: string | null;
  note: string | null;
}

// ---------------------------------------------------------------------------
// Inventory utilisation  (part of GET /dashboard/executive)
//
// Of company-owned steel standing in the yard, how much is TIED to in-window
// demand and how much is idle. THE DENOMINATOR IS INVENTORY, NOT DEMAND — the
// opposite of `SoftAllocationCoverage`, whose denominator is demand quantity.
// Read the two blocks as answering different questions; do not average them.
//
// `tied_tonnes` / `not_tied_tonnes` are a metric-tonnes CONVENIENCE over
// `tied_by_unit` / `not_tied_by_unit`. `available=false` on either measure
// means nothing on that side could be converted; `available=true` with a
// `reason` means the total is PARTIAL and `unconvertible` names what was
// excluded and why (PC-only products, or a null `Product.weight`, per C-06).
// A product with in-scope demand but no on-hand row is reported in
// `unknown_position`, never folded into either total as a zero.
// ---------------------------------------------------------------------------

/** One product's on-hand split into tied-to-demand vs idle, this horizon. */
export interface InventoryUtilisationProductOut {
  business_unit_id: string;
  product_id: string;
  product_description: string | null;
  unit_of_measure: UnitOfMeasure;
  on_hand_quantity: number;
  demand_in_window: number;
  /** Overdue portion of demand_in_window (ROS passed) — counted, labelled. */
  demand_overdue: number;
  /** Netted out of demand FIRST, same draw order as the soft-allocation channel. */
  customer_owned_quantity: number;
  /** `tied + not_tied === on_hand_quantity` exactly. */
  tied: number;
  not_tied: number;
  /** Per-row MT conversions (C-05); unavailable = cannot convert. */
  tied_tonnes: Figure;
  not_tied_tonnes: Figure;
}

/** A tied-or-not_tied quantity that could not be converted to metric tonnes. */
export interface UnconvertibleQuantityOut {
  business_unit_id: string;
  product_id: string;
  product_description: string | null;
  unit_of_measure: UnitOfMeasure;
  /** "tied" | "not_tied". */
  side: string;
  quantity: number;
  reason: string;
}

/** In-scope demand exists but this (BU, product) has no on-hand row at all. */
export interface UnknownPositionProductOut {
  business_unit_id: string;
  product_id: string;
  product_description: string | null;
  unit_of_measure: UnitOfMeasure;
}

export interface InventoryUtilisationOut {
  horizon_months: number;
  available: boolean;
  /** `Figure`-shaped: render `reason` in place of the number when `available` is false. */
  tied_tonnes: Figure;
  not_tied_tonnes: Figure;
  products: InventoryUtilisationProductOut[];
  tied_by_unit: QuantityByUnit[];
  not_tied_by_unit: QuantityByUnit[];
  unconvertible: UnconvertibleQuantityOut[];
  unknown_position: UnknownPositionProductOut[];
  unknown_position_count: number;
  reason: string | null;
  /** The anti-confusion sentence distinguishing this from the removed allocation block. Render verbatim. */
  note: string;
}

export interface ExecutiveDashboard {
  generated_at: string;
  customer_id: string | null;
  business_unit_id: string | null;
  business_unit_name: string | null;
  customer_name: string | null;
  scope: ExecutiveScope;
  demand_trend: DemandTrend;
  coverage: ExecutiveCoverage;
  supply_risk: ExecutiveSupplyRisk;
  soft_allocation_coverage: SoftAllocationCoverage;
  first_runout: FirstRunout;
  incoming_supply: IncomingSupply;
  inventory_utilisation: InventoryUtilisationOut;
  notes: string[];
  /** The demand scope EVERY block was computed under. `scope_is_default`
   *  false = read-only recompute, not the stored official verdicts. */
  status_scope: string[];
  profile_scope: string[];
  scope_is_default: boolean;
  /** Customers a scoped recompute skipped (no BU / missing on-hand row). */
  skipped_customers: string[];
}

/** The allocation horizons the endpoint accepts. Anything else 400s. */
export const ALLOCATION_HORIZONS = [12, 18, 24, 36] as const;
export type AllocationHorizon = (typeof ALLOCATION_HORIZONS)[number];

export interface ExecutiveQuery {
  customer_id?: string;
  business_unit_id?: string;
  allocation_horizon_months?: AllocationHorizon;
  /** Horizon for `inventory_utilisation`. Independent of `allocation_horizon_months`. */
  inventory_utilisation_horizon_months?: number;
  /** Demand scope overrides. Omitted = the platform's coverage-scope default. */
  status?: string[];
  profile?: string[];
}

// ---------------------------------------------------------------------------
// Cross-customer sharing  (GET /analysis/cross-customer-sharing)
//
// A READ-ONLY WHAT-IF that deliberately disagrees with the official coverage
// badge. It answers "could this customer's uncovered demand be met if inventory
// were shared across customers inside the same Business Unit?" — a question the
// official, customer-scoped verdict does not and must not answer. Rendering it
// indistinguishably from the official verdict would undo the reason the verdict
// is customer-scoped at all, so every screen showing it labels it a projection
// with the same `.whatif-banner` / `.whatif-tag` vocabulary as Scenarios.
//
// Two traps:
//
//   * `contributions` may legitimately be EMPTY while `would_be_covered` is
//     true. That means the surplus is BU-level unallocated stock, not a transfer
//     from a named customer. `explanation` says exactly that — render it rather
//     than an empty donor list.
//   * `shared_quantity` is the binding number. The contribution split is an
//     INDICATIVE hint about who to call, not an instruction.
//
// The BU is a hard boundary: stock in another Business Unit is never offered,
// whatever its quantity.
// ---------------------------------------------------------------------------

export interface SharingContribution {
  from_customer_id: string;
  from_customer_name: string;
  quantity: number;
}

export interface SharingUncoveredLine {
  demand_line_id: string;
  well_id: string;
  well_name: string;
  product_id: string;
  product_description: string | null;
  quantity: number;
  ros_date: string;
  /** The official verdict, e.g. "Uncovered (customer-scoped)". Unchanged by this. */
  official_status: string;
  /** The PROJECTION. Never render as the official badge. */
  would_be_covered: boolean;
  /** The binding figure. */
  shared_quantity: number;
  shortfall: number;
  /** Indicative only, and legitimately empty for BU-level unallocated stock. */
  contributions: SharingContribution[];
  /** Full sentence written for a planner. Render verbatim. */
  explanation: string;
}

export interface ProductSurplus {
  product_id: string;
  product_description: string | null;
  bu_on_hand: number;
  committed_in_bu: number;
  shareable: number;
}

export interface CrossCustomerSharing {
  customer_id: string;
  customer_name: string;
  business_unit_id: string | null;
  business_unit_name: string | null;
  /** Other customers in the same BU holding shareable stock. May be empty. */
  donor_customer_ids: string[];
  uncovered_lines: SharingUncoveredLine[];
  product_surplus: ProductSurplus[];
  covered_by_sharing_count: number;
  still_uncovered_count: number;
  notes: string[];
}

// ---------------------------------------------------------------------------
// Customer-owned inventory  (GET/POST /customer-owned-inventory/{customer_id})
//
// This is the ONE inventory table this platform itself owns via upload —
// on-hand, assignments and on-order are all read-only Oracle projections.
// `has_uploaded` is the whole point and must be branched on before anything
// else:
//   has_uploaded=false                 -> no upload has ever happened. Never 0.
//   has_uploaded=true, positions=[]     -> uploaded, and genuinely owns nothing.
//   has_uploaded=true, positions=[...]  -> render the table.
// Consumed FIRST for its product, ahead of company stock and any Oracle
// assignment, and never offered to another customer.
// ---------------------------------------------------------------------------

export interface CustomerOwnedPosition {
  product_id: string;
  product_description: string | null;
  quantity: number;
  unit_of_measure: UnitOfMeasure;
  source_system: string;
  source_reference: string | null;
  uploaded_at: string;
}

export interface CustomerOwnedInventoryOut {
  customer_id: string;
  customer_name: string;
  business_unit_id: string | null;
  has_uploaded: boolean;
  last_uploaded_at: string | null;
  positions: CustomerOwnedPosition[];
  note: string;
}

export interface CustomerOwnedUploadSummary {
  id: string;
  customer_id: string;
  filename: string | null;
  sheet_name: string | null;
  row_count: number;
  applied_count: number;
  created_count: number;
  replaced_count: number;
  error_count: number;
  uploaded_at: string;
  source_system: string;
}

/** One spreadsheet row's outcome. `action` is "Created" | "Replaced" | "Error". */
export interface CustomerOwnedUploadRow {
  row_number: number;
  action: string;
  raw_product: string | null;
  raw_quantity: string | null;
  product_id: string | null;
  product_description: string | null;
  quantity: number | null;
  previous_quantity: number | null;
  unit_of_measure: UnitOfMeasure | null;
  error: string | null;
}

/** column_contract values are {before, after} per well, e.g. ["Uncovered", "Covered"]. */
export interface CustomerOwnedUploadResult {
  upload_id: string;
  customer_id: string;
  filename: string | null;
  sheet_name: string | null;
  row_count: number;
  created_count: number;
  replaced_count: number;
  applied_count: number;
  error_count: number;
  rows: CustomerOwnedUploadRow[];
  /** well_id -> [status_before, status_after]. Non-empty entries are the consequence to surface. */
  coverage_changes: Record<string, (string | null)[]>;
  column_contract: { required?: string[]; optional?: string[] };
}

// ---------------------------------------------------------------------------
// Administration — the platform's two ADJUSTABLE assumptions
//
// Every other setting reachable from the Administration screen (Business Unit,
// customer, allocation policy) is read-only, maintained upstream, and has no
// mutation endpoint. These two are different in kind: THIS platform owns them.
// Lead-time components are its own modelling assumptions — Oracle holds no such
// table, and they arrived by being typed into a seed script. The coverage scope
// default is a statement about how this platform evaluates demand, and was a
// Python constant editable only by a deploy.
//
// Both are CONSEQUENTIAL. Every write below can move a coverage verdict, and the
// scope PUT moves every verdict on the platform at once. The backend performs the
// change and reports its blast radius rather than refusing — so each response type
// carries a before/after diff, and the screen must render it. See app/api/admin.py.
// ---------------------------------------------------------------------------

/** One additive lead-time term. Four dimensions sum to a product's lead time. */
export interface LeadTimeComponentRow {
  id: string;
  /** "OD/WT" | "Grade" | "Connection" | "Logistics" — a closed set. */
  dimension: string;
  /** The attribute value this term applies to, or "*" for the wildcard row. */
  attribute_value: string;
  months: number;
  /** Presentation only. Never matched on, never affects arithmetic. */
  label: string | null;
  /**
   * Server-DERIVED: true exactly when `attribute_value` is the wildcard, meaning
   * the row applies to every product on its dimension. Served rather than inferred
   * client-side so the admin table and the lead-time breakdown cannot disagree
   * about which rows are shared.
   */
  shared: boolean;
}

export interface LeadTimeComponentsResponse {
  components: LeadTimeComponentRow[];
  /** The four permitted dimensions, in breakdown order. Render the select from THIS. */
  dimensions: string[];
  /** The wildcard sentinel, "*". Never hard-code it. */
  wildcard: string;
  /**
   * Dimensions with NO row at all. While this is non-empty EVERY product on the
   * platform is "not modelled" — the table looks healthy and is not, which is why
   * the server computes this rather than leaving the screen to notice.
   */
  dimensions_with_no_rows: string[];
  incomplete_note: string | null;
}

export interface LeadTimeComponentInput {
  dimension: string;
  attribute_value: string;
  months: number;
  label?: string | null;
}

/**
 * PATCH body. `months` and `label` only — deliberately not `dimension` or
 * `attribute_value`.
 *
 * That pair is the row's IDENTITY: it is the only thing the resolver matches on, so
 * changing it re-points the term at a different set of products rather than editing
 * it. The server refuses to do that in a PATCH; re-pointing is DELETE + POST, so the
 * destructive half reports what it broke.
 */
export interface LeadTimeComponentPatchInput {
  months?: number;
  label?: string | null;
}

/** One product whose resolved lead time moved because a component changed. */
export interface ProductLeadTimeChange {
  product_id: string;
  product_description: string | null;
  total_months_before: number;
  total_months_after: number;
  modelled_before: boolean;
  modelled_after: boolean;
  /** Why it is not modelled any more. Empty when `modelled_after` is true. */
  missing_dimensions_after: string[];
  /**
   * The consequential one. A product that became unmodelled has no order date and
   * can no longer be judged Unrecoverable — so any Unrecoverable verdict resting on
   * it has been RETRACTED.
   */
  became_unmodelled: boolean;
  became_modelled: boolean;
}

export interface WellCoverageRollupChange {
  well_id: string;
  well_name: string;
  coverage_before: CoverageStatus;
  coverage_after: CoverageStatus;
}

/** What a component create / update / delete actually did. Render the diff. */
export interface LeadTimeComponentChange {
  /** "created" | "updated" | "deleted". */
  action: string;
  /** Null for a delete — the row is gone, so there is nothing to render. */
  component: LeadTimeComponentRow | null;
  /** Denominator for `product_changes`: how many products were re-resolved. */
  products_examined: number;
  product_changes: ProductLeadTimeChange[];
  recomputed_customer_ids: string[];
  well_changes: WellCoverageRollupChange[];
  /** Server prose covering the whole change. Show it verbatim. */
  note: string;
}

/** The coverage scope the engine is evaluating under right now. */
export interface CoverageScopeDefaults {
  /** DemandStatus values in scope. Selects whole WELLS. */
  status_filter: string[];
  /** DemandProfile values in scope. Selects LINES, cutting inside a well. */
  profile_filter: string[];
  /** Every value the server will accept. Render checkboxes from these, not a literal. */
  available_statuses: string[];
  available_profiles: string[];
  shipped_status_filter: string[];
  shipped_profile_filter: string[];
  /**
   * True => somebody has adjusted the scope and a row exists. This is NOT derivable
   * from the filter values: an administrator may save the shipped values back, so
   * "adjusted, and happens to match what shipped" is a different fact from "never
   * adjusted".
   */
  persisted: boolean;
  updated_at: string | null;
  matches_shipped_default: boolean;
  note: string;
}

export interface CoverageScopeDefaultsInput {
  status_filter: string[];
  profile_filter: string[];
}

/** What saving the coverage scope did. Every field here is evidence, not decoration. */
export interface CoverageScopeDefaultsChange {
  status_filter_before: string[];
  profile_filter_before: string[];
  status_filter_after: string[];
  profile_filter_after: string[];
  /** True => that scope was already in effect; nothing written, nothing recomputed. */
  unchanged: boolean;
  /** True => every customer was recomputed inside this request. */
  recomputed_synchronously: boolean;
  recomputed_customer_ids: string[];
  /** How many wells were VISITED — the denominator for `well_changes`. */
  recomputed_well_count: number;
  /** Only the wells whose rollup MOVED. */
  well_changes: WellCoverageRollupChange[];
  note: string;
}


// ---------------------------------------------------------------------------
// Substitution master data  (/admin/technical-substitutions,
//                            /admin/customer-substitution-rules)
//
// The two tables the substitution engine GATES on. Layer 1 is an engineering
// compatibility claim; layer 2 is whether a customer permits that pair. Both were
// seed-only until now.
//
// TWO THINGS THIS UI MUST NOT GET WRONG, both of them server-stated:
//
//   * Registering a technical substitution does NOT make a substitution usable. It
//     makes the substitute OFFERED. Layers 2 and 3 must clear as well.
//   * Layer 2 is a TRUE ALLOW-LIST. A pair with no rule is blocked exactly as an
//     `allowed: false` rule blocks it — so deleting a permitting rule REVERTS the
//     substitution to blocked. Never render a delete here as a cleanup.
//
// Every mutation returns a before/after diff, same as the lead-time endpoints, because
// every mutation can move a stored coverage verdict. Render it.
// ---------------------------------------------------------------------------

/**
 * One customer whose coverage could NOT be re-derived after a substitution edit.
 *
 * The backend refuses to invent an on-hand quantity, and a technical substitution is
 * BU-agnostic while the recompute is BU-scoped — so registering a pair can require an
 * inventory row some Business Unit never received. The write proceeds anyway (one
 * warehouse's missing feed must not veto an engineering fact) and this says whose
 * stored verdicts therefore still PREDATE the change. Render it; a screen that shows
 * only `well_changes` would imply those customers were unaffected.
 */
export interface CoverageRecomputeFailure {
  customer_id: string;
  customer_name: string;
  /** The server's own message: names the product, the BU and the fix. Show verbatim. */
  reason: string;
}

/** One layer-1 engineering claim. DIRECTIONAL: A→B does not imply B→A. */
export interface TechnicalSubstitutionRow {
  id: string;
  from_product_id: string;
  /** Joined from Product. Null only when the product itself has no description. */
  from_product_description: string | null;
  to_product_id: string;
  to_product_description: string | null;
  /** Customer rules naming this pair, and how many of them permit it. */
  customer_rule_count: number;
  allowing_customer_rule_count: number;
  /** Well-layer approvals naming this pair, and how many are Approved. */
  well_approval_count: number;
  approved_well_approval_count: number;
}

export interface TechnicalSubstitutionsResponse {
  substitutions: TechnicalSubstitutionRow[];
  /**
   * Rows no customer rule permits. They offer a candidate every demand line will
   * report as blocked at the customer layer — the table looks configured and is not,
   * which is why the server counts this rather than leaving the screen to notice.
   */
  unpermitted_count: number;
  note: string;
}

export interface TechnicalSubstitutionInput {
  from_product_id: string;
  to_product_id: string;
}

/** What registering or withdrawing a layer-1 claim did. */
export interface TechnicalSubstitutionChange {
  /** "created" | "deleted". */
  action: string;
  /** Null for a delete — the row is gone, so there is nothing to render. */
  substitution: TechnicalSubstitutionRow | null;
  well_approvals_affected: number;
  /**
   * The number that makes a delete honest: customer decisions on specific demand
   * lines that were relying on this pair. The rows are KEPT as history and stop
   * having any effect.
   */
  approved_well_approvals_affected: number;
  pending_well_approvals_affected: number;
  customer_rules_affected: number;
  allowing_customer_rules_affected: number;
  /**
   * A customer ABSENT here is either in `coverage_recompute_failures` or does not
   * exist. Absence never means "nothing to do".
   */
  recomputed_customer_ids: string[];
  /** Denominator for `well_changes`. */
  wells_examined: number;
  well_changes: WellCoverageRollupChange[];
  /** Non-empty is a real caveat on every other number in the report. */
  coverage_recompute_failures: CoverageRecomputeFailure[];
  note: string;
}

/** One layer-2 rule: whether THIS customer permits THIS pair. */
export interface CustomerSubstitutionRuleRow {
  id: string;
  customer_id: string;
  customer_name: string | null;
  from_product_id: string;
  from_product_description: string | null;
  to_product_id: string;
  to_product_description: string | null;
  allowed: boolean;
  /**
   * False => this rule has NO effect at all: the candidate list is built from the
   * technical rows and a rule outside that set is never consulted. Show it as
   * inert; do not show it as live configuration.
   */
  technical_substitution_exists: boolean;
  /** Why it is inert, in the server's words. Null when it is live. */
  inert_reason: string | null;
}

export interface CustomerSubstitutionRulesResponse {
  rules: CustomerSubstitutionRuleRow[];
  /** Echoed back, so the screen can prove it is showing a filtered list. */
  customer_id: string | null;
  inert_count: number;
  note: string;
}

export interface CustomerSubstitutionRuleInput {
  customer_id: string;
  from_product_id: string;
  to_product_id: string;
  /** Required, never defaulted — see the server schema for why in both directions. */
  allowed: boolean;
}

/** What creating, flipping or removing a layer-2 rule did. */
export interface CustomerSubstitutionRuleChange {
  /** "created" | "updated" | "deleted". */
  action: string;
  rule: CustomerSubstitutionRuleRow | null;
  /**
   * Null means the row did NOT EXIST on that side of the change — so a create reads
   * `null → true` and a delete reads `true → null`. Null is not a third permission
   * value; an absent rule is treated as not permitted.
   */
  allowed_before: boolean | null;
  allowed_after: boolean | null;
  /** Non-null => the change was MADE and something about it needs saying. */
  warning: string | null;
  recomputed_customer_ids: string[];
  wells_examined: number;
  well_changes: WellCoverageRollupChange[];
  /** Non-empty is a real caveat on every other number in the report. */
  coverage_recompute_failures: CoverageRecomputeFailure[];
  note: string;
}

/** DELETE that returns a body. `deleteJSON` discards it; this change report must not be. */
async function deleteWithBody<T>(path: string): Promise<T> {
  const res = await authFetch(`${API_BASE}${path}`, { method: "DELETE" });
  if (!res.ok) throw await detailedError(res, path);
  return res.json();
}

export const adminApi = {
  getLeadTimeComponents: () =>
    getJSON<LeadTimeComponentsResponse>("/admin/lead-time-components"),
  createLeadTimeComponent: (body: LeadTimeComponentInput) =>
    postDetail<LeadTimeComponentChange>("/admin/lead-time-components", body),
  updateLeadTimeComponent: (id: string, body: LeadTimeComponentPatchInput) =>
    patchJSON<LeadTimeComponentChange>(
      `/admin/lead-time-components/${encodeURIComponent(id)}`,
      body
    ),
  deleteLeadTimeComponent: (id: string) =>
    deleteWithBody<LeadTimeComponentChange>(
      `/admin/lead-time-components/${encodeURIComponent(id)}`
    ),
  getCoverageScopeDefaults: () =>
    getJSON<CoverageScopeDefaults>("/admin/coverage-scope-defaults"),
  setCoverageScopeDefaults: (body: CoverageScopeDefaultsInput) =>
    putJSON<CoverageScopeDefaultsChange>("/admin/coverage-scope-defaults", body),

  // ---- Substitution master data ----
  getTechnicalSubstitutions: () =>
    getJSON<TechnicalSubstitutionsResponse>("/admin/technical-substitutions"),
  createTechnicalSubstitution: (body: TechnicalSubstitutionInput) =>
    postDetail<TechnicalSubstitutionChange>("/admin/technical-substitutions", body),
  deleteTechnicalSubstitution: (id: string) =>
    deleteWithBody<TechnicalSubstitutionChange>(
      `/admin/technical-substitutions/${encodeURIComponent(id)}`
    ),
  /** `customerId` omitted => every customer's rules. */
  getCustomerSubstitutionRules: (customerId?: string) =>
    getJSON<CustomerSubstitutionRulesResponse>(
      `/admin/customer-substitution-rules${search([["customer_id", customerId]])}`
    ),
  createCustomerSubstitutionRule: (body: CustomerSubstitutionRuleInput) =>
    postDetail<CustomerSubstitutionRuleChange>(
      "/admin/customer-substitution-rules",
      body
    ),
  /** Flip `allowed`. A normal commercial change, which is why it is not a delete. */
  setCustomerSubstitutionRuleAllowed: (id: string, allowed: boolean) =>
    patchJSON<CustomerSubstitutionRuleChange>(
      `/admin/customer-substitution-rules/${encodeURIComponent(id)}`,
      { allowed }
    ),
  deleteCustomerSubstitutionRule: (id: string) =>
    deleteWithBody<CustomerSubstitutionRuleChange>(
      `/admin/customer-substitution-rules/${encodeURIComponent(id)}`
    ),
};

// ---------------------------------------------------------------------------
// Company-owned inventory  (GET/PATCH/POST/DELETE /company-inventory/*)
//
// MVP-COMPROMISE[C-03]: on-hand, on-order and Oracle assignments are all
// Oracle-owned domains. This screen exists only because the MVP has no Oracle
// interface — see MVP_COMPROMISES.md C-03. The mutation endpoints below are a
// manual maintenance hole, not a feature this platform is meant to keep once
// the Oracle feed exists: `PLATFORM_MAINTAINABLE_SOURCES` gates them off
// automatically the day a row's `source_system` stops being "synthetic" or
// "manual" (see `editable` / `not_editable_reason` below), with no code change
// on this side required.
//
// THREE RULES EVERY SCREEN CONSUMING THIS MUST OBEY:
//
//   1. Absent is not zero. `CompanyOnHandRowOut.known === false` means no
//      `InventoryOnHand` row exists — the quantity is UNKNOWN, never `0` and
//      never a dash. `quantity: 0` with `known: true` is a different, measured
//      fact ("we hold none") and must look different.
//   2. Every row carries provenance (`source_system`, `synced_at`) so a
//      planner can see at a glance which figures a person typed in.
//   3. `editable` / `not_editable_reason` gate the controls. A row a real feed
//      now owns must show its controls disabled and say why, never hide the
//      row and never leave an enabled control that will 403.
//
// Every write (`CompanyInventoryWriteOut`) returns the coverage consequences
// it caused, exactly like `CustomerOwnedUploadResult` and the admin mutation
// endpoints — a write that changed coverage must say so.
// ---------------------------------------------------------------------------

/** On-hand for one (Business Unit, product). `known=false` means UNKNOWN, never 0. */
export interface CompanyOnHandRowOut {
  business_unit_id: string;
  product_id: string;
  product_description: string | null;
  unit_of_measure: UnitOfMeasure;
  known: boolean;
  /** Null when `known` is false — there is no row to address yet. */
  row_id: string | null;
  /** Null when `known` is false. Do not render as 0. */
  quantity: number | null;
  source_system: string | null;
  synced_at: string | null;
  editable: boolean;
  not_editable_reason: string | null;
}

/**
 * One `InventoryOnOrder` row (one PO line). Several may legitimately exist for
 * the same product — this is per-row, not per-product, which is why on-order
 * has no template/upload path (see the module doc on `api.companyInventory`).
 */
export interface CompanyOnOrderRowOut {
  row_id: string;
  business_unit_id: string;
  product_id: string;
  product_description: string | null;
  unit_of_measure: UnitOfMeasure;
  quantity: number;
  expected_arrival_date: string | null;
  source_system: string;
  source_reference: string | null;
  synced_at: string | null;
  editable: boolean;
  not_editable_reason: string | null;
}

/** One `InventoryAssignment` row: a quantity hard-tied to ONE demand line. */
export interface CompanyAssignmentLineOut {
  row_id: string;
  demand_line_id: string;
  well_id: string;
  well_name: string | null;
  customer_id: string;
  quantity: number;
  unit_of_measure: UnitOfMeasure;
  source_system: string;
  source_reference: string | null;
  synced_at: string | null;
  editable: boolean;
  not_editable_reason: string | null;
}

/** Oracle assignment quantity for one product, aggregated, with its lines. */
export interface CompanyAssignmentGroupOut {
  product_id: string;
  product_description: string | null;
  unit_of_measure: UnitOfMeasure;
  total_quantity: number;
  lines: CompanyAssignmentLineOut[];
}

/** The company-owned inventory position for one Business Unit. */
export interface CompanyInventoryPositionOut {
  business_unit_id: string;
  business_unit_name: string;
  on_hand: CompanyOnHandRowOut[];
  on_order: CompanyOnOrderRowOut[];
  assignments: CompanyAssignmentGroupOut[];
}

/** One customer whose coverage could not be re-derived after a write. Stored
 *  verdicts were rolled back to their prior state and now PREDATE this write. */
export interface RecomputeFailureOut {
  customer_id: string;
  customer_name: string;
  reason: string;
}

/**
 * Before/after for one row write, plus the coverage consequences it caused.
 *
 * `coverage_changes` lists every well whose rollup moved, including wells this
 * write never named directly — an on-hand or on-order edit can move every
 * customer sharing that Business Unit's pool. `recompute_failures` names any
 * customer whose pass could not be re-derived; the write itself still
 * succeeded.
 */
export interface CompanyInventoryWriteOut {
  row_id: string | null;
  before: Record<string, unknown>;
  /** Null on a delete — the row is gone, so there is nothing to render as "after". */
  after: Record<string, unknown> | null;
  recomputed_customer_ids: string[];
  coverage_changes: Record<string, (string | null)[]>;
  recompute_failures: RecomputeFailureOut[];
}

export interface CompanyOnHandEditIn {
  quantity: number;
}

export interface CompanyOnHandCreateIn {
  business_unit_id: string;
  product_id: string;
  quantity: number;
}

export interface CompanyOnOrderEditIn {
  quantity: number;
  expected_arrival_date?: string | null;
}

export interface CompanyOnOrderCreateIn {
  business_unit_id: string;
  product_id: string;
  quantity: number;
  expected_arrival_date?: string | null;
}

export interface CompanyAssignmentEditIn {
  quantity: number;
}

export interface CompanyAssignmentCreateIn {
  demand_line_id: string;
  product_id: string;
  quantity: number;
}

/** What happened to ONE spreadsheet row of the on-hand template/upload. */
export interface CompanyInventoryUploadRowOut {
  row_number: number;
  action: string;
  raw_product: string | null;
  raw_quantity: string | null;
  product_id: string | null;
  product_description: string | null;
  quantity: number | null;
  previous_quantity: number | null;
  unit_of_measure: UnitOfMeasure | null;
  error: string | null;
}

/**
 * The result of one on-hand upload for a Business Unit.
 *
 * `coverage_changes` lists every well whose rollup moved, including wells the
 * file never named — on-hand quantity is the foundation of every coverage
 * verdict for every customer in this Business Unit.
 */
export interface CompanyInventoryUploadOut {
  upload_id: string;
  business_unit_id: string;
  filename: string | null;
  sheet_name: string | null;
  row_count: number;
  created_count: number;
  replaced_count: number;
  applied_count: number;
  error_count: number;
  rows: CompanyInventoryUploadRowOut[];
  recomputed_customer_ids: string[];
  coverage_changes: Record<string, (string | null)[]>;
  recompute_failures: RecomputeFailureOut[];
  column_contract: { required?: string[]; optional?: string[] };
}

/** One historical company on-hand upload, newest first. */
export interface CompanyInventoryUploadSummaryOut {
  id: string;
  business_unit_id: string;
  filename: string | null;
  sheet_name: string | null;
  row_count: number;
  applied_count: number;
  created_count: number;
  replaced_count: number;
  error_count: number;
  uploaded_at: string;
  source_system: string;
}

export const companyInventoryApi = {
  getPosition: (businessUnitId: string, productId?: string) =>
    getJSON<CompanyInventoryPositionOut>(
      `/company-inventory${search([
        ["business_unit_id", businessUnitId],
        ["product_id", productId],
      ])}`
    ),
  /**
   * The on-hand template as an .xlsx, already reflecting this BU's current
   * KNOWN rows. A URL, not a fetch — same single download idiom used
   * everywhere else in this app (see `customerOwnedTemplateUrl`).
   */
  templateUrl: (businessUnitId: string) =>
    withToken(
      `${API_BASE}/company-inventory/${encodeURIComponent(
        businessUnitId
      )}/template`
    ),
  uploadOnHand: (businessUnitId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return postForm<CompanyInventoryUploadOut>(
      `/company-inventory/${encodeURIComponent(businessUnitId)}/uploads`,
      form
    );
  },
  listUploads: (businessUnitId: string) =>
    getJSON<CompanyInventoryUploadSummaryOut[]>(
      `/company-inventory/${encodeURIComponent(businessUnitId)}/uploads`
    ),

  // ---- On-hand ----
  patchOnHand: (rowId: string, body: CompanyOnHandEditIn) =>
    patchJSON<CompanyInventoryWriteOut>(
      `/company-inventory/on-hand/${encodeURIComponent(rowId)}`,
      body
    ),
  createOnHand: (body: CompanyOnHandCreateIn) =>
    postDetail<CompanyInventoryWriteOut>("/company-inventory/on-hand", body),
  deleteOnHand: (rowId: string) =>
    deleteWithBody<CompanyInventoryWriteOut>(
      `/company-inventory/on-hand/${encodeURIComponent(rowId)}`
    ),

  // ---- On-order ----
  patchOnOrder: (rowId: string, body: CompanyOnOrderEditIn) =>
    patchJSON<CompanyInventoryWriteOut>(
      `/company-inventory/on-order/${encodeURIComponent(rowId)}`,
      body
    ),
  createOnOrder: (body: CompanyOnOrderCreateIn) =>
    postDetail<CompanyInventoryWriteOut>("/company-inventory/on-order", body),
  deleteOnOrder: (rowId: string) =>
    deleteWithBody<CompanyInventoryWriteOut>(
      `/company-inventory/on-order/${encodeURIComponent(rowId)}`
    ),

  // ---- Oracle assignments ----
  patchAssignment: (rowId: string, body: CompanyAssignmentEditIn) =>
    patchJSON<CompanyInventoryWriteOut>(
      `/company-inventory/assignments/${encodeURIComponent(rowId)}`,
      body
    ),
  createAssignment: (body: CompanyAssignmentCreateIn) =>
    postDetail<CompanyInventoryWriteOut>(
      "/company-inventory/assignments",
      body
    ),
  deleteAssignment: (rowId: string) =>
    deleteWithBody<CompanyInventoryWriteOut>(
      `/company-inventory/assignments/${encodeURIComponent(rowId)}`
    ),
};
