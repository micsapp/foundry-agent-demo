"""
Knowledge Base Manager & Microsoft Foundry Vector Store Sync Tool.

Handles:
1. Searching and reading Knowledge Base files in `knowledge_base/` (editable Markdown, read-only PDF manuals)
2. Validating proposed KB file updates via GuardrailEngine (PII scan, path check, diff generation)
3. Applying approved KB updates locally AND syncing the updated file to the Foundry project's vector store
4. Reusing one vector store across runs: only new or changed files are uploaded (tracked in .kb_vector_store.json)
5. `azure_blob` mode: the KB files, their backups and the sync record live in an Azure blob container
   (KB_BLOB_CONTAINER_URL) instead, so the Azure Function that serves `propose_kb_file_update` over MCP and this
   app edit and re-index the same files.

File Search never reads these files directly: it searches the vector store's indexed copy of each one. An update
therefore saves the file, uploads the new version to the vector store (waiting until it's indexed) and then
removes the old copy, so answers can't come from outdated text.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from guardrails import GuardrailEngine, GuardrailResult

KB_DIR = Path(__file__).parent / "knowledge_base"
KB_BACKUP_DIR = Path(__file__).parent / ".kb_backups"
KB_PATTERNS = ("*.md", "*.pdf")  # PDFs are uploaded and searched but never edited by the agent
KB_SYNC_FILE = Path(__file__).parent / ".kb_vector_store.json"  # which store we use + hash of each uploaded file
KB_SYNC_BLOB = "vector_store_sync.json"  # the same record in azure_blob mode, next to the files
UNTRACKED_GRACE_SECONDS = 300  # a store file this new may be another writer's upload whose record isn't saved yet

_pdf_texts: Dict[str, str] = {}


def _pdf_text(data: bytes) -> str:
    """Extracts PDF text with one "## Page N" section per page (cached by content hash)."""
    digest = hashlib.sha256(data).hexdigest()
    if digest not in _pdf_texts:
        try:
            from pypdf import PdfReader
        except ImportError:
            return "(Install pypdf to read PDF text locally: pip install pypdf)"
        pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages]
        _pdf_texts[digest] = "\n\n".join(f"## Page {i}\n{text.strip()}" for i, text in enumerate(pages, 1))
    return _pdf_texts[digest]


class BlobKnowledgeBase:
    """The Knowledge Base in an Azure blob container: top-level *.md and *.pdf blobs are the files, backups/ holds
    each file as it was before an update, and vector_store_sync.json is the sync record.

    Signs in with DefaultAzureCredential (the Function App's managed identity, or your `az login`); both need
    Storage Blob Data Contributor on the container. Downloads are cached by ETag.
    """

    def __init__(self, container_url: str, credential: Any = None) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import ContainerClient

        self.url = container_url
        self.container = ContainerClient.from_container_url(container_url, credential=credential or DefaultAzureCredential())
        self._cache: Dict[str, Any] = {}  # blob name -> (etag, bytes)

    def names(self) -> List[str]:
        return sorted(
            b.name for b in self.container.list_blobs() if "/" not in b.name and b.name.lower().endswith((".md", ".pdf"))
        )

    def read(self, name: str) -> Optional[bytes]:
        from azure.core import MatchConditions
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        blob = self.container.get_blob_client(name)
        cached = self._cache.get(name)
        try:
            if cached:
                download = blob.download_blob(etag=cached[0], match_condition=MatchConditions.IfModified)
            else:
                download = blob.download_blob()
            data = download.readall()
        except ResourceNotFoundError:
            self._cache.pop(name, None)
            return None
        except HttpResponseError as exc:
            if exc.status_code != 304 or not cached:  # 304 Not Modified (raised as a failed condition)
                raise
            return cached[1]
        self._cache[name] = (download.properties.etag, data)
        return data

    def write(self, name: str, data: bytes) -> None:
        result = self.container.get_blob_client(name).upload_blob(data, overwrite=True)
        self._cache[name] = (result["etag"], data)


class KnowledgeBaseTool:
    """Manages KB source files and syncs approved updates with Foundry vector stores."""

    def __init__(
        self,
        guardrails: GuardrailEngine,
        openai_client: Optional[Any] = None,
        vector_store_id: Optional[str] = None,
        kb_mode: str = "local",
        blob_container_url: Optional[str] = None,
    ) -> None:
        self.guardrails = guardrails
        self.openai_client = openai_client  # from AIProjectClient.get_openai_client()
        self.vector_store_id = vector_store_id
        self.kb_mode = kb_mode
        self.blob_container_url = (blob_container_url or os.getenv("KB_BLOB_CONTAINER_URL", "")).strip()
        self._blob: Optional[BlobKnowledgeBase] = None  # created on first use in azure_blob mode
        self.remote_file_map: Dict[str, str] = {}  # filename -> foundry file_id
        self._sync_state: Dict[str, Any] = {}  # contents of KB_SYNC_FILE (or KB_SYNC_BLOB)
        if KB_DIR.exists() or kb_mode != "azure_blob":  # the Azure Function has no local copy of the files
            KB_DIR.mkdir(parents=True, exist_ok=True)
            KB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            self._ensure_initial_backups()

    @property
    def location(self) -> str:
        if self.kb_mode == "azure_blob":
            return f"Azure blob container {self.blob_container_url or '(KB_BLOB_CONTAINER_URL not set)'}"
        return "knowledge_base/"

    @property
    def blob(self) -> BlobKnowledgeBase:
        if self._blob is None:
            if not self.blob_container_url:
                raise RuntimeError("KB_BLOB_CONTAINER_URL is not set; ./deploy_feedback_function.sh writes it to .env.")
            self._blob = BlobKnowledgeBase(self.blob_container_url)
        return self._blob

    def _ensure_initial_backups(self) -> None:
        """Saves a pristine copy of initial KB files so the user can reset the demo anytime."""
        init_dir = KB_BACKUP_DIR / "initial"
        if not init_dir.exists():
            init_dir.mkdir(parents=True, exist_ok=True)
            for md_file in KB_DIR.glob("*.md"):
                shutil.copy2(md_file, init_dir / md_file.name)

    def reset_kb_files(self) -> None:
        """Restores all KB markdown files from the initial backup snapshot (and the vector store, if connected)."""
        init_dir = KB_BACKUP_DIR / "initial"
        if init_dir.exists():
            for md_file in init_dir.glob("*.md"):
                if self.kb_mode == "azure_blob":
                    self.blob.write(md_file.name, md_file.read_bytes())
                else:
                    shutil.copy2(md_file, KB_DIR / md_file.name)
        if self.openai_client is not None and self.vector_store_id:
            self.sync_files()

    # ------------------------------------------------------------------
    # File storage: knowledge_base/ on this machine, or the blob container
    # ------------------------------------------------------------------
    def _read_bytes(self, filename: str) -> Optional[bytes]:
        if self.kb_mode == "azure_blob":
            return self.blob.read(filename)
        target = KB_DIR / filename
        return target.read_bytes() if target.exists() else None

    def _write_file(self, filename: str, content: str) -> None:
        """Saves a KB file, first keeping the current version as a timestamped backup."""
        current = self._read_bytes(filename)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.kb_mode == "azure_blob":
            if current is not None:
                self.blob.write(f"backups/{filename}.{ts}.bak", current)
            self.blob.write(filename, content.encode("utf-8"))
            return
        if current is not None:
            (KB_BACKUP_DIR / f"{filename}.{ts}.bak").write_bytes(current)
        (KB_DIR / filename).write_text(content, encoding="utf-8")

    def _read_sync_state(self) -> Dict[str, Any]:
        if self.kb_mode == "azure_blob":
            data = self.blob.read(KB_SYNC_BLOB)
            return json.loads(data) if data else {}
        return self._load_sync_state()

    @staticmethod
    def _load_sync_state() -> Dict[str, Any]:
        try:
            return json.loads(KB_SYNC_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_sync_state(self) -> None:
        text = json.dumps(self._sync_state, indent=2)
        if self.kb_mode == "azure_blob":
            self.blob.write(KB_SYNC_BLOB, text.encode("utf-8"))
        else:
            KB_SYNC_FILE.write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------
    # Foundry vector store: one store per project, reused across runs
    # ------------------------------------------------------------------
    def connect_vector_store(
        self, openai_client: Any, store_name: str, project_endpoint: str, store_id: str = ""
    ) -> str:
        """
        Picks the vector store and uploads only the KB files that changed since the last run.

        Uses, in order: `store_id` (AZURE_VECTOR_STORE_ID), the store in the sync record for this project, the
        newest store named `store_name`, or a new store. Returns a status summary.
        """
        self.openai_client = openai_client
        state = self._read_sync_state()
        if state.get("project_endpoint") != project_endpoint:
            state = {}
        store, how = None, "reused"
        if store_id:
            store = openai_client.vector_stores.retrieve(store_id)  # a wrong ID should fail loudly
        elif state.get("vector_store_id"):
            store = self._find_store(state["vector_store_id"])
        if store is None:
            store = next(
                (s for s in openai_client.vector_stores.list(limit=100, order="desc") if s.name == store_name), None
            )
            how = "adopted"
        if store is None:
            store, how = openai_client.vector_stores.create(name=store_name), "new"
        if state.get("vector_store_id") != store.id:
            state = {"files": {}}  # the file list belongs to another store
        state.update(project_endpoint=project_endpoint, vector_store_id=store.id)
        self._sync_state = state
        self.vector_store_id = store.id
        return f"vector store {store.id} ({how}; {self._sync()})"

    def attach_vector_store(self, openai_client: Any, project_endpoint: str) -> None:
        """Uses the vector store the app last synced (azure_blob mode: read from the shared sync record)."""
        state = self._read_sync_state()
        if not state.get("vector_store_id") or state.get("project_endpoint") != project_endpoint:
            raise RuntimeError(
                f"The sync record in {self.location} names no vector store for {project_endpoint}. Start the app "
                "once in foundry mode with KB_MODE=azure_blob (or re-run ./deploy_feedback_function.sh)."
            )
        self.openai_client = openai_client
        self.vector_store_id = state["vector_store_id"]
        self._sync_state = state

    def sync_files(self) -> str:
        """Makes the vector store match the KB files: uploads new or changed files, removes deleted ones."""
        if self.kb_mode == "azure_blob":
            # The Azure Function (or the app) may have re-indexed a file since we last read the record
            fresh = self._read_sync_state()
            if fresh.get("vector_store_id") == self.vector_store_id:
                self._sync_state = fresh
        return self._sync()

    def _sync(self) -> str:
        client, store_id = self.openai_client, self.vector_store_id
        known: Dict[str, Dict[str, str]] = self._sync_state.setdefault("files", {})
        remote = {f.id: f.status for f in client.vector_stores.files.list(vector_store_id=store_id, limit=100)}
        local = self.list_kb_files()
        # Drop copies of our files that the record doesn't know about: a store we haven't tracked yet, or an
        # upload whose record was lost. Very recent ones may belong to a writer that hasn't saved its record yet.
        tracked = {entry["file_id"] for entry in known.values()}
        for file_id in remote:
            if file_id not in tracked:
                info = client.files.retrieve(file_id)
                if info.filename in local and (getattr(info, "created_at", 0) or 0) < time.time() - UNTRACKED_GRACE_SECONDS:
                    self._delete_remote_file(file_id)
        uploaded = unchanged = 0
        for fname in local:
            data = self._read_bytes(fname) or b""
            digest = hashlib.sha256(data).hexdigest()
            entry = known.get(fname)
            if entry and entry["sha256"] == digest and remote.get(entry["file_id"]) == "completed":
                unchanged += 1
            else:
                # The new copy is indexed (upload_and_poll waits) before the old one goes, so File Search
                # always has one copy of the file
                vs_file = client.vector_stores.files.upload_and_poll(vector_store_id=store_id, file=(fname, data))
                if entry:
                    self._delete_remote_file(entry["file_id"])
                known[fname] = {"file_id": vs_file.id, "sha256": digest}
                uploaded += 1
            self.remote_file_map[fname] = known[fname]["file_id"]
        removed = [fname for fname in known if fname not in local]
        for fname in removed:
            self._delete_remote_file(known.pop(fname)["file_id"])
            self.remote_file_map.pop(fname, None)
        self._save_sync_state()
        return f"{uploaded} uploaded, {unchanged} unchanged, {len(removed)} removed"

    def _find_store(self, store_id: str) -> Optional[Any]:
        try:
            return self.openai_client.vector_stores.retrieve(store_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None  # deleted since the last run
            raise

    def _delete_remote_file(self, file_id: str) -> None:
        """Detaches a file from the vector store and deletes it from project storage."""
        try:
            self.openai_client.vector_stores.files.delete(file_id=file_id, vector_store_id=self.vector_store_id)
        except Exception:
            pass  # already detached
        try:
            self.openai_client.files.delete(file_id)
        except Exception:
            pass  # already deleted

    def list_kb_files(self) -> List[str]:
        """Returns sorted list of Markdown and PDF files in the knowledge base."""
        if self.kb_mode == "azure_blob":
            return self.blob.names()
        return sorted(f.name for pattern in KB_PATTERNS for f in KB_DIR.glob(pattern))

    def read_kb_file(self, filename: str) -> str:
        """Reads the content of a specific KB file (extracted text for PDFs)."""
        clean_name = Path(filename).name
        data = self._read_bytes(clean_name)
        if data is None:
            return ""
        if clean_name.lower().endswith(".pdf"):
            return _pdf_text(data)
        return data.decode("utf-8")

    def search_kb(self, query: str) -> List[Dict[str, str]]:
        """Searches across all KB files for relevant sections matching the query.

        Keywords are weighted by rarity (IDF), so words found in every file ("what", "and")
        don't let long documents such as the PDF manuals outrank a short, on-topic file.
        """
        keywords = {
            w.lower()
            for w in query.replace("?", " ").replace(",", " ").split()
            if len(w) > 2 or any(c.isdigit() for c in w)  # keep codes like P1
        }
        contents = {fname: self.read_kb_file(fname) for fname in self.list_kb_files()}
        lowered = {fname: text.lower() for fname, text in contents.items()}
        doc_freq = {kw: sum(kw in text for text in lowered.values()) for kw in keywords}
        results: List[Dict[str, str]] = []
        for fname, content in contents.items():
            score = sum(math.log((len(contents) + 1) / doc_freq[kw]) for kw in keywords if kw in lowered[fname])
            if score > 0 or not keywords:
                results.append(
                    {
                        "filename": fname,
                        "score": f"{score:.2f}",
                        "content": content,
                    }
                )
        results.sort(key=lambda r: float(r["score"]), reverse=True)
        return results

    def propose_kb_update(
        self, filename: str, proposed_content: str, change_summary: str
    ) -> GuardrailResult:
        """
        Runs the proposed KB file update through GuardrailEngine.
        Returns a GuardrailResult (either BLOCKED, PENDING_HITL, or PASSED).
        """
        current_content = self.read_kb_file(filename) if "/" not in filename and ".." not in filename else ""
        return self.guardrails.validate_kb_update(
            filename=filename,
            current_content=current_content,
            proposed_content=proposed_content,
            change_summary=change_summary,
        )

    def commit_kb_update(
        self, filename: str, new_content: str, change_summary: str
    ) -> Dict[str, Any]:
        """
        Saves the approved KB update (keeping a timestamped backup of the old version)
        and syncs the new file to the Foundry vector store if configured.
        """
        clean_name = Path(filename).name
        self._write_file(clean_name, new_content)

        foundry_sync_status = "Local KB file updated (Demo mode — Vector Store sync simulated)."
        if self.openai_client is not None and self.vector_store_id:
            try:
                self.sync_files()  # replaces the old copy of this file in the vector store
                foundry_sync_status = (
                    f"Synced to Foundry vector store ({self.vector_store_id}) "
                    f"with new file_id={self.remote_file_map.get(clean_name)}."
                )
            except Exception as exc:
                foundry_sync_status = f"File saved; Foundry vector store sync warning: {exc}"

        return {
            "success": True,
            "filename": clean_name,
            "change_summary": change_summary,
            "foundry_sync": foundry_sync_status,
        }
