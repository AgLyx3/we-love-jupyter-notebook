export type CellOutput = Record<string, unknown>;

export interface NotebookCellData {
  cellId: string;
  index: number;
  cellType: "code" | "markdown" | "raw";
  source: string;
  metadata: Record<string, unknown>;
  outputs: CellOutput[];
  executionCount: number | null;
}

export interface NotebookSnapshot {
  sessionId: string;
  filename: string;
  notebookPath?: string | null;
  workspaceRoot?: string | null;
  revision: number;
  dirty: boolean;
  metadata: Record<string, unknown>;
  nbformat: number;
  nbformatMinor: number;
  cells: NotebookCellData[];
}

export interface TurnScope {
  editableCellIds: string[];
  contextCellIds: string[];
  sessionId: string | null;
  notebookRevision: number | null;
}

export interface AgentChange { cellId: string; previousSource: string; nextSource: string }
export interface AgentTurn {
  turnId: string;
  sessionId: string;
  baseRevision: number;
  prompt: string;
  editableCellIds: string[];
  contextCellIds: string[];
  undoEligible: boolean;
  state: string;
  attempts: number;
  finalOutput: string | null;
  appliedRevision: number | null;
  executionOperationId: string | null;
  changes: AgentChange[];
  error: ApiErrorBody | null;
  createdAt: string;
  completedAt: string | null;
  historyTruncated?: boolean;
}

export interface ExecutionAttempt {
  executionAttemptId: string;
  cellId: string;
  cellIndex: number;
  sourcePreview: string;
  state: string;
  risk: { level: string; reasons: string[]; matchedPatterns: string[] };
  decision: string | null;
  outputs?: CellOutput[];
  outputsTruncated: boolean;
  executionCount: number | null;
  error: ApiErrorBody | null;
}

export interface ExecutionOperation {
  operationId: string;
  sessionId: string;
  baseRevision: number;
  currentDocumentRevision: number | null;
  kind: string;
  parentTurnId: string | null;
  state: string;
  currentExecutionAttemptId: string | null;
  attempts: ExecutionAttempt[];
  error: ApiErrorBody | null;
  createdAt: string;
  completedAt: string | null;
}

export interface KernelStatus {
  kernelSessionId: string | null;
  state: string;
  executionAttemptId: string | null;
}

export interface SessionStatus {
  sessionId: string;
  documentRevision: number;
  activeTurn: AgentTurn | null;
  activeExecution: ExecutionOperation | null;
  turnHistory: AgentTurn[];
  turnHistoryTruncated: boolean;
}

export interface NotebookCloseResult {
  closedSessionId: string;
  cleanupErrors: string[];
}

export interface DirectoryEntry { name: string; path: string; kind: "directory" | "notebook" }
export interface DirectoryListing { path: string; parent: string | null; entries: DirectoryEntry[] }
export interface ApiErrorBody { code: string; message: string; details: Record<string, unknown> }

export class ApiError extends Error {
  constructor(public status: number, public body: ApiErrorBody) { super(body.message); }
  get isConflict() { return this.status === 409; }
}

