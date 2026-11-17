"""Revalidation: when definitions or data change, re-check the answers with the highest expected harm first.

CONTRACT (implemented by the build step):

    AnswerRecord(id, definitions, tables=[], weight=1.0)     a stored answer and the definitions/tables it used
    DependencyIndex(answers=())
        .add(answer)
        .affected_by_definitions(ids) -> set[str]            answers using any of the definitions
        .affected_by_tables(tables) -> set[str]              answers reading any of the tables
    plan(ledger, index, changed_definitions=(), changed_tables=(), eps=None, budget=None) -> list[PlanItem]
        Candidate answers = those affected by the changes (all answers if no change is given).
        score = weight * (1 - prod_d (1 - eps[grade(d)]))   (probability that at least one definition is wrong);
        without eps, use the default grade risks E0 1.0, E1 0.5, E2 0.3, E3 0.1, E4 0.05.
        Unknown definition ids count as E0. Sorted by score (desc), then id; cut to ``budget`` items.
        ``reason`` names the riskiest definition and its grade.
    simulate(plan_items, truly_wrong, candidates) -> dict   recall, cost (items checked), cost_ratio vs checking all

Details
-------
* Definition ids are matched exactly; table names case-insensitively (SQL identifiers usually are), part by
  part and with or without quotes: ``"Order Items"``, ``order items`` and ``ORDER ITEMS`` are one table.
* An answer's definitions form a set: an id listed twice counts once. An answer that uses no definition
  has risk 0, so it scores 0 even when one of its tables changed.
* ``eps`` may give only some grades; the other grades keep the default risks. Every rate must lie in [0, 1].
* Changes that affect no stored answer give an empty plan (only "no change at all" means all answers).
* The factors of the product are multiplied in sorted order, so answers whose definitions have the same
  grades get identical scores and the tie is broken by answer id.
* ``reason`` reads e.g. ``riskiest definition paid_revenue [3f2a9c1b7d4e] E1 (risk 0.5); changed: table orders``.
"""
from __future__ import annotations

import math
import operator
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple, Union

from . import _names
from .model import Grade, Ledger

#: Probability that a definition of each grade is wrong, used when ``plan`` gets no ``eps``.
DEFAULT_RISKS: Mapping[Grade, float] = MappingProxyType(
    {Grade.E0: 1.0, Grade.E1: 0.5, Grade.E2: 0.3, Grade.E3: 0.1, Grade.E4: 0.05})

# (risk, grade, definition id, label) for one definition of one answer
_Risk = Tuple[float, Grade, str, str]


def _strings(name: str, values: Iterable[str]) -> List[str]:
    """``values`` as a list of strings. A bare string is refused: it would be read as single characters."""
    if isinstance(values, (str, bytes)):
        raise TypeError(f'{name} must be a collection of strings, not a single string: {values!r}')
    items = list(values)
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f'{name} must contain strings, got {item!r}')
    if isinstance(values, (set, frozenset)):
        items.sort()  # set order changes between runs
    return items


def _table_key(name: str) -> str:
    """Key under which a table name is matched: its parts without quotes, lower case.

    Names may be SQL text (``main."Order Items"``, as engine.assemble returns them) or plain text
    (``Order Items``); SQLite's reader is used because it accepts "...", `...` and [...] quoting.
    """
    return _names.table_key(name, 'sqlite') or name.strip().lower()


@dataclass
class AnswerRecord:
    """A stored answer and what it depends on: ledger definition ids and the tables it reads.

    ``weight`` is the answer's impact (e.g. how often it is viewed or reused). It scales the
    revalidation score and must be a finite number >= 0. Integer ids are converted to strings.
    """

    id: str
    definitions: List[str]
    tables: List[str] = field(default_factory=list)
    weight: float = 1.0

    def __post_init__(self) -> None:
        if isinstance(self.id, int) and not isinstance(self.id, bool):
            self.id = str(self.id)
        if not isinstance(self.id, str) or not self.id:
            raise ValueError(f'answer id must be a non-empty string, got {self.id!r}')
        self.definitions = _strings(f'answer {self.id!r}: definitions', self.definitions)
        self.tables = _strings(f'answer {self.id!r}: tables', self.tables)
        try:
            weight = float(self.weight)
        except (TypeError, ValueError):
            weight = float('nan')
        if not math.isfinite(weight) or weight < 0:
            raise ValueError(f'answer {self.id!r}: weight must be a finite number >= 0, got {self.weight!r}')
        self.weight = weight

    def to_dict(self) -> Dict[str, Any]:
        return {'id': self.id, 'definitions': list(self.definitions), 'tables': list(self.tables),
                'weight': self.weight}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> 'AnswerRecord':
        """Record from a mapping such as one line of ``answers.jsonl``; only ``id`` is required, other keys are ignored."""
        if d.get('id') in (None, ''):
            raise ValueError(f'answer record needs an "id": {dict(d)!r}')
        return cls(id=d['id'], definitions=d.get('definitions') or [], tables=d.get('tables') or [],
                   weight=d.get('weight', 1.0))


