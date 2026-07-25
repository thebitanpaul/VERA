"""Step [8] -- escalate an unanswered question to the designated team.

The work-item payload is provider-neutral; only the transport differs between
the demo file sink and the production Azure DevOps MCP sink. Switching to
production changes one environment variable, not the payload.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path

from .config import settings
from .registry import _norm_trial_id

log = logging.getLogger(__name__)


def build_work_item(title: str, trial_id: str, question: str, reason: str) -> dict:
    """The provider-neutral work-item summary of an unanswered question."""
    return {
        "type": "Issue",
        "title": title,
        "trial": _norm_trial_id(trial_id),
        "question": question,
        "reason": reason,
        "source": "VERA",
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }


def default_title(trial_id: str) -> str:
    return f"VERA: unanswered question for Trial {_norm_trial_id(trial_id)}"


def to_ado_payload(item: dict) -> dict:
    """Map the neutral work item onto the fields an ADO 'create work item' call
    expects. This shape is what the MCP tool receives in production."""
    desc = (f"Unanswered question escalated by VERA.\n\n"
            f"Trial: {item['trial']}\n"
            f"Question: {item['question']}\n"
            f"Reason for abstention: {item['reason']}\n"
            f"Raised at: {item['created_at']}")
    return {
        "project": settings.ado_project,
        "workItemType": item["type"],
        "fields": {
            "System.Title": item["title"],
            "System.Description": desc,
            "System.Tags": "VERA; escalation; abstention",
        },
    }


class EscalationSink:
    """Where an escalation goes. The ONLY thing that changes for production is
    which sink is selected; the work-item payload is identical."""

    mode = "base"

    def send(self, item: dict) -> dict:
        raise NotImplementedError


class FileEscalationSink(EscalationSink):
    """Demo sink: write the work item as JSON and report the ADO payload that
    WOULD be sent to the MCP server."""

    mode = "file"

    def __init__(self, out_dir: Path | None = None):
        self.out_dir = Path(out_dir) if out_dir else settings.escalation_dir

    def send(self, item: dict) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        path = self.out_dir / f"{stamp}-trial{item['trial']}.json"
        path.write_text(json.dumps(item, indent=2), encoding="utf-8")
        payload = to_ado_payload(item)
        log.info("escalation written to %s (ADO MCP not configured)", path)
        return {"mode": self.mode, "path": str(path),
                "work_item": item, "ado_payload": payload}


class AdoMcpEscalationSink(EscalationSink):
    """Production sink: create the work item through the ADO MCP server.

    The MCP call is the single production swap point. In this demo the MCP
    client is not wired into the process, so this constructs the exact payload
    and marks where the tool call goes.
    """

    mode = "mcp"

    def send(self, item: dict) -> dict:
        payload = to_ado_payload(item)
        log.info("escalation via ADO MCP tool 'wit_create_work_item': %s",
                 payload["fields"]["System.Title"])
        # --- Production: the ONE line that changes for real ADO integration ---
        #   result = mcp_client.call_tool("wit_create_work_item", payload)
        #   return {"mode": self.mode, "id": result["id"], ...}
        return {"mode": self.mode, "work_item": item, "ado_payload": payload,
                "note": ("demo: MCP client not attached in-process; wire "
                         "mcp_client.call_tool here for production")}


def get_escalation_sink() -> EscalationSink:
    """ADO MCP sink when ``VERA_ADO_MCP`` is truthy; else the file sink."""
    return AdoMcpEscalationSink() if settings.ado_mcp_enabled else FileEscalationSink()
