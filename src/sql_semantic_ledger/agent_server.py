"""Ledger server for data agents (MCP). Planned for stage 2 (years 4-5).

Intended tools: find_definitions(name|table), decide(definition_ids), error_bound(definition_ids),
record_answer(answer_id, definition_ids, tables).
"""
from __future__ import annotations

from ._unimplemented import planned

TOOLS = ('find_definitions', 'decide', 'error_bound', 'record_answer')


def serve(ledger_path: str, host: str = '127.0.0.1', port: int = 8765):
    """Serve a ledger to data agents over MCP. Planned (stage 2)."""
    planned('trust.agent_server')
