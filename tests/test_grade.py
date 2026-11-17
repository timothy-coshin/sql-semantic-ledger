"""Tests for grade.decide: the confirm / warn / reject rule, the error bound and the reasons."""
import pytest

from sql_semantic_ledger import Definition, Evidence, EvidenceKind, Grade, Kind, Ledger
from sql_semantic_ledger.bound import error_bound, estimate_eps
from sql_semantic_ledger.grade import Decision, Policy, Verdict, decide


def _filter(name, expression, refs=('q1', 'q2'), table='orders'):
    """A FILTER definition supported by the given past queries (two refs give E1)."""
    d = Definition(name=name, kind=Kind.FILTER, expression=expression, tables=[table])
    d.evidence += [Evidence(EvidenceKind.HISTORY, ref=r) for r in refs]
    return d


def _certified(d, by='analyst', at='2026-10-01T09:00:00+00:00'):
    d.evidence.append(Evidence(EvidenceKind.CERTIFICATION, ref=by, detail='checked the concept sentence', at=at))
    return d


def _plan_example():
    """The research plan's worked example: the number of active customers last quarter."""
    active = _certified(_filter('활성 고객', "customers.last_paid_at >= DATE('now', '-90 day') AND customers.is_test = 0",
                                refs=('q11', 'q12', 'q19'), table='customers'))
    quarter = _filter('지난 분기', "orders.fiscal_quarter = 'last'", refs=('q3', 'q8'))
    return active, quarter


# ------------------------------------------------------------------------------------------- the plan's example
def test_plan_example_warns_then_confirms_after_certification():
    active, quarter = _plan_example()
    assert (active.grade, quarter.grade) == (Grade.E3, Grade.E1)
    eps, delta = {'E3': 0.05, 'E1': 0.3}, 0.02

    first = decide([active, quarter], eps=eps, delta=delta)
    assert first.verdict is Verdict.WARN
    assert first.bound == pytest.approx(delta + eps['E3'] + eps['E1'])
    assert first.grades == {active.id: 'E3', quarter.id: 'E1'}
    text = '\n'.join(first.reasons)
    assert '활성 고객 (E3)' in text and 'certified by analyst at 2026-10-01T09:00:00+00:00' in text
    assert '지난 분기 (E1)' in text and 'q3, q8' in text and '-> warn' in text      # basis is shown with the answer

    _certified(quarter, by='finance-lead')                                          # the owner certifies 'last quarter'
    second = decide([active, quarter], eps=eps, delta=delta)
    assert second.verdict is Verdict.CONFIRM
    assert second.bound == pytest.approx(delta + eps['E3'] + eps['E3'])
    assert any(r.startswith('error bound 0.12') for r in second.reasons)


def test_plan_example_with_estimated_eps():
    active, quarter = _plan_example()
    eps = estimate_eps({'E3': (1, 120), 'E1': (18, 60)})
    decision = decide([active, quarter], eps=eps, delta=0.02)
    assert decision.verdict is Verdict.WARN
    assert decision.bound == pytest.approx(error_bound([active, quarter], eps, 0.02))
    assert decision.bound == pytest.approx(0.02 + eps[Grade.E3] + eps[Grade.E1])


# ------------------------------------------------------------------------------------------- the rule
def test_no_definitions_rejects():
    decision = decide([])
    assert decision == Decision(Verdict.REJECT, bound=None, reasons=['no matching definition'], grades={})
    assert decide([], eps={'E3': 0.05}, delta=0.02).bound == pytest.approx(0.02)


def test_e0_definition_rejects():
    active, _ = _plan_example()
    guess = Definition(name='model guess', kind=Kind.FILTER, expression='orders.flag = 1', tables=['orders'])
    guess.evidence.append(Evidence(EvidenceKind.MODEL, detail='proposed by a model'))
    decision = decide([active, guess])
    assert decision.verdict is Verdict.REJECT
    assert any(r.startswith('model guess (E0): no supporting evidence') and r.endswith('-> reject')
               for r in decision.reasons)
    single = _filter('one query only', 'orders.x = 1', refs=('q1',))
    assert 'E1 needs 2' in decide([single]).reasons[0]


def test_falsified_definition_rejects_even_when_certified():
    active, _ = _plan_example()
    active.evidence.append(Evidence(EvidenceKind.DATA_CHECK, ref='selects_rows', passed=False))
    decision = decide([active])
    assert decision.verdict is Verdict.REJECT and decision.grades[active.id] == 'E0'
    assert 'falsified by failed check selects_rows' in decision.reasons[0]


def test_expired_definition_rejects_unless_validity_is_not_required():
    active, _ = _plan_example()
    active.valid_until = '2026-01-01T00:00:00+00:00'
    before = decide([active], at='2025-12-31T00:00:00+00:00')
    assert before.verdict is Verdict.CONFIRM
    after = decide([active], at='2026-06-01T00:00:00+00:00')
    assert after.verdict is Verdict.REJECT and 'expired (valid until 2026-01-01' in after.reasons[0]
    lenient = decide([active], policy=Policy(require_valid=False), at='2026-06-01T00:00:00+00:00')
    assert lenient.verdict is Verdict.CONFIRM and 'not enforced' in lenient.reasons[0]


def test_not_yet_valid_definition_rejects():
    active, _ = _plan_example()
    active.valid_from = '2027-01-01T00:00:00+00:00'
    decision = decide([active], at='2026-11-17T00:00:00+00:00')
    assert decision.verdict is Verdict.REJECT and 'not valid yet' in decision.reasons[0]


