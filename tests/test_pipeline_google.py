from unittest.mock import Mock

import pytest

from based_inventory.pipeline.google_api import GoogleAPI, GoogleAPIError


def response(status, payload):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


def test_token_refresh_and_429_retry_do_not_log_provider_secrets():
    session = Mock()
    session.post.return_value = response(200, {"access_token": "private-token", "expires_in": 3600})
    session.request.side_effect = [response(429, {}), response(200, {"values": [[4]]})]
    api = GoogleAPI("id", "secret", "refresh", session=session, sleep=Mock())
    assert api.values(
        "1vI9dM06MtptojGSxBAfNqOu0l0ZPyAGj0-D88ZF82eY", "'Verified Sales History'!A1"
    ) == [[4]]
    assert session.request.call_count == 2


def test_protected_files_and_canonical_formula_cells_cannot_be_written():
    api = GoogleAPI("id", "secret", "refresh", session=Mock())
    with pytest.raises(ValueError, match="write allowlist"):
        api.batch("1lbWCa0FxTxk4tFFrre3oiBi8QKVifP4mSsVVRF3B2P8", [])
    with pytest.raises(ValueError, match="formula-derived"):
        api.batch(
            "1vI9dM06MtptojGSxBAfNqOu0l0ZPyAGj0-D88ZF82eY",
            [{"updateCells": {"start": {"sheetId": 2106291003}}}],
        )


def test_failed_refresh_error_does_not_echo_secret_response():
    session = Mock()
    session.post.return_value = response(400, {"error_description": "refresh-secret"})
    with pytest.raises(GoogleAPIError) as error:
        GoogleAPI("id", "secret", "refresh", session=session).values("file", "A1")
    assert "secret" not in str(error.value)


def test_existing_formulas_are_not_replaced_by_source_values():
    api = GoogleAPI("id", "secret", "refresh", session=Mock())
    api.metadata = Mock(
        return_value={
            "sheets": [{"properties": {"sheetId": 1740410291, "title": "Rolling7Demand"}}]
        }
    )
    api.cells = Mock(
        return_value={
            "sheets": [
                {
                    "properties": {"sheetId": 1740410291},
                    "data": [
                        {
                            "startRow": 7,
                            "startColumn": 19,
                            "rowData": [
                                {"values": [{"userEnteredValue": {"formulaValue": "=SUM(A1:A7)"}}]}
                            ],
                        }
                    ],
                }
            ]
        }
    )
    api.json = Mock()
    with pytest.raises(ValueError, match="formula"):
        api.batch(
            "1vI9dM06MtptojGSxBAfNqOu0l0ZPyAGj0-D88ZF82eY",
            [
                {
                    "updateCells": {
                        "start": {"sheetId": 1740410291, "rowIndex": 7, "columnIndex": 19},
                        "rows": [{"values": [{"userEnteredValue": {"numberValue": 0}}]}],
                        "fields": "userEnteredValue",
                    }
                }
            ],
        )
    api.json.assert_not_called()


def test_range_tail_clear_cannot_bypass_formula_preflight():
    api = GoogleAPI("id", "secret", "refresh", session=Mock())
    api.metadata = Mock(
        return_value={"sheets": [{"properties": {"sheetId": 1740410293, "title": "Amazon Sales"}}]}
    )
    api.cells = Mock(
        return_value={
            "sheets": [
                {
                    "properties": {"sheetId": 1740410293},
                    "data": [
                        {
                            "startRow": 2,
                            "startColumn": 0,
                            "rowData": [
                                {"values": [{"userEnteredValue": {"formulaValue": "=A1"}}]}
                            ],
                        }
                    ],
                }
            ]
        }
    )
    api.json = Mock()
    with pytest.raises(ValueError, match="formula"):
        api.batch(
            "1vI9dM06MtptojGSxBAfNqOu0l0ZPyAGj0-D88ZF82eY",
            [
                {
                    "updateCells": {
                        "range": {
                            "sheetId": 1740410293,
                            "startRowIndex": 1,
                            "endRowIndex": 3,
                            "startColumnIndex": 0,
                            "endColumnIndex": 5,
                        },
                        "rows": [{"values": [{"userEnteredValue": {"numberValue": 1}}]}],
                        "fields": "userEnteredValue",
                    }
                }
            ],
        )
    api.json.assert_not_called()
