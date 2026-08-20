// @vitest-environment jsdom
import { act } from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ConfirmButton from "./ConfirmButton";

describe("ConfirmButton", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("arms on first click without confirming", () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton label="Delete" armedLabel="Confirm?" onConfirm={onConfirm} />);
    const button = screen.getByRole("button");

    fireEvent.click(button);

    expect(button.textContent).toBe("Confirm?");
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("confirms on second click and resets label", () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton label="Delete" armedLabel="Confirm?" onConfirm={onConfirm} />);
    const button = screen.getByRole("button");

    fireEvent.click(button);
    fireEvent.click(button);

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(button.textContent).toBe("Delete");
  });

  it("disarms after 4s without firing", () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton label="Delete" armedLabel="Confirm?" onConfirm={onConfirm} />);
    const button = screen.getByRole("button");

    fireEvent.click(button);
    expect(button.textContent).toBe("Confirm?");

    act(() => {
      vi.advanceTimersByTime(4000);
    });

    expect(button.textContent).toBe("Delete");
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("disarms on blur without firing", () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton label="Delete" armedLabel="Confirm?" onConfirm={onConfirm} />);
    const button = screen.getByRole("button");

    fireEvent.click(button);
    fireEvent.blur(button);

    expect(button.textContent).toBe("Delete");
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
