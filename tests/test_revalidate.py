"""Tests for revalidate: dependency index, budgeted revalidation plan and recall/cost simulation."""
from types import SimpleNamespace

import pytest

from sql_semantic_ledger import Definition, Evidence, EvidenceKind, Grade, Kind, Ledger
from sql_semantic_ledger.revalidate import DEFAULT_RISKS, AnswerRecord, DependencyIndex, PlanItem, plan, simulate


def _define(led, name, grade):
    """Add a FILTER definition and give it exactly the evidence for ``grade``."""
    d = led.add(Definition(name, Kind.FILTER, f'orders.{name} = 1', tables=['orders']))
    if grade >= Grade.E1:
        led.add_evidence(d.id, Evidence(EvidenceKind.HISTORY, ref=f'{name}-q1'))
        led.add_evidence(d.id, Evidence(EvidenceKind.CODE, ref=f'src/{name}.js:1'))
    if grade >= Grade.E2:
        led.add_evidence(d.id, Evidence(EvidenceKind.DATA_CHECK, ref='selects_rows'))
    if grade >= Grade.E3:
        led.certify(d.id, by='analyst')
    if grade >= Grade.E4:
        led.add_evidence(d.id, Evidence(EvidenceKind.COUNTEREXAMPLE, ref='no_violations'))
    assert d.grade == grade
    return d


@pytest.fixture
def world():
    led = Ledger()
    g = {grade: _define(led, f'g{int(grade)}', grade) for grade in Grade}
    answers = [
        AnswerRecord('a_cert', [g[Grade.E3].id, g[Grade.E4].id], ['orders']),
        AnswerRecord('a_e0', [g[Grade.E0].id], ['orders']),
        AnswerRecord('a_e1', [g[Grade.E1].id], ['customers']),
        AnswerRecord('a_e2', [g[Grade.E2].id], ['customers']),
        AnswerRecord('a_mixed', [g[Grade.E3].id, g[Grade.E1].id], ['orders', 'customers']),
        AnswerRecord('a_e4', [g[Grade.E4].id], ['payments']),
    ]
    return SimpleNamespace(ledger=led, g=g, index=DependencyIndex(answers))


def _ids(items):
    return [item.answer_id for item in items]


# ---------------------------------------------------------------------- dependency index
def test_index_finds_answers_by_definition_and_table(world):
    g, index = world.g, world.index
    assert len(index) == 6 and 'a_e0' in index and 'nope' not in index
    assert [a.id for a in index] == sorted(a.id for a in index)
    assert index.affected_by_definitions([g[Grade.E3].id]) == {'a_cert', 'a_mixed'}
    assert index.affected_by_definitions([g[Grade.E0].id, g[Grade.E4].id]) == {'a_e0', 'a_cert', 'a_e4'}
    assert index.affected_by_tables(['ORDERS']) == {'a_cert', 'a_e0', 'a_mixed'}      # case-insensitive
    assert index.affected_by_tables(['payments', 'customers']) == {'a_e1', 'a_e2', 'a_mixed', 'a_e4'}
    assert index.affected_by_definitions(['unknown']) == set() and index.affected_by_tables([]) == set()
    assert index.get('a_e1').tables == ['customers']


def test_table_names_match_with_or_without_quotes():
    """engine.assemble returns tables as SQL text (``"Order Lines"``); people type them without quotes."""
    index = DependencyIndex([AnswerRecord('lines', [], ['"Order Lines"']), AnswerRecord('main', [], ['main.orders'])])
    for name in ('Order Lines', 'order lines', '"ORDER LINES"', '`order lines`', '[Order Lines]', ' Order Lines '):
        assert index.affected_by_tables([name]) == {'lines'}, name
    assert index.affected_by_tables(['main."Orders"']) == {'main'}
    assert index.affected_by_tables(['orders']) == set()          # a schema-qualified name needs its schema
    led = Ledger()
    d = led.add(Definition('qty', Kind.MEASURE, 'SUM("Order Lines".qty)', tables=['"Order Lines"']))
    index.add(AnswerRecord('qty_answer', [d.id], ['"Order Lines"']))
    items = plan(led, index, changed_tables=['order lines'])
    assert [i.answer_id for i in items] == ['qty_answer', 'lines']          # 'lines' uses no definition: score 0
    assert items[0].reason.endswith('changed: table "Order Lines"')


