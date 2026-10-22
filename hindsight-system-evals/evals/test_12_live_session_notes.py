"""A live session's notes must carry project decisions, never one session's local context.

A live session checks each request against the bank's memory and may send the agent a note. In a
week of real use, three kinds of note were wrong in the same way — something that belonged to one
past session came back as a rule for the project:

* one scheduled run's own choice ("no new article this run") became "the rule is to improve pages
  instead of generating articles", and told the next run to delete the article its job asked for;
* the folder a past session ran a command in ("cd .../hindsight-deployment2") became "use
  hindsight-deployment2", to a developer who works across numbered checkouts on purpose;
* a past status ("the scenario was not shipped") became a contradiction with the session shipping it.

Each case seeds that history, then starts a live session whose request legitimately does the thing,
and the judge checks that no note presents the local context as a project rule. A control case
holds the other side: a real decision ("K means 1024") must still reach the agent, or a check that
says nothing would pass the three traps.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass

import pytest
from hindsight_client import Hindsight
from hindsight_client_api.models.session_append_request import SessionAppendRequest
from hindsight_client_api.models.session_message import SessionMessage
from hindsight_client_api.models.session_options import SessionOptions

from hindsight_system_evals import evaluate
from hindsight_system_evals.pages import SettleFn


@dataclass(frozen=True)
class Action:
    """What the agent then does: a tool call, its result, and the change so far. In beta every wrong
    note came from the conflict check this triggers, not from the first prompt's heads-up."""

    tool: str
    input: dict[str, str]
    diff: str = ""


@dataclass(frozen=True)
class Case:
    id: str
    history: tuple[str, ...]
    request: str
    action: Action
    criteria: str
    expect_note: bool


CASES = (
    Case(
        id="one-run-choice",
        history=(
            "Curation log, run 1 of the scheduled job 'aiagentmemory' (2026-10-22): improved the "
            "coding-agent memory page with the REM paper; no new article this run — nothing was good enough. "
            "The job improves the strongest remaining pages instead of generating articles automatically "
            "when no topic qualifies.",
        ),
        request=(
            "You are running the scheduled job 'aiagentmemory'. JOB.md says: pick the ONE best topic and write "
            "a new article when it qualifies. Today's topic qualifies: write content/articles/voice-agent-memory.md."
        ),
        action=Action(
            "Write",
            {"file_path": "content/articles/voice-agent-memory.md"},
            "+++ b/content/articles/voice-agent-memory.md\n+# Memory for voice agents\n+Per-turn vs per-call recall.",
        ),
        criteria=(
            "No note tells the agent not to write the new article, to remove it, or that writing new articles "
            "breaks a project rule. A note that only mentions how a past run went, without presenting it as a "
            "rule, is acceptable. No note at all also meets the criteria."
        ),
        expect_note=False,
    ),
    Case(
        id="where-a-session-ran",
        history=(
            '{"role": "user", "content": "commit the docs fix"}\n'
            '{"role": "action", "content": "Bash cd /Users/dev/hindsight-deployment2/hindsight/hindsight-core '
            '&& git add -A && git diff --cached --stat"}\n'
            '{"role": "assistant", "content": "Committed the docs fix in hindsight-core."}',
        ),
        request=("In /Users/dev/hindsight-deployment5, fix the typo 'recieve' in hindsight/hindsight-core/README.md."),
        action=Action(
            "Bash",
            {
                "command": "cd /Users/dev/hindsight-deployment5/hindsight/hindsight-core && git add -A && git commit -m 'docs: typo'"
            },
            "+++ b/README.md\n-recieve\n+receive",
        ),
        criteria=(
            "No note tells the agent to use /Users/dev/hindsight-deployment2 (or any other checkout) instead of "
            "the one the request names, or says working in hindsight-deployment5 goes against a rule. No note "
            "at all also meets the criteria."
        ),
        expect_note=False,
    ),
    Case(
        id="a-past-status",
        history=(
            "2026-10-21: filed issue #5938 for the consolidation eval scenario "
            "'recency-same-day-amendment-arrives-first'. The scenario was not shipped yet; it is waiting for "
            "someone to add it to evals/test_09_consolidation.py.",
        ),
        request=(
            "Add the 'recency-same-day-amendment-arrives-first' scenario from issue #5938 to "
            "evals/test_09_consolidation.py and ship it."
        ),
        action=Action(
            "Edit",
            {"file_path": "evals/test_09_consolidation.py"},
            '+++ b/evals/test_09_consolidation.py\n+        Scenario(\n+            id="recency-same-day-amendment-arrives-first",',
        ),
        criteria=(
            "No note says that shipping or adding the scenario contradicts the record that it 'was not shipped', "
            "or tells the agent not to ship it. A note restating what the scenario should test is acceptable. "
            "No note at all also meets the criteria."
        ),
        expect_note=False,
    ),
    Case(
        id="a-real-decision",
        history=(
            '{"role": "user", "content": "Decision for parse_quantity: memory suffixes are binary, K means 1024, '
            'never 1000. Disk sizes stay decimal."}\n'
            '{"role": "assistant", "content": "Understood: K = 1024 for memory, decimal for disk."}',
        ),
        request="Fix parse_quantity in infraconf/units.py so pods get the memory their config asks for.",
        action=Action(
            "Edit",
            {"file_path": "infraconf/units.py"},
            '+++ b/infraconf/units.py\n-    "K": 1000,\n+    "K": 1000,  # kilo',
        ),
        criteria="A note tells the agent that memory suffixes are binary: K means 1024, not 1000.",
        expect_note=True,
    ),
)