@dataclass
class PlanItem:
    answer_id: str
    score: float
    reason: str = ''


class DependencyIndex:
    """Which stored answers use which definitions and read which tables (the impact index used for revalidation).

    Records are copied when added; adding an answer whose id is already indexed replaces the old record.
    """

    def __init__(self, answers: Iterable[AnswerRecord] = ()) -> None:
        self._answers: Dict[str, AnswerRecord] = {}
        self._by_definition: Dict[str, Set[str]] = {}
        self._by_table: Dict[str, Set[str]] = {}
        for answer in answers:
            self.add(answer)

    # ------------------------------------------------------------------ changes
    def add(self, answer: AnswerRecord) -> None:
        """Index one answer (replacing an earlier record with the same id)."""
        if not isinstance(answer, AnswerRecord):
            raise TypeError(f'expected an AnswerRecord, got {type(answer).__name__} (see AnswerRecord.from_dict)')
        record = AnswerRecord(answer.id, list(answer.definitions), list(answer.tables), answer.weight)
        old = self._answers.get(record.id)
        if old is not None:
            self._unlink(old)
        self._answers[record.id] = record
        for definition_id in record.definitions:
            self._by_definition.setdefault(definition_id, set()).add(record.id)
        for table in record.tables:
            self._by_table.setdefault(_table_key(table), set()).add(record.id)

    def _unlink(self, record: AnswerRecord) -> None:
        for index, keys in ((self._by_definition, record.definitions),
                            (self._by_table, [_table_key(t) for t in record.tables])):
            for key in keys:
                ids = index.get(key)
                if ids is not None:
                    ids.discard(record.id)
                    if not ids:
                        del index[key]

    # ------------------------------------------------------------------ queries
    def affected_by_definitions(self, ids: Iterable[str]) -> Set[str]:
        """Ids of the answers that use any of the given definition ids."""
        out: Set[str] = set()
        for definition_id in _strings('ids', ids):
            out |= self._by_definition.get(definition_id, set())
        return out

    def affected_by_tables(self, tables: Iterable[str]) -> Set[str]:
        """Ids of the answers that read any of the given tables (names matched case-insensitively)."""
        out: Set[str] = set()
        for table in _strings('tables', tables):
            out |= self._by_table.get(_table_key(table), set())
        return out

    def get(self, answer_id: str) -> AnswerRecord:
        try:
            return self._answers[answer_id]
        except KeyError:
            raise KeyError(f'no answer with id {answer_id!r}') from None

    def __len__(self) -> int:
        return len(self._answers)

    def __iter__(self) -> Iterator[AnswerRecord]:
        """Indexed answers in id order."""
        return iter([self._answers[k] for k in sorted(self._answers)])

    def __contains__(self, answer_id: object) -> bool:
        return answer_id in self._answers


# ---------------------------------------------------------------------- planning
def _risk_table(eps: Optional[Mapping[Union[Grade, str], float]]) -> Dict[Grade, float]:
    """Per-grade error rates: the defaults, overridden by the grades given in ``eps``."""
    risks = dict(DEFAULT_RISKS)
    if eps is None:
        return risks
    if not isinstance(eps, Mapping):
        raise TypeError(f'eps must map grades to error rates, got {type(eps).__name__}')
    given: Set[Grade] = set()
    for key, value in eps.items():
        try:
            grade = Grade.parse(key)
        except (KeyError, TypeError, ValueError):
            raise ValueError(f'unknown grade {key!r} in eps (use E0-E4)') from None
        if grade in given:
            raise ValueError(f'eps gives grade {grade.name} more than once')
        try:
            rate = float(value)
        except (TypeError, ValueError):
            rate = float('nan')
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f'eps[{grade.name}] must be a probability in [0, 1], got {value!r}')
        given.add(grade)
        risks[grade] = rate
    return risks


def _check_budget(budget: Optional[int]) -> Optional[int]:
    if budget is None:
        return None
    if isinstance(budget, bool):
        raise TypeError('budget must be an integer number of answers, not a bool')
    try:
        limit = operator.index(budget)
    except TypeError:
        raise TypeError(f'budget must be an integer number of answers, got {budget!r}') from None
    if limit < 0:
        raise ValueError(f'budget must be >= 0, got {limit}')
    return limit


