import { useEffect, useRef, useState } from "preact/hooks";

import {
  addKnowledgeText,
  addKnowledgeUrl,
  ApiError,
  deleteKnowledgeDocument,
  KnowledgeDocument,
  KnowledgeHit,
  listKnowledge,
  reindexKnowledgeDocument,
  searchKnowledgePreview,
  uploadKnowledgeFiles,
} from "../api";

export function KnowledgeView({ tenantId }: { tenantId: string }) {
  const [documents, setDocuments] = useState<KnowledgeDocument[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  function load(): void {
    listKnowledge(tenantId)
      .then((body) => setDocuments(body.documents))
      .catch((err) => setError(err instanceof ApiError ? err.message : "failed to load"));
  }

  useEffect(load, [tenantId]);

  // Poll while anything is still processing, so status updates without a
  // manual refresh — ingestion runs as a background task server-side
  // (app/rag/ingest.py), so there's nothing to await here.
  useEffect(() => {
    if (!documents?.some((d) => d.status === "pending" || d.status === "indexing")) {
      return;
    }
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [documents, tenantId]);

  return (
    <div>
      {error && <div class="admin-error-banner">{error}</div>}
      <PasteText tenantId={tenantId} onAdded={load} />
      <UploadFiles tenantId={tenantId} onAdded={load} />
      <AddUrl tenantId={tenantId} onAdded={load} />
      <DocumentList tenantId={tenantId} documents={documents} onChanged={load} />
      <SearchPreview tenantId={tenantId} />
    </div>
  );
}

function PasteText({ tenantId, onAdded }: { tenantId: string; onAdded: () => void }) {
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: Event): Promise<void> {
    event.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await addKnowledgeText(tenantId, title, text);
      setTitle("");
      setText("");
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not add text");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="admin-section">
      <h2>Paste text</h2>
      {error && <div class="admin-error-banner">{error}</div>}
      <form onSubmit={submit}>
        <div class="admin-field">
          <label>Title</label>
          <input
            type="text"
            value={title}
            onInput={(e) => setTitle((e.target as HTMLInputElement).value)}
          />
        </div>
        <div class="admin-field">
          <label>Text</label>
          <textarea
            value={text}
            style={{ minHeight: "8rem" }}
            onInput={(e) => setText((e.target as HTMLTextAreaElement).value)}
          />
        </div>
        <button class="admin-btn" type="submit" disabled={busy || !text.trim()}>
          {busy ? "Adding…" : "Add"}
        </button>
      </form>
    </div>
  );
}

function UploadFiles({ tenantId, onAdded }: { tenantId: string; onAdded: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  async function handleFiles(fileList: FileList | null): Promise<void> {
    if (!fileList || fileList.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      await uploadKnowledgeFiles(tenantId, Array.from(fileList));
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "upload failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="admin-section">
      <h2>Upload files</h2>
      <p class="admin-muted">
        .pdf .docx .md .txt .csv — several at once, each queued independently.
      </p>
      {error && <div class="admin-error-banner">{error}</div>}
      <div
        class={`admin-dropzone${dragOver ? " admin-dropzone--active" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          void handleFiles(e.dataTransfer?.files ?? null);
        }}
        onClick={() => inputRef.current?.click()}
      >
        {busy ? "Uploading…" : "Drag files here, or click to choose"}
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.docx,.md,.txt,.csv"
          style={{ display: "none" }}
          onChange={(e) => void handleFiles((e.target as HTMLInputElement).files)}
        />
      </div>
    </div>
  );
}

function AddUrl({ tenantId, onAdded }: { tenantId: string; onAdded: () => void }) {
  const [url, setUrl] = useState("");
  const [crawl, setCrawl] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: Event): Promise<void> {
    event.preventDefault();
    if (!url.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await addKnowledgeUrl(tenantId, url.trim(), crawl);
      setUrl("");
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not fetch that URL");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="admin-section">
      <h2>From a URL</h2>
      {error && <div class="admin-error-banner">{error}</div>}
      <form onSubmit={submit}>
        <div class="admin-field-row">
          <div class="admin-field" style={{ flex: 2 }}>
            <label>URL</label>
            <input
              type="text"
              value={url}
              placeholder="https://example.com/faq"
              onInput={(e) => setUrl((e.target as HTMLInputElement).value)}
            />
          </div>
          <label class="admin-checkbox" style={{ alignSelf: "center" }}>
            <input
              type="checkbox"
              checked={crawl}
              onChange={(e) => setCrawl((e.target as HTMLInputElement).checked)}
            />
            Crawl same-domain links
          </label>
        </div>
        <button class="admin-btn" type="submit" disabled={busy || !url.trim()}>
          {busy ? (crawl ? "Crawling…" : "Fetching…") : crawl ? "Crawl" : "Fetch"}
        </button>
      </form>
    </div>
  );
}

const STATUS_LABEL: Record<string, string> = {
  pending: "Pending",
  indexing: "Indexing…",
  ready: "Ready",
  failed: "Failed",
};

function DocumentList({
  tenantId,
  documents,
  onChanged,
}: {
  tenantId: string;
  documents: KnowledgeDocument[] | null;
  onChanged: () => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function reindex(documentId: string): Promise<void> {
    setBusyId(documentId);
    setError(null);
    try {
      await reindexKnowledgeDocument(tenantId, documentId);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not re-index");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(documentId: string): Promise<void> {
    setBusyId(documentId);
    setError(null);
    try {
      await deleteKnowledgeDocument(tenantId, documentId);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not delete");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div class="admin-section">
      <h2>Documents</h2>
      {error && <div class="admin-error-banner">{error}</div>}
      {documents === null && <p class="admin-muted">Loading…</p>}
      {documents && documents.length === 0 && <p class="admin-muted">Nothing indexed yet.</p>}
      {documents && documents.length > 0 && (
        <table class="admin-table">
          <thead>
            <tr>
              <th>Title</th>
              <th>Source</th>
              <th>Status</th>
              <th>Chunks</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {documents.map((doc) => (
              <tr key={doc.id}>
                <td>{doc.title || "(untitled)"}</td>
                <td>{doc.source_type}</td>
                <td>
                  <span
                    class={`admin-badge ${
                      doc.status === "ready"
                        ? "admin-badge--ok"
                        : doc.status === "failed"
                          ? "admin-badge--warn"
                          : ""
                    }`}
                    title={doc.error ?? ""}
                  >
                    {STATUS_LABEL[doc.status] ?? doc.status}
                  </span>
                </td>
                <td>{doc.chunk_count}</td>
                <td>
                  {doc.source_type === "url" && (
                    <button
                      class="admin-btn admin-btn--secondary"
                      disabled={busyId === doc.id}
                      onClick={() => void reindex(doc.id)}
                    >
                      Re-index
                    </button>
                  )}{" "}
                  <button
                    class="admin-btn admin-btn--danger"
                    disabled={busyId === doc.id}
                    onClick={() => void remove(doc.id)}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function SearchPreview({ tenantId }: { tenantId: string }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<KnowledgeHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: Event): Promise<void> {
    event.preventDefault();
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const body = await searchKnowledgePreview(tenantId, query.trim());
      setHits(body.hits);
    } catch (err) {
      setError(err instanceof Error ? err.message : "search failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="admin-section">
      <h2>Search preview</h2>
      <p class="admin-muted">
        See what the bot would actually retrieve for a question, before it goes live.
      </p>
      {error && <div class="admin-error-banner">{error}</div>}
      <form onSubmit={submit} class="admin-field-row">
        <div class="admin-field" style={{ flex: 1 }}>
          <input
            type="text"
            value={query}
            placeholder="Ask a question…"
            onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
          />
        </div>
        <button
          class="admin-btn"
          type="submit"
          disabled={busy || !query.trim()}
          style={{ alignSelf: "flex-start" }}
        >
          {busy ? "Searching…" : "Search"}
        </button>
      </form>
      {hits && hits.length === 0 && <p class="admin-muted">No matches.</p>}
      {hits && hits.length > 0 && (
        <div>
          {hits.map((hit) => (
            <div class="admin-list-item" key={hit.chunk_id}>
              <div class="admin-list-item-header">
                <strong>{hit.document_title || "Untitled"}</strong>
                <span class="admin-muted">{(hit.similarity * 100).toFixed(0)}% match</span>
              </div>
              <p style={{ whiteSpace: "pre-wrap" }}>{hit.content}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
