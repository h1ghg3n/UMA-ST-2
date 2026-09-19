# UMA-ST-2

UMA-ST-2는 우마무스메 커뮤니티의 이벤트와 운영 데이터를 관리하는 Discord 기반 서비스입니다.

[한국어](#한국어) · [English](#english) · [日本語](#日本語)

현재 공개 버전은 V2 architecture를 사용하는 `v2.0.0 Beta 1`입니다. Python package version은
`2.0.0b1`입니다. 기존 V1은 `v1` branch와
`v0.1.0` tag에서 확인할 수 있습니다.

---

# 한국어

## 개요

UMA-ST-2는 Circle Match, Circle Point, Betting, Rating과 WIN5 운영을 지원합니다. Python 3.13,
`discord.py`, SQLAlchemy, Alembic과 MariaDB를 사용하며 Docker Compose 배포를 기본으로 합니다.

V2는 다음 dependency 방향을 따르는 modular monolith입니다.

```text
Discord / CLI -> Adapters -> Application -> Domain
                               ^
                               |
                        Infrastructure
                  (implements outbound ports)
                               |
                     MariaDB / Discord API
```

상세 구조는 [ARCHITECTURE.md](ARCHITECTURE.md)를 참고하십시오.

## 주요 기능

- Persona, DiscordAccount와 GameAccount를 분리한 identity 관리
- Circle Point 잔액과 transaction 이력
- Circle Match 생성, 조건과 Entry 설정, Betting 시작·종료
- Odds 조회와 Discord 공지
- Result 검토·확정, GameAccount별 Rating 계산과 Settlement
- Settlement rollback과 후속 publication
- WIN5 Season·Round lifecycle, 일반·특별 Round 제출과 scoring
- Match, WIN5와 Circle Point XLSX export
- Guild channel, role, timezone과 공지 설정

## 공개 범위

이 repository는 fresh MariaDB에서 시작하는 V2 runtime과 test를 제공합니다. 특정 운영 DB를 이전하기 위한
V1→V2 replay, protected manifest, cutover evidence와 실제 운영 데이터는 포함하지 않습니다.

Web, OAuth, OCR과 AI/LLM 기능도 현재 공개 범위에 포함되지 않습니다.

## 요구 사항

- Docker Engine과 Docker Compose
- Discord Application과 Bot Token
- Slash Command를 등록할 Discord server
- Discord Guild ID
- MariaDB credential을 저장할 local secret file

Docker 없이 실행하려면 Python `3.13.x`가 필요합니다.

## Fresh installation

Repository를 clone하고 환경 파일과 secret directory를 준비하십시오.

```bash
git clone https://github.com/h1ghg3n/UMA-ST-2.git
cd UMA-ST-2
cp .env.example .env
mkdir -p secrets
```

다음 파일에는 해당 credential 값만 저장하십시오.

```text
secrets/discord_token
secrets/mariadb_app_password
secrets/mariadb_root_password
```

`.env`에서 `DISCORD_GUILD_ID`와 필요한 runtime 값을 설정한 뒤 서비스를 시작하십시오.

```bash
docker compose --profile runtime up --build -d
docker compose logs -f bot
```

Compose는 MariaDB를 시작하고 Alembic `head`를 적용한 다음 bot을 실행합니다. Discord Token과 DB password는
container environment 값으로 전달하지 않고 Docker secret file로 mount합니다.

## 초기 데이터

Fresh schema에는 community별 account, master data와 Rating rule이 들어 있지 않습니다. 운영 전에 검토한
master-data JSON과 Rating-rule XLSX를 별도로 준비해 등록하십시오. 실제 운영 source 파일은 이 repository에
포함하지 않습니다.

다음은 Docker Compose를 사용하는 예시입니다.

```bash
docker compose --profile runtime run --rm \
  --volume ./master-data.reviewed.json:/tmp/master-data.reviewed.json:ro \
  bot uma-st-2-seed-master-data \
  --manifest /tmp/master-data.reviewed.json
```

```bash
docker compose --profile runtime run --rm \
  --volume ./rating-rules.xlsx:/tmp/rating-rules.xlsx:ro \
  bot uma-st-2-seed-rating-rules \
  --workbook /tmp/rating-rules.xlsx \
  --source-identifier initial-rating-rules
```

두 command는 입력을 검증한 뒤 대상 DB에 바로 반영합니다. 원본 파일과 대상 DB를 먼저 확인하고, 시험 실행은 disposable DB에서 수행하십시오. 성공 출력의 checksum과 receipt를 보관하십시오.

## Branch

- `main`: 현재 V2 public runtime
- `v1`: 마지막 V1 public maintenance snapshot
- `v0.1.0`: 최초 V1 public release

## 보안

Token, password, 실제 Discord ID, PID, DB dump, XLSX와 운영 manifest를 commit하지 마십시오. 공개 repository의
예시는 실제 계정정보가 아닌 임의 값만 사용합니다.

## 라이선스와 제3자 권리

프로젝트가 작성한 source code는 [MIT License](LICENSE)에 따라 제공됩니다. 저작권과 domain 기여에 대한
표기는 [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md), dependency license는
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 참고하십시오.

『우마무스메 프리티 더비』와 관련 명칭, 상표 및 저작물의 권리는 Cygames, Inc.와 각 권리자에게 있습니다.
UMA-ST-2는 비공식 community project이며 Cygames, Inc. 또는 다른 권리자와 제휴하거나 후원·승인받지
않았습니다.

---

# English

## Overview

The current public release is `v2.0.0 Beta 1`, with Python package version `2.0.0b1`.

UMA-ST-2 is a Discord-based service for operating Umamusume community events. It supports Circle Match,
Circle Point, betting, GameAccount-scoped Rating, and WIN5 workflows.

The public snapshot uses Python 3.13, `discord.py`, SQLAlchemy, Alembic, MariaDB, and Docker Compose. Its
dependency direction is `adapter -> application -> domain`, with concrete database and Discord integrations
implemented by infrastructure modules. See [ARCHITECTURE.md](ARCHITECTURE.md) for the technical boundary.

## Public scope

This repository provides the V2 runtime, fresh Alembic schema, and public test suite. It does not include private
V1-to-V2 replay tools, protected manifests, cutover evidence, credentials, member data, or operational workbooks.
Web, OAuth, OCR, and AI/LLM features are not included in this snapshot.

## Quick start

```bash
git clone https://github.com/h1ghg3n/UMA-ST-2.git
cd UMA-ST-2
cp .env.example .env
mkdir -p secrets
docker compose --profile runtime up --build -d
```

Store only the corresponding secret value in each of these files:

```text
secrets/discord_token
secrets/mariadb_app_password
secrets/mariadb_root_password
```

Set `DISCORD_GUILD_ID` and the required runtime values in `.env`. A fresh database also requires reviewed master
data and Rating rules before normal Match operation.

## Branches

- `main`: current V2 public runtime
- `v1`: final V1 public maintenance snapshot
- `v0.1.0`: initial V1 public release

## License and third-party rights

Project-owned source code is provided under the [MIT License](LICENSE). See
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for authorship,
domain contributions, and dependency licenses.

Umamusume-related names, trademarks, and copyrighted materials belong to Cygames, Inc. and their respective
rights holders. UMA-ST-2 is an unofficial community project and is not affiliated with, sponsored by, or endorsed
by Cygames, Inc. or other rights holders.

---

# 日本語

## 概要

現在の公開バージョンは`v2.0.0 Beta 1`です。Python package versionは`2.0.0b1`です。

UMA-ST-2は、ウマ娘コミュニティのイベント運営を支援するDiscordベースのサービスです。Circle Match、
Circle Point、Betting、GameAccount単位のRating、WIN5を扱います。

公開snapshotはPython 3.13、`discord.py`、SQLAlchemy、Alembic、MariaDB、Docker Composeを使用します。
依存方向は`adapter -> application -> domain`です。技術的な境界は
[ARCHITECTURE.md](ARCHITECTURE.md)を参照してください。

## 公開範囲

このrepositoryにはV2 runtime、fresh Alembic schema、公開testを含めています。非公開のV1→V2 replay、
protected manifest、cutover evidence、credential、メンバー情報、運営用workbookは含めていません。
Web、OAuth、OCR、AI/LLM機能も現在の公開範囲外です。

## 起動

```bash
git clone https://github.com/h1ghg3n/UMA-ST-2.git
cd UMA-ST-2
cp .env.example .env
mkdir -p secrets
docker compose --profile runtime up --build -d
```

`secrets/discord_token`、`secrets/mariadb_app_password`、`secrets/mariadb_root_password`には、対応する
credential値だけを保存してください。`.env`には`DISCORD_GUILD_ID`と必要なruntime設定を記入します。
Fresh databaseでMatchを運用する前に、確認済みのmaster dataとRating ruleを別途登録してください。

## Branch

- `main`: 現在のV2 public runtime
- `v1`: 最終V1 public maintenance snapshot
- `v0.1.0`: 最初のV1 public release

## Licenseと第三者の権利

プロジェクトが作成したsource codeは[MIT License](LICENSE)で提供します。著作者、domain contribution、
dependency licenseは[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md)と
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)を参照してください。

『ウマ娘 プリティーダービー』に関連する名称、商標、著作物の権利は、Cygames, Inc.および各権利者に
帰属します。UMA-ST-2は非公式のcommunity projectであり、Cygames, Inc.または各権利者との提携、後援、
承認関係はありません。
