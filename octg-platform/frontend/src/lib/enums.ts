// Single source for status/profile/unit constants (spec §3, ruling: 5-file hardcoding is a known smell)
export const DEMAND_STATUSES = ["Planned", "Budgeted", "Confirmed"] as const;
export type DemandStatus = (typeof DEMAND_STATUSES)[number];

export const DEMAND_PROFILES = ["Primary", "Contingency"] as const;
export type DemandProfile = (typeof DEMAND_PROFILES)[number];

export const UNITS = ["Mtr", "PC", "MT"] as const;
export type Unit = (typeof UNITS)[number];
