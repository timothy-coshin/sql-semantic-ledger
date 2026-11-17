"""Error-rate upper bounds:  P(answer to q is wrong) <= delta + sum_{d in D(q)} eps(e(d)).

D(q) are the definitions an answer uses, e(d) the evidence grade of d, eps(e) the probability that a
definition of grade e is wrong (for E3+, the measured certification error rate), and delta the error of
choosing a wrong definition or assembling wrong SQL although each definition is right.

CONTRACT (implemented by the build step):

    error_bound(definitions, eps, delta=0.0) -> float           min(1, delta + sum eps[grade(d)])
    estimate_eps(counts, confidence=0.95, method='clopper-pearson') -> dict[Grade, float]
        counts maps a grade to (errors, n). Returns one-sided upper confidence bounds. 'clopper-pearson'
        is exact (pure Python, bisection on the binomial CDF; scipy not required); 'wilson' also supported.
        n == 0 gives 1.0.
    compose(bounds) -> float                                    min(1, sum) over the steps of a multi-step analysis
    calibrate_threshold(...)                                    planned: statistical guarantee (stage 2)

Details:
    * Grade keys may be Grade members or text such as 'E1' / 'e1' (or the number 1). Every probability
      (eps values, delta, step bounds) must lie in [0, 1]; anything else raises ValueError.
    * A grade missing from ``eps`` counts as 1.0: nothing is known about it, so the bound stays valid.
    * D(q) is a set, so a definition listed twice counts once.
    * Helpers: normalize_eps(eps), clopper_pearson_upper(errors, n, confidence), wilson_upper(errors, n, confidence).
"""
from __future__ import annotations

import math
from numbers import Integral
from statistics import NormalDist
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence, Set, Tuple, Union

from ._unimplemented import planned
from .model import Definition, Grade

GradeKey = Union[Grade, str]

_BISECTION_STEPS = 200          # far more than needed: the loop stops once the interval cannot shrink
_NEGLIGIBLE = 1e-17             # relative size at which further binomial terms no longer change the sum


# ---------------------------------------------------------------------------------------------- validation
def _grade(key: Any) -> Grade:
    """Parse a grade key ('E1', 'e1', 1 or Grade.E1); ValueError when it is not E0-E4."""
    try:
        return Grade.parse(key)
    except (KeyError, ValueError, TypeError):
        raise ValueError(f'unknown evidence grade {key!r} (expected E0-E4)') from None


def _probability(value: Any, what: str) -> float:
    """``value`` as a float in [0, 1]; ValueError otherwise (NaN included)."""
    try:
        p = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{what} must be a number in [0, 1], got {value!r}') from None
    if not 0.0 <= p <= 1.0:
        raise ValueError(f'{what} must be in [0, 1], got {value!r}')
    return p


def _confidence(value: Any) -> float:
    """A confidence level strictly between 0 and 1."""
    try:
        c = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'confidence must be a number in (0, 1), got {value!r}') from None
    if not 0.0 < c < 1.0:
        raise ValueError(f'confidence must be in (0, 1), got {value!r}')
    return c


def _count(value: Any, what: str) -> int:
    """A non-negative whole number (ints, or floats such as 3.0); ValueError otherwise."""
    if isinstance(value, bool):
        raise ValueError(f'{what} must be a whole number, got {value!r}')
    if isinstance(value, Integral):
        number = int(value)
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        raise ValueError(f'{what} must be a whole number, got {value!r}')
    if number < 0:
        raise ValueError(f'{what} must not be negative, got {value!r}')
    return number


def _errors_and_n(errors: Any, n: Any) -> Tuple[int, int]:
    k, total = _count(errors, 'errors'), _count(n, 'n')
    if k > total:
        raise ValueError(f'errors ({k}) cannot exceed n ({total})')
    return k, total


def normalize_eps(eps: Mapping[GradeKey, float]) -> Dict[Grade, float]:
    """Per-grade error rates keyed by :class:`Grade`, sorted by grade, each checked to lie in [0, 1].

    Raises TypeError if ``eps`` is not a mapping and ValueError for an unknown grade, a grade given twice
    (e.g. both ``'E1'`` and ``Grade.E1``) or a rate outside [0, 1].
    """
    if not isinstance(eps, Mapping):
        raise TypeError(f'eps must be a mapping from grade to error rate, got {type(eps).__name__}')
    rates: Dict[Grade, float] = {}
    for key, value in eps.items():
        grade = _grade(key)
        if grade in rates:
            raise ValueError(f'grade {grade.name} is given more than once in eps')
        rates[grade] = _probability(value, f'eps[{grade.name}]')
    return dict(sorted(rates.items()))


# ---------------------------------------------------------------------------------------------- bounds
def error_bound(definitions: Sequence[Definition], eps: Mapping[Union[Grade, str], float], delta: float = 0.0) -> float:
    """Upper bound on the probability that an answer using ``definitions`` is wrong.

    ``min(1, delta + sum of eps[grade(d)])`` over the distinct definitions (by id), using each definition's
    current grade. A grade missing from ``eps`` contributes 1.0.

    >>> error_bound([], {'E3': 0.05}, delta=0.02)
    0.02
    """
    rates = normalize_eps(eps)
    terms = [_probability(delta, 'delta')]
    seen: Set[str] = set()
    for definition in definitions:
        if not isinstance(definition, Definition):
            raise TypeError(f'error_bound expects Definition objects, got {type(definition).__name__}')
        if definition.id in seen:
            continue
        seen.add(definition.id)
        terms.append(rates.get(definition.grade, 1.0))
    return min(1.0, math.fsum(terms))


