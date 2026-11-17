"""Core data model: business definitions, evidence, evidence grades and the append-only ledger.

The ledger ("장부") keeps every business definition together with the evidence behind it and an
append-only event log, so that the trust in a definition is computed from evidence instead of a
"verified" flag.

Evidence grades (from the research plan):

    E0  proposed by a model, no evidence
    E1  the same-form definition/condition is used by >= 2 different past queries or code sites
    E2  E1, and actual data values and execution results also agree (data checks pass)
    E3  certified by a person
    E4  certified, and passed counterexample checks after certification

A failed data check or counterexample, or a person's rejection (the latest certification decision is
negative), falsifies the current version (grade E0, ``falsified``).
Revising a definition starts a new version; evidence gathered for older versions no longer counts.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

SCHEMA_VERSION = 1


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class Grade(IntEnum):
    """Evidence grade of a business definition (higher = stronger evidence)."""

    E0 = 0
    E1 = 1
    E2 = 2
    E3 = 3
    E4 = 4

    @property
    def certified(self) -> bool:
        """True for grades that include a person's certification (E3, E4)."""
        return self >= Grade.E3

    @property
    def machine_only(self) -> bool:
        """True for grades backed only by machine evidence (E1, E2)."""
        return self in (Grade.E1, Grade.E2)

    @classmethod
    def parse(cls, value: Any) -> "Grade":
        if isinstance(value, Grade):
            return value
        if isinstance(value, int):
            return cls(value)
        text = str(value).strip().upper()
        return cls[text] if text in cls.__members__ else cls(int(text.lstrip('E')))


class Kind(str, Enum):
    """What a definition describes."""

    MEASURE = 'measure'        # aggregate expression, e.g. SUM(orders.amount)
    FILTER = 'filter'          # condition, e.g. orders.status = 'paid'
    JOIN = 'join'              # join role, e.g. orders.customer_id = customers.id
    DIMENSION = 'dimension'    # grouping attribute, e.g. customers.region
    METRIC = 'metric'          # business metric = measure + applicability conditions


class EvidenceKind(str, Enum):
    MODEL = 'model'                    # proposed by a model (E0 source)
    HISTORY = 'history'                # a past query that uses the definition
    CODE = 'code'                      # service code that implements it (API SQL/ORM, UI JavaScript, ...)
    DATA_CHECK = 'data_check'          # a check run against the database
    CERTIFICATION = 'certification'    # a person's decision
    COUNTEREXAMPLE = 'counterexample'  # a counterexample check after certification


@dataclass
class Evidence:
    """One piece of evidence about one version of a definition."""

    kind: EvidenceKind
    detail: str = ''
    passed: bool = True
    ref: Optional[str] = None          # query id, check name, certifier, ...
    version: int = 1                   # definition version this evidence is about
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {'kind': self.kind.value, 'detail': self.detail, 'passed': self.passed,
                'ref': self.ref, 'version': self.version, 'at': self.at}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'Evidence':
        return cls(kind=EvidenceKind(d['kind']), detail=d.get('detail', ''), passed=bool(d.get('passed', True)),
                   ref=d.get('ref'), version=int(d.get('version', 1)), at=d.get('at') or now_iso())


def make_id(kind: 'Kind', expression: str, conditions: Iterable[str] = ()) -> str:
    """Stable short id from the kind, normalized expression and conditions."""
    key = '|'.join([kind.value, expression.strip(), '&'.join(sorted(c.strip() for c in conditions))])
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]