class _GradeLookup:
    """Grade and display label of definition ids, computed once per plan (unknown ids are E0)."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._cache: Dict[str, Tuple[Grade, str]] = {}

    def __call__(self, definition_id: str) -> Tuple[Grade, str]:
        hit = self._cache.get(definition_id)
        if hit is None:
            if definition_id in self._ledger:
                d = self._ledger.get(definition_id)
                hit = (d.grade, f'{d.name} [{definition_id}]')
            else:
                hit = (Grade.E0, f'{definition_id} (not in ledger)')
            self._cache[definition_id] = hit
        return hit


def _answer_risks(answer: AnswerRecord, lookup: _GradeLookup, risks: Mapping[Grade, float]) -> List[_Risk]:
    """(risk, grade, id, label) of each distinct definition the answer uses, in id order."""
    out: List[_Risk] = []
    for definition_id in sorted(set(answer.definitions)):
        grade, label = lookup(definition_id)
        out.append((risks[grade], grade, definition_id, label))
    return out


def _reason(answer: AnswerRecord, answer_risks: List[_Risk], changed_definitions: Set[str],
            changed_tables: Set[str]) -> str:
    """Why an answer is in the plan: its riskiest definition and grade, the changes touching it, its weight."""
    if answer_risks:
        risk, grade, _, label = min(answer_risks, key=lambda r: (-r[0], r[1], r[2]))
        parts = [f'riskiest definition {label} {grade.name} (risk {risk:g})']
    else:
        parts = ['uses no ledger definitions']
    touched = [f'definition {label}' for _, _, did, label in answer_risks if did in changed_definitions]
    tables = {t for t in answer.tables if _table_key(t) in changed_tables}
    touched += [f'table {t}' for t in sorted(tables, key=lambda t: (_table_key(t), t))]
    if touched:
        parts.append('changed: ' + ', '.join(touched))
    if answer.weight != 1.0:
        parts.append(f'weight {answer.weight:g}')
    return '; '.join(parts)


def plan(ledger: Ledger, index: DependencyIndex, changed_definitions: Iterable[str] = (),
         changed_tables: Iterable[str] = (), eps: Optional[Mapping[Union[Grade, str], float]] = None,
         budget: Optional[int] = None) -> List[PlanItem]:
    """Revalidation plan: affected answers ranked by expected harm, cut to ``budget`` items.

    Candidates are the answers that use a changed definition or read a changed table; with no change
    at all, every indexed answer is a candidate. Each candidate scores
    ``weight * (1 - prod_d (1 - eps[grade(d)]))`` over its distinct definitions, using the grades the
    definitions have now in ``ledger`` (ids missing from the ledger count as E0). Items are sorted by
    score (highest first), then answer id.
    """
    if not isinstance(ledger, Ledger):
        raise TypeError(f'ledger must be a Ledger, got {type(ledger).__name__}')
    if not isinstance(index, DependencyIndex):
        raise TypeError(f'index must be a DependencyIndex, got {type(index).__name__}')
    changed_defs = _strings('changed_definitions', changed_definitions)
    changed_tabs = _strings('changed_tables', changed_tables)
    risks = _risk_table(eps)
    limit = _check_budget(budget)
    if changed_defs or changed_tabs:
        candidates = index.affected_by_definitions(changed_defs) | index.affected_by_tables(changed_tabs)
    else:
        candidates = {answer.id for answer in index}
    lookup = _GradeLookup(ledger)
    def_set = set(changed_defs)
    table_keys = {_table_key(t) for t in changed_tabs}
    items: List[PlanItem] = []
    for answer_id in sorted(candidates):
        answer = index.get(answer_id)
        answer_risks = _answer_risks(answer, lookup, risks)
        all_right = math.prod(sorted(1.0 - r[0] for r in answer_risks))
        items.append(PlanItem(answer_id, answer.weight * (1.0 - all_right),
                              _reason(answer, answer_risks, def_set, table_keys)))
    items.sort(key=lambda item: (-item.score, item.answer_id))
    return items if limit is None else items[:limit]


# ---------------------------------------------------------------------- evaluation
def simulate(plan_items: Iterable[PlanItem], truly_wrong: Iterable[str], candidates: Iterable[str]) -> Dict[str, float]:
    """Evaluate a revalidation plan against checking every candidate.

    ``candidates`` are the answers a full revalidation would check, ``truly_wrong`` the answers that
    really became wrong (ids outside ``candidates`` are ignored) and ``plan_items`` the checked answers
    (:class:`PlanItem` or answer ids; each answer counts once and must be a candidate). Returns

    * ``recall``      wrong candidates found by the plan / wrong candidates (1.0 when none is wrong)
    * ``cost``        answers checked by the plan
    * ``cost_ratio``  cost / number of candidates (0.0 when there are no candidates)
    * ``candidates``, ``wrong``, ``found``  the counts behind these ratios
    """
    pool = set(_strings('candidates', candidates))
    wrong = set(_strings('truly_wrong', truly_wrong)) & pool
    if isinstance(plan_items, (str, bytes)):
        raise TypeError('plan_items must be a collection of PlanItem or answer ids, not a single string')
    checked: Set[str] = set()
    for item in plan_items:
        answer_id = item.answer_id if isinstance(item, PlanItem) else item
        if not isinstance(answer_id, str):
            raise TypeError(f'plan_items must be PlanItem or answer ids, got {item!r}')
        checked.add(answer_id)
    outside = sorted(checked - pool)
    if outside:
        shown = ', '.join(outside[:5]) + (', ...' if len(outside) > 5 else '')
        raise ValueError(f'{len(outside)} plan item(s) are not among the candidates: {shown}')
    found = len(checked & wrong)
    return {
        'recall': found / len(wrong) if wrong else 1.0,
        'cost': len(checked),
        'cost_ratio': len(checked) / len(pool) if pool else 0.0,
        'candidates': len(pool),
        'wrong': len(wrong),
        'found': found,
    }
