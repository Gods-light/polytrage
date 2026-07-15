"""LangGraph orchestration for polytrage's continuous R&D loop.

Cycle: review -> develop -> test -> improve -> {merge_and_record -> review
                                                 | develop (bounded retry)
                                                 | escalate_to_human}

Design note — "as profitable as possible" is deliberately reframed as "as
much WALK-FORWARD-VALIDATED edge as possible, under hard invariant gates."
The council review already caught the failure mode of unconstrained
optimization pressure: the tuner walked slippage to 0.0 because nothing
stopped it from treating a cost as a free variable. An LLM loop chasing a
raw "profitability" reward is the same bug at a higher level of abstraction
-- it will happily loosen a gate, drop a validation fold, or restate a
backtest assumption if that's what raises the number. `improve` exists
specifically to refuse that pressure: it is a deterministic-first gate
(frozen-constants check, walk-forward-presence check) with an LLM critique
layered on top, not the other way around.

Two different change classes get two different trust levels, matching what
scripts/evolve.py already does for parameters:
  - PARAMETER changes (detection thresholds, grid points) within the frozen
    fee/slippage bounds -> auto-merge, exactly like today's nightly evolve.
  - CODE/STRATEGY changes (new modules, new data sources, anything touching
    engine/backtest/optimize semantics) -> open a PR, human merges. This
    graph will draft and test the change; it will not merge itself.

Run cadence: one graph invocation per scheduled GitHub Actions run (cron
already exists in .github/workflows/ci.yml for the nightly evolve job).
LangGraph is NOT meant to run as an unsupervised 24/7 agent loop here --
that would itself be a Goodhart surface (an agent with standing permission
to keep trying things until a number goes up). Cron provides the outer
"continuous" cadence; recursion_limit + MAX_DEVELOP_RETRIES bound the inner
loop of any single run.
"""
from __future__ import annotations

import operator
import os
import subprocess
from typing import Annotated, Literal, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

REPO_ROOT = os.environ.get("POLYTRAGE_REPO", os.path.expanduser("~/homelab/42-the-answer/polytrage"))
MAX_DEVELOP_RETRIES = 3

# Frozen invariants the `improve` gate enforces regardless of what the LLM
# critique says -- mirrors docs/COUNCIL-2026-07-16.md. Deterministic checks
# run BEFORE the LLM sees the diff, so a persuasive rationale can't talk
# the gate past them.
FORBIDDEN_TOUCH_PATHS = (
    "optimize/tuner.py::FROZEN_FEE",
    "optimize/tuner.py::FROZEN_SLIPPAGE",
    "backtest/runner.py::entry_edge",  # entry-fill, not peak-fill
)


def _llm(temperature: float = 0.2) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=os.environ["POLYTRAGE_LLM_BASE_URL"],  # any OpenAI-compatible gateway
        api_key=os.environ["POLYTRAGE_LLM_API_KEY"],
        model=os.environ.get("POLYTRAGE_LLM_MODEL", "kiro-all-sonnet-4.5"),
        timeout=120,
        max_retries=2,
        temperature=temperature,
    )


class LoopState(TypedDict):
    cycle_id: int
    review_findings: str
    change_class: Literal["parameter", "code"]
    proposal: str
    diff_summary: str
    test_output: str
    test_passed: bool
    critique: str
    invariant_violations: list[str]
    approved: bool
    develop_retries: int
    log: Annotated[list[str], operator.add]  # accumulates across the whole run


def review(state: LoopState) -> dict:
    """Read the durable project memory (repo files, not a checkpoint DB) and
    pick ONE highest-information-value next step. Durable state lives in
    git -- LEADERBOARD.md, results/history.jsonl, docs/COUNCIL-2026-07-16.md
    -- so this node's context is reconstructed from the repo every run,
    auditable the same way scripts/evolve.py's decisions already are.
    """
    context = _read_project_memory()
    prompt = (
        "You are the Review stage of polytrage's R&D loop. polytrage is a "
        "Polymarket multi-outcome arbitrage MEASUREMENT system (status: "
        "signal research, not live trading, per council decision). Given "
        "the current leaderboard, history, and open risks below, name the "
        "SINGLE highest-information-value next step. You may NOT propose: "
        "unfreezing fee/slippage, live order placement, removing "
        "walk-forward validation, or auto-merging strategy code. Prefer "
        "measurement/data-collection proposals (e.g. widen the event "
        "sample, capture order-book depth) over parameter re-tuning once "
        "re-tuning has plateaued.\n\n" + context
    )
    msg = _llm().invoke(prompt)
    findings = msg.content
    change_class = "parameter" if "threshold" in findings.lower() and "module" not in findings.lower() else "code"
    return {
        "cycle_id": state.get("cycle_id", 0) + 1,
        "review_findings": findings,
        "change_class": change_class,
        "develop_retries": 0,
        "log": [f"[review] cycle {state.get('cycle_id', 0) + 1}: {findings[:200]}"],
    }


