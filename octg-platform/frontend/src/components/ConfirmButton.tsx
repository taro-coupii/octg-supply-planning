import { useEffect, useRef, useState } from "react";

/**
 * Two-step button for FINAL decisions.
 *
 * A substitution decision cannot be re-decided (the API 409s a second
 * decision; reversal means raising a new request), yet it used to commit on a
 * single click with no confirmation anywhere -- QA 2026-08-14, decided by the
 * product owner. The first click ARMS the button (label changes to
 * `confirmLabel`, styling shifts); the second click within the window commits.
 * The armed state disarms by itself after a few seconds, and on blur, so an
 * accidental click melts away instead of lying in wait.
 *
 * Deliberately not window.confirm(): a native modal reads as an error, blocks
 * the whole tab, and cannot be styled to the calm palette. Two-step keeps the
 * decision in place, next to the row it affects.
 */
export default function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  disabled,
  className,
}: {
  label: string;
  confirmLabel: string;
  onConfirm: () => void;
  disabled?: boolean;
  className?: string;
}) {
  const [armed, setArmed] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    []
  );

  const disarm = () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    setArmed(false);
  };

  return (
    <button
      type="button"
      className={`${className ?? ""}${armed ? " confirm-armed" : ""}`}
      disabled={disabled}
      aria-live="polite"
      onBlur={disarm}
      onClick={() => {
        if (!armed) {
          setArmed(true);
          if (timer.current !== null) window.clearTimeout(timer.current);
          timer.current = window.setTimeout(() => setArmed(false), 4000);
          return;
        }
        disarm();
        onConfirm();
      }}
    >
      {armed ? confirmLabel : label}
    </button>
  );
}
