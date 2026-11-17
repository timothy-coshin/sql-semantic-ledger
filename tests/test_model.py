import json

from sql_semantic_ledger import Definition, Evidence, EvidenceKind, Grade, Kind, Ledger
from sql_semantic_ledger.features import FEATURES, table
from sql_semantic_ledger._unimplemented import NotImplementedYet, planned


def _metric():
    return Definition(name='paid_revenue', kind=Kind.METRIC, expression='SUM(orders.amount)',
                      conditions=["orders.status = 'paid'"], tables=['orders'], columns=['orders.amount', 'orders.status'])


def test_grade_ladder():
    d = _metric()
    assert d.grade == Grade.E0
    d.evidence.append(Evidence(EvidenceKind.HISTORY, ref='q1'))
    assert d.grade == Grade.E0 and d.support == 1
    d.evidence.append(Evidence(EvidenceKind.HISTORY, ref='q1'))  # same query again is not independent
    assert d.support == 1
    d.evidence.append(Evidence(EvidenceKind.CODE, ref='src/api/orders.js:12'))
    assert d.support == 2 and d.grade == Grade.E1 and d.sources == ['code', 'history']
    d.evidence.append(Evidence(EvidenceKind.DATA_CHECK, ref='executes', passed=True))
    assert d.grade == Grade.E2
    d.evidence.append(Evidence(EvidenceKind.CERTIFICATION, ref='analyst', passed=True))
    assert d.grade == Grade.E3 and d.grade.certified
    d.evidence.append(Evidence(EvidenceKind.COUNTEREXAMPLE, ref='no_negative_amounts', passed=True))
    assert d.grade == Grade.E4


def test_failed_check_falsifies():
    d = _metric()
    d.evidence += [Evidence(EvidenceKind.HISTORY, ref='q1'), Evidence(EvidenceKind.HISTORY, ref='q2'),
                   Evidence(EvidenceKind.DATA_CHECK, ref='executes', passed=False)]
    assert d.falsified and d.grade == Grade.E0


def test_ledger_certify_revise_expire_roundtrip(tmp_path):
    led = Ledger()
    d = led.add(_metric())
    led.add_evidence(d.id, Evidence(EvidenceKind.HISTORY, ref='q1'))
    led.add_evidence(d.id, Evidence(EvidenceKind.HISTORY, ref='q2'))
    led.certify(d.id, by='analyst', note='matches the finance report')
    assert led.get(d.id).grade == Grade.E3
    led.revise(d.id, conditions=["orders.status = 'paid'", 'orders.is_test = 0'], note='exclude test orders')
    assert led.get(d.id).version == 2 and led.get(d.id).grade == Grade.E0  # old evidence no longer counts
    led.expire(d.id, at='2030-01-01T00:00:00+00:00')
    assert led.get(d.id).is_valid('2029-12-31T00:00:00+00:00') and not led.get(d.id).is_valid('2030-01-02T00:00:00+00:00')
    types = [e.type for e in led.events]
    assert types[0] == 'add' and 'certify' in types and 'revise' in types and types[-1] == 'expire'
    path = tmp_path / 'ledger.json'
    led.save(path)
    back = Ledger.load(path)
    assert len(back) == 1 and back.get(d.id).version == 2 and len(back.events) == len(led.events)
    assert json.loads(path.read_text(encoding='utf-8'))['schema_version'] == 1


def test_add_merges_evidence_and_find():
    led = Ledger()
    a = _metric(); a.evidence.append(Evidence(EvidenceKind.HISTORY, ref='q1'))
    b = _metric(); b.evidence.append(Evidence(EvidenceKind.HISTORY, ref='q2'))
    led.add(a); led.add(b)
    assert len(led) == 1 and led.get(a.id).support == 2
    assert led.find(name='PAID_REVENUE') and led.find(min_grade='E1') and not led.find(min_grade=Grade.E3)
    assert led.summary()['by_grade']['E1'] == 1