def test_index_replaces_a_record_with_the_same_id(world):
    g, index = world.g, world.index
    original = AnswerRecord('a_e0', [g[Grade.E1].id], ['payments'])
    index.add(original)
    original.definitions.append(g[Grade.E0].id)                # the index keeps its own copy
    assert len(index) == 6
    assert index.affected_by_definitions([g[Grade.E0].id]) == set()
    assert 'a_e0' not in index.affected_by_tables(['orders'])
    assert index.affected_by_tables(['payments']) == {'a_e0', 'a_e4'}
    assert index.affected_by_definitions([g[Grade.E1].id]) == {'a_e0', 'a_e1', 'a_mixed'}


# ---------------------------------------------------------------------- plan
def test_change_to_one_definition_affects_only_the_answers_using_it(world):
    g1 = world.g[Grade.E1]
    items = plan(world.ledger, world.index, changed_definitions=[g1.id])
    assert _ids(items) == ['a_mixed', 'a_e1']                  # 0.55 and 0.5; nothing else is touched
    world.ledger.revise(g1.id, expression='orders.g1 = 2', note='business rule changed')
    items = plan(world.ledger, world.index, changed_definitions=[g1.id])
    assert _ids(items) == ['a_e1', 'a_mixed']                  # the new version has no evidence: E0, risk 1
    assert [item.score for item in items] == [1.0, 1.0]
    assert items[0].reason == f'riskiest definition g1 [{g1.id}] E0 (risk 1); changed: definition g1 [{g1.id}]'


def test_ranking_puts_low_grade_answers_before_certified_ones(world):
    items = plan(world.ledger, world.index)                    # no change given: every answer is a candidate
    assert _ids(items) == ['a_e0', 'a_mixed', 'a_e1', 'a_e2', 'a_cert', 'a_e4']
    expected = [1.0, 1 - 0.9 * 0.5, 0.5, 0.3, 1 - 0.9 * 0.95, 0.05]
    assert [item.score for item in items] == pytest.approx(expected)
    certified_only = {'a_cert', 'a_e4'}
    positions = {item.answer_id: n for n, item in enumerate(items)}
    assert max(positions[a] for a in positions if a not in certified_only) < min(positions[a] for a in certified_only)


def test_reason_names_the_riskiest_definition_and_its_grade(world):
    g = world.g
    items = {item.answer_id: item for item in plan(world.ledger, world.index, changed_tables=['Orders'])}
    assert items['a_mixed'].reason == (f'riskiest definition g1 [{g[Grade.E1].id}] E1 (risk 0.5); '
                                       f'changed: table orders')
    assert items['a_cert'].reason.startswith(f'riskiest definition g3 [{g[Grade.E3].id}] E3 (risk 0.1)')


def test_custom_and_partial_eps(world):
    eps = {'E0': 0.9, 'E1': 0.4, Grade.E2: 0.2, 'e3': 0.02, 4: 0.01}
    scores = {i.answer_id: i.score for i in plan(world.ledger, world.index, eps=eps)}
    assert scores['a_e0'] == pytest.approx(0.9)
    assert scores['a_cert'] == pytest.approx(1 - 0.98 * 0.99)
    assert scores['a_mixed'] == pytest.approx(1 - 0.98 * 0.6)
    partial = {i.answer_id: i.score for i in plan(world.ledger, world.index, eps={'E3': 0.2})}
    assert partial['a_cert'] == pytest.approx(1 - 0.8 * 0.95)  # E4 keeps its default 0.05
    assert partial['a_e1'] == pytest.approx(DEFAULT_RISKS[Grade.E1])


def test_default_risks_follow_the_contract_and_are_read_only():
    assert dict(DEFAULT_RISKS) == {Grade.E0: 1.0, Grade.E1: 0.5, Grade.E2: 0.3, Grade.E3: 0.1, Grade.E4: 0.05}
    with pytest.raises(TypeError):
        DEFAULT_RISKS[Grade.E0] = 0.5


