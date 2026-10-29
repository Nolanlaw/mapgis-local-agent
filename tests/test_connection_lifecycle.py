import sqlite3
import pytest
from mapgis_agent.core import Bridge


def test_transaction_context_releases_connection_and_preserves_commits(tmp_path):
    bridge=Bridge(tmp_path)
    with bridge.connect() as db:
        db.execute('CREATE TABLE lifecycle (value INTEGER)')
        db.execute('INSERT INTO lifecycle VALUES (17)')
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):
        db.execute('SELECT value FROM lifecycle')
    with bridge.connect() as second:
        assert second.execute('SELECT value FROM lifecycle').fetchone()[0]==17
    with pytest.raises(ValueError):
        with bridge.connect() as failed:
            failed.execute('INSERT INTO lifecycle VALUES (99)')
            raise ValueError('rollback')
    with bridge.connect() as final:
        assert final.execute('SELECT value FROM lifecycle').fetchall()[0][0]==17
        assert final.execute('SELECT COUNT(*) FROM lifecycle').fetchone()[0]==1
