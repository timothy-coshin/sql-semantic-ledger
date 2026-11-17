"""믿기 (trust): confirm, warn or reject an answer by the evidence grades of the definitions it uses.

Rule from the research plan: if every definition used is certified by a person (E3 or above) the answer
is confirmed; if a definition backed only by machine evidence (E1-E2) is mixed in, the answer goes out
with a warning; if a definition without evidence (E0, including falsified ones) is mixed in, or no
definition matches, or a check fails, the answer is rejected.

CONTRACT (implemented by the build step):

    decide(definitions, policy=Policy(), eps=None, delta=0.0, at=None) -> Decision
        * no definitions                         -> REJECT ('no matching definition')
        * any falsified (failed check or a person's rejection) or grade E0 -> REJECT
        * policy.require_valid and any definition not valid at ``at`` (expired) -> REJECT
        * all grades >= policy.confirm_min (E3)  -> CONFIRM; but if eps is given and policy.target is set and
                                                    the error bound > target -> WARN (reason states the bound)
        * otherwise                              -> WARN
        ``bound`` = bound.error_bound(definitions, eps, delta) when eps is given, else None.
        ``reasons`` explain the verdict per definition (name, grade); ``grades`` maps id -> grade name.

Reasons: one line per distinct definition, in the given order, naming its grade and its basis (who
certified it and when, which past queries or code sites support it, which check falsified it), followed by
one line with the error bound and its terms when ``eps`` is given. A definition listed twice counts once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .bound import error_bound, normalize_eps
from .model import Definition, Evidence, EvidenceKind, Grade, now_iso


class Verdict(str, Enum):
    CONFIRM = 'confirm'
    WARN = 'warn'
    REJECT = 'reject'


@dataclass
class Policy:
    confirm_min: Grade = Grade.E3
    target: Optional[float] = None       # e.g. 0.05: confirmed answers must have error bound <= target
    require_valid: bool = True


@dataclass
class Decision:
    verdict: Verdict
    bound: Optional[float] = None
    reasons: List[str] = field(default_factory=list)
    grades: Dict[str, str] = field(default_factory=dict)


_OK, _WARN, _REJECT = 'ok', 'warn', 'reject'
_SUPPORT_KINDS = (EvidenceKind.HISTORY, EvidenceKind.CODE)
_CHECK_KINDS = (EvidenceKind.DATA_CHECK, EvidenceKind.COUNTEREXAMPLE)


def decide(definitions: Sequence[Definition], policy: Optional[Policy] = None,
           eps: Optional[Mapping[Union[Grade, str], float]] = None, delta: float = 0.0,
           at: Optional[str] = None) -> Decision:
    """Confirm, warn or reject an answer that uses ``definitions`` (see the module docstring for the rule).

    ``at`` is the ISO time at which validity is judged (default: now). Raises TypeError for items that are not
    :class:`Definition` and ValueError for an invalid policy, eps or delta.
    """
    policy = policy if policy is not None else Policy()
    confirm_min = _policy_grade(policy.confirm_min)
    target = _policy_target(policy.target)
    used = _distinct(definitions)
    bound = error_bound(used, eps, delta) if eps is not None else None
    if not used:
        return Decision(Verdict.REJECT, bound=bound, reasons=['no matching definition'])

    assessed = [_assess(d, confirm_min, policy.require_valid, at) for d in used]
    statuses = {status for status, _ in assessed}
    if _REJECT in statuses:
        verdict = Verdict.REJECT
    elif statuses == {_OK}:
        verdict = Verdict.CONFIRM
    else:
        verdict = Verdict.WARN
    reasons = [text for _, text in assessed]
    if bound is not None:
        line = _bound_line(used, eps, delta, bound)
        if target is not None and bound > target:
            line += f' > target {target:g}'
            if verdict is Verdict.CONFIRM:
                verdict = Verdict.WARN
                line += ' -> warn'
        elif target is not None:
            line += f' <= target {target:g}'
        reasons.append(line)
    return Decision(verdict, bound=bound, reasons=reasons, grades={d.id: d.grade.name for d in used})


# ---------------------------------------------------------------------------------------------- inputs
def _policy_grade(value: Any) -> Grade:
    try:
        return Grade.parse(value)
    except (KeyError, ValueError, TypeError):
        raise ValueError(f'policy.confirm_min must be a grade E0-E4, got {value!r}') from None


def _policy_target(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        target = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'policy.target must be a number in [0, 1], got {value!r}') from None
    if not 0.0 <= target <= 1.0:
        raise ValueError(f'policy.target must be in [0, 1], got {value!r}')
    return target


def _distinct(definitions: Sequence[Definition]) -> List[Definition]:
    """The definitions in the given order, each id once."""
    out: List[Definition] = []
    seen = set()
    for d in definitions:
        if not isinstance(d, Definition):
            raise TypeError(f'decide expects Definition objects, got {type(d).__name__}')
        if d.id not in seen:
            seen.add(d.id)
            out.append(d)
    return out


# ---------------------------------------------------------------------------------------------- reasons
def _assess(d: Definition, confirm_min: Grade, require_valid: bool, at: Optional[str]) -> Tuple[str, str]:
    """(status, reason) for one definition; status is 'ok', 'warn' or 'reject'."""
    grade = d.grade
    head = f'{d.name} ({grade.name})'
    problems: List[str] = []
    failed = _failed_checks(d) if d.falsified else []
    if d.rejected:
        cert = _latest_certification(d)
        problems.append(f'certification rejected by {cert.ref or "an unnamed reviewer"} at {cert.at}')
    if failed:
        problems.append('falsified by failed check ' + ', '.join(failed))
    if grade == Grade.E0 and not d.rejected and not failed:
        problems.append(_no_evidence(d))
    validity = '' if d.is_valid(at) else _validity(d, at)
    if validity and require_valid:
        problems.append(validity)
    if problems:
        return _REJECT, f'{head}: ' + '; '.join(problems) + ' -> reject'
    text = f'{head}: {_basis(d)}'
    if validity:
        text += f'; {validity}, not enforced by the policy'
    if grade >= confirm_min:
        return _OK, f'{text}; meets {confirm_min.name}'
    return _WARN, f'{text}; below {confirm_min.name} -> warn'


def _basis(d: Definition) -> str:
    """What a definition of grade E1 or higher rests on: certification, support, passed checks."""
    parts: List[str] = []
    cert = _latest_certification(d)
    if d.certified and cert is not None:
        parts.append(f'certified by {cert.ref or "an unnamed reviewer"} at {cert.at}')
        if d.grade == Grade.E4:
            n = len(d.current_evidence(EvidenceKind.COUNTEREXAMPLE))
            parts.append(f'passed {n} counterexample check{"" if n == 1 else "s"}')
    else:
        parts.append('machine evidence only')
        if cert is not None:
            parts.append(f'certification rejected by {cert.ref or "an unnamed reviewer"} at {cert.at}')
    if d.support:
        parts.append(_support_text(d))
    checks = d.current_evidence(EvidenceKind.DATA_CHECK)
    if checks and all(e.passed for e in checks):
        parts.append('data checks passed (' + _listing(sorted({e.ref or 'check' for e in checks})) + ')')
    return '; '.join(parts)


def _no_evidence(d: Definition) -> str:
    n = d.support
    text = 'no supporting evidence' if n == 0 else f'{_support_text(d)}, E1 needs 2'
    cert = _latest_certification(d)
    if cert is not None and not cert.passed:
        text += f'; certification rejected by {cert.ref or "an unnamed reviewer"} at {cert.at}'
    return text


def _support_text(d: Definition) -> str:
    refs = sorted({e.ref or e.detail or e.kind.value for e in d.current_evidence()
                   if e.kind in _SUPPORT_KINDS and e.passed})
    noun = 'past query or code site' if d.support == 1 else 'past queries or code sites'
    return f'supported by {d.support} {noun} ({_listing(refs)})'


def _failed_checks(d: Definition) -> List[str]:
    return sorted({e.ref or e.kind.value for e in d.current_evidence() if e.kind in _CHECK_KINDS and not e.passed})


def _latest_certification(d: Definition) -> Optional[Evidence]:
    certs = sorted(d.current_evidence(EvidenceKind.CERTIFICATION), key=lambda e: e.at)
    return certs[-1] if certs else None


def _validity(d: Definition, at: Optional[str]) -> str:
    t = at or now_iso()
    if d.valid_until is not None and t >= d.valid_until:
        return f'expired (valid until {d.valid_until})'
    if d.valid_from is not None and t < d.valid_from:
        return f'not valid yet (valid from {d.valid_from})'
    return 'outside its validity period'


def _bound_line(used: Sequence[Definition], eps: Mapping[Union[Grade, str], float], delta: float,
                bound: float) -> str:
    """'error bound B = delta x + eps(E3) y + ...' with the terms of :func:`bound.error_bound`."""
    rates = normalize_eps(eps)
    terms = [f'delta {float(delta):g}'] + [f'eps({d.grade.name}) {rates.get(d.grade, 1.0):g}' for d in used]
    capped = ' (capped at 1)' if bound >= 1.0 and float(delta) + sum(rates.get(d.grade, 1.0) for d in used) > 1.0 else ''
    return f'error bound {bound:.4g}{capped} = ' + ' + '.join(terms)


def _listing(items: Sequence[str], limit: int = 3) -> str:
    shown = ', '.join(items[:limit])
    return shown + (f', +{len(items) - limit} more' if len(items) > limit else '')
