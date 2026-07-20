from contextlib import nullcontext

import pytest

from jobs.migrate_viewing_history_record_ids import apply_record_id_migration


class FakeConnection:
    def __init__(self):
        self.updates = []

    def transaction(self):
        return nullcontext()

    def execute(self, sql, parameters):
        self.updates.append(parameters)


class FakeSheets:
    def __init__(self):
        self.backfills = []

    def backfill_record_id(self, sheet_name, row_number, history_id):
        self.backfills.append((sheet_name, row_number, history_id))


def relationship(status, history_id, row):
    return {
        "status": status,
        "local": {"id": history_id, "sheet_name": "2026", "row_number": row},
        "sheet_rows": [{"sheet_name": "2026", "row_number": row}],
    }


def test_apply_backfills_only_reconciled_rows_and_updates_relinked_locator():
    report = {
        "relationship_counts": {},
        "relationships": [
            relationship("matched", "id-1", 2),
            relationship("relinked", "id-2", 3),
            relationship("sheet_only", None, 4),
        ],
    }
    connection, sheets, checkpoints = FakeConnection(), FakeSheets(), []

    result = apply_record_id_migration(
        report,
        connection,
        sheets,
        on_complete=lambda ids: checkpoints.append(set(ids)),
    )

    assert sheets.backfills == [("2026", 2, "id-1"), ("2026", 3, "id-2")]
    assert connection.updates[0][0:2] == ("2026", 3)
    assert connection.updates[0][3] == "id-2"
    assert checkpoints == [{"id-1"}, {"id-1", "id-2"}]
    assert result["eligible_count"] == 2


def test_apply_is_resumable_and_refuses_unresolved_relationships():
    report = {
        "relationship_counts": {},
        "relationships": [relationship("matched", "id-1", 2), relationship("matched", "id-2", 3)],
    }
    connection, sheets = FakeConnection(), FakeSheets()

    apply_record_id_migration(report, connection, sheets, {"id-1"})

    assert sheets.backfills == [("2026", 3, "id-2")]
    report["relationship_counts"]["ambiguous"] = 1
    with pytest.raises(ValueError, match="unresolved"):
        apply_record_id_migration(report, connection, sheets)
