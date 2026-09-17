"""Magentic completion is controlled by the investigator's accepted tool result."""

import json
from agent_framework import Message
from agent_framework.orchestrations import (
    StandardMagenticManager,
    MagenticProgressLedger,
)


class InvestigatorManager(StandardMagenticManager):
    def __init__(self, *, tools, **kwargs):
        super().__init__(**kwargs)
        self.tools = tools

    async def create_progress_ledger(self, magentic_context):
        if self.tools.completed_decision is not None:
            ledger = MagenticProgressLedger.from_dict(
                {
                    k: {"answer": v, "reason": "Investigator completion accepted"}
                    for k, v in {
                        "is_request_satisfied": True,
                        "is_in_loop": False,
                        "is_progress_being_made": True,
                        "next_speaker": "RCAInvestigator",
                        "instruction_or_question": "Return accepted diagnosis",
                    }.items()
                }
            )
        else:
            ledger = await super().create_progress_ledger(magentic_context)
        accepted = self.tools.completed_decision is not None
        attempted = ledger.is_request_satisfied.answer
        ledger.is_request_satisfied.answer = accepted
        ledger.is_request_satisfied.reason = (
            "Investigator completion accepted"
            if accepted
            else "Investigator must submit supported diagnosis"
        )
        if not accepted:
            ledger.next_speaker.answer = "RCAInvestigator"
            if attempted:
                ledger.instruction_or_question.answer = "Continue investigating. Inspect exact KB facts, execute supporting Prolog, then call finish_investigation with diagnosis, evidence, binding references and assumptions."
                ledger.is_progress_being_made.answer = True
                ledger.is_in_loop.answer = False
        self.tools.trace.emit(
            "completion_gate",
            ledger.to_dict(),
            accepted=accepted,
            manager_requested_completion=attempted,
        )
        return ledger

    async def prepare_final_answer(self, magentic_context):
        result = (
            self.tools.completed_decision
            or self.tools.latest_decision
            or {"answers": [], "investigation_status": "incomplete"}
        )
        self.tools.trace.emit("manager_final", result)
        return Message(
            role="assistant", contents=[json.dumps(result)], author_name="RCAManager"
        )