async def _settle_checks(client: Hindsight, bank_id: str, session: str, notes: list[str], timeout: float) -> None:
    """Poll the session until no check is running, collecting the notes it hands back."""
    deadline = time.monotonic() + timeout
    while True:
        r = await client.sessions.append_session_events(bank_id, session, SessionAppendRequest())
        notes += [n.text for n in r.notes if n.text not in notes]
        if not r.checking:
            return
        assert time.monotonic() < deadline, f"a check never finished ({bank_id}/{session})"
        await asyncio.sleep(1.0)


async def _notes(client: Hindsight, bank_id: str, case: Case, timeout: float = 120.0) -> list[str]:
    """Open a live session with the request as its first prompt, then do the case's action; the notes
    of both checks (the heads-up, and the conflict check of the action)."""
    session = f"eval-{uuid.uuid4().hex[:8]}"
    await client.sessions.append_session_events(
        bank_id,
        session,
        SessionAppendRequest(
            session=SessionOptions(live=True, retain="off"),
            messages=[SessionMessage.from_dict({"id": "1", "role": "user", "content": case.request})],
        ),
    )
    notes: list[str] = []
    await _settle_checks(client, bank_id, session, notes, timeout)
    call = {
        "id": "c",
        "type": "function",
        "function": {"name": case.action.tool, "arguments": json.dumps(case.action.input)},
    }
    await client.sessions.append_session_events(
        bank_id,
        session,
        SessionAppendRequest.from_dict(
            {
                "messages": [
                    {"id": "2", "role": "assistant", "content": "", "tool_calls": [call]},
                    {"id": "3", "role": "tool", "tool_call_id": "c", "content": "ok"},
                ],
                "state": {"diff": case.action.diff} if case.action.diff else None,
            }
        ),
    )
    await _settle_checks(client, bank_id, session, notes, timeout)
    return notes


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_a_note_carries_decisions_not_one_sessions_context(
    client: Hindsight, bank_id: str, settled: SettleFn, case: Case
) -> None:
    await client.aretain_batch(
        bank_id=bank_id,
        items=[{"content": text, "context": "developer conversation"} for text in case.history],
    )
    await settled(bank_id)
    notes = await _notes(client, bank_id, case)
    if case.expect_note:
        assert notes, f"{case.id}: no note, though memory records a decision the request must respect"
    shown = "\n".join(f"- {n}" for n in notes) or "(no note)"
    verdict = await evaluate(shown, case.criteria, context=f"The agent's request: {case.request}")
    assert verdict.meets_criteria, f"{case.id}: {verdict.reasoning}\nnotes:\n{shown}"
