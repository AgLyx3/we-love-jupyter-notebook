import { renderHook, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAutoSave } from "./useAutoSave";

afterEach(() => vi.useRealTimers());

describe("useAutoSave", () => {
  it("saves the latest source once editing goes idle, resetting on each change", () => {
    vi.useFakeTimers();
    const save = vi.fn();
    const { rerender } = renderHook(({ s }) => useAutoSave(s, true, false, save, 1000), { initialProps: { s: "a" } });
    act(() => vi.advanceTimersByTime(600));
    rerender({ s: "ab" }); // a keystroke before the debounce elapses
    act(() => vi.advanceTimersByTime(600));
    expect(save).not.toHaveBeenCalled(); // timer was reset
    act(() => vi.advanceTimersByTime(400));
    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith("ab");
  });

  it("does not save while paused (agent/kernel busy)", () => {
    vi.useFakeTimers();
    const save = vi.fn();
    renderHook(() => useAutoSave("x", true, true, save, 1000));
    act(() => vi.advanceTimersByTime(3000));
    expect(save).not.toHaveBeenCalled();
  });

  it("does not save when the cell is not dirty", () => {
    vi.useFakeTimers();
    const save = vi.fn();
    renderHook(() => useAutoSave("x", false, false, save, 1000));
    act(() => vi.advanceTimersByTime(3000));
    expect(save).not.toHaveBeenCalled();
  });
});
