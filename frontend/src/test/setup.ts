import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(cleanup);

export class EventSourceMock {
  static instances: EventSourceMock[] = [];
  listeners = new Map<string, ((event: MessageEvent) => void)[]>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) { EventSourceMock.instances.push(this); }
  addEventListener(name: string, listener: (event: MessageEvent) => void) { this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]); }
  emit(name: string, data: Record<string, unknown>, sequence: number) {
    const event = { data: JSON.stringify({ data }), lastEventId: String(sequence) } as MessageEvent;
    this.listeners.get(name)?.forEach((listener) => listener(event));
  }
  close() {}
}

Object.defineProperty(globalThis, "EventSource", { value: EventSourceMock, writable: true });
Object.defineProperty(globalThis, "URL", {
  value: { ...URL, createObjectURL: () => "blob:test", revokeObjectURL: () => undefined },
  writable: true,
});

afterEach(() => { EventSourceMock.instances = []; });
