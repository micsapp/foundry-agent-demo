"""
Knowledge Base accuracy check: asks the agent every question in kb_eval_questions.json and
checks each answer for the expected facts.

    python3 kb_eval.py                 # all questions
    python3 kb_eval.py q t05 n         # only ids starting with q, t05 or n
    python3 kb_eval.py -v              # also print every answer
    python3 kb_eval.py --out results.json

Uses the AGENT_MODE from .env. Meant for AGENT_MODE=foundry: demo mode only does a local keyword
lookup, so most answers there won't pass. Each question gets a fresh Foundry conversation so
earlier answers can't help later ones. Like the Streamlit app, it reuses the project's vector
store and agent version, uploading only Knowledge Base files that changed.

Questions the Knowledge Base can't answer must also be logged with `log_feedback`. The entries the
agent logs during the eval are removed from the feedback log at the end (add --keep-log to keep them).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from agent_service import FoundryAgentOrchestrator

QUESTIONS_FILE = Path(__file__).parent / "kb_eval_questions.json"

UNKNOWN_PHRASES = (
    "not mention", "doesn't mention", "does not mention", "no mention", "not in the knowledge base",
    "no information", "not specified", "doesn't specify", "does not specify", "couldn't find",
    "could not find", "can't find", "cannot find", "unable to find", "not found", "not covered",
    "isn't covered", "doesn't include", "does not include", "not documented", "isn't documented",
    "doesn't say", "does not say", "not stated", "doesn't state", "does not state", "don't have",
    "do not have", "doesn't contain", "does not contain", "not available in", "no details",
    "does not document", "doesn't document", "does not list", "doesn't list", "not described",
)


def normalize(text: str) -> str:
    text = text.lower().replace("**", "").replace("`", "")
    text = text.translate({0x2011: "-", 0x2013: "-", 0x2019: "'", 0x00A0: " "})  # odd hyphens, ’, nbsp
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # 50,000 -> 50000
    text = re.sub(r"\s*\+\s*", "+", text)  # Ctrl + Shift + J -> ctrl+shift+j
    return re.sub(r"\s+", " ", text)


def contains(text: str, needle: str) -> bool:
    """Numbers and short words must match as whole tokens ("5" must not match "15"); longer words may be stems."""
    needle = normalize(needle)
    if len(needle) <= 3 or needle[0].isdigit() or needle[-1].isdigit():
        return re.search(rf"(?<![\w.]){re.escape(needle)}(?!\w|\.\d)", text) is not None
    return needle in text


def grade(q: dict, answer: str, logged: bool | None = None) -> tuple[bool, list[str]]:
    """`logged`: whether the agent logged the question as unanswered (None = not checked)."""
    text = normalize(answer)
    problems = [f"forbidden /{pat}/" for pat in q.get("reject", []) if re.search(pat, answer)]
    if q.get("expect_unknown"):
        if not any(p in text for p in UNKNOWN_PHRASES):
            problems.append("did not say the Knowledge Base lacks this")
        if logged is False:
            problems.append("did not log the unanswered question")
    for item in q.get("expect", []):
        options = item if isinstance(item, list) else [item]
        if not any(contains(text, o) for o in options):
            problems.append("missing " + " | ".join(options))
    return not problems, problems


def run_questions(orch: FoundryAgentOrchestrator, questions: list[dict], verbose: bool, results: list[dict]) -> int:
    """Asks each question in a fresh conversation and appends the graded result; returns how many passed."""
    passed = 0
    for q in questions:
        if orch.openai_client is not None:
            orch.foundry_conversation_id = orch.openai_client.conversations.create().id
        start = time.monotonic()
        turn = orch.handle_user_message(q["question"])
        secs = time.monotonic() - start
        answer = turn.reply_markdown
        logged = [t["arguments"].get("kind") for t in turn.tool_traces if t["tool"] == "log_feedback"]
        ok, problems = grade(q, answer, logged="unanswered_question" in logged)
        passed += ok
        note = "  (logged as unanswered)" if "unanswered_question" in logged and not q.get("expect_unknown") else ""
        print(f"{'PASS' if ok else 'FAIL'}  {q['id']}  {secs:5.1f}s  {q['question']}{note}")
        if not ok:
            print(f"        expected: {q['answer']}")
            print(f"        problems: {'; '.join(problems)}")
        if verbose or not ok:
            snippet = answer if verbose else answer[:400] + ("…" if len(answer) > 400 else "")
            print("        answer:   " + snippet.replace("\n", "\n                  "))
        results.append({**q, "agent_answer": answer, "logged": logged, "passed": ok, "problems": problems,
                        "seconds": round(secs, 1), "_traces": turn.tool_traces})
    return passed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="only run questions whose id starts with one of these")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every answer")
    ap.add_argument("--out", help="write questions, answers and grades to this JSON file")
    ap.add_argument("--keep-log", action="store_true", help="keep the feedback-log entries the agent wrote")
    args = ap.parse_args()

    load_dotenv()
    questions = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))["questions"]
    if args.ids:
        questions = [q for q in questions if q["id"].startswith(tuple(args.ids))]
    mode = os.getenv("AGENT_MODE", "demo")
    print(f"AGENT_MODE={mode}  model={os.getenv('MODEL_DEPLOYMENT_NAME', '-')}  questions={len(questions)}")
    if mode != "foundry":
        print("Note: demo mode is a local keyword lookup, not an LLM; set AGENT_MODE=foundry for a real test.")

    orch = FoundryAgentOrchestrator(
        agent_mode=mode, db_mode=os.getenv("DB_MODE", "sqlite_mock"), kb_mode=os.getenv("KB_MODE", "local")
    )
    if mode == "foundry":
        print(f"Foundry: {orch.foundry_status}")
        if orch.openai_client is None:
            return 1
    print()

    results: list[dict] = []
    try:
        passed = run_questions(orch, questions, args.verbose, results)
    finally:
        entry_ids = [t["entry_id"] for r in results for t in r.pop("_traces") if t.get("entry_id")]
        if entry_ids and not args.keep_log:
            try:
                removed = orch.feedback_log.delete(entry_ids)
                print(f"\nRemoved the {removed} feedback-log entries this run wrote ({orch.feedback_log.location}).")
            except Exception as exc:
                print(f"\nCouldn't remove this run's feedback-log entries {', '.join(entry_ids)} from "
                      f"{orch.feedback_log.location}: {type(exc).__name__}. Delete them by hand, or `az login` and re-run.")

    groups: dict[str, list[bool]] = {}
    for r in results:
        groups.setdefault(r["id"][0], []).append(r["passed"])
    names = {"q": "Quorvia MQ", "t": "Tallybrook Ledger", "f": "Fernwick Field Service",
             "x": "Cross-document", "n": "Not in KB (should say so)"}
    print("\nScore by group:")
    for key, marks in groups.items():
        print(f"  {names.get(key, key):28} {sum(marks)}/{len(marks)}")
    print(f"\nTotal: {passed}/{len(results)} passed")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {args.out}")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
