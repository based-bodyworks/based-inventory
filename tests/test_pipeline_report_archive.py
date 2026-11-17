from unittest.mock import Mock

import pytest

from based_inventory.pipeline.report_archive import DriveArchive
from based_inventory.pipeline.store import Store
from tests.test_pipeline_models import verified


def test_archive_reuses_reserved_id_and_verifies_saved_bytes_and_destination():
    store = Mock(spec=Store)
    store.plan_once.return_value = {"file_id": "reserved-file"}
    store.receipts.return_value = {}
    google = Mock()
    metadata = {
        "id": "reserved-file",
        "driveId": "0ANDGo4RyWVY2Uk9PVA",
        "parents": ["1Fw8-tnitkJKllxKA2ysAgvaGibWFYDxL"],
        "appProperties": {"salesVersion": verified().version_id},
    }
    google.json.side_effect = [{"ids": ["unused-id"]}, metadata, metadata]
    archive = DriveArchive(google)
    archive.download = Mock(return_value=verified().report)
    assert (
        archive.ensure_report(store, verified())
        == "https://drive.google.com/file/d/reserved-file/view"
    )
    assert google.request.call_count == 1


def test_archive_rejects_an_existing_file_in_the_wrong_folder():
    store = Mock(spec=Store)
    store.plan_once.return_value = {"file_id": "reserved-file"}
    store.receipts.return_value = {}
    google = Mock()
    google.json.side_effect = [
        {"ids": ["unused-id"]},
        {
            "id": "reserved-file",
            "driveId": "personal-drive",
            "parents": [],
            "appProperties": {"salesVersion": verified().version_id},
        },
    ]
    with pytest.raises(ValueError, match="destination"):
        DriveArchive(google).ensure_report(store, verified())
