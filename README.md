# UMA-ST-2

UMA-ST-2 is a Discord-based event and data management service for Umamusume communities.

## Development status

| Version | Status | Scope |
|---|---|---|
| V1 | MAINTENANCE | Current executable public Discord/MariaDB service |
| V2 | ACTIVE DEVELOPMENT | Architecture and feature rewrite tracked separately |

V1 remains the runnable code in this public repository and is maintained for operational fixes and compatibility. V2 is the active development line; its feature-by-feature progress and the current WIN5 vertical slice are tracked in [ROADMAP.md](ROADMAP.md).

[English](#english) · [日本語](#日本語) · [한국어](#한국어)

![UMA-ST-2 V1 architecture](architecture.svg)

---

# English

## Overview

UMA-ST-2 is a Discord-first community service for managing Circle Match events, Circle Points,
betting, Rating, and WIN5 competitions.

The V1 service is a modular Python application backed by MariaDB and primarily deployed with
Docker Compose. Its main components are:

- a Discord bot built with `discord.py`;
- MariaDB persistent storage;
- SQLAlchemy transaction and persistence boundaries;
- Alembic database migrations;
- XLSX import and export support;
- Docker Compose deployment.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the technical overview. The internal V1 Python package
remains `umacircle_bot` as a compatibility identifier; new public product and CLI identifiers use
`UMA-ST-2` and `uma-st-2`.

## Features

### Account and identity management

- Discord-based account registration requests
- Persona, DiscordAccount, and GameAccount management
- In-game account identification by PID
- Operator approval and rejection workflows
- Existing account and historical-record linking
- Role-ID-based staff authorization

### Circle Match

- Race creation, conditions, and entry management
- Circle Point betting and immutable odds snapshots
- Result submission, review, correction, and confirmation
- GameAccount-scoped Rating calculation
- Persona-owned placement rewards
- Atomic Rating, betting, and placement-reward settlement
- Append-only settlement rollback and Discord publication

### WIN5

- Season and Round lifecycle management
- Normal TOP1, TOP3, and TOP5 predictions
- Special Round winner predictions
- Cancellation and resubmission while a Round is open
- Result entry, scoring, Season standings, and XLSX export

### Circle Point and operations

- Persona-owned Circle Point wallets and append-only transaction history
- Registration grants, betting stakes and payouts, Match rewards, and WIN5 rewards
- Staff grants and adjustments
- Guild, channel, and role configuration
- Migration preflight, XLSX export, and backup directory support

## Requirements

- Docker Engine with Docker Compose
- A Discord application and bot token
- A Discord server where the bot can register slash commands
- The target Discord Guild ID and staff Role IDs
- MariaDB credentials configured through `.env`

Python 3.11 or later is required when running without Docker.

## Quick start

Clone the repository and create the local runtime files:

```bash
git clone https://github.com/h1ghg3n/UMA-ST-2.git
cd UMA-ST-2
cp .env.example .env
mkdir -p secrets exports backups data
```

Edit `.env` and configure at least:

```dotenv
DISCORD_GUILD_ID=your-guild-id
OWNER_ROLE_ID=your-owner-role-id

MYSQL_DATABASE=uma_st2
MYSQL_USER=uma_st2
MYSQL_PASSWORD=replace-this-password
MYSQL_ROOT_PASSWORD=replace-this-root-password

DATABASE_URL=mysql+pymysql://uma_st2:replace-this-password@mariadb:3306/uma_st2?charset=utf8mb4
```

Create `secrets/discord_token` and place only the Discord bot token in that file. Do not put the
token directly in `.env`, source files, Docker image layers, or command history.

Start the service and inspect the bot log:

```bash
docker compose up --build -d
docker compose logs -f bot
```

Docker Compose starts MariaDB, applies the Alembic migration, and then starts the Discord bot.

## Initial Circle Match Rating rules

A fresh database can start without historical replay or backfill. Before settling a Rating-enabled
Circle Match, seed a Rating rule version from a compatible XLSX workbook containing the
`Rate 기준표` worksheet.

Place the workbook at `data/rating-rules.xlsx`, inspect it, and review the reported SHA-256:

```bash
docker compose run --rm bot \
  uma-st-2-seed-rating-rules /app/data/rating-rules.xlsx \
  --source-identifier initial-rating-rules
```

Apply the reviewed workbook explicitly:

```bash
docker compose run --rm bot \
  uma-st-2-seed-rating-rules /app/data/rating-rules.xlsx \
  --source-identifier initial-rating-rules \
  --apply \
  --confirm-checksum <reviewed-sha256>
```

Rating rule versions are immutable after creation.

## Discord commands

Common member commands:

| Command | Purpose |
|---|---|
| `/help` | Show the main commands and registration flow |
| `/account register` | Request GameAccount registration |
| `/account registration-status` | Check a registration request |
| `/account info` | View linked accounts and Circle Points |
| `/match races` | View Circle Matches currently open for betting |
| `/match bet` | Place a Circle Point bet |
| `/win5 info` | View the active WIN5 Season |
| `/win5 rounds` | View open WIN5 Rounds |
| `/win5 submit` | Submit a normal WIN5 prediction |
| `/win5 special-submit` | Submit a Special Round prediction |
| `/win5 submissions` | View submissions and scores |
| `/win5 cancel` | Cancel an open submission |
| `/win5 standings` | View Season standings |

Operator commands are organized under `/staff`, `/staff persona`, `/match staff`, `/win5 staff`,
`/settings`, and `/export`. Export commands include `/export win5 season` and
`/export circle-points`.

Some legacy account-linking commands are disabled by default. Do not enable them without completing
the corresponding operator review and cutover procedure.

## Data, recovery, and security

MariaDB is the live source of truth. The legacy import, replay, reconciliation, and rebuild commands
included in V1 were developed for bounded historical recovery procedures; they are not a general
public migration API.

For a new installation, start with a fresh MariaDB database, let Alembic migrate it to the current
head, configure Discord roles and channels, and seed Rating rules before the first Rating-enabled
Circle Match. Use verified MariaDB backups for recovery.

- Never commit `secrets/discord_token`.
- Use Discord Role IDs for staff authorization.
- Use strong MariaDB passwords and do not expose MariaDB directly to the public network.
- Back up the MariaDB volume before operational maintenance.
- Keep exports and backups outside the container.

## Project scope

This repository contains the V1 Discord and MariaDB service. Web applications, OAuth, OCR, AI/LLM
features, and the V2 database redesign are outside the V1 scope.

## License and third-party rights

The UMA-ST-2 source code is provided under the [MIT License](LICENSE).

Copyright © 2026 h1ghg3n.

Uma Musume: Pretty Derby and related game names, trademarks, and copyrighted materials belong to
Cygames, Inc. and their respective rights holders.

© Cygames, Inc.

UMA-ST-2 is an unofficial community project. It is not affiliated with, sponsored by, or endorsed
by Cygames, Inc. or other respective rights holders. This game-related notice does not apply to the
original source code of this project.

See [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for domain contributions and third-party license
information.

---

# 日本語

## 概要

UMA-ST-2は、ウマ娘コミュニティ向けのDiscordベースのイベント・データ管理サービスです。
Circle Match、Circle Point、ベッティング、Rating、WIN5大会を管理できます。

V1はMariaDBを使用するPython製のモジュラーアプリケーションで、主にDocker Composeで運用します。
主な構成は`discord.py`、MariaDB、SQLAlchemy、Alembic、XLSX入出力です。

技術的な構成は[ARCHITECTURE.md](ARCHITECTURE.md)を参照してください。V1の内部Python package名
`umacircle_bot`は互換性のために維持されます。公開製品名とCLI名には`UMA-ST-2`と`uma-st-2`を使用します。

## 主な機能

### アカウントとIdentity管理

- Discordからのアカウント登録申請
- Persona、DiscordAccount、GameAccountの管理
- ゲーム内PIDによるアカウント識別
- 運営者による登録申請の承認・却下
- 既存アカウントおよび過去記録のリンク
- Discord Role IDによる権限管理

### Circle Match

- Raceの作成、条件設定、Entry管理
- Circle Pointベッティングと変更不可のOdds snapshot
- Resultの提出、確認、修正、確定
- GameAccount単位のRating計算
- Personaに対する着順報酬
- Rating、ベッティング、着順報酬の一括精算
- 追記方式の精算rollbackとDiscordへの結果公開

### WIN5

- SeasonとRoundのlifecycle管理
- TOP1、TOP3、TOP5予想
- Special Roundの1着予想
- 開催中Roundの提出取消・再提出
- Result入力、採点、Seasonランキング、XLSX出力

### Circle Pointと運営機能

- Persona単位のCircle Point walletと追記方式のtransaction履歴
- 登録grant、betting stake・payout、Match報酬、WIN5報酬
- 運営者によるgrant・adjustment
- Guild、channel、role設定
- Migration preflight、XLSX出力、backup directory

## 必要環境と起動

- Docker EngineおよびDocker Compose
- Discord ApplicationとBot Token
- Slash Commandを登録するDiscord Server
- Discord Guild IDと運営Role ID
- `.env`に設定するMariaDB認証情報

Dockerを使用しない場合はPython 3.11以上が必要です。

```bash
git clone https://github.com/h1ghg3n/UMA-ST-2.git
cd UMA-ST-2
cp .env.example .env
mkdir -p secrets exports backups data
```

`.env`にGuild ID、Role ID、MariaDBパスワード、`DATABASE_URL`を設定します。
`secrets/discord_token`にはDiscord Bot Tokenだけを保存してください。Tokenを`.env`、ソースコード、
Docker image layer、command historyに直接記録しないでください。

```bash
docker compose up --build -d
docker compose logs -f bot
```

Docker ComposeはMariaDBを起動し、Alembic migrationを適用してからBotを起動します。

## 初期Ratingルール

新規Databaseの起動に過去データのreplayやbackfillは必要ありません。Ratingを使用するCircle Matchを
精算する前に、`Rate 기준표` worksheetを含む互換XLSXからRating rule versionを登録してください。

```bash
docker compose run --rm bot \
  uma-st-2-seed-rating-rules /app/data/rating-rules.xlsx \
  --source-identifier initial-rating-rules
```

表示されたSHA-256を確認してから明示的に適用します。

```bash
docker compose run --rm bot \
  uma-st-2-seed-rating-rules /app/data/rating-rules.xlsx \
  --source-identifier initial-rating-rules \
  --apply \
  --confirm-checksum <reviewed-sha256>
```

作成済みのRating rule versionは変更されません。

## 主なDiscord command

一般メンバーは`/help`、`/account register`、`/account info`、`/match races`、`/match bet`、
`/win5 info`、`/win5 rounds`、`/win5 submit`、`/win5 special-submit`、`/win5 submissions`、
`/win5 cancel`、`/win5 standings`を使用できます。

運営commandは`/staff`、`/staff persona`、`/match staff`、`/win5 staff`、`/settings`、`/export`に
分かれています。XLSX出力には`/export win5 season`と`/export circle-points`を使用します。

Legacy account linkは初期状態で無効です。専用の確認およびcutover手順を完了せずに有効化しないでください。

## データ、復旧、セキュリティ

MariaDBが稼働中データのsource of truthです。V1に含まれるlegacy import、replay、reconciliation、
rebuild commandは特定の過去データを復旧するためのmaintenance toolであり、汎用migration APIではありません。

新規環境ではfresh MariaDB、Alembic migration、Rating rule seed、検証済みMariaDB backupを使用してください。
Bot Tokenをcommitせず、強力なDB passwordとDiscord Role IDによる権限管理を使用してください。

## プロジェクト範囲

このrepositoryにはV1 Discord・MariaDB serviceが含まれます。Web application、OAuth、OCR、AI/LLM機能、
V2 database redesignはV1の範囲外です。

## ライセンスと第三者の権利

UMA-ST-2のソースコードは[MIT License](LICENSE)で提供されます。

Copyright © 2026 h1ghg3n.

『ウマ娘 プリティーダービー』および関連する名称、商標、著作物の権利は、Cygames, Inc.および
各権利者に帰属します。

© Cygames, Inc.

UMA-ST-2は非公式のコミュニティプロジェクトです。Cygames, Inc.および各権利者との提携、後援、
承認関係はありません。このゲーム関連の権利表示は、本projectが独自に作成したsource codeには適用されません。

ドメイン面での協力と第三者ライセンスの詳細は[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md)および
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)を参照してください。

---

# 한국어

## 개요

UMA-ST-2는 우마무스메 커뮤니티의 이벤트와 데이터를 관리하기 위한 Discord 기반 서비스입니다.
Circle Match, Circle Point, 베팅, Rating 및 WIN5 대회를 지원합니다.

V1은 MariaDB를 사용하는 Python 모듈러 애플리케이션이며 Docker Compose 배포를 기본으로 합니다.
주요 구성 요소는 `discord.py`, MariaDB, SQLAlchemy, Alembic 및 XLSX 입출력입니다.

상세한 기술 구조는 [ARCHITECTURE.md](ARCHITECTURE.md)를 참고하십시오. V1 내부 Python package
`umacircle_bot`은 compatibility identifier로 유지하며, 공개 제품명과 CLI에는 `UMA-ST-2`와
`uma-st-2`를 사용합니다.

## 주요 기능

### 계정 및 Identity 관리

- Discord 계정 등록 요청
- Persona, DiscordAccount, GameAccount 관리
- 인게임 PID 기반 계정 식별
- 운영자의 등록 요청 승인 및 반려
- 기존 계정과 과거 기록 연결
- Discord Role ID 기반 권한 관리

### Circle Match

- Race 생성, 조건 설정 및 Entry 관리
- Circle Point 베팅과 변경 불가능한 Odds snapshot
- Result 제출, 검토, 정정 및 확정
- GameAccount별 Rating 계산
- 소유 Persona에 대한 착순 보상
- Rating, 베팅 및 착순 보상의 원자적 정산
- 추가 기록 방식의 정산 rollback과 Discord 결과 공개

### WIN5

- Season 및 Round lifecycle 관리
- 일반 TOP1, TOP3, TOP5 예측
- Special Round 우승자 예측
- 열린 Round의 제출 취소 및 재제출
- Result 입력, 채점, Season 순위 및 XLSX 내보내기

### Circle Point 및 운영 기능

- Persona 소유 Circle Point wallet과 추가 기록 방식의 transaction 이력
- 등록 grant, 베팅 stake·payout, Match 보상 및 WIN5 보상
- 운영자 grant와 adjustment
- Guild, channel 및 role 설정
- Migration preflight, XLSX 내보내기 및 backup directory

## 요구 사항과 실행 방법

- Docker Engine 및 Docker Compose
- Discord Application과 Bot Token
- Slash Command를 등록할 Discord 서버
- Discord Guild ID와 운영 Role ID
- `.env`에 설정할 MariaDB 인증정보

Docker를 사용하지 않을 경우 Python 3.11 이상이 필요합니다.

```bash
git clone https://github.com/h1ghg3n/UMA-ST-2.git
cd UMA-ST-2
cp .env.example .env
mkdir -p secrets exports backups data
```

`.env`에서 Guild ID, 운영 Role ID, MariaDB 비밀번호 및 `DATABASE_URL`을 설정합니다.
`secrets/discord_token` 파일에는 Discord Bot Token만 저장하십시오. Token을 `.env`, 소스 코드,
Docker image layer 또는 command history에 직접 기록하지 마십시오.

```bash
docker compose up --build -d
docker compose logs -f bot
```

Docker Compose는 MariaDB를 시작하고 Alembic migration을 적용한 뒤 Discord Bot을 시작합니다.

## 초기 Circle Match Rating 규칙

Fresh database를 시작할 때 과거 데이터 replay나 backfill은 필요하지 않습니다. Rating을 사용하는
Circle Match를 정산하기 전에는 `Rate 기준표` worksheet가 포함된 호환 XLSX로 Rating rule version을
등록해야 합니다.

```bash
docker compose run --rm bot \
  uma-st-2-seed-rating-rules /app/data/rating-rules.xlsx \
  --source-identifier initial-rating-rules
```

출력된 SHA-256을 검토한 뒤 명시적으로 적용합니다.

```bash
docker compose run --rm bot \
  uma-st-2-seed-rating-rules /app/data/rating-rules.xlsx \
  --source-identifier initial-rating-rules \
  --apply \
  --confirm-checksum <reviewed-sha256>
```

생성된 Rating rule version은 변경되지 않습니다.

## 주요 Discord command

일반 사용자는 `/help`, `/account register`, `/account registration-status`, `/account info`,
`/match races`, `/match bet`, `/win5 info`, `/win5 rounds`, `/win5 submit`, `/win5 special-submit`,
`/win5 submissions`, `/win5 cancel`, `/win5 standings`를 사용할 수 있습니다.

운영 command는 `/staff`, `/staff persona`, `/match staff`, `/win5 staff`, `/settings`, `/export`로
구분됩니다. XLSX 내보내기에는 `/export win5 season`과 `/export circle-points`를 사용합니다.

Legacy account-link 기능은 기본적으로 비활성화되어 있습니다. 해당 운영 검수 및 cutover 절차를
완료하지 않고 활성화해서는 안 됩니다.

## 데이터, 복구 및 보안

MariaDB가 실제 운영 데이터의 source of truth입니다. V1에 포함된 legacy import, replay,
reconciliation 및 rebuild command는 특정 과거 데이터 복구용 maintenance tool이며 범용 migration
API가 아닙니다.

신규 설치에서는 fresh MariaDB, Alembic migration, Rating rule seed와 검증된 MariaDB backup을
사용하십시오. Bot Token을 commit하지 말고, 강력한 DB password와 Discord Role ID 기반 권한 관리를
적용하십시오.

## 프로젝트 범위

이 repository에는 V1 Discord 및 MariaDB service가 포함됩니다. Web application, OAuth, OCR,
AI/LLM 기능과 V2 database redesign은 V1 범위에 포함되지 않습니다.

## 라이선스 및 제3자 권리

UMA-ST-2의 소스 코드는 [MIT License](LICENSE)로 배포됩니다.

Copyright © 2026 h1ghg3n.

『우마무스메 프리티 더비』 및 관련 게임 명칭, 상표와 저작물의 권리는 Cygames, Inc. 및 각 권리자에게
있습니다.

© Cygames, Inc.

UMA-ST-2는 비공식 커뮤니티 프로젝트입니다. Cygames, Inc. 또는 다른 권리자와 제휴하거나 후원·승인받은
프로젝트가 아닙니다. 이 게임 관련 권리 고지는 프로젝트가 독자적으로 작성한 source code에는 적용되지
않습니다.

도메인 기여와 제3자 라이선스에 대한 자세한 내용은 [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md)와
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 참고하십시오.
