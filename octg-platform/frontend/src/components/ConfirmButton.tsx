import { useEffect, useRef, useState } from "react";
import type { ConfirmState } from "../lib/confirm";
import { ARM_TIMEOUT_MS, press } from "../lib/confirm";

type Props = {
  label: string;
  armedLabel: string;
  onConfirm: () => void;
  className?: string;
};

export default function ConfirmButton({ label, armedLabel, onConfirm, className }: Props) {
  const [state, setState] = useState<ConfirmState>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (state === "armed") {
      timer.current = setTimeout(() => setState("idle"), ARM_TIMEOUT_MS);
    }
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [state]);

  const handleClick = () => {
    const next = press(state);
    setState(next.state);
    if (next.fire) onConfirm();
  };

  return (
    <button
      type="button"
      className={`confirm-btn ${state === "armed" ? "armed" : ""} ${className ?? ""}`}
      onClick={handleClick}
      onBlur={() => setState("idle")}
    >
      {state === "armed" ? armedLabel : label}
    </button>
  );
}