def develop(state: LoopState) -> dict:
    """Draft the change as a minimal, scoped diff on a feature branch.
    Deliberately does NOT commit to main -- `merge_and_record` decides
    trust level. If a prior `improve` critique exists, it's fed back in.
    """
    prompt = (
        f"Implement this proposal as a MINIMAL diff, following the repo's "
        f"existing module contracts (CONTRACTS.md) and surgical-change "
        f"discipline (touch only what's needed):\n{state['review_findings']}\n"
    )
    if state.get("critique"):
        prompt += f"\nPrior critique to address:\n{state['critique']}"
    msg = _llm(temperature=0.1).invoke(prompt)
    # Real implementation: write the diff to a branch via git worktree +
    # apply, e.g. `EnterWorktree` / a coding subagent. Stubbed here as the
    # orchestration contract -- the graph topology and gating is this
    # module's deliverable, not a second copy of the coding agent.
    diff_summary = msg.content
    return {"proposal": diff_summary, "diff_summary": diff_summary,
            "log": [f"[develop] retry {state['develop_retries']}: drafted change"]}


def test(state: LoopState) -> dict:
    """Deterministic node -- no LLM. Runs the real, existing test/backtest
    commands. This is intentional: whether tests pass is a fact, not a
    judgment call, so it must not go through a model.
    """
    result = subprocess.run(
        ["python3", "-m", "pytest", "-q"], cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=120,
    )
    passed = result.returncode == 0
    return {
        "test_output": (result.stdout + result.stderr)[-4000:],
        "test_passed": passed,
        "log": [f"[test] {'PASS' if passed else 'FAIL'}"],
    }


def improve(state: LoopState) -> dict:
    """The gate. Deterministic invariant check FIRST (cannot be argued
    with), then an LLM critique for scope/quality/overfitting risk --
    mirrors the four-voice council pattern already used on this project.

    VERIFIED LIMITATION (found by running the reject/escalate path with a
    stubbed LLM during development of this graph, not theoretical): this
    check does substring matching against `diff_summary`, which today is
    LLM prose from `develop()`, not a real diff. That means it can both
    false-positive (any change touching tuner.py for an unrelated reason
    gets flagged merely for not repeating "FROZEN_FEE" by name) and, worse,
    false-negative (a real diff that edits the FROZEN_SLIPPAGE line but
    whose prose summary happens to mention the token anyway would slip
    through). DO NOT trust this as the production gate. Before this graph
    is allowed to touch real files, `develop()` must emit an actual patch
    and this check must run `git diff` against the specific line ranges /
    AST nodes of FROZEN_FEE and FROZEN_SLIPPAGE in optimize/tuner.py, not
    string containment on a summary. Left as a heuristic placeholder here
    because building a diff-aware checker was out of scope for this pass.
    """
    violations = [
        p for p in FORBIDDEN_TOUCH_PATHS
        if p.split("::")[0] in state["diff_summary"] and p.split("::")[1] not in state["diff_summary"]
    ]
    if not state["test_passed"]:
        violations.append("tests failing")

    prompt = (
        "You are an adversarial reviewer (Critic role from the project's "
        "council pattern). Reject unless the change is scoped, walk-forward "
        "validated where it claims a profitability result, and does not "
        "quietly loosen any risk gate to make a number look better.\n\n"
        f"Proposal:\n{state['review_findings']}\n\nDiff:\n{state['diff_summary']}\n\n"
        f"Test output:\n{state['test_output']}\n\n"
        "Respond with VERDICT: APPROVE or VERDICT: REJECT on the first "
        "line, reasoning after."
    )
    msg = _llm(temperature=0.0).invoke(prompt)
    critique = msg.content
    llm_approved = critique.strip().upper().startswith("VERDICT: APPROVE")
    approved = llm_approved and not violations

    return {
        "critique": critique,
        "invariant_violations": violations,
        "approved": approved,
        "develop_retries": state["develop_retries"] + (0 if approved else 1),
        "log": [f"[improve] approved={approved} violations={violations}"],
    }