def test_features_and_planned():
    ids = {f.id for f in FEATURES}
    assert {'make.code_js', 'trust.agent_server', 'trust.guarantee'} <= ids
    assert '미구현' in table('ko') and 'planned' in table('en')
    try:
        planned('trust.agent_server')
    except NotImplementedYet as e:
        assert 'Stage 2' in str(e)
    else:
        raise AssertionError('planned() must raise')


def test_e4_counts_counterexample_checks_after_certification():
    d = _metric()
    d.evidence += [Evidence(EvidenceKind.HISTORY, ref='q1'), Evidence(EvidenceKind.HISTORY, ref='q2'),
                   Evidence(EvidenceKind.COUNTEREXAMPLE, ref='orders.amount < 0', at='2026-10-01T09:00:00+00:00'),
                   Evidence(EvidenceKind.CERTIFICATION, ref='analyst', at='2026-10-02T09:00:00+00:00')]
    assert d.grade == Grade.E3 and d.counterexamples_after_certification() == []   # checked before certification
    d.evidence.append(Evidence(EvidenceKind.COUNTEREXAMPLE, ref='orders.amount IS NULL', at='2026-10-03T09:00:00+00:00'))
    assert d.grade == Grade.E4
    d.evidence.append(Evidence(EvidenceKind.CERTIFICATION, ref='lead', at='2026-10-04T09:00:00+00:00'))
    assert d.grade == Grade.E4                     # another reviewer's certification keeps the checks
    d.evidence.append(Evidence(EvidenceKind.CERTIFICATION, ref='auditor', passed=False, at='2026-10-05T09:00:00+00:00'))
    assert d.grade == Grade.E0 and d.counterexamples_after_certification() == []
    d.evidence.append(Evidence(EvidenceKind.CERTIFICATION, ref='lead', at='2026-10-06T09:00:00+00:00'))
    assert d.grade == Grade.E3                     # after a rejection, only checks after the new certification count
    d.evidence.append(Evidence(EvidenceKind.COUNTEREXAMPLE, ref='orders.amount > 1e9', at='2026-10-07T09:00:00+00:00'))
    assert d.grade == Grade.E4
    assert [e.ref for e in d.counterexamples_after_certification()] == ['orders.amount > 1e9']
    d.evidence.append(Evidence(EvidenceKind.COUNTEREXAMPLE, ref='orders.amount = 0', passed=False,
                               at='2026-09-01T00:00:00+00:00'))
    assert d.falsified and d.grade == Grade.E0     # a failed check falsifies whenever it ran


def test_e4_same_second_evidence_keeps_recording_order():
    t = '2026-10-01T09:00:00+00:00'
    before = _metric()
    before.evidence += [Evidence(EvidenceKind.COUNTEREXAMPLE, ref='orders.amount < 0', at=t),
                        Evidence(EvidenceKind.CERTIFICATION, ref='analyst', at=t)]
    after = _metric()
    after.evidence += [Evidence(EvidenceKind.CERTIFICATION, ref='analyst', at=t),
                       Evidence(EvidenceKind.COUNTEREXAMPLE, ref='orders.amount < 0', at=t)]
    assert before.grade == Grade.E3 and after.grade == Grade.E4


def test_rejection_falsifies_and_recertification_restores():
    from sql_semantic_ledger.grade import Verdict, decide
    led = Ledger()
    d = led.add(_metric())
    led.add_evidence(d.id, Evidence(EvidenceKind.HISTORY, ref='q1'))
    led.add_evidence(d.id, Evidence(EvidenceKind.HISTORY, ref='q2'))
    assert led.get(d.id).grade == Grade.E1
    led.certify(d.id, by='analyst', passed=False, note='wrong status value')
    assert led.get(d.id).rejected and led.get(d.id).falsified and led.get(d.id).grade == Grade.E0
    assert decide([led.get(d.id)]).verdict == Verdict.REJECT
    later = Evidence(EvidenceKind.CERTIFICATION, ref='lead', passed=True, at='2999-01-01T00:00:00+00:00')
    led.add_evidence(d.id, later)
    assert not led.get(d.id).rejected and led.get(d.id).grade == Grade.E3
