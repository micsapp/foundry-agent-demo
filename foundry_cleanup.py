"""
Removes Foundry objects this demo leaves behind in your project.

    python3 foundry_cleanup.py              # show what exists and what would be removed (changes nothing)
    python3 foundry_cleanup.py --yes        # remove old agent versions, unused KB vector stores and old files
    python3 foundry_cleanup.py --all --yes  # remove everything the demo created, including both agents and the
                                            # vector store in use; the next run uploads everything again (the
                                            # public chat page is down until then)

Only touches the agents named Oracle-And-KB-Guarded-Agent and Oracle-And-KB-Public-Agent (the public chat page's),
vector stores named Enterprise-Oracle-KB-Store, and uploaded files named like the Knowledge Base files.
Conversations can't be listed, so they're left alone.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

from agent_service import FOUNDRY_AGENT_NAME, KB_VECTOR_STORE_NAME, PUBLIC_AGENT_NAME
from kb_tool import KB_DIR, KB_PATTERNS, KB_SYNC_FILE, KnowledgeBaseTool


def when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually delete (otherwise only report)")
    ap.add_argument("--all", action="store_true", help="also delete the agent and the vector store in use")
    args = ap.parse_args()

    load_dotenv()
    endpoint = (os.getenv("PROJECT_ENDPOINT") or os.getenv("FOUNDRY_PROJECT_ENDPOINT") or "").strip()
    if not endpoint:
        print("PROJECT_ENDPOINT is not set in .env.")
        return 1

    from azure.ai.projects import AIProjectClient
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    oai = project.get_openai_client()
    print(f"Foundry project: {endpoint}\n")

    # --- Agent versions: keep each agent's latest one (the app reuses it while its definition is unchanged)
    agents: dict[str, tuple[list, list]] = {}  # name -> (all versions, versions to remove)
    for name in (FOUNDRY_AGENT_NAME, PUBLIC_AGENT_NAME):
        try:
            versions = sorted((v.as_dict() for v in project.agents.list_versions(agent_name=name)),
                              key=lambda v: v.get("created_at") or 0)
        except ResourceNotFoundError:
            versions = []
        agents[name] = (versions, versions if args.all else versions[:-1])
        print(f'Agent "{name}": {len(versions)} version(s)')
        for v in versions:
            verdict = "remove" if v in agents[name][1] else "keep (latest)"
            print(f"  v{v['version']:<4} {when(v.get('created_at'))}   {verdict}")

    # --- Vector stores: keep the ones the app uses
    state = KnowledgeBaseTool._load_sync_state()
    keep_stores: set[str] = set()
    if not args.all:
        if state.get("project_endpoint") == endpoint and state.get("vector_store_id"):
            keep_stores.add(state["vector_store_id"])
        if os.getenv("AZURE_VECTOR_STORE_ID", "").strip():
            keep_stores.add(os.getenv("AZURE_VECTOR_STORE_ID", "").strip())
        for versions, _ in agents.values():
            for tool in (versions[-1] if versions else {}).get("definition", {}).get("tools", []):
                keep_stores.update(tool.get("vector_store_ids", []))
    stores = [s for s in oai.vector_stores.list(limit=100) if s.name == KB_VECTOR_STORE_NAME]
    old_stores = [s for s in stores if s.id not in keep_stores]
    print(f'\nVector stores named "{KB_VECTOR_STORE_NAME}": {len(stores)}')
    for s in stores:
        verdict = "remove" if s in old_stores else "keep (in use)"
        print(f"  {s.id}  {s.file_counts.completed:>3} file(s)  {when(s.created_at)}   {verdict}")

    # --- Uploaded files: those in removed stores, plus stray copies of KB files not used by a kept store
    def store_file_ids(store_id: str) -> set[str]:
        try:
            return {f.id for f in oai.vector_stores.files.list(vector_store_id=store_id, limit=100)}
        except Exception:
            return set()

    kept_files = set().union(*(store_file_ids(sid) for sid in keep_stores))
    kb_names = {f.name for pattern in KB_PATTERNS for f in KB_DIR.glob(pattern)} | set(state.get("files", {}))
    all_files = {f.id: f for f in oai.files.list()}
    old_files = set().union(*(store_file_ids(s.id) for s in old_stores))
    old_files |= {fid for fid, f in all_files.items() if f.filename in kb_names}
    old_files -= kept_files
    print(f"\nUploaded Knowledge Base files: {len(kept_files) + len(old_files)} "
          f"({len(kept_files)} in use, {len(old_files)} to remove)")
    for fid in sorted(old_files, key=lambda i: getattr(all_files.get(i), "filename", "")):
        f = all_files.get(fid)
        print(f"  {fid}  {getattr(f, 'filename', '?'):<42} {when(getattr(f, 'created_at', None))}   remove")

    old_version_count = sum(len(old) for _, old in agents.values())
    total = old_version_count + len(old_stores) + len(old_files)
    summary = f"{old_version_count} agent version(s), {len(old_stores)} vector store(s), {len(old_files)} file(s)"
    if not total:
        print("\nNothing to remove.")
        return 0
    if not args.yes:
        print(f"\nWould remove {summary}. Run again with --yes to remove them.")
        return 0

    print(f"\nRemoving {summary}…")
    failures = 0
    steps = []
    for name, (versions, old_versions) in agents.items():
        if args.all and versions:
            steps.append((f"agent {name}", lambda name=name: project.agents.delete(agent_name=name)))
        else:
            steps += [(f"{name} v{v['version']}",
                       lambda name=name, v=v: project.agents.delete_version(agent_name=name, agent_version=v["version"]))
                      for v in old_versions]
    steps += [(f"vector store {s.id}", lambda s=s: oai.vector_stores.delete(s.id)) for s in old_stores]
    steps += [(f"file {fid}", lambda fid=fid: oai.files.delete(fid)) for fid in sorted(old_files)]
    for label, step in steps:
        try:
            step()
            print(f"  ✓ {label}")
        except Exception as exc:
            failures += 1
            print(f"  ✗ {label}: {type(exc).__name__}: {exc}")
    if args.all and state.get("project_endpoint") == endpoint:
        KB_SYNC_FILE.unlink(missing_ok=True)  # the next run starts from an empty store
    print(f"\nDone{f' with {failures} error(s)' if failures else ''}.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
