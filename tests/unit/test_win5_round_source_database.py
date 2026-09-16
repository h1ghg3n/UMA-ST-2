"""SQLAlchemy WIN5 Round provenance boundary tests."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from uma_st2.domain.win5 import Win5RoundSourceKind
from uma_st2.infrastructure.database import Base
from uma_st2.infrastructure.database.orm import Win5RoundORM, Win5SeasonORM
from uma_st2.infrastructure.database.win5_publication import SqlAlchemyWin5PublicationStore
from uma_st2.infrastructure.database.win5_round_lifecycle import SqlAlchemyWin5RoundLifecycleRepository

NOW = datetime(2026, 8, 30, 0, 0)


def test_imported_round_provenance_reaches_command_target_and_blocks_publication_source() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                Win5SeasonORM.__table__.insert().values(
                    id=7,
                    name="2026 하반기",
                    status="active",
                    active_marker=True,
                    starts_at=None,
                    ends_at=None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            connection.execute(
                Win5RoundORM.__table__.insert().values(
                    id=11,
                    season_id=7,
                    source_kind="imported_v1",
                    type="normal",
                    status="scored",
                    name="Imported WIN5 History",
                    opens_at=NOW,
                    closes_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )

        with Session(engine) as session, session.begin():
            target = SqlAlchemyWin5RoundLifecycleRepository(session).lock_round(round_id=11)
            assert target is not None
            assert target.source_kind is Win5RoundSourceKind.IMPORTED_V1

        with Session(engine) as session:
            store = SqlAlchemyWin5PublicationStore(session)
            with pytest.raises(ValueError, match="Round or Season is missing"):
                store.load_scored_round_source(
                    guild_id="987654321",
                    round_id=11,
                    persona_ids=(),
                )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
