import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ReviewBar from "./ReviewBar";

function bar(over: Partial<Parameters<typeof ReviewBar>[0]> = {}) {
  return <ReviewBar total={3} reviewed={0} keptCount={0} undoableCount={3} disabled={false}
    onPrevious={vi.fn()} onNext={vi.fn()} onKeepAll={vi.fn()} onUndoAll={vi.fn()} {...over} />;
}

describe("locating changes from the review bar", () => {
  it("makes the counter the way in to the first change", async () => {
    // "3 changes" is the first thing read after an Apply and the question it
    // raises is "where?". Leaving the chevrons as the only route makes the user
    // hunt for a control to answer a question the bar just posed.
    const onNext = vi.fn();
    render(bar({ onNext }));
    await userEvent.click(screen.getByRole("button", { name: "Go to the first change" }));
    expect(onNext).toHaveBeenCalled();
  });

  it("steps in both directions", async () => {
    const onPrevious = vi.fn();
    const onNext = vi.fn();
    render(bar({ onPrevious, onNext }));
    await userEvent.click(screen.getByRole("button", { name: "Previous change" }));
    await userEvent.click(screen.getByRole("button", { name: "Next change" }));
    expect(onPrevious).toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });
});

describe("a tuned record reuses the bar without borrowing the agent's wording", () => {
  it("describes the changes as tuned", () => {
    render(bar({ origin: "tune" }));
    expect(screen.getByText(/3 Pending Reviews/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Review tuned changes" })).toBeInTheDocument();
  });

  it("labels its navigation as tuned too", () => {
    render(bar({ origin: "tune" }));
    expect(screen.getByRole("button", { name: "Next tuned change" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to the first tuned change" })).toBeInTheDocument();
  });

  it("still says agent for an agent turn", () => {
    render(bar());
    expect(screen.getByText(/3 Pending Reviews/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Review agent changes" })).toBeInTheDocument();
  });

  it("keeps the tuned wording in the undo-all confirmation", async () => {
    render(bar({ origin: "tune", keptCount: 1, undoableCount: 2 }));
    await userEvent.click(screen.getByRole("button", { name: /Reject All/ }));
    expect(screen.getByText(/Reject 2 unreviewed tuned changes\?/)).toBeInTheDocument();
  });
});

describe("jumping to the first change", () => {
  it("uses the explicit first-jump when the host provides one", async () => {
    // The counter says "first". Wiring it to onNext advances from wherever the
    // cursor is, so after stepping to change 3 it would land on 4.
    const onFirst = vi.fn();
    const onNext = vi.fn();
    render(bar({ onFirst, onNext }));
    await userEvent.click(screen.getByRole("button", { name: "Go to the first change" }));
    expect(onFirst).toHaveBeenCalled();
    expect(onNext).not.toHaveBeenCalled();
  });
});
