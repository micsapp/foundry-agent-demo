"""
Knowledge Base Manager & Microsoft Foundry Vector Store Sync Tool.

Handles:
1. Searching and reading Knowledge Base files in `knowledge_base/` (editable Markdown, read-only PDF manuals)
2. Validating proposed KB file updates via GuardrailEngine (PII scan, path check, diff generation)
3. Applying approved KB updates locally AND syncing the updated file to the Foundry project's vector store
4. Reusing one vector store across runs: only new or changed files are uploaded (tracked in .kb_vector_store.json)
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from guardrails import GuardrailEngine, GuardrailResult

KB_DIR = Path(__file__).parent / "knowledge_base"
KB_BACKUP_DIR = Path(__file__).parent / ".kb_backups"
KB_PATTERNS = ("*.md", "*.pdf")  # PDFs are uploaded and searched but never edited by the agent
KB_SYNC_FILE = Path(__file__).parent / ".kb_vector_store.json"  # which store we use + hash of each uploaded file


@lru_cache(maxsize=32)
def _pdf_text(path: str, mtime: float) -> str:
    """Extracts PDF text with one "## Page N" section per page (mtime is part of the cache key)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "(Install pypdf to read PDF text locally: pip install pypdf)"
    pages = [page.extract_text() or "" for page in PdfReader(path).pages]
    return "\n\n".join(f"## Page {i}\n{text.strip()}" for i, text in enumerate(pages, 1))


class KnowledgeBaseTool:
    """Manages KB source files and syncs approved updates with Foundry vector stores."""

    def __init__(
        self,
        guardrails: GuardrailEngine,
        openai_client: Optional[Any] = None,
        vector_store_id: Optional[str] = None,
    ) -> None:
        self.guardrails = guardrails
        self.openai_client = openai_client  # from AIProjectClient.get_openai_client()
        self.vector_store_id = vector_store_id
        self.remote_file_map: Dict[str, str] = {}  # filename -> foundry file_id
        self._sync_state: Dict[str, Any] = {}  # contents of KB_SYNC_FILE
        KB_DIR.mkdir(parents=True, exist_ok=True)
        KB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_initial_backups()

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
                shutil.copy2(md_file, KB_DIR / md_file.name)
        if self.openai_client is not None and self.vector_store_id:
            self.sync_files()

    # ------------------------------------------------------------------
    # Foundry vector store: one store per project, reused across runs
    # ------------------------------------------------------------------
    def connect_vector_store(
        self, openai_client: Any, store_name: str, project_endpoint: str, store_id: str = ""
    ) -> str:
        """
        Picks the vector store and uploads only the KB files that changed since the last run.

        Uses, in order: `store_id` (AZURE_VECTOR_STORE_ID), the store recorded in .kb_vector_store.json
        for this project, the newest store named `store_name`, or a new store. Returns a status summary.
        """
        self.openai_client = openai_client
        state = self._load_sync_state()
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
        return f"vector store {store.id} ({how}; {self.sync_files()})"

    def sync_files(self) -> str:
        """Makes the vector store match knowledge_base/: uploads new or changed files, removes deleted ones."""
        client, store_id = self.openai_client, self.vector_store_id
        known: Dict[str, Dict[str, str]] = self._sync_state.setdefault("files", {})
        remote = {f.id: f.status for f in client.vector_stores.files.list(vector_store_id=store_id, limit=100)}
        local = self.list_kb_files()
        if not known and remote:  # a store we haven't tracked yet: drop its old copies of our files
            for file_id in remote:
                if client.files.retrieve(file_id).filename in local:
                    self._delete_remote_file(file_id)
        uploaded = unchanged = 0
        for fname in local:
            data = (KB_DIR / fname).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            entry = known.get(fname)
            if entry and entry["sha256"] == digest and remote.get(entry["file_id"]) == "completed":
                unchanged += 1
            else:
                if entry:
                    self._delete_remote_file(entry["file_id"])
                vs_file = client.vector_stores.files.upload_and_poll(vector_store_id=store_id, file=(fname, data))
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

    @staticmethod
    def _load_sync_state() -> Dict[str, Any]:
        try:
            return json.loads(KB_SYNC_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_sync_state(self) -> None:
        KB_SYNC_FILE.write_text(json.dumps(self._sync_state, indent=2), encoding="utf-8")

    def list_kb_files(self) -> List[str]:
        """Returns sorted list of Markdown and PDF files in the knowledge base."""
        return sorted(f.name for pattern in KB_PATTERNS for f in KB_DIR.glob(pattern))

    def read_kb_file(self, filename: str) -> str:
        """Reads the content of a specific KB file (extracted text for PDFs)."""
        clean_name = Path(filename).name
        target = KB_DIR / clean_name
        if not target.exists():
            return ""
        if target.suffix.lower() == ".pdf":
            return _pdf_text(str(target), target.stat().st_mtime)
        return target.read_text(encoding="utf-8")

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
        Writes the approved KB update to disk, creates a timestamped backup,
        and syncs the new file to the Foundry vector store if configured.
        """
        clean_name = Path(filename).name
        target_path = KB_DIR / clean_name

        # 1. Backup existing version before overwriting
        if target_path.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = KB_BACKUP_DIR / f"{clean_name}.{ts}.bak"
            shutil.copy2(target_path, backup_path)

        # 2. Write updated content to the KB source file
        target_path.write_text(new_content, encoding="utf-8")

        # 3. Sync with the Foundry vector store if the OpenAI client is connected
        foundry_sync_status = "Local KB file updated (Demo mode — Vector Store sync simulated)."
        if self.openai_client is not None and self.vector_store_id:
            try:
                self.sync_files()  # replaces the old copy of this file in the vector store
                foundry_sync_status = (
                    f"Synced to Foundry vector store ({self.vector_store_id}) "
                    f"with new file_id={self.remote_file_map.get(clean_name)}."
                )
            except Exception as exc:
                foundry_sync_status = f"Local file saved; Foundry vector store sync warning: {exc}"

        return {
            "success": True,
            "filename": clean_name,
            "change_summary": change_summary,
            "foundry_sync": foundry_sync_status,
        }
