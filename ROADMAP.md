# UMA-ST-2 Roadmap

## Version Status

UMA-ST-2는 현재 V1 유지보수와 V2 재구성을 분리해 진행합니다.

상태 표기는 다음 의미를 사용합니다.

- `DONE`: 구현과 현재 기준 검증 완료
- `CURRENT`: 현재 진행 위치
- `IN PROGRESS`: 현재 개발 중인 feature / vertical slice
- `FOUNDATION`: 기반은 구현됐지만 사용자 기능 전체는 아직 완성되지 않음
- `PLANNED`: 다음 범위로 확정됐지만 아직 구현하지 않음
- `MAINTENANCE`: 신규 구조 확장보다 유지보수가 목적

### V1 — Maintenance

V1은 현재 운영 기능의 유지보수 단계입니다.

- bug fix와 필요한 운영 수정
- 기존 데이터와 복구 절차 유지
- 신규 구조와 기능 개발은 V2를 기준으로 진행

현재 public repository의 실행 가능한 Discord/MariaDB 서비스는 V1입니다.

### V2 — Active Development

V2는 기존 기능을 단순 이식하는 방식이 아니라 `adapter -> application -> domain` dependency 방향과 새로운 canonical DB를 기준으로 재구성하는 rewrite입니다.

V2 개발 상태는 아래 roadmap으로 추적하며, public V1 runtime과 혼동하지 않습니다.

## Feature Progress

| Feature | Status | Current scope |
|---|---|---|
| Architecture / Canonical DB | DONE | layer boundary, SQLAlchemy ORM, fresh Alembic baseline, UoW |
| Identity / Circle Point | FOUNDATION | domain / persistence 기반 |
| WIN5 | IN PROGRESS | 현재 주요 vertical slice |
| Circle Match / Betting / Rating | FOUNDATION | domain / ORM 중심; application/runtime 후속 |
| Publication | PLANNED | 일부 contract 확정, runtime 미구현 |
| Runtime / Deployment | PLANNED | V2 executable startup/config/deployment 후속 |
| Web / OAuth / OCR | PLANNED | 후속 adapter / runtime 범위 |

## Current Feature — WIN5

현재 V2에서 가장 앞선 기능은 WIN5이며, 이 feature만 vertical slice 수준까지 펼칩니다.

| Vertical Slice | Status |
|---|---|
| Member info / rounds / submissions | DONE |
| Normal / Special submission editing | DONE |
| Versioned cancellation / history | DONE |
| Normal result / scoring | DONE |
| Special result / scoring | DONE |
| Round lifecycle / setup | IN PROGRESS |
| Publication / runtime integration | PLANNED |

## WIN5 — Round lifecycle / setup

현재 진행 중인 vertical slice의 Phase 진행도는 이렇습니다.

| Phase | Scope | Status |
|---|---|---|
| Phase 1 | Round open / close | DONE |
| Phase 2 | Normal / Special Round creation | DONE |
| Phase 3 | Normal Round initial Entry creation | DONE |
| Phase 4 | Guarded setup Round delete / recreate | CURRENT |
| Phase 5 | Season activation / close / cancel guards | PLANNED |
| Phase 6 | Special Race void persistence / lifecycle | PLANNED |

### Current — Phase 4

잘못 구성된 setup Round를 일부 row의 임의 수정으로 복구하지 않고, 검증된 전체 Round graph delete / recreate 흐름으로 처리하는 단계입니다.

현재 기준으로 다음 사항을 보존합니다.

- setup 상태에서만 허용되는 명시적 복구 경계
- 기존 downstream fact가 있는 graph를 무심코 제거하지 않는 fail-closed 처리
- Round / Race / Entry 관계를 하나의 운영 단위로 재검증
- 삭제·재생성 자체의 audit / transaction 의미를 명확히 유지

## Next

현재 WIN5 lifecycle/setup 이후의 가까운 순서는 다음과 같습니다.

1. Season activation / close / cancel guard 확정 및 구현
2. Special Race void persistence와 lifecycle 처리
3. post-commit publication과 V2 runtime integration

---

이 roadmap은 구현 상태를 설명하기 위한 문서입니다. `FOUNDATION`과 `PLANNED` 항목은 사용자-facing 기능이 완료됐다는 뜻이 아니며, 현재 public V1 runtime과 V2 개발 상태를 구분해 읽어야 합니다.