const API = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API}${path}`, { ...init, headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { error?: ApiErrorBody } | null;
    throw new ApiError(response.status, payload?.error ?? { code: "request_failed", message: response.statusText || "Request failed", details: {} });
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

async function blobRequest(path: string): Promise<Blob> {
  const response = await fetch(`${API}${path}`);
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { error?: ApiErrorBody } | null;
    throw new ApiError(response.status, payload?.error ?? { code: "download_failed", message: "Notebook download failed", details: {} });
  }
  return response.blob();
}

const mutation = (snapshot: Pick<NotebookSnapshot, "sessionId" | "revision">) => ({ sessionId: snapshot.sessionId, expectedDocumentRevision: snapshot.revision });

export const api = {
  current: () => request<NotebookSnapshot>("/notebooks/current"),
  scope: () => request<TurnScope>("/turn-scope"),
  kernel: () => request<KernelStatus>("/kernel/status"),
  status: () => request<SessionStatus>("/session/status"),
  download: () => blobRequest("/notebooks/download"),
  close: (snapshot: Pick<NotebookSnapshot, "sessionId" | "revision">) => request<NotebookCloseResult>("/notebooks/current", { method: "DELETE", body: JSON.stringify(mutation(snapshot)) }),
  open: (path: string, current?: NotebookSnapshot, workspaceRoot?: string) => request<NotebookSnapshot>("/notebooks/open", { method: "POST", body: JSON.stringify({ path, ...(workspaceRoot ? { workspaceRoot } : {}), ...(current ? mutation(current) : {}) }) }),
  listFiles: (path?: string) => request<DirectoryListing>(`/files${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  save: (snapshot: NotebookSnapshot) => request<NotebookSnapshot>("/notebooks/save", { method: "POST", body: JSON.stringify(mutation(snapshot)) }),
  saveAs: (snapshot: NotebookSnapshot, path: string, workspaceRoot?: string) => request<NotebookSnapshot>("/notebooks/save-as", { method: "POST", body: JSON.stringify({ path, ...(workspaceRoot ? { workspaceRoot } : {}), ...mutation(snapshot) }) }),
  upload: (file: File, current?: NotebookSnapshot) => {
    const body = new FormData();
    body.append("file", file);
    if (current) { body.append("sessionId", current.sessionId); body.append("expectedDocumentRevision", String(current.revision)); }
    return request<NotebookSnapshot>("/notebooks/upload", { method: "POST", body });
  },
  saveSource: (snapshot: NotebookSnapshot, cellId: string, source: string) =>
    request<{ sessionId: string; cellId: string; source: string; revision: number; dirty: boolean }>(`/cells/${encodeURIComponent(cellId)}/source`, { method: "POST", body: JSON.stringify({ ...mutation(snapshot), source }) }),
  addScope: (snapshot: NotebookSnapshot, cellId: string, editable: boolean) =>
    request<TurnScope>(`/turn-scope/${editable ? "editable-cells" : "context-cells"}`, { method: "POST", body: JSON.stringify({ ...mutation(snapshot), cellId }) }),
  removeScope: (snapshot: NotebookSnapshot, cellId: string) =>
    request<TurnScope>(`/turn-scope/cells/${encodeURIComponent(cellId)}`, { method: "DELETE", body: JSON.stringify(mutation(snapshot)) }),
  clearScope: (snapshot: NotebookSnapshot) => request<TurnScope>("/turn-scope", { method: "DELETE", body: JSON.stringify(mutation(snapshot)) }),
  runCell: (snapshot: NotebookSnapshot, cellId: string) => request<ExecutionOperation>(`/execution/cells/${encodeURIComponent(cellId)}/run`, { method: "POST", body: JSON.stringify(mutation(snapshot)) }),
  runAll: (snapshot: NotebookSnapshot) => request<ExecutionOperation>("/execution/run-all", { method: "POST", body: JSON.stringify(mutation(snapshot)) }),
  execution: (id: string) => request<ExecutionOperation>(`/execution/${encodeURIComponent(id)}`),
  interrupt: (snapshot: NotebookSnapshot, status: KernelStatus) => request<KernelStatus>(`/kernel/${encodeURIComponent(status.kernelSessionId ?? "")}/interrupt`, { method: "POST", body: JSON.stringify({ ...mutation(snapshot), executionAttemptId: status.executionAttemptId }) }),
  restart: (snapshot: NotebookSnapshot, status: KernelStatus) => request<KernelStatus>(`/kernel/${encodeURIComponent(status.kernelSessionId ?? "")}/restart`, { method: "POST", body: JSON.stringify({ ...mutation(snapshot), executionAttemptId: status.executionAttemptId }) }),
  startTurn: (snapshot: NotebookSnapshot, prompt: string) => request<AgentTurn>("/agent-turns", { method: "POST", body: JSON.stringify({ ...mutation(snapshot), prompt }) }),
  turn: (id: string) => request<AgentTurn>(`/agent-turns/${encodeURIComponent(id)}`),
  cancelTurn: (snapshot: NotebookSnapshot, id: string) => request<AgentTurn>(`/agent-turns/${encodeURIComponent(id)}/cancel`, { method: "POST", body: JSON.stringify(mutation(snapshot)) }),
  undoTurn: (snapshot: NotebookSnapshot, id: string) => request<NotebookSnapshot>(`/agent-turns/${encodeURIComponent(id)}/undo`, { method: "POST", body: JSON.stringify(mutation(snapshot)) }),
  revertCell: (snapshot: NotebookSnapshot, turnId: string, cellId: string) => request<NotebookSnapshot>(`/agent-turns/${encodeURIComponent(turnId)}/cells/${encodeURIComponent(cellId)}/revert`, { method: "POST", body: JSON.stringify(mutation(snapshot)) }),
  decide: (operation: ExecutionOperation, attempt: ExecutionAttempt, decision: "approve" | "skip" | "cancel") => {
    if (operation.currentDocumentRevision == null) throw new Error("Execution correlation is incomplete");
    return request<ExecutionOperation>(`/execution/${encodeURIComponent(attempt.executionAttemptId)}/${decision}`, { method: "POST", body: JSON.stringify({ sessionId: operation.sessionId, expectedDocumentRevision: operation.currentDocumentRevision, turnId: operation.parentTurnId, cellId: attempt.cellId }) });
  },
};

export function connectEvents(sessionId: string, after: number, handlers: { notebook: () => void; turn: (id: string) => void; execution: (id: string) => void; disconnected: () => void; connected: () => void; cursor: (sequence: number) => void }) {
  const source = new EventSource(`${API}/events?sessionId=${encodeURIComponent(sessionId)}&after=${after}`);
  const listen = (name: string, callback: (data: Record<string, unknown>) => void) => source.addEventListener(name, (event) => {
    const message = event as MessageEvent;
    const sequence = Number(message.lastEventId);
    if (Number.isFinite(sequence)) handlers.cursor(sequence);
    const envelope = JSON.parse(message.data) as { data: Record<string, unknown> };
    callback(envelope.data);
  });
  listen("notebook.updated", () => handlers.notebook());
  listen("turn.updated", (data) => handlers.turn(String(data.turnId)));
  listen("execution.updated", (data) => handlers.execution(String(data.operationId)));
  source.onerror = () => handlers.disconnected();
  source.onopen = () => handlers.connected();
  return () => source.close();
}
