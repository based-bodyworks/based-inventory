"""Read the providers again; saved success receipts alone do not prove health."""

from __future__ import annotations

from .merchant_publish import equal_rows
from .models import digest
from .publisher import MerchantPublisher, merchant_daily
from .report_archive import DriveArchive
from .source_refresh import verify_sources


def native_readbacks(store, google, slack):
    def check(version):
        results = dict.fromkeys(
            (
                "history",
                "canonical",
                "rolling",
                "erp",
                "inventory",
                "report_archive",
                "slack",
                "target",
                "amazon",
                "workbook",
            ),
            False,
        )
        receipts = store.receipts(version.version_id)
        saved = store.connection.execute(
            "SELECT plan FROM pipeline_plans WHERE version_id=%s AND destination='history'",
            (version.version_id,),
        ).fetchone()
        publisher = MerchantPublisher(store, google)
        if saved:
            plan = saved["plan"]
            try:
                actual = publisher.collect()
                for destination, key in (
                    ("history", "history"),
                    ("rolling", "rolling"),
                    ("erp", "daily"),
                ):
                    transform = merchant_daily if key == "daily" else lambda value: value
                    results[destination] = equal_rows(
                        transform(actual[key]), transform(plan[key + "_after"])
                    )
                publisher.verify_canonical(version)
                results["canonical"] = True
                publisher.verify_inventory(plan["rolling_after"])
                results["inventory"] = True
            except (ValueError, RuntimeError, KeyError, StopIteration):
                pass
        try:
            report = receipts.get("report_archive", {})
            archive = DriveArchive(google)
            archive.validate_destination(
                archive.metadata(report["provider_id"]), version.version_id
            )
            results["report_archive"] = digest(archive.download(report["provider_id"])) == digest(
                version.report
            )
        except (ValueError, RuntimeError, KeyError):
            pass
        try:
            slack.verify(receipts.get("slack", {})["provider_id"], version)
            results["slack"] = True
        except (ValueError, RuntimeError, KeyError):
            pass
        try:
            sources = verify_sources(store, version.version_id, google)
            for key in ("target", "amazon", "workbook"):
                results[key] = sources.get(key) is True
            results["inventory"] = results["inventory"] and sources.get("inventory") is True
        except (ValueError, RuntimeError, KeyError, TypeError):
            pass
        return results

    return check