def test_expired_definition_in_a_ledger_rejects():
    led = Ledger()
    active, _ = _plan_example()
    led.add(active)
    led.expire(active.id, at='2026-03-01T00:00:00+00:00', note='new definition of active customers')
    assert decide([led.get(active.id)], at='2026-04-01T00:00:00+00:00').verdict is Verdict.REJECT


def test_all_certified_confirms_and_e4_counts_as_certified():
    active, quarter = _plan_example()
    _certified(quarter)
    quarter.evidence.append(Evidence(EvidenceKind.COUNTEREXAMPLE, ref='orders.fiscal_quarter IS NULL'))
    assert quarter.grade == Grade.E4
    decision = decide([active, quarter])
    assert decision.verdict is Verdict.CONFIRM and decision.bound is None
    assert any('passed 1 counterexample check' in r for r in decision.reasons)


def test_machine_evidence_only_warns_and_e2_reason_lists_checks():
    e2 = _filter('paid', "orders.status = 'paid'")
    e2.evidence.append(Evidence(EvidenceKind.DATA_CHECK, ref='selects_rows'))
    assert e2.grade == Grade.E2
    decision = decide([e2])
    assert decision.verdict is Verdict.WARN
    assert 'machine evidence only' in decision.reasons[0] and 'data checks passed (selects_rows)' in decision.reasons[0]


def test_rejected_certification_is_reported():
    d = _filter('paid', "orders.status = 'paid'")
    d.evidence.append(Evidence(EvidenceKind.CERTIFICATION, ref='reviewer', passed=False))
    decision = decide([d])
    assert decision.verdict is Verdict.REJECT and 'certification rejected by reviewer' in decision.reasons[0]


def test_reject_beats_warn_and_reasons_cover_every_definition():
    active, quarter = _plan_example()
    guess = Definition(name='guess', kind=Kind.FILTER, expression='orders.y = 2', tables=['orders'])
    decision = decide([active, quarter, guess])
    assert decision.verdict is Verdict.REJECT
    assert [r.split(':')[0] for r in decision.reasons] == ['활성 고객 (E3)', '지난 분기 (E1)', 'guess (E0)']


# ------------------------------------------------------------------------------------------- policy and bound
def test_bound_above_target_turns_confirm_into_warn():
    active, quarter = _plan_example()
    _certified(quarter)
    eps = {'E3': 0.05}
    strict = decide([active, quarter], policy=Policy(target=0.1), eps=eps, delta=0.02)
    assert strict.verdict is Verdict.WARN and strict.bound == pytest.approx(0.12)
    assert strict.reasons[-1].startswith('error bound 0.12 = delta 0.02 + eps(E3) 0.05 + eps(E3) 0.05 > target 0.1')
    assert strict.reasons[-1].endswith('-> warn')
    relaxed = decide([active, quarter], policy=Policy(target=0.15), eps=eps, delta=0.02)
    assert relaxed.verdict is Verdict.CONFIRM and relaxed.reasons[-1].endswith('<= target 0.15')


def test_target_without_eps_does_not_change_the_verdict():
    active, _ = _plan_example()
    decision = decide([active], policy=Policy(target=0.0))
    assert decision.verdict is Verdict.CONFIRM and decision.bound is None


def test_bound_above_target_keeps_warn_and_reject():
    _, quarter = _plan_example()
    decision = decide([quarter], policy=Policy(target=0.05), eps={'E1': 0.3})
    assert decision.verdict is Verdict.WARN and '> target 0.05' in decision.reasons[-1]


def test_missing_grade_in_eps_gives_bound_one():
    active, _ = _plan_example()
    decision = decide([active], policy=Policy(target=0.5), eps={'E1': 0.3})
    assert decision.bound == 1.0 and decision.verdict is Verdict.WARN


def test_confirm_min_can_be_raised_or_lowered():
    active, quarter = _plan_example()
    assert decide([active], policy=Policy(confirm_min=Grade.E4)).verdict is Verdict.WARN
    assert decide([quarter], policy=Policy(confirm_min='E1')).verdict is Verdict.CONFIRM
    guess = Definition(name='guess', kind=Kind.FILTER, expression='orders.y = 2', tables=['orders'])
    assert decide([guess], policy=Policy(confirm_min='E0')).verdict is Verdict.REJECT   # E0 is never confirmed


def test_duplicates_count_once():
    active, quarter = _plan_example()
    decision = decide([quarter, active, quarter], eps={'E3': 0.05, 'E1': 0.3})
    assert len(decision.reasons) == 3 and decision.bound == pytest.approx(0.35)


def test_invalid_inputs():
    active, _ = _plan_example()
    with pytest.raises(TypeError):
        decide([active.id])
    with pytest.raises(ValueError, match='confirm_min'):
        decide([active], policy=Policy(confirm_min='E7'))
    with pytest.raises(ValueError, match='target'):
        decide([active], policy=Policy(target=1.5))
    with pytest.raises(ValueError):
        decide([active], eps={'E3': 2.0})
    with pytest.raises(ValueError):
        decide([active], eps={'E3': 0.05}, delta=-0.1)


def test_decide_accepts_any_iterable_and_keeps_order():
    active, quarter = _plan_example()
    decision = decide(iter([quarter, active]))
    assert list(decision.grades) == [quarter.id, active.id] and decision.verdict is Verdict.WARN
