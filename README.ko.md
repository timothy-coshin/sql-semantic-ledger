# sql-semantic-ledger

[English](README.md) | [한국어](README.ko.md)

> **상태: 프리알파 연구 미리보기(0.1.0a1).** 구현된 기능은 테스트를 거쳤지만 API는 아직 바뀔 수 있습니다.
> [기능 표](#기능)에서 *미구현(계획)*으로 표시된 기능은 구현되지 않았으며, API 스텁이 있는 경우 호출하면
> `NotImplementedYet`을 냅니다.

**sql-semantic-ledger**는 **LEDGER**
([*Lifecycle of Evidence-graded Definitions: Generation, Evaluation, Revalidation*](#동작-방식), 증거 등급이 매겨진
정의의 생애주기: 생성·평가·재검증)의 참조 구현입니다. SQL 데이터베이스의 업무 정의(`SUM(orders.amount)` 같은 측정값,
`orders.status = 'paid'` 같은 조건, 조인, 그리고 집계와 그 집계가 적용되는 조건을 묶은 지표)를 각 정의의 근거가 되는
증거와 함께 추가 전용 장부에 기록합니다. 정의는 SQL 사용 이력과 서비스 코드 속 SQL 문자열에서 귀납하고, 증거(독립적인
사용, 데이터 검사, 사람의 인증, 반례 검사)에 따라 E0–E4 등급을 매기며, 그 등급으로 답변을 확정·경고·거부하면서 오답률
상한을 함께 계산합니다. 정의나 데이터가 바뀌면 다시 검증할 답변의 순서를 정합니다. 정의의 신뢰도는 '검증됨' 표시가
아니라 증거로부터 계산됩니다.

## 동작 방식

| 단계 | 코드가 하는 일 | 모듈 |
|---|---|---|
| **생성**(만들기) | sqlglot으로 SQL을 정규화하고(별칭 해석, 조건의 정규형) 집계, 행 단위 조건, 등호 조인, 집계와 조건의 쌍을 정의 후보로 만듭니다. 같은 형태로 서로 다른 과거 질의나 코드 위치 두 곳 이상에서 쓰인 후보만 남깁니다(E1). 데이터베이스에 대한 데이터 검사를 통과하면 E2가 되고, 실패하면 반증되어 E0이 됩니다. 지표 저장소 YAML 파일의 정의도 같은 정규형으로 더할 수 있고(같은 정의는 같은 id를 받아 증거가 합쳐짐), 이름은 같지만 제품마다 내용이 다른 정의는 충돌로 보고합니다. | `parse`, `induce`, `code_sources`, `multisource`, `checks` |
| **평가**(믿기) | 장부의 정의로 SQL을 규칙에 따라 조립하고, 답변에 쓰인 정의들의 등급에 따라 확정·경고·거부를 정하며 아래의 오답률 상한을 계산합니다. 등급별 오류율은 판정한 표본에서 추정할 수 있습니다. | `engine`, `grade`, `bound` |
| **재검증**(고치기) | 저장된 답변이 어떤 정의와 테이블을 썼는지 색인하고, 변경의 영향을 받는 답변을 '가중치 × 쓰인 정의 가운데 하나라도 틀렸을 확률' 순으로 예산 안에서 고릅니다. 이 계획을 영향받는 답변 전체를 검사하는 경우와 비교하는 시뮬레이션도 있습니다. | `revalidate` |

모든 변경(새 정의, 증거, 인증, 개정, 만료)은 장부의 이벤트 기록에 추가됩니다. 정의를 개정하면 새 버전이 시작되고, 그
등급은 새 버전의 증거만으로 계산됩니다.

## 설치

아직 PyPI에 올리지 않았으므로 저장소나 소스 체크아웃에서 설치합니다.

```bash
pip install "sql-semantic-ledger @ git+<repository URL>"          # 핵심 기능(의존성은 sqlglot 하나)
pip install "sql-semantic-ledger[yaml] @ git+<repository URL>"    # + PyYAML: 지표 저장소 YAML 파일 읽기

git clone <repository URL> sql-semantic-ledger && cd sql-semantic-ledger
pip install -e ".[dev]"                                           # + pytest: 테스트 실행
```

체크아웃은 설치하지 않고 `PYTHONPATH=src`로도 실행할 수 있습니다. Python 3.9 이상과 sqlglot 30.x(30.8 이상)가
필요합니다. 정의 id를 sqlglot이 정규화한 SQL로 계산하므로 의존성을 테스트한 범위로 묶어 두었습니다.

## 빠른 시작

먼저 예제 데이터베이스를 만듭니다: `python examples/toy_shop/make_db.py shop.sqlite`. 그다음 저장소 최상위 폴더에서:

```python
import sqlite3
from sql_semantic_ledger.checks import run_checks
from sql_semantic_ledger.code_sources import scan_repository
from sql_semantic_ledger.engine import assemble
from sql_semantic_ledger.grade import Policy, decide
from sql_semantic_ledger.induce import induce, load_history

ledger = induce(load_history('examples/toy_shop/history.sql') + scan_repository('examples/toy_shop/app'))
run_checks(ledger, sqlite3.connect('file:shop.sqlite?mode=ro', uri=True))   # E1 -> E2, 검사 실패 시 E0
paid = ledger.find(name='sum_orders_amount_where_orders_is_test_eq_0_and_orders_status_eq_paid')[0]
ledger.certify(paid.id, by='finance lead')                                  # 사람의 인증: E3
query = assemble(ledger, paid.id, group_by=['customers.region'])            # 장부의 정의로 SQL 조립
decision = decide([ledger.get(i) for i in query.definitions], Policy(target=0.1), delta=0.02,
                  eps={'E0': 1.0, 'E1': 0.6, 'E2': 0.3, 'E3': 0.05, 'E4': 0.02})   # 예시용 오류율
print(decision.verdict.value, round(decision.bound, 2))   # warn 0.37: 함께 쓰인 조인이 E2에 그침
```

`ledger.save('ledger.json')`은 장부를 이벤트 기록과 함께 저장하고, `Ledger.load`가 다시 읽습니다.

## 예제: 장난감 쇼핑몰

[`examples/toy_shop`](examples/toy_shop)에는 가상의 쇼핑몰이 들어 있습니다. `make_db.py`(고객과 주문이 있는 SQLite
데이터베이스), `history.sql`(분석가 질의 17개), `app/orders.js`와 `app/report.py`(API 처리기와 보고서 스크립트 속
SQL), `metrics.yaml`(`paid_revenue`를 서로 다르게 정의한 두 제품), `answers.jsonl`(저장된 답변과 그 답변이 쓴 정의)입니다.
`demo.py`는 Python API로 전체 과정을 실행합니다.

```bash
PYTHONPATH=src python examples/toy_shop/demo.py            # --out DIR을 주면 데이터베이스와 장부를 남깁니다
```

정의 15개를 귀납하고(모두 E1), 데이터 검사 31개를 실행하며(13개 정의가 E2가 되고, 예전 상태값을 쓰는 정의 2개는
반증됨), 결제 매출 지표를 인증하고(E3), 답변 세 개를 판정하고, 데이터 변경과 정의 개정 뒤의 재검증 계획을 세우고,
지표 저장소의 충돌을 보고합니다. 출력 일부:

```text
  Q: Paid revenue by region
     SQL: SELECT customers.region, SUM(orders.amount) AS "sum_orders_amount_where_..." FROM orders JOIN customers ON customers.id = orders.customer_id WHERE orders.is_test = 0 AND orders.status = 'paid' GROUP BY customers.region ORDER BY customers.region
     result: Busan 253.5, Daegu 254, Seoul 484.5
     -> WARN (error bound 0.37)
        ...
        join_customers_orders (E2): machine evidence only; supported by 6 past queries or code sites (...); data checks passed (...); below E3 -> warn
        error bound 0.37 = delta 0.02 + eps(E3) 0.05 + eps(E2) 0.3 > target 0.1
```

## 명령줄 도구

```bash
python examples/toy_shop/make_db.py shop.sqlite
ledger scan-code examples/toy_shop/app -o code.jsonl          # JS/TS/Python 문자열 속 SQL 찾기
ledger induce examples/toy_shop/history.sql --code-root examples/toy_shop/app -o ledger.json
ledger check ledger.json --db shop.sqlite                     # 종료 코드 1: 정의 2개가 반증됨
ledger show ledger.json --min-grade E2 --kind metric
ledger certify ledger.json ae289fba8e7e --by "finance lead" --note "matches the monthly report"
ledger decide ledger.json ae289fba8e7e join_customers_orders \
    --eps E0=1,E1=0.6,E2=0.3,E3=0.05,E4=0.02 --delta 0.02 --target 0.1
ledger plan ledger.json --answers examples/toy_shop/answers.jsonl \
    --changed ae289fba8e7e --changed-table orders --budget 3
ledger features --lang ko
```

| 명령 | 하는 일 |
|---|---|
| `features [--lang en\|ko]` | 아래의 기능 표를 출력합니다. |
| `induce HISTORY [--dialect D] [--min-support 2] [--code-root DIR] [--no-metrics] -o LEDGER.json` | `.sql` 이력(`;`로 구분한 문장)이나 `.jsonl` 파일, 그리고 `--code-root` 아래에서 찾은 SQL로 정의를 귀납합니다. `LEDGER.json`이 있으면 그 장부에 합칩니다. |
| `scan-code ROOT [-o queries.jsonl] [--dialect D]` | 코드에서 찾은 SQL을 JSON 줄 `{"sql", "id", "kind": "code", "ref"}`로 씁니다. `induce`가 이 파일을 다시 읽습니다. |
| `check LEDGER.json --db DB.sqlite` | SQLite 파일(읽기 전용으로 엶)에 데이터 검사를 실행하고 증거로 기록합니다. 장부의 테이블이 하나도 없는 데이터베이스는 거부합니다. |
| `certify LEDGER.json ID --by NAME [--reject] [--note TEXT]` | 사람의 인증(E3)이나 거절을 기록합니다. |
| `show LEDGER.json [--min-grade E1] [--kind metric]` | 정의를 증거가 강한 순으로 보여 줍니다. |
| `decide LEDGER.json ID [ID ...] [--eps ...] [--delta P] [--target P]` | 주어진 정의를 쓰는 답변을 확정·경고·거부하고, `--eps`가 있으면 오답률 상한을 출력합니다. |
| `plan LEDGER.json --answers answers.jsonl [--changed ID ...] [--changed-table T ...] [--budget N] [--eps ...]` | 변경의 영향을 받는 저장된 답변을 재검증 순서로 늘어놓습니다. |

정의는 id 또는 겹치지 않는 이름으로 지정할 수 있습니다. `show`, `check`, `decide`, `plan`은 `--json`을 받습니다(UTF-8,
한글 등 비ASCII 문자를 그대로 유지). `-v`는 진행 상황을 표준 오류로 기록합니다. 종료 코드는 0 성공, 1 `check`에서
실패한 검사가 있음, 2 오류(잘못된 인자, 없거나 잘못된 파일, 모르는 정의)입니다. 명령줄 도구에는 아직 `revise` 명령이
없으므로 개정은 Python의 `Ledger.revise`로 합니다.

## 증거 등급과 답변 규칙

| 등급 | 증거 | 부여하는 곳 |
|---|---|---|
| E0 | 증거 없음(예: 모델의 제안) 또는 반증됨 | 독립적인 사용이 두 곳 미만이거나, 현재 버전의 데이터 검사·반례 검사가 실패함 |
| E1 | 같은 형태의 정의가 서로 다른 과거 질의나 코드 위치 두 곳 이상에서 쓰임 | `induce`(같은 질의를 다시 실행한 것은 한 번으로 셈, 이력 질의와 코드 위치는 서로 다른 출처) |
| E2 | E1이면서 데이터 검사를 통과함 | `checks.run_checks`, `ledger check` |
| E3 | 사람이 인증함 | `Ledger.certify`, `ledger certify` |
| E4 | 인증 뒤 반례 검사도 통과함 | `checks.counterexample_checks` |

등급은 저장하지 않고 정의의 현재 버전에 대한 증거로부터 그때그때 계산합니다. `grade.decide`는 답변에 쓰인 정의들의
등급을 판정으로 바꿉니다.

* **확정(confirm)**: 모든 정의가 사람의 인증을 받았을 때(E3 이상, `Policy.confirm_min`)
* **경고(warn)**: 기계 증거(E1–E2)에만 기대는 정의가 섞였을 때
* **거부(reject)**: 증거가 없는 정의(반증된 정의를 포함한 E0)가 섞였거나, 맞는 정의가 없거나, 유효기간을 벗어난 정의가
  있을 때

등급별 오류율과 `Policy(target=...)`를 주면, 확정 답변이라도 오답률 상한이 목표를 넘으면 경고로 낮춥니다. 판정마다
정의별 근거(등급, 누가 언제 인증했는지, 어떤 과거 질의나 코드 위치가 지지하는지, 어떤 검사가 반증했는지)가 함께 나옵니다.

## 오답률 상한

질문 *q*에 대한 답변이 정의 집합 *D(q)*를 쓸 때:

$$P(\text{the answer to } q \text{ is wrong}) \le \min\Big(1,\ \delta + \sum_{d \in D(q)} \varepsilon\big(e(d)\big)\Big)$$

*e(d)*는 정의 *d*의 증거 등급, *ε(e)*는 등급 *e*인 정의가 틀렸을 확률(E3·E4는 인증의 오류율), *δ*는 쓰인 정의가 모두
맞는데도 답변이 틀릴 확률(맞지 않는 정의를 골랐거나 SQL을 잘못 조립한 경우)입니다. `bound.error_bound`가 이 값을
계산하며, `eps`에 없는 등급은 1로 셈해 상한이 여전히 성립하게 합니다. `bound.estimate_eps`는 등급별로 판정한 표본
`(errors, n)`에서 단측 신뢰상한(정확한 Clopper-Pearson 또는 Wilson)을 구하고, `bound.compose`는 다단계 분석의 단계별
상한을 더합니다(합집합 상한). 상한은 넣은 오류율만큼만 믿을 수 있습니다. 예제의 오류율은 측정값이 아니라 예시입니다.

## 기능

`features.table('ko')`로 생성한 표입니다(`ledger features --lang ko`). '구현'은 동작하며 테스트를 거쳤다는 뜻, '일부
구현'은 제한된 첫 버전, '미구현(계획)'은 구현되지 않았다는 뜻이며 계획된 기능의 API 스텁을 호출하면
`NotImplementedYet`을 냅니다. '계획 단계'는 프로젝트의 5개년 로드맵에서의 위치입니다(1단계 = 1–3차 년도, 2단계 = 4–5차
년도).

| 기능 | 영역 | 상태 | 계획 단계 | 모듈 |
|---|---|---|---|---|
| 업무 정의·증거·인증 기록을 남기는 추가 전용 장부 | 장부 | 구현 | 1단계 | `model` |
| SQL 정규화와 정의 후보 생성(집계·조건·조인·지표) | 만들기 | 구현 | 1단계 1차 | `parse, induce` |
| 서로 다른 질의 2개 이상의 지지 검사(E1) | 만들기 | 구현 | 1단계 1차 | `induce` |
| 실제 데이터에 의한 반증 검사(E2) | 만들기 | 구현 | 1단계 1-2차 | `checks` |
| 인증 뒤 반례 검사(E4) | 믿기 | 일부 구현 | 1단계 2-3차 | `checks` |
| 서비스 코드 속 SQL 문자열에서 정의 귀납 | 만들기(2단계) | 구현 | 2단계 4차 | `code_sources` |
| ORM 질의(예: Prisma, SQLAlchemy, Django)에서 정의 귀납 | 만들기(2단계) | 미구현(계획) | 2단계 4차 | `code_sources` |
| 화면 자바스크립트 계산 로직을 정의로 끌어올리는 정적 분석 | 만들기(2단계) | 미구현(계획) | 2단계 4차 | `code_sources` |
| 지표 저장소(YAML) 정의 읽기와 출처·제품 간 정의 충돌 탐지 | 만들기(2단계) | 일부 구현 | 2단계 4차 | `multisource` |
| 증거 등급에 따른 답변의 확정·경고·거부 | 믿기 | 구현 | 1단계 2차 | `grade` |
| 확정 답변의 오답률 상한 P <= delta + sum eps(e(d)) | 믿기 | 구현 | 1단계 2-3차 | `bound` |
| 등급별 오류율의 유한 표본 신뢰상한 추정 | 믿기 | 구현 | 1단계 3차 | `bound` |
| 다단계 분석의 오답률 상한 합성 | 믿기(2단계) | 구현 | 2단계 5차 | `bound` |
| 확정 결론 오답률의 통계적 보장(확정 기준 보정) | 믿기(2단계) | 미구현(계획) | 2단계 5차 | `bound` |
| 장부 정의로 SQL을 규칙 조립 | 믿기 | 일부 구현 | 1단계 2차 | `engine` |
| 자연어 질문에 맞는 장부 정의 고르기(모델) | 믿기 | 미구현(계획) | 1단계 2차 | `engine, propose` |
| 데이터 에이전트용 장부 서버(MCP) | 믿기(2단계) | 미구현(계획) | 2단계 4-5차 | `agent_server` |
| 정의·데이터 변경의 영향 범위 계산 | 고치기 | 구현 | 1단계 3차 | `revalidate` |
| 오류율 x 영향 순의 선택적 재검증 계획 | 고치기 | 구현 | 1단계 3차 | `revalidate` |
| 전수 재검증 대비 재현율·비용 평가 | 고치기 | 구현 | 2단계 5차 | `revalidate` |
| 실제 정의 개정 벤치마크(SemEvolve) 수집·판정 | 고치기 | 미구현(계획) | 1단계 3차 | `-` |
| 명령줄 도구 ledger | 도구 | 구현 | - | `cli` |

기능 목록의 보충 설명:

* 반례 검사(일부 구현): 데이터베이스에서 위반 행을 세는 질의만 지원하며, 사용자가 보고한 반례는 계획 단계입니다.
* 서비스 코드 속 SQL: 테이블을 읽는 질의로 파싱되는 문자열·템플릿 리터럴(리터럴끼리 이은 것 포함)을 읽습니다. 끼워 넣은
  값은 매개변수로 바꾸고, 실행 중에 질의 구조를 만드는 문자열은 건너뜁니다.
* 지표 저장소(일부 구현): 최소한의 YAML 형식만 읽으며, dbt MetricFlow·metric-hub 전체 형식은 계획 단계입니다.
* SQL 조립(일부 구현): 측정값 하나와 조건, group-by를 측정값의 테이블과 등록된 조인 위에서 조립합니다.
* SemEvolve 벤치마크는 앞으로 모을 자료이고 API가 아니므로 스텁이 없습니다.

## 알려진 한계

* 답변 엔진은 호출하는 쪽이 고른 정의로 SQL을 조립합니다. 자연어 질문에 맞는 정의를 고르는 기능은 계획 단계입니다.
  조립은 내부 조인만 쓰고, 일대다 조인으로 행이 불어나는 문제를 보정하지 않으며, HAVING 조건을 지원하지 않습니다.
* SQL 조각은 검사 질의에 그대로 들어가므로 데이터베이스의 방언으로 쓰여 있어야 하며, 테이블·칼럼 이름도 그 방언으로
  필요한 곳에만 따옴표를 붙여 씁니다(`induce`가 쓰는 대로 `"order items"`). 명령줄의 `check`는 SQLite 파일에서
  동작하고, 다른 데이터베이스는 Python에서 DB-API 연결로 검사할 수 있습니다(`run_checks(ledger, conn, dialect=...)`).
* `.sql` 이력의 문장은 위치에 따른 id(q1, q2, ...)를 받습니다. 여러 이력을 한 장부에 합칠 때는 id가 겹치지 않는 `.jsonl`
  기록을 쓰십시오. id가 같은 서로 다른 질의는 한 번의 사용으로 셉니다(지지를 부풀려 세지는 않으며, 나중 이력에서 다시
  실행된 같은 질의도 한 번으로 셉니다). 코드 위치는 검사한 최상위 폴더에 대한 상대 경로이므로 코드는 매번 같은 폴더에서
  검사하십시오.
* 코드 분석은 문자열 리터럴 속 SQL만 읽습니다. ORM 질의와 화면 자바스크립트 계산은 계획 단계입니다.

## 개발

```bash
pip install -e ".[dev]"
python -m pytest -q          # 예제 데이터에서의 명령줄 도구 테스트와 demo.py 실행 포함
```

## 인용

논문이나 보존용 릴리스가 나오기 전까지의 임시 인용 정보입니다.

```bibtex
@software{kim_sql_semantic_ledger,
  author  = {Kim, Wooyoung},
  title   = {sql-semantic-ledger: reference implementation of LEDGER (Lifecycle of Evidence-graded Definitions: Generation, Evaluation, Revalidation)},
  version = {0.1.0a1},
  year    = {2026},
  url     = {<repository URL>}
}
```

## 라이선스와 저자

Apache License 2.0([LICENSE](LICENSE) 참고). 저자: 김우영(Wooyoung Kim) <timothy@yonsei.ac.kr>.
