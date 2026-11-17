"""Tests for bound: error_bound, estimate_eps (Clopper-Pearson, Wilson), compose and the planned guarantee."""
import math

import pytest

from sql_semantic_ledger import Definition, Evidence, EvidenceKind, Grade, Kind
from sql_semantic_ledger._unimplemented import NotImplementedYet
from sql_semantic_ledger.bound import (calibrate_threshold, clopper_pearson_upper, compose, error_bound,
                                       estimate_eps, normalize_eps, wilson_upper)


def _definition(name, grade):
    """A FILTER definition whose evidence yields ``grade`` (E0, E1, E2, E3 or E4)."""
    d = Definition(name=name, kind=Kind.FILTER, expression=f"t.{name} = 1", tables=['t'])
    grade = Grade.parse(grade)
    if grade >= Grade.E1:
        d.evidence += [Evidence(EvidenceKind.HISTORY, ref=f'{name}-q1'), Evidence(EvidenceKind.HISTORY, ref=f'{name}-q2')]
    if grade == Grade.E2:
        d.evidence.append(Evidence(EvidenceKind.DATA_CHECK, ref='selects_rows'))
    if grade >= Grade.E3:
        d.evidence.append(Evidence(EvidenceKind.CERTIFICATION, ref='analyst'))
    if grade == Grade.E4:
        d.evidence.append(Evidence(EvidenceKind.COUNTEREXAMPLE, ref='t.x < 0'))
    assert d.grade == grade
    return d


# ------------------------------------------------------------------------------------------- error_bound
def test_error_bound_is_delta_plus_sum_of_grade_rates():
    eps = {'E1': 0.3, Grade.E3: 0.05}
    defs = [_definition('active', 'E3'), _definition('quarter', 'E1')]
    assert error_bound(defs, eps, delta=0.02) == pytest.approx(0.02 + 0.05 + 0.3)
    assert error_bound(defs, eps) == pytest.approx(0.35)


def test_error_bound_caps_at_one_and_counts_each_definition_once():
    eps = {'E1': 0.6, 'E2': 0.3}
    a, b = _definition('a', 'E1'), _definition('b', 'E2')
    assert error_bound([a, b], eps, delta=0.2) == 1.0
    assert error_bound([b, b, b], eps) == pytest.approx(0.3)


def test_error_bound_without_definitions_is_delta():
    assert error_bound([], {'E3': 0.05}, delta=0.02) == pytest.approx(0.02)
    assert error_bound([], {}) == 0.0


def test_missing_grade_counts_as_one():
    assert error_bound([_definition('a', 'E2')], {'E3': 0.05}) == 1.0


def test_lowercase_and_integer_grade_keys():
    d = _definition('a', 'E4')
    assert error_bound([d], {'e4': 0.01}) == pytest.approx(0.01)
    assert error_bound([d], {4: 0.02}) == pytest.approx(0.02)


@pytest.mark.parametrize('eps, delta', [({'E1': 1.5}, 0.0), ({'E1': -0.1}, 0.0), ({'E1': float('nan')}, 0.0),
                                        ({'E1': 'many'}, 0.0), ({'E1': 0.1}, -0.01), ({'E1': 0.1}, 2.0)])
def test_error_bound_rejects_values_outside_unit_interval(eps, delta):
    with pytest.raises(ValueError):
        error_bound([_definition('a', 'E1')], eps, delta=delta)


def test_error_bound_rejects_bad_keys_and_items():
    with pytest.raises(ValueError, match='unknown evidence grade'):
        error_bound([], {'E9': 0.1})
    with pytest.raises(ValueError, match='more than once'):
        error_bound([], {'E1': 0.1, Grade.E1: 0.2})
    with pytest.raises(TypeError):
        error_bound(['not a definition'], {'E1': 0.1})
    with pytest.raises(TypeError):
        error_bound([], [('E1', 0.1)])


def test_normalize_eps_sorts_by_grade():
    rates = normalize_eps({'E3': 0.05, 'e0': 1, Grade.E1: 0.5})
    assert list(rates) == [Grade.E0, Grade.E1, Grade.E3] and rates[Grade.E0] == 1.0


# ------------------------------------------------------------------------------------------- Clopper-Pearson
def test_clopper_pearson_known_values():
    assert clopper_pearson_upper(0, 10) == pytest.approx(0.2589, abs=1e-4)
    assert clopper_pearson_upper(1, 10) == pytest.approx(0.3942, abs=1e-4)
    assert clopper_pearson_upper(0, 10) == pytest.approx(1 - 0.05 ** (1 / 10), abs=1e-12)


@pytest.mark.parametrize('n', [1, 3, 30, 1000, 100000])
@pytest.mark.parametrize('confidence', [0.8, 0.95, 0.99])
def test_clopper_pearson_matches_closed_forms(n, confidence):
    alpha = 1 - confidence
    # 0 errors: (1 - p)^n = alpha;  n - 1 errors: 1 - p^n = alpha
    assert clopper_pearson_upper(0, n, confidence) == pytest.approx(1 - alpha ** (1 / n), rel=1e-9, abs=1e-12)
    assert clopper_pearson_upper(n - 1, n, confidence) == pytest.approx(confidence ** (1 / n), rel=1e-9)