def compose(bounds: Iterable[float]) -> float:
    """Error bound of a multi-step analysis: ``min(1, sum of the step bounds)`` (union bound).

    Each step bound must lie in [0, 1]; an empty sequence of steps gives 0.0.

    >>> compose([0.25, 0.5]), compose([0.7, 0.6])
    (0.75, 1.0)
    """
    steps = [_probability(b, f'bound of step {i + 1}') for i, b in enumerate(bounds)]
    return min(1.0, math.fsum(steps))


# ---------------------------------------------------------------------------------------------- estimation
def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), summed in log space so large n neither overflows nor underflows.

    The terms are unimodal in i, so the sum starts at the largest term with i <= k and walks away from it
    until the terms stop mattering.
    """
    if k < 0:
        return 0.0
    if k >= n or p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    log_p, log_q, log_n = math.log(p), math.log1p(-p), math.lgamma(n + 1)

    def log_term(i: int) -> float:
        return log_n - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_p + (n - i) * log_q

    top = min(k, int((n + 1) * p))           # binomial mode, capped at k: the largest term in the sum
    peak = log_term(top)
    total = 0.0
    for i in range(top, -1, -1):             # downwards from the peak: decreasing terms
        term = math.exp(log_term(i) - peak)
        total += term
        if term < _NEGLIGIBLE * total:
            break
    for i in range(top + 1, k + 1):          # upwards to k (only when the mode lies below k): decreasing terms
        term = math.exp(log_term(i) - peak)
        total += term
        if term < _NEGLIGIBLE * total:
            break
    return min(1.0, math.exp(peak) * total)


def clopper_pearson_upper(errors: int, n: int, confidence: float = 0.95) -> float:
    """Exact one-sided Clopper-Pearson upper confidence bound for an error rate.

    The largest p with P(X <= errors; n, p) >= 1 - confidence, found by bisection on the binomial CDF and
    rounded up (the returned value never lies below the exact bound). ``n == 0`` or ``errors == n`` give 1.0.

    >>> round(clopper_pearson_upper(0, 10), 4), round(clopper_pearson_upper(1, 10), 4)
    (0.2589, 0.3942)
    """
    k, total = _errors_and_n(errors, n)
    alpha = 1.0 - _confidence(confidence)
    if total == 0 or k == total:
        return 1.0
    lo, hi = 0.0, 1.0                        # CDF(lo) = 1 > alpha and CDF(hi) = 0 <= alpha
    for _ in range(_BISECTION_STEPS):
        mid = 0.5 * (lo + hi)
        if not lo < mid < hi:
            break
        if _binom_cdf(k, total, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def wilson_upper(errors: int, n: int, confidence: float = 0.95) -> float:
    """One-sided Wilson score upper confidence bound for an error rate (``n == 0`` gives 1.0)."""
    k, total = _errors_and_n(errors, n)
    c = _confidence(confidence)
    if total == 0:
        return 1.0
    z = NormalDist().inv_cdf(c)
    z2, rate = z * z, k / total
    centre = rate + z2 / (2 * total)
    margin = z * math.sqrt(rate * (1.0 - rate) / total + z2 / (4 * total * total))
    return min(1.0, max(0.0, (centre + margin) / (1.0 + z2 / total)))


_METHODS: Dict[str, Callable[[int, int, float], float]] = {
    'clopper-pearson': clopper_pearson_upper,
    'exact': clopper_pearson_upper,
    'wilson': wilson_upper,
}


def estimate_eps(counts: Mapping[Union[Grade, str], Tuple[int, int]], confidence: float = 0.95,
                 method: str = 'clopper-pearson') -> Dict[Grade, float]:
    """Per-grade error rates eps(e) as one-sided upper confidence bounds from observed counts.

    ``counts`` maps a grade to ``(errors, n)``: how many of ``n`` judged definitions of that grade were wrong.
    ``method`` is ``'clopper-pearson'`` (exact; alias ``'exact'``) or ``'wilson'``. The result is sorted by grade.

    >>> {g.name: round(v, 4) for g, v in estimate_eps({'E3': (0, 10), 'E1': (1, 10)}).items()}
    {'E1': 0.3942, 'E3': 0.2589}
    """
    if not isinstance(counts, Mapping):
        raise TypeError(f'counts must be a mapping from grade to (errors, n), got {type(counts).__name__}')
    key = str(method).strip().lower().replace('_', '-')
    if key not in _METHODS:
        raise ValueError(f"unknown method {method!r}; use 'clopper-pearson' or 'wilson'")
    upper, level = _METHODS[key], _confidence(confidence)
    estimates: Dict[Grade, float] = {}
    for grade_key, pair in counts.items():
        grade = _grade(grade_key)
        if grade in estimates:
            raise ValueError(f'grade {grade.name} is given more than once in counts')
        try:
            errors, n = pair
        except (TypeError, ValueError):
            raise ValueError(f'counts[{grade.name}] must be a pair (errors, n), got {pair!r}') from None
        try:
            estimates[grade] = upper(errors, n, level)
        except ValueError as exc:
            raise ValueError(f'counts[{grade.name}]: {exc}') from None
    return dict(sorted(estimates.items()))


def calibrate_threshold(*args, **kwargs):
    """Calibrate the confirmation rule so confirmed conclusions meet an error target. Planned (stage 2)."""
    planned('trust.guarantee')
