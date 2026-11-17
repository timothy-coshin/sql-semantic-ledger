"""sql-semantic-ledger: a certified, evolving semantic layer built from SQL usage histories and service code.

Reference implementation of LEDGER (Lifecycle of Evidence-graded Definitions: Generation, Evaluation,
Revalidation). See ``sql_semantic_ledger.features`` for what is implemented and what is still planned.
Only the core model is imported here; import the other modules explicitly, e.g.
``from sql_semantic_ledger.induce import induce``.
"""
from .features import FEATURES
from .model import Definition, Event, Evidence, EvidenceKind, Grade, Kind, Ledger

__version__ = '0.1.0a1'
__all__ = ['Definition', 'Event', 'Evidence', 'EvidenceKind', 'Grade', 'Kind', 'Ledger', 'FEATURES', '__version__']