def test_clopper_pearson_bound_meets_its_definition():
    # the upper bound p_U solves P(X <= k; n, p_U) = 1 - confidence
    k, n, alpha = 7, 50, 0.05
    p = clopper_pearson_upper(k, n, 1 - alpha)
    cdf = math.fsum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))
    assert cdf == pytest.approx(alpha, abs=1e-9)
    assert p > k / n


def test_clopper_pearson_large_n_is_finite_and_ordered():
    upper = clopper_pearson_upper(5000, 1_000_000)
    assert 0.005 < upper < 0.0052
    assert clopper_pearson_upper(50, 10_000) > clopper_pearson_upper(5000, 1_000_000)  # same rate, fewer samples


def test_clopper_pearson_edges():
    assert clopper_pearson_upper(0, 0) == 1.0
    assert clopper_pearson_upper(10, 10) == 1.0
    assert clopper_pearson_upper(3.0, 12.0) == clopper_pearson_upper(3, 12)


@pytest.mark.parametrize('errors, n', [(11, 10), (-1, 10), (1.5, 10), (1, -2), (True, 10), ('1', 10)])
def test_counts_must_be_valid(errors, n):
    with pytest.raises(ValueError):
        clopper_pearson_upper(errors, n)


@pytest.mark.parametrize('confidence', [0.0, 1.0, 1.2, -0.5, float('nan'), 'high'])
def test_confidence_must_be_inside_unit_interval(confidence):
    with pytest.raises(ValueError):
        clopper_pearson_upper(1, 10, confidence)


# ------------------------------------------------------------------------------------------- Wilson
def test_wilson_known_values():
    assert wilson_upper(0, 10) == pytest.approx(0.2129, abs=1e-4)
    assert wilson_upper(1, 10) == pytest.approx(0.3477, abs=1e-4)
    assert wilson_upper(10, 10) == pytest.approx(1.0)
    assert wilson_upper(0, 0) == 1.0


def test_wilson_is_below_exact_bound_for_small_counts():
    for k in range(0, 5):
        assert wilson_upper(k, 20) < clopper_pearson_upper(k, 20)


# ------------------------------------------------------------------------------------------- estimate_eps
def test_estimate_eps_returns_grade_keyed_upper_bounds():
    est = estimate_eps({'E3': (0, 10), Grade.E1: (1, 10), 'e2': (0, 0)})
    assert list(est) == [Grade.E1, Grade.E2, Grade.E3]
    assert est[Grade.E3] == pytest.approx(0.2589, abs=1e-4)
    assert est[Grade.E1] == pytest.approx(0.3942, abs=1e-4)
    assert est[Grade.E2] == 1.0


def test_estimate_eps_methods_and_monotonicity():
    counts = {'E1': (3, 40)}
    exact = estimate_eps(counts)[Grade.E1]
    assert estimate_eps(counts, method='clopper_pearson')[Grade.E1] == exact
    assert estimate_eps(counts, method='Wilson')[Grade.E1] < exact
    assert estimate_eps(counts, confidence=0.99)[Grade.E1] > exact          # more confidence, wider bound
    assert estimate_eps({'E1': (4, 40)})[Grade.E1] > exact                  # more errors, larger bound
    assert estimate_eps({'E1': (30, 400)})[Grade.E1] < exact                # same rate, more data, tighter bound
    assert 3 / 40 < exact < 1


def test_estimate_eps_feeds_error_bound():
    eps = estimate_eps({'E3': (1, 100), 'E1': (30, 100)})
    defs = [_definition('active', 'E3'), _definition('quarter', 'E1')]
    assert error_bound(defs, eps, delta=0.01) == pytest.approx(0.01 + eps[Grade.E3] + eps[Grade.E1])


def test_estimate_eps_failures():
    with pytest.raises(ValueError, match='unknown method'):
        estimate_eps({'E1': (1, 10)}, method='bayes')
    with pytest.raises(ValueError, match='E1'):
        estimate_eps({'E1': (11, 10)})
    with pytest.raises(ValueError, match='pair'):
        estimate_eps({'E1': 5})
    with pytest.raises(ValueError, match='more than once'):
        estimate_eps({'E1': (1, 10), 'e1': (2, 10)})
    with pytest.raises(ValueError, match='unknown evidence grade'):
        estimate_eps({'X': (1, 10)})
    with pytest.raises(TypeError):
        estimate_eps([('E1', (1, 10))])


# ------------------------------------------------------------------------------------------- compose / planned
def test_compose_sums_steps_and_caps():
    assert compose([0.25, 0.5]) == 0.75
    assert compose([0.7, 0.6]) == 1.0
    assert compose([]) == 0.0
    assert compose(iter([0.1, 0.1, 0.1])) == pytest.approx(0.3)


def test_compose_of_error_bounds_for_multistep_analysis():
    eps = {'E3': 0.05, 'E1': 0.3}
    step1 = error_bound([_definition('active', 'E3')], eps, delta=0.01)
    step2 = error_bound([_definition('quarter', 'E1')], eps, delta=0.01)
    assert compose([step1, step2]) == pytest.approx(0.06 + 0.31)


@pytest.mark.parametrize('bounds', [[0.1, -0.2], [float('nan')], [1.5], ['x']])
def test_compose_rejects_invalid_bounds(bounds):
    with pytest.raises(ValueError):
        compose(bounds)


def test_calibrate_threshold_is_planned():
    with pytest.raises(NotImplementedYet, match='trust.guarantee'):
        calibrate_threshold(target=0.05)
