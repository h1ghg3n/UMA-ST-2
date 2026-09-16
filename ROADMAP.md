# UMA-ST-2 Roadmap

## 현재 상태

Public `main`은 V2 runtime을 기준으로 유지합니다. 마지막 V1 public snapshot은 `v1` branch에 보존합니다.

현재 package version은 `0.1.3`입니다. Pre-1.0 단계이므로 minor release 사이에서 command와 schema가 변경될 수
있습니다.

## 구현된 범위

| 영역 | 상태 |
|---|---|
| V2 Domain/Application/Infrastructure 구조 | 구현됨 |
| Fresh MariaDB schema와 Alembic migration | 구현됨 |
| Persona, DiscordAccount, GameAccount | 구현됨 |
| Circle Point | 구현됨 |
| Circle Match Entry와 Betting | 구현됨 |
| Result, Rating, Settlement와 rollback | 구현됨 |
| WIN5 일반·특별 Round | 구현됨 |
| Discord publication과 Settings | 구현됨 |
| XLSX export | 구현됨 |
| Docker Compose deployment | 구현됨 |

## 다음 범위

1. Public fresh-install 절차와 sample input을 보완합니다.
2. Discord View lifecycle과 운영 flow의 regression test를 지속적으로 보강합니다.
3. Match와 WIN5 reporting을 확장합니다.
4. Web/OAuth read-only service를 별도 단계에서 검토합니다.
5. OCR과 AI/LLM 기능은 Web/OAuth 기반이 안정된 뒤 검토합니다.

V1→V2 transition, 특정 community data migration과 실제 production cutover는 public roadmap에 포함하지
않습니다.
