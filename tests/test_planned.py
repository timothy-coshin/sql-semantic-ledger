"""Planned features: every API stub raises NotImplementedYet with a message that matches the feature table."""
from __future__ import annotations

import pytest

from sql_semantic_ledger import Ledger
from sql_semantic_ledger import agent_server, bound, code_sources, engine, multisource, propose
from sql_semantic_ledger._unimplemented import NotImplementedYet, planned
from sql_semantic_ledger.features import BY_ID, FEATURES

STUBS = [
    ('trust.engine_nl', lambda: engine.answer('paid revenue by region?', Ledger())),
    ('trust.engine_nl', lambda: propose.propose_with_model({'orders': ['amount']})),
    ('trust.agent_server', lambda: agent_server.serve('ledger.json')),
    ('make.code_orm', lambda: code_sources.orm_queries('src/')),
    ('make.code_js', lambda: code_sources.lift_js_computation('src/app.js')),
    ('make.multisource', lambda: multisource.load_dbt_semantic_models('semantic_models.yml')),
    ('trust.guarantee', lambda: bound.calibrate_threshold(target=0.05)),
]


@pytest.mark.parametrize('feature_id, call', STUBS, ids=[f'{f}-{i}' for i, (f, _) in enumerate(STUBS)])
def test_every_stub_raises_with_its_feature(feature_id, call):
    feature = BY_ID[feature_id]
    assert feature.status in ('planned', 'partial')
    with pytest.raises(NotImplementedYet) as caught:
        call()
    message = str(caught.value)
    assert f'[{feature_id}]' in message and feature.name_en in message and feature.stage_en in message
    assert isinstance(caught.value, NotImplementedError)


def test_planned_and_partial_messages_say_what_exists():
    with pytest.raises(NotImplementedYet, match='is planned for .* and is not implemented in this release'):
        planned('trust.guarantee')
    with pytest.raises(NotImplementedYet) as caught:                 # YAML loading and conflicts do work
        multisource.load_dbt_semantic_models('semantic_models.yml')
    message = str(caught.value)
    assert 'only partly implemented' in message and 'not implemented in this release' not in message
    assert BY_ID['make.multisource'].note in message
    with pytest.raises(NotImplementedYet, match='not implemented yet'):
        planned('no.such.feature')


def test_every_planned_feature_with_a_module_has_a_stub():
    covered = {feature_id for feature_id, _ in STUBS}
    for f in FEATURES:
        if f.status == 'planned' and f.module != '-':
            assert f.id in covered, f'{f.id} is planned in {f.module} but has no stub test'
