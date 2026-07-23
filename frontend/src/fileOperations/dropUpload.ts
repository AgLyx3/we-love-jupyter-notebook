// Extracts .ipynb files from a drag-and-drop, supporting both dropped files and
// dropped folders (traversed via the webkitGetAsEntry filesystem API). Falls
// back to the plain file list when the entry API is unavailable.

interface EntryReader {
  readEntries: (onSuccess: (entries: FsEntry[]) => void, onError: (error: unknown) => void) => void;
}

export interface FsEntry {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  file?: (onSuccess: (file: File) => void, onError: (error: unknown) => void) => void;
  createReader?: () => EntryReader;
}

interface DropSource {
  items?: ArrayLike<{ webkitGetAsEntry?: () => FsEntry | null }> | null;
  files?: ArrayLike<File> | null;
}

const isNotebook = (name: string): boolean => name.toLowerCase().endsWith(".ipynb") && !name.startsWith(".");

function readAllEntries(reader: EntryReader): Promise<FsEntry[]> {
  return new Promise((resolve, reject) => {
    const all: FsEntry[] = [];
    const pump = () => reader.readEntries((batch) => {
      if (!batch.length) { resolve(all); return; }
      all.push(...batch);
      pump(); // readEntries returns results in chunks; keep reading until empty
    }, reject);
    pump();
  });
}

async function walk(entry: FsEntry, out: File[], depth: number): Promise<void> {
  if (entry.isFile && entry.file) {
    if (!isNotebook(entry.name)) return;
    out.push(await new Promise<File>((resolve, reject) => entry.file!(resolve, reject)));
    return;
  }
  if (entry.isDirectory && entry.createReader && depth < 8) {
    for (const child of await readAllEntries(entry.createReader())) await walk(child, out, depth + 1);
  }
}

export async function extractNotebookFiles(source: DropSource): Promise<File[]> {
  // Capture entries synchronously — the DataTransfer is only valid during the
  // drop event, before the first await.
  const entries: FsEntry[] = [];
  if (source.items) {
    for (const item of Array.from(source.items)) {
      const entry = item.webkitGetAsEntry?.();
      if (entry) entries.push(entry);
    }
  }
  if (entries.length) {
    const out: File[] = [];
    for (const entry of entries) await walk(entry, out, 0);
    out.sort((a, b) => a.name.localeCompare(b.name));
    return out;
  }
  return Array.from(source.files ?? []).filter((file) => isNotebook(file.name));
}
