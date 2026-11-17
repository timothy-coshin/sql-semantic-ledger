"""Interface for definitions proposed by a language model (grade E0 until evidence arrives). Planned."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from ._unimplemented import planned
from .model import Definition


class DefinitionProposer(Protocol):
    def propose(self, schema: Dict[str, Any], question: Optional[str] = None) -> List[Definition]:
        """Return candidate definitions (source='model'); they stay E0 until supported by evidence."""
        ...


def propose_with_model(*args, **kwargs):
    """Ask a language model for candidate definitions. Planned."""
    planned('trust.engine_nl')