def test_unknown_definition_ids_count_as_e0(world):
    world.index.add(AnswerRecord('a_ghost', ['deadbeef0000', world.g[Grade.E4].id], ['orders'], weight=2))
    items = plan(world.ledger, world.index, changed_definitions=['deadbeef0000'])
    assert _ids(items) == ['a_ghost'] and items[0].score == 2.0
    assert items[0].reason.startswith('riskiest definition deadbeef0000 (not in ledger) E0 (risk 1)')
    assert items[0].reason.endswith('; weight 2')


def test_budget_cuts_the_plan(world):
    led, index = world.ledger, world.index
    assert _ids(plan(led, index, budget=2)) == ['a_e0', 'a_mixed']
    assert plan(led, index, budget=0) == []
    assert len(plan(led, index, budget=100)) == 6
    assert _ids(plan(led, index, changed_tables=['customers'], budget=1)) == ['a_mixed']


@pytest.mark.parametrize('budget, error', [(-1, ValueError), (1.5, TypeError), ('2', TypeError), (True, TypeError)])
def test_bad_budget(world, budget, error):
    with pytest.raises(error):
        plan(world.ledger, world.index, budget=budget)


def test_changes_that_touch_no_answer_give_an_empty_plan(world):
    assert plan(world.ledger, world.index, changed_definitions=['used-by-nobody']) == []
    assert plan(world.ledger, world.index, changed_tables=['inventory']) == []
    assert len(plan(world.ledger, world.index)) == 6


def test_table_and_definition_changes_are_combined(world):
    items = plan(world.ledger, world.index, changed_definitions=[world.g[Grade.E4].id], changed_tables=['customers'])
    assert _ids(items) == ['a_mixed', 'a_e1', 'a_e2', 'a_cert', 'a_e4']
    assert items[0].reason.endswith('changed: table customers')


def test_weight_scales_the_score_and_answers_without_definitions_score_zero(world):
    world.index.add(AnswerRecord('a_heavy', [world.g[Grade.E2].id], weight=4))
    world.index.add(AnswerRecord('a_raw', [], ['orders'], weight=5))
    items = plan(world.ledger, world.index)
    by_id = {item.answer_id: item for item in items}
    assert by_id['a_heavy'].score == pytest.approx(1.2) and _ids(items)[0] == 'a_heavy'
    assert by_id['a_raw'].score == 0.0 and _ids(items)[-1] == 'a_raw'
    raw = plan(world.ledger, world.index, changed_tables=['orders'])[-1]
    assert raw.answer_id == 'a_raw' and raw.reason == 'uses no ledger definitions; changed: table orders; weight 5'


def test_equal_scores_are_identical_and_ordered_by_id(world):
    led, g = world.ledger, world.g
    h = {grade: _define(led, f'h{int(grade)}', grade) for grade in (Grade.E1, Grade.E2, Grade.E3)}
    index = DependencyIndex([
        AnswerRecord('z2', [h[Grade.E1].id, g[Grade.E2].id, h[Grade.E3].id]),
        AnswerRecord('z1', [g[Grade.E1].id, h[Grade.E2].id, g[Grade.E3].id, g[Grade.E3].id]),  # repeats count once
    ])
    items = plan(led, index)
    assert _ids(items) == ['z1', 'z2'] and items[0].score == items[1].score
    assert items[0].score == pytest.approx(1 - 0.5 * 0.7 * 0.9)


@pytest.mark.parametrize('eps, error', [
    ({'E9': 0.1}, ValueError), ({'X': 0.1}, ValueError), ({'E1': 1.5}, ValueError), ({'E1': -0.1}, ValueError),
    ({'E1': float('nan')}, ValueError), ({'E1': 'high'}, ValueError), ({'E1': 0.1, Grade.E1: 0.2}, ValueError),
    ([0.1, 0.2], TypeError),
])
def test_bad_eps(world, eps, error):
    with pytest.raises(error):
        plan(world.ledger, world.index, eps=eps)


def test_bad_arguments(world):
    with pytest.raises(TypeError):
        plan(world.ledger, list(world.index))
    with pytest.raises(TypeError):
        plan(Ledger.to_dict(world.ledger), world.index)
    with pytest.raises(TypeError):
        plan(world.ledger, world.index, changed_definitions=world.g[Grade.E1].id)   # a bare string
    with pytest.raises(TypeError):
        world.index.affected_by_tables('orders')
    with pytest.raises(TypeError):
        world.index.add({'id': 'a9', 'definitions': []})
    with pytest.raises(KeyError):
        world.index.get('missing')


