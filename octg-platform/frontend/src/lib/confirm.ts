export type ConfirmState = "idle" | "armed";

export const ARM_TIMEOUT_MS = 4000;

export function press(state: ConfirmState): { state: ConfirmState; fire: boolean } {
  if (state === "idle") return { state: "armed", fire: false };
  return { state: "idle", fire: true };
}
