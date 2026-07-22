import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(cleanup);

class EventSourceMock {
  addEventListener() {}
  close() {}
}

Object.defineProperty(globalThis, "EventSource", { value: EventSourceMock, writable: true });
Object.defineProperty(globalThis, "URL", {
  value: { ...URL, createObjectURL: () => "blob:test", revokeObjectURL: () => undefined },
  writable: true,
});