@dataclass
class Definition:
    """A business definition with its evidence.

    ``expression`` is a normalized SQL fragment (aggregate, predicate or join condition) and
    ``conditions`` are normalized SQL predicates that state when the definition applies.
    """

    name: str
    kind: Kind
    expression: str
    conditions: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    source: str = 'history'            # history | model | manual | yaml:<product> | ...
    evidence: List[Evidence] = field(default_factory=list)
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    version: int = 1
    meta: Dict[str, Any] = field(default_factory=dict)
    id: str = ''

    def __post_init__(self) -> None:
        if isinstance(self.kind, str) and not isinstance(self.kind, Kind):
            self.kind = Kind(self.kind)
        if not self.id:
            self.id = make_id(self.kind, self.expression, self.conditions)

    # ------------------------------------------------------------------ evidence views
    def current_evidence(self, kind: Optional[EvidenceKind] = None) -> List[Evidence]:
        """Evidence about the current version (optionally of one kind)."""
        return [e for e in self.evidence if e.version == self.version and (kind is None or e.kind == kind)]

    @property
    def support(self) -> int:
        """Number of distinct past queries or code sites that use this version (independent support)."""
        return len({(e.kind, e.ref or e.detail) for e in self.current_evidence()
                    if e.kind in (EvidenceKind.HISTORY, EvidenceKind.CODE) and e.passed})

    @property
    def sources(self) -> List[str]:
        """Kinds of independent sources that support the current version (e.g. ['code', 'history'])."""
        return sorted({e.kind.value for e in self.current_evidence()
                       if e.kind in (EvidenceKind.HISTORY, EvidenceKind.CODE) and e.passed})

    @property
    def falsified(self) -> bool:
        """True if a data check or counterexample check failed for the current version, or a person rejected it."""
        return self.rejected or any(not e.passed for e in self.current_evidence()
                                    if e.kind in (EvidenceKind.DATA_CHECK, EvidenceKind.COUNTEREXAMPLE))

    @property
    def rejected(self) -> bool:
        """True if the latest certification decision for the current version is a rejection."""
        certs = sorted(self.current_evidence(EvidenceKind.CERTIFICATION), key=lambda e: e.at)
        return bool(certs) and not certs[-1].passed

    @property
    def certified(self) -> bool:
        """True if the latest certification decision for the current version is positive."""
        certs = sorted(self.current_evidence(EvidenceKind.CERTIFICATION), key=lambda e: e.at)
        return bool(certs) and certs[-1].passed

    @property
    def grade(self) -> Grade:
        """Evidence grade computed from the current version's evidence (see module docstring)."""
        if self.falsified:
            return Grade.E0
        if self.certified:
            ce = self.current_evidence(EvidenceKind.COUNTEREXAMPLE)
            return Grade.E4 if ce and all(e.passed for e in ce) else Grade.E3
        if self.support >= 2:
            checks = self.current_evidence(EvidenceKind.DATA_CHECK)
            return Grade.E2 if checks and all(e.passed for e in checks) else Grade.E1
        return Grade.E0

    def is_valid(self, at: Optional[str] = None) -> bool:
        """True if ``at`` (default: now) lies inside the validity period."""
        t = at or now_iso()
        return (self.valid_from is None or self.valid_from <= t) and (self.valid_until is None or t < self.valid_until)

    # ------------------------------------------------------------------ serialization
    def to_dict(self) -> Dict[str, Any]:
        return {'id': self.id, 'name': self.name, 'kind': self.kind.value, 'expression': self.expression,
                'conditions': list(self.conditions), 'tables': list(self.tables), 'columns': list(self.columns),
                'source': self.source, 'evidence': [e.to_dict() for e in self.evidence],
                'valid_from': self.valid_from, 'valid_until': self.valid_until, 'version': self.version,
                'meta': dict(self.meta), 'grade': self.grade.name}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'Definition':
        return cls(name=d['name'], kind=Kind(d['kind']), expression=d['expression'],
                   conditions=list(d.get('conditions', [])), tables=list(d.get('tables', [])),
                   columns=list(d.get('columns', [])), source=d.get('source', 'history'),
                   evidence=[Evidence.from_dict(e) for e in d.get('evidence', [])],
                   valid_from=d.get('valid_from'), valid_until=d.get('valid_until'),
                   version=int(d.get('version', 1)), meta=dict(d.get('meta', {})), id=d.get('id', ''))


@dataclass
class Event:
    """One append-only ledger entry."""

    seq: int
    type: str
    definition_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {'seq': self.seq, 'type': self.type, 'definition_id': self.definition_id, 'payload': self.payload, 'at': self.at}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'Event':
        return cls(seq=int(d['seq']), type=d['type'], definition_id=d['definition_id'],
                   payload=dict(d.get('payload', {})), at=d.get('at') or now_iso())


