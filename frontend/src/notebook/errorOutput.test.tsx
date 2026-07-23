import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Outputs } from "./NotebookCell";

const esc = String.fromCharCode(27);
const errorOutput = { output_type: "error", ename: "NameError", evalue: "name 'x' is not defined", traceback: [`${esc}[31mTraceback${esc}[39m line ${esc}[32m1${esc}[39m`] };

describe("error output add-to-chat", () => {
  it("attaches the whole error, ANSI-stripped, in one click — no selection needed", async () => {
    const onAddErrorToChat = vi.fn();
    render(<Outputs outputs={[errorOutput]} onAddErrorToChat={onAddErrorToChat} />);
    await userEvent.click(screen.getByRole("button", { name: "Add error to agent chat" }));
    expect(onAddErrorToChat).toHaveBeenCalledTimes(1);
    const text = onAddErrorToChat.mock.calls[0][0] as string;
    expect(text).toContain("NameError: name 'x' is not defined");
    expect(text).toContain("Traceback line 1");
    expect(text).not.toContain("[31m");
  });

  it("renders the traceback without ANSI control codes", () => {
    render(<Outputs outputs={[errorOutput]} onAddErrorToChat={vi.fn()} />);
    expect(screen.getByText(/Traceback line 1/)).toBeInTheDocument();
  });

  it("hides the button while mutations are disabled", () => {
    render(<Outputs outputs={[errorOutput]} disabled onAddErrorToChat={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Add error to agent chat" })).not.toBeInTheDocument();
  });
});
