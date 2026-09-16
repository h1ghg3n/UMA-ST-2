# UMA-ST-2 V2 Architecture

## 범위

이 문서는 public V2 snapshot의 실행 구조만 설명합니다. Private V1→V2 transition, replay, cutover와
실제 운영환경 구성은 다루지 않습니다.

## 구조

UMA-ST-2는 layer-first modular monolith입니다.

```text
Discord / CLI -> Adapters -> Application -> Domain
                               ^
                               |
                        Infrastructure
                          /          \
                     MariaDB     Discord API
```

Dependency 방향은 `adapter -> application -> domain`입니다. Infrastructure는 Application이 정의한
outbound boundary를 구현합니다. 구체적인 dependency 조립은 Composition Root에서만 수행합니다.

## Layer 역할

### Domain

Domain은 외부 framework에 의존하지 않는 business meaning과 계산을 소유합니다.

- Persona와 GameAccount identity
- Circle Point value와 balance rule
- Match, Betting, Rating과 Settlement rule
- WIN5 lifecycle과 scoring
- Publication state

Domain은 Discord SDK, SQLAlchemy, process configuration을 import하지 않습니다.

### Application

Application은 use case, authorization decision, transaction 의미와 outbound port를 소유합니다.

- state-changing command orchestration
- read-only query orchestration
- Unit of Work 경계
- settlement와 rollback 순서
- publication intent 생성

Application은 concrete SQLAlchemy `Session`이나 Discord interaction object를 받지 않습니다.

### Adapters

Adapters는 외부 요청과 Application command/query 사이를 변환합니다.

- Discord slash command, View와 Modal
- CLI argument와 출력
- Application result의 사용자 표시

Adapter는 database commit, rollback과 ORM mutation을 직접 수행하지 않습니다.

### Infrastructure

Infrastructure는 Application port의 concrete implementation을 제공합니다.

- SQLAlchemy ORM과 repository
- MariaDB Engine, Session과 Unit of Work
- Discord publication delivery
- XLSX rendering
- Master-data와 Rating-rule input

## Runtime lifecycle

하나의 실행 runtime이 SQLAlchemy Engine과 connection pool을 소유합니다. 각 Application operation은 별도
Session과 짧은 Unit of Work를 사용합니다.

```text
Process
  -> Engine / Pool
      -> Operation A / Session A
      -> Operation B / Session B
```

성공한 mutation은 Unit of Work가 commit하고, 실패한 mutation은 rollback합니다. 외부 Discord 전송을 기다리는
동안 MariaDB transaction이나 row lock을 유지하지 않습니다.

## 주요 data ownership

- Persona: Discord access, Circle Point와 WIN5 owner
- DiscordAccount: Discord actor identity
- GameAccount: Circle Match Entry, Result와 Rating provenance
- Match: 조건, Entry, Betting, Result와 Settlement lifecycle
- WIN5 Season/Round: WIN5 submission, result와 score lifecycle

Rating은 GameAccount별로 관리하며 Circle Point는 Persona가 소유합니다.

## Publication

공개 메시지의 의미는 Application에서 결정하고, Discord 전송은 database commit 이후 수행합니다. Delivery
failure는 이미 확정된 canonical business state를 되돌리지 않습니다.

## Database와 migration

MariaDB가 runtime source of truth입니다. Public V2는 하나의 fresh Alembic baseline과 이후 V2 migration만
제공합니다. V1 schema를 public V2 schema로 변환하는 migration은 포함하지 않습니다.

## Deployment

기본 Compose package는 다음 service를 사용합니다.

```text
Docker Compose
├─ mariadb
├─ migrate
└─ bot
```

Credential은 host의 secret file에서 `/run/secrets/`로 mount합니다. Bot image에는 credential, 운영 데이터,
test와 private document를 포함하지 않습니다.

## 제외된 범위

- V1→V2 transition과 historical replay
- 실제 community data와 protected manifest
- Jetson-specific cutover procedure
- Web/OAuth adapter
- OCR과 AI/LLM execution
- generic event bus와 microservice 분할

이 기능들은 현재 public runtime의 정상 실행에 필요하지 않습니다.