# ---------------------------------------------------------------------- answer records
def test_answer_record_validation_and_dict_round_trip():
    record = AnswerRecord.from_dict({'id': 3, 'definitions': ['d1'], 'tables': ['t'], 'weight': 2, 'question': 'ignored'})
    assert record == AnswerRecord('3', ['d1'], ['t'], 2.0)
    assert AnswerRecord.from_dict(record.to_dict()) == record
    assert AnswerRecord.from_dict({'id': 'a'}) == AnswerRecord('a', [], [], 1.0)
    assert AnswerRecord('a', {'d2', 'd1'}).definitions == ['d1', 'd2']        # a set becomes a sorted list
    with pytest.raises(TypeError):
        AnswerRecord('a', 'd1')                                                # bare string
    with pytest.raises(TypeError):
        AnswerRecord.from_dict({'id': 'a', 'definitions': 'd1'})
    with pytest.raises(TypeError):
        AnswerRecord('a', [1, 2])
    for bad_id in ('', None, True):
        with pytest.raises(ValueError):
            AnswerRecord(bad_id, [])
    for bad_weight in (-1, float('inf'), float('nan'), 'heavy', None):
        with pytest.raises(ValueError):
            AnswerRecord('a', [], weight=bad_weight)
    with pytest.raises(ValueError):
        AnswerRecord.from_dict({'definitions': ['d1']})


# ---------------------------------------------------------------------- simulate
def test_simulate_numbers():
    items = [PlanItem('a2', 0.9), PlanItem('a3', 0.5), PlanItem('a4', 0.1)]
    out = simulate(items, truly_wrong=['a2', 'a5', 'x9'], candidates=['a1', 'a2', 'a3', 'a4', 'a5', 'a6'])
    assert out == {'recall': 0.5, 'cost': 3, 'cost_ratio': 0.5, 'candidates': 6, 'wrong': 2, 'found': 1}


def test_simulate_edge_cases():
    everything = ['a1', 'a2', 'a3', 'a4']
    full = simulate([PlanItem(a, 1.0) for a in everything], ['a1', 'a4'], everything)
    assert full['recall'] == 1.0 and full['cost_ratio'] == 1.0
    assert simulate([], [], everything) == {'recall': 1.0, 'cost': 0, 'cost_ratio': 0.0, 'candidates': 4,
                                            'wrong': 0, 'found': 0}
    assert simulate([], ['x'], []) == {'recall': 1.0, 'cost': 0, 'cost_ratio': 0.0, 'candidates': 0, 'wrong': 0,
                                       'found': 0}
    mixed = simulate(['a1', 'a1', PlanItem('a2', 0.0)], {'a1'}, set(everything))   # plain ids, repeats once
    assert mixed['cost'] == 2 and mixed['cost_ratio'] == 0.5 and mixed['recall'] == 1.0
    assert simulate([], ['a3'], everything)['recall'] == 0.0
    with pytest.raises(ValueError, match='not among the candidates'):
        simulate([PlanItem('zz', 1.0)], [], everything)
    with pytest.raises(TypeError):
        simulate('a1', [], everything)
    with pytest.raises(TypeError):
        simulate([], 'a1', everything)


def test_plan_and_simulate_end_to_end(world):
    """At the same budget the risk-ranked plan finds the wrong answers that an id-ordered pass misses."""
    led, g, index = world.ledger, world.g, world.index
    led.revise(g[Grade.E1].id, expression='orders.g1 = 2')     # the rule changed: answers using g1 may be stale
    changed = [g[Grade.E1].id, g[Grade.E4].id]
    candidates = index.affected_by_definitions(changed)
    truly_wrong = {'a_e1', 'a_mixed'}
    ranked = plan(led, index, changed_definitions=changed, budget=2)
    assert _ids(ranked) == ['a_e1', 'a_mixed']
    assert simulate(ranked, truly_wrong, candidates) == {'recall': 1.0, 'cost': 2, 'cost_ratio': 0.5,
                                                         'candidates': 4, 'wrong': 2, 'found': 2}
    by_id = sorted(candidates)[:2]                             # baseline: same budget, no ranking
    assert simulate(by_id, truly_wrong, candidates)['recall'] == 0.5
