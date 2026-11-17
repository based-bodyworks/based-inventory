"""Archive the exact report with a pre-reserved Drive ID and content readback."""

from urllib.parse import quote

from .google_api import GoogleAPIError
from .models import digest

DRIVE = "0ANDGo4RyWVY2Uk9PVA"
FOLDER = "1Fw8-tnitkJKllxKA2ysAgvaGibWFYDxL"
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BASE = "https://www.googleapis.com/drive/v3/files"


class DriveArchive:
    def __init__(self, google):
        self.google = google

    def metadata(self, file_id):
        return self.google.json(
            "GET",
            BASE + "/" + quote(file_id, safe=""),
            params={
                "supportsAllDrives": "true",
                "fields": "id,driveId,parents,appProperties,size,mimeType",
            },
        )

    def download(self, file_id):
        return self.google.request(
            "GET",
            BASE + "/" + quote(file_id, safe=""),
            params={"alt": "media", "supportsAllDrives": "true"},
        ).content

    @staticmethod
    def validate_destination(metadata, version_id):
        if metadata.get("driveId") != DRIVE or FOLDER not in metadata.get("parents", []):
            raise ValueError("Report archive destination is not Marketing Operations")
        if metadata.get("appProperties", {}).get("salesVersion") != version_id:
            raise ValueError("Report archive belongs to a different sales version")

    def ensure_report(self, store, version):
        store.assert_current(version.version_id)
        proposed = self.google.json(
            "GET", BASE + "/generateIds", params={"count": 1, "space": "drive", "type": "files"}
        )
        reserved = store.plan_once(
            version.version_id, "report_archive", {"file_id": proposed["ids"][0]}
        )
        file_id = reserved["file_id"]
        try:
            metadata = self.metadata(file_id)
        except GoogleAPIError as error:
            if error.status != 404:
                raise
            try:
                self.google.json(
                    "POST",
                    BASE,
                    params={"supportsAllDrives": "true"},
                    json={
                        "id": file_id,
                        "name": f"{version.day}_BASED Merchant Sales.xlsx",
                        "mimeType": MIME,
                        "parents": [FOLDER],
                        "appProperties": {"salesVersion": version.version_id},
                    },
                )
            except GoogleAPIError:
                # A known ID makes a lost create response recoverable without a duplicate file.
                self.validate_destination(self.metadata(file_id), version.version_id)
            metadata = self.metadata(file_id)
        self.validate_destination(metadata, version.version_id)
        receipt = store.receipts(version.version_id).get("report_archive")
        if not receipt or receipt["status"] != "verified":
            self.google.request(
                "PATCH",
                f"https://www.googleapis.com/upload/drive/v3/files/{quote(file_id, safe='')}",
                params={"uploadType": "media", "supportsAllDrives": "true"},
                headers={"Content-Type": MIME},
                data=version.report,
            )
        self.validate_destination(self.metadata(file_id), version.version_id)
        if digest(self.download(file_id)) != digest(version.report):
            raise ValueError("Saved cloud report hash mismatch")
        store.record_receipt(version.version_id, "report_archive", digest(version.report), file_id)
        return f"https://drive.google.com/file/d/{file_id}/view"