class Ledger:
    """The ledger of business definitions: current state plus an append-only event log.

    Every change (add, evidence, certification, revision, expiry) is appended to ``events``;
    nothing is overwritten silently, so the history of each definition can be audited.
    """

    def __init__(self) -> None:
        self._defs: Dict[str, Definition] = {}
        self.events: List[Event] = []

    # ------------------------------------------------------------------ basics
    def __len__(self) -> int:
        return len(self._defs)

    def __iter__(self) -> Iterator[Definition]:
        return iter(list(self._defs.values()))

    def __contains__(self, definition_id: object) -> bool:
        return definition_id in self._defs

    def get(self, definition_id: str) -> Definition:
        try:
            return self._defs[definition_id]
        except KeyError:
            raise KeyError(f'no definition with id {definition_id!r}') from None

    def find(self, name: Optional[str] = None, kind: Optional[Kind] = None,
             min_grade: Optional[Grade] = None, table: Optional[str] = None) -> List[Definition]:
        """Definitions matching all given filters (name is matched case-insensitively)."""
        out = []
        for d in self._defs.values():
            if name is not None and d.name.lower() != name.lower():
                continue
            if kind is not None and d.kind != Kind(kind):
                continue
            if min_grade is not None and d.grade < Grade.parse(min_grade):
                continue
            if table is not None and table not in d.tables:
                continue
            out.append(d)
        return out

    def _log(self, type_: str, definition_id: str, **payload: Any) -> Event:
        ev = Event(seq=len(self.events) + 1, type=type_, definition_id=definition_id, payload=payload)
        self.events.append(ev)
        return ev

    # ------------------------------------------------------------------ changes
    def add(self, definition: Definition) -> Definition:
        """Add a definition; if one with the same id exists, merge the new evidence into it."""
        cur = self._defs.get(definition.id)
        if cur is None:
            self._defs[definition.id] = definition
            self._log('add', definition.id, name=definition.name, kind=definition.kind.value,
                      expression=definition.expression, source=definition.source)
            return definition
        seen = {(e.kind, e.ref, e.detail, e.version) for e in cur.evidence}
        for e in definition.evidence:
            if (e.kind, e.ref, e.detail, e.version) not in seen:
                cur.evidence.append(e)
                self._log('evidence', cur.id, kind=e.kind.value, ref=e.ref, passed=e.passed)
        return cur

    def add_evidence(self, definition_id: str, evidence: Evidence) -> Definition:
        d = self.get(definition_id)
        evidence.version = d.version
        d.evidence.append(evidence)
        self._log('evidence', d.id, kind=evidence.kind.value, ref=evidence.ref, passed=evidence.passed, detail=evidence.detail)
        return d

    def certify(self, definition_id: str, by: str, note: str = '', passed: bool = True) -> Definition:
        """Record a person's certification decision (``passed=False`` records a rejection)."""
        d = self.add_evidence(definition_id, Evidence(EvidenceKind.CERTIFICATION, detail=note, passed=passed, ref=by))
        self._log('certify', d.id, by=by, passed=passed, note=note)
        return d

    def revise(self, definition_id: str, expression: Optional[str] = None,
               conditions: Optional[List[str]] = None, note: str = '') -> Definition:
        """Start a new version of a definition; evidence for older versions no longer counts."""
        d = self.get(definition_id)
        old = {'expression': d.expression, 'conditions': list(d.conditions), 'version': d.version, 'grade': d.grade.name}
        if expression is not None:
            d.expression = expression
        if conditions is not None:
            d.conditions = list(conditions)
        d.version += 1
        self._log('revise', d.id, old=old, new={'expression': d.expression, 'conditions': d.conditions,
                                               'version': d.version}, note=note)
        return d

    def expire(self, definition_id: str, at: Optional[str] = None, note: str = '') -> Definition:
        d = self.get(definition_id)
        d.valid_until = at or now_iso()
        self._log('expire', d.id, valid_until=d.valid_until, note=note)
        return d

    # ------------------------------------------------------------------ reporting and persistence
    def summary(self) -> Dict[str, Any]:
        by_grade = {g.name: 0 for g in Grade}
        by_kind: Dict[str, int] = {}
        for d in self._defs.values():
            by_grade[d.grade.name] += 1
            by_kind[d.kind.value] = by_kind.get(d.kind.value, 0) + 1
        return {'definitions': len(self._defs), 'by_grade': by_grade, 'by_kind': by_kind, 'events': len(self.events)}

    def to_dict(self) -> Dict[str, Any]:
        return {'schema_version': SCHEMA_VERSION, 'definitions': [d.to_dict() for d in self._defs.values()],
                'events': [e.to_dict() for e in self.events]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Ledger':
        led = cls()
        for dd in data.get('definitions', []):
            d = Definition.from_dict(dd)
            led._defs[d.id] = d
        led.events = [Event.from_dict(e) for e in data.get('events', [])]
        return led

    def save(self, path: 'str | Path') -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding='utf-8')

    @classmethod
    def load(cls, path: 'str | Path') -> 'Ledger':
        return cls.from_dict(json.loads(Path(path).read_text(encoding='utf-8')))