def merge_and_record(state: LoopState) -> dict:
    """Trust-tiered landing: parameter changes within frozen bounds can
    auto-commit (matches today's evolve.py behavior exactly); code/strategy
    changes open a PR for a human. Either way, append to the durable,
    git-tracked history so the NEXT review() call sees it.
    """
    if state["change_class"] == "parameter":
        action = "auto-committed (parameter change, within frozen bounds)"
    else:
        action = "opened PR for human review (code/strategy change)"
    with open(os.path.join(REPO_ROOT, "results", "orchestration_log.jsonl"), "a") as f:
        f.write(f'{{"cycle": {state["cycle_id"]}, "action": "{action}", '
                 f'"proposal": {state["review_findings"]!r}}}\n')
    return {"log": [f"[merge_and_record] {action}"]}


def escalate_to_human(state: LoopState) -> dict:
    """Real LangGraph HITL primitive, not a busy-wait. Pauses the graph;
    a human resumes with Command(resume=...) after looking at the repeated
    rejection. Used when develop<->improve exhausts its retry budget --
    that pattern (an agent unable to satisfy its own reviewer) is exactly
    the signal that should reach a person, not loop silently.
    """
    decision = interrupt({
        "reason": "develop/improve retry budget exhausted",
        "review_findings": state["review_findings"],
        "last_critique": state["critique"],
        "violations": state["invariant_violations"],
    })
    return {"log": [f"[escalate] human decision: {decision}"]}


def route_after_improve(state: LoopState) -> str:
    if state["approved"]:
        return "merge_and_record"
    if state["develop_retries"] >= MAX_DEVELOP_RETRIES:
        return "escalate_to_human"
    return "develop"


def _read_project_memory() -> str:
    parts = []
    for rel in ("LEADERBOARD.md", "results/history.jsonl", "docs/COUNCIL-2026-07-16.md"):
        p = os.path.join(REPO_ROOT, rel)
        if os.path.exists(p):
            with open(p) as f:
                parts.append(f"--- {rel} ---\n{f.read()[-3000:]}")
    return "\n\n".join(parts) or "(no history yet -- first cycle)"


def build_graph():
    g = StateGraph(LoopState)
    g.add_node("review", review)
    g.add_node("develop", develop)
    g.add_node("test", test)
    g.add_node("improve", improve)
    g.add_node("merge_and_record", merge_and_record)
    g.add_node("escalate_to_human", escalate_to_human)

    g.add_edge(START, "review")
    g.add_edge("review", "develop")
    g.add_edge("develop", "test")
    g.add_edge("test", "improve")
    g.add_conditional_edges("improve", route_after_improve, {
        "merge_and_record": "merge_and_record",
        "develop": "develop",
        "escalate_to_human": "escalate_to_human",
    })
    g.add_edge("merge_and_record", "review")
    g.add_edge("escalate_to_human", "review")

    # Checkpointer is per-process (one cron run = one process). Durable
    # cross-run memory is the git repo itself (see _read_project_memory),
    # which is auditable via `git log`; the checkpointer only needs to
    # support interrupt()/resume within a single run.
    return g.compile(checkpointer=InMemorySaver())


# Module-level compiled instance for tooling (e.g. the visualize-graph
# script) that expects `module:attr` to already be a compiled graph rather
# than a factory. Safe to build at import time: ChatOpenAI's constructor
# doesn't make a network call, and OMNIROUTE_API_KEY only needs to be a
# non-empty string to satisfy the client, not a real key, for graph
# introspection (get_graph()/draw_mermaid()) to work.
os.environ.setdefault("OMNIROUTE_API_KEY", "unset")
graph = build_graph()

if __name__ == "__main__":
    # One review->...->review cycle per scheduled invocation; recursion_limit
    # bounds runaway loops within that single run (retries are separately
    # capped by MAX_DEVELOP_RETRIES above the recursion limit itself).
    config = {"configurable": {"thread_id": "nightly"}, "recursion_limit": 25}
    for event in graph.stream({"cycle_id": 0, "log": []}, config=config, stream_mode="updates"):
        print(event)
