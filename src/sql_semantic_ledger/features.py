"""What is implemented in this release and what is still planned, mapped to the research roadmap.

``status`` is one of:

* ``implemented``  works and is tested in this release
* ``partial``      a first, limited version (see ``note``)
* ``planned``      not implemented yet; calling it raises :class:`NotImplementedYet`

``area`` is the lifecycle phase (Generation, Evaluation, Revalidation) or the ledger and tooling, and ``stage`` is
the roadmap stage and year. Both are stored with their Korean labels, which ``table('ko')`` and the Korean README
use; ``area_en`` and ``stage_en`` give the English forms that ``table('en')`` uses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List


AREA_EN = {'장부': 'Ledger', '만들기': 'Generation', '믿기': 'Evaluation', '고치기': 'Revalidation', '도구': 'Tooling'}


def _area_en(area: str) -> str:
    """English area name from the Korean label, e.g. '만들기(2단계)' -> 'Generation (stage 2)'."""
    base, _, rest = area.partition('(')
    text = AREA_EN.get(base.strip(), base.strip())
    return text + (' (stage 2)' if '2단계' in rest else '')


def _stage_en(stage: str) -> str:
    """English roadmap stage from the Korean label, e.g. '2단계 4-5차' -> 'Stage 2, years 4-5'."""
    m = re.fullmatch(r'(\d)단계(?: (\d)(?:-(\d))?차)?', stage.strip())
    if not m:
        return stage
    out = f'Stage {m.group(1)}'
    if m.group(2):
        out += f', year {m.group(2)}' if not m.group(3) else f', years {m.group(2)}-{m.group(3)}'
    return out


@dataclass(frozen=True)
class Feature:
    id: str
    area: str
    name_ko: str
    name_en: str
    status: str
    stage: str
    module: str
    note: str = ''

    @property
    def area_en(self) -> str:
        """English name of the area (e.g. 'Generation (stage 2)')."""
        return _area_en(self.area)

    @property
    def stage_en(self) -> str:
        """English plan stage (e.g. 'Stage 2, year 4')."""
        return _stage_en(self.stage)


FEATURES: List[Feature] = [
    Feature('ledger.store', '장부', '업무 정의·증거·인증 기록을 남기는 추가 전용 장부',
            'Append-only ledger of definitions, evidence and certifications', 'implemented', '1단계', 'model'),
    Feature('make.candidates', '만들기', 'SQL 정규화와 정의 후보 생성(집계·조건·조인·지표)',
            'SQL normalization and candidate generation (aggregates, conditions, joins, metrics)', 'implemented', '1단계 1차', 'parse, induce'),
    Feature('make.support', '만들기', '서로 다른 질의 2개 이상의 지지 검사(E1)',
            'Independent-support check: >= 2 different queries or code sites (E1)', 'implemented', '1단계 1차', 'induce'),
    Feature('make.datacheck', '만들기', '실제 데이터에 의한 반증 검사(E2)',
            'Falsification against the database (E2)', 'implemented', '1단계 1-2차', 'checks'),
    Feature('trust.counterexample', '믿기', '인증 뒤 반례 검사(E4)',
            'Counterexample checks after certification (E4)', 'partial', '1단계 2-3차', 'checks',
            'violating-row queries against the database; user-reported counterexamples are planned'),
    Feature('make.code_sql', '만들기(2단계)', '서비스 코드 속 SQL 문자열에서 정의 귀납',
            'Induction from SQL strings embedded in service code (JS/TS/Python)', 'implemented', '2단계 4차', 'code_sources',
            'string and template literals (and concatenated literals) that parse as queries over a table; '
            'interpolated values become parameters, and queries whose structure is built at run time are skipped'),
    Feature('make.code_orm', '만들기(2단계)', 'ORM 질의(예: Prisma, SQLAlchemy, Django)에서 정의 귀납',
            'Induction from ORM queries', 'planned', '2단계 4차', 'code_sources'),
    Feature('make.code_js', '만들기(2단계)', '화면 자바스크립트 계산 로직을 정의로 끌어올리는 정적 분석',
            'Static analysis that lifts UI JavaScript computations to definitions', 'planned', '2단계 4차', 'code_sources'),
    Feature('make.multisource', '만들기(2단계)', '지표 저장소(YAML) 정의 읽기와 출처·제품 간 정의 충돌 탐지',
            'Metric-store (YAML) definitions and cross-source / cross-product conflict detection', 'partial', '2단계 4차', 'multisource',
            'a minimal YAML schema; full dbt MetricFlow / metric-hub schemas are planned'),
    Feature('trust.decide', '믿기', '증거 등급에 따른 답변의 확정·경고·거부',
            'Confirm / warn / reject an answer by the evidence grades of its definitions', 'implemented', '1단계 2차', 'grade'),
    Feature('trust.bound', '믿기', '확정 답변의 오답률 상한 P <= delta + sum eps(e(d))',
            'Error-rate upper bound for an answer', 'implemented', '1단계 2-3차', 'bound'),
    Feature('trust.eps', '믿기', '등급별 오류율의 유한 표본 신뢰상한 추정',
            'Finite-sample upper confidence bounds for per-grade error rates', 'implemented', '1단계 3차', 'bound'),
    Feature('trust.multistep', '믿기(2단계)', '다단계 분석의 오답률 상한 합성',
            'Composing error bounds over multi-step analyses (union bound)', 'implemented', '2단계 5차', 'bound'),
    Feature('trust.guarantee', '믿기(2단계)', '확정 결론 오답률의 통계적 보장(확정 기준 보정)',
            'Statistical guarantee for confirmed conclusions (threshold calibration)', 'planned', '2단계 5차', 'bound'),
    Feature('trust.engine_sql', '믿기', '장부 정의로 SQL을 규칙 조립',
            'Rule-based SQL assembly from ledger definitions', 'partial', '1단계 2차', 'engine',
            'one measure, conditions and group-by over the measure tables plus registered joins'),
    Feature('trust.engine_nl', '믿기', '자연어 질문에 맞는 장부 정의 고르기(모델)',
            'Choosing ledger definitions for a natural-language question (model)', 'planned', '1단계 2차', 'engine, propose'),
    Feature('trust.agent_server', '믿기(2단계)', '데이터 에이전트용 장부 서버(MCP)',
            'Ledger server for data agents (MCP)', 'planned', '2단계 4-5차', 'agent_server'),
    Feature('fix.impact', '고치기', '정의·데이터 변경의 영향 범위 계산',
            'Impact of definition or table changes on stored answers', 'implemented', '1단계 3차', 'revalidate'),
    Feature('fix.plan', '고치기', '오류율 x 영향 순의 선택적 재검증 계획',
            'Selective revalidation plan ranked by error rate x impact within a budget', 'implemented', '1단계 3차', 'revalidate'),
    Feature('fix.simulate', '고치기', '전수 재검증 대비 재현율·비용 평가',
            'Recall and cost against full revalidation', 'implemented', '2단계 5차', 'revalidate'),
    Feature('fix.semevolve', '고치기', '실제 정의 개정 벤치마크(SemEvolve) 수집·판정',
            'Collecting and judging real definition revisions (SemEvolve benchmark)', 'planned', '1단계 3차', '-'),
    Feature('tool.cli', '도구', '명령줄 도구 ledger', 'Command-line tool `ledger`', 'implemented', '-', 'cli'),
]

BY_ID: Dict[str, Feature] = {f.id: f for f in FEATURES}
STATUS_KO = {'implemented': '구현', 'partial': '일부 구현', 'planned': '미구현(계획)'}


def table(lang: str = 'ko') -> str:
    """Markdown table of all features (``lang`` 'ko' or 'en')."""
    if lang == 'en':
        rows = ['| Feature | Area | Status | Plan stage | Module |', '|---|---|---|---|---|']
        rows += [f'| {f.name_en} | {f.area_en} | {f.status} | {f.stage_en} | `{f.module}` |' for f in FEATURES]
    else:
        rows = ['| 기능 | 영역 | 상태 | 계획 단계 | 모듈 |', '|---|---|---|---|---|']
        rows += [f'| {f.name_ko} | {f.area} | {STATUS_KO[f.status]} | {f.stage} | `{f.module}` |' for f in FEATURES]
    return '\n'.join(rows)
