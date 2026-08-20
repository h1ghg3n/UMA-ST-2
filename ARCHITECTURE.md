# UmaCircle V1 Architecture

> Scope: public `maintenance/v1` architecture only.
>
> This document intentionally excludes the V2 Web/OCR/Model Router/Resource Router architecture.

![UmaCircle V1 architecture](architecture.svg)

## 1. Overview

UmaCircle V1 is a Discord-first modular monolith backed by MariaDB.

The main runtime path is:

```text
Discord
  -> Discord command / adapter layer
  -> application command or query boundary
  -> domain/service logic
  -> SQLAlchemy persistence
  -> MariaDB
```

CLI maintenance tools use the same service and persistence code where practical, but import,
export, migration, rebuild, replay, and cutover tooling are not part of the normal Discord
request path.

## 2. Runtime components

### Discord runtime

`umacircle_bot.bot` is the process entry point for the normal V1 service.

At startup it:

1. loads and validates runtime settings;
2. verifies the target database with the runtime preflight;
3. configures the process-scoped SQLAlchemy Engine and connection pool;
4. creates and starts the Discord bot;
5. disposes the Engine during process shutdown.

The Discord layer exposes the main user and operator surfaces:

- account registration and identity operations;
- Circle Match lookup, betting, result, settlement, and staff controls;
- WIN5 participation and staff controls;
- guild settings;
- Point/account administration;
- XLSX export commands.

Discord handlers should not own SQLAlchemy `Session`, transaction commit/rollback, or row-lock
semantics directly.

### Application boundary

V1 uses explicit application command/query runners.

- **Command**: owns one state-changing transaction boundary and commits on success.
- **Query**: is read-only, rejects ORM/SQL writes, and explicitly rolls back the read transaction.
- **Failure**: rolls back the operation.
- **Completion**: closes the operation-scoped Session.

This keeps transaction lifecycle out of Discord handlers even though V1 is not yet the fully
recomposed V2 architecture.

### Engine and Session lifecycle

The SQLAlchemy Engine and connection pool are process scoped.

```text
Bot process
  -> one Engine / connection pool
      -> operation A: Session A
      -> operation B: Session B
      -> operation C: Session C
```

A running process cannot be rebound to another database URL. A concrete Session is created per
application operation and closed after that operation.

## 3. Code organization

The V1 tree is transitional rather than a perfectly clean layered architecture.

Important areas include:

```text
src/umacircle_bot/
├─ bot.py                  # Discord process entry point
├─ config.py               # runtime configuration
├─ runtime_preflight.py    # database/runtime startup checks
├─ db/                     # SQLAlchemy models and session lifecycle
├─ adapters/discord/       # newer Discord adapter code
├─ domain/                 # extracted business rules / domain logic
├─ services/               # application services, persistence orchestration, legacy/rebuild logic
├─ sheets/                 # XLSX-oriented parsing / workbook support
├─ scripts/                # CLI import/export/rebuild/migration/preflight tools
├─ discord_commands.py     # large legacy Discord command surface
├─ discord_delivery.py     # Discord publication/delivery
└─ discord_channel_provisioning.py
```

V1 contains both newer layered code and older compatibility/legacy code. The maintenance branch
preserves that mixed structure for stability rather than continuing a large internal rewrite.

## 4. Persistence

MariaDB is the authoritative runtime datastore.

SQLAlchemy is used for ORM and transaction handling, with Alembic for the V1 schema history.

Major persisted concepts include:

- Persona and Discord/Game accounts;
- Circle Point balances and ledger/history;
- Circle Match races, entries, results, bets, settlement, and Rating;
- WIN5 seasons, rounds, submissions, results, and scoring;
- guild settings and Discord publication state;
- import/rebuild/reconciliation state required by the V1 maintenance model.

The normal service must not treat XLSX files as the authoritative live database.

## 5. Import and export boundary

V1 includes multiple CLI and Discord-facing import/export tools.

Conceptually:

```text
XLSX / JSON / legacy source
  -> parser / compatibility mapping
  -> application/service validation
  -> MariaDB

MariaDB
  -> application/service query
  -> XLSX / JSON export
```

Import, backfill, migration, rebuild, and cutover tools are operational tooling and are kept
separate from the normal Discord runtime path.

## 6. Deployment

The public maintenance deployment uses Docker Compose.

```text
Docker Compose
├─ bot
│   └─ UmaCircle V1 runtime
├─ migrate
│   └─ safe migration command
└─ mariadb
    └─ MariaDB 11.4.x
```

The bot waits for MariaDB health and migration completion before normal startup.

Persistent database data is stored in a Docker volume. Export, backup, and application data
directories are mounted separately where configured.

## 7. Legacy replay and rebuild tooling

V1 contains substantial replay, rebuild, backfill, and historical reconciliation code. These tools
exist because V1 accumulated several data-model and migration generations during development.

They should **not** be interpreted as a simple, general-purpose recovery API.

Important limitations:

- some older replay/rebuild lanes were superseded by later V1 recovery decisions;
- some replay paths are preserved as historical implementation evidence rather than final release
  authority;
- the bounded current WIN5 replay was validated as a disposable rehearsal, not as a generic
  production cutover mechanism;
- several legacy compatibility paths depend on specific manifests, historical assumptions, frozen
  XLSX inputs, or reviewed identity mappings;
- production rollback is based on a verified database snapshot rather than assuming that the full
  historical Alembic downgrade/replay chain is a reliable recovery mechanism.

For public V1 use, these commands should be considered **maintenance/recovery tooling with narrow
preconditions**. They may be difficult or inappropriate to use outside the historical environment
for which they were created.

Normal users should prefer:

```text
normal runtime
  -> MariaDB as source of truth
  -> verified backup/restore for recovery
  -> documented import/rebuild procedures only when their stated prerequisites are satisfied
```

## 8. Architectural constraints

The V1 maintenance architecture follows these practical rules:

1. Discord handlers do not own ORM Sessions or transaction commit/rollback.
2. The application command/query boundary owns operation-level transaction behavior.
3. The SQLAlchemy Engine/pool is process scoped; Sessions are operation scoped.
4. Discord publication happens after the relevant database transaction is committed.
5. Import, migration, replay, backfill, and cutover tooling remain outside the normal Discord
   request path.
6. MariaDB is the live source of truth.
7. Legacy replay/rebuild tools are maintenance-only and must not be treated as a generic supported
   runtime interface.
8. V1 is maintained for stability; the larger architecture redesign belongs to V2 and is outside
   this document.

## 9. What is intentionally not shown

The following belong to later V2 work and are intentionally excluded from this V1 diagram:

- Web application runtime;
- OCR workflows;
- Model Router integration;
- Resource Router / lease admission;
- V2 composition root and package layout;
- V2 canonical database redesign.
