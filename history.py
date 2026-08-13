from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


class HistoryArchive:
    """Private, append-only daily snapshots for forward validation."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _canonical_hash(posts: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            posts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _prediction_view(dashboard: dict[str, Any]) -> dict[str, Any]:
        sector_fields = (
            "rank", "name", "posts", "comments", "score", "sentiment", "confidence",
            "signalTier", "evidencePosts", "uniqueAuthors", "keywords", "topStocks"
        )
        stock_fields = (
            "rank", "name", "code", "sector", "posts", "comments", "score", "sentiment",
            "confidence", "signalTier", "evidencePosts", "uniqueAuthors"
        )
        return {
            "summary": dict(dashboard.get("summary") or {}),
            "sectors": [
                {key: item.get(key) for key in sector_fields}
                for item in (dashboard.get("sectors") or [])
            ],
            "stocks": [
                {key: item.get(key) for key in stock_fields}
                for item in (dashboard.get("stocks") or [])
            ],
        }

    def archive(
        self,
        source_date: str,
        posts: list[dict[str, Any]],
        dashboard: dict[str, Any],
        captured_at: datetime,
        source: str,
    ) -> dict[str, Any]:
        post_hash = self._canonical_hash(posts)
        date_root = self.root / source_date
        snapshots_root = date_root / "snapshots"
        manifest_path = date_root / "manifest.json"
        snapshots_root.mkdir(parents=True, exist_ok=True)

        manifest = self._read_json(manifest_path, {"sourceDate": source_date, "snapshots": []})
        for item in manifest.get("snapshots", []):
            if item.get("postsSha256") == post_hash:
                return item

        captured_iso = captured_at.isoformat(timespec="seconds")
        snapshot_id = f"{captured_at.strftime('%H%M%S')}-{post_hash[:10]}"
        snapshot_path = snapshots_root / f"{snapshot_id}.json"
        payload = {
            "schemaVersion": 1,
            "snapshotId": snapshot_id,
            "sourceDate": source_date,
            "capturedAt": captured_iso,
            "source": source,
            "postsSha256": post_hash,
            "postCount": len(posts),
            "prediction": self._prediction_view(dashboard),
            "posts": posts,
        }
        self._write_json_atomic(snapshot_path, payload)

        summary = {
            "snapshotId": snapshot_id,
            "capturedAt": captured_iso,
            "source": source,
            "postCount": len(posts),
            "postsSha256": post_hash,
            "path": snapshot_path.relative_to(date_root).as_posix(),
        }
        manifest.setdefault("snapshots", []).append(summary)
        manifest["snapshots"].sort(key=lambda item: str(item.get("capturedAt") or ""))
        manifest["latestSnapshotId"] = manifest["snapshots"][-1]["snapshotId"]
        self._write_json_atomic(manifest_path, manifest)
        return summary

    def list_dates(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        result: list[dict[str, Any]] = []
        for date_root in self.root.iterdir():
            if not date_root.is_dir():
                continue
            manifest = self._read_json(date_root / "manifest.json", {})
            snapshots = manifest.get("snapshots") or []
            if not snapshots:
                continue
            latest = snapshots[-1]
            result.append(
                {
                    "sourceDate": manifest.get("sourceDate") or date_root.name,
                    "snapshotCount": len(snapshots),
                    "latestSnapshotId": latest.get("snapshotId"),
                    "capturedAt": latest.get("capturedAt"),
                    "postCount": latest.get("postCount", 0),
                    "postsSha256": latest.get("postsSha256"),
                    "validationCount": len(manifest.get("validations") or []),
                }
            )
        return sorted(result, key=lambda item: item["sourceDate"], reverse=True)

    def latest_public_snapshot(
        self, source_date: str, snapshot_id: str | None = None
    ) -> dict[str, Any] | None:
        payload = self.latest_snapshot(source_date, snapshot_id=snapshot_id)
        if not payload:
            return None
        # Raw posts remain on disk and are never served through the history API.
        public = {
            key: value
            for key, value in payload.items()
            if key != "posts"
        }
        public["validations"] = self._public_validations(source_date)
        return public

    def latest_snapshot(
        self, source_date: str, snapshot_id: str | None = None
    ) -> dict[str, Any] | None:
        """Load a private snapshot for server-side reconstruction only."""
        date_root = self.root / source_date
        manifest = self._read_json(date_root / "manifest.json", {})
        snapshots = manifest.get("snapshots") or []
        if not snapshots:
            return None
        latest = next(
            (item for item in snapshots if item.get("snapshotId") == snapshot_id),
            None,
        ) if snapshot_id else snapshots[-1]
        if not latest:
            return None
        payload = self._read_json(date_root / str(latest.get("path") or ""), {})
        if not payload:
            return None
        return payload | {"validations": manifest.get("validations") or []}

    def snapshot_at_or_before(
        self, source_date: str, cutoff_at: datetime
    ) -> dict[str, Any] | None:
        """Return the final immutable prediction available before a cutoff."""
        date_root = self.root / source_date
        manifest = self._read_json(date_root / "manifest.json", {})
        eligible: list[dict[str, Any]] = []
        for item in manifest.get("snapshots") or []:
            try:
                captured_at = datetime.fromisoformat(str(item.get("capturedAt") or ""))
            except ValueError:
                continue
            if captured_at <= cutoff_at:
                eligible.append(item)
        if not eligible:
            return None
        return self.latest_snapshot(source_date, snapshot_id=eligible[-1].get("snapshotId"))

    def _public_validations(self, source_date: str) -> list[dict[str, Any]]:
        date_root = self.root / source_date
        manifest = self._read_json(date_root / "manifest.json", {})
        result: list[dict[str, Any]] = []
        for item in manifest.get("validations") or []:
            payload = self._read_json(date_root / str(item.get("path") or ""), {})
            if payload:
                result.append(payload)
        return result

    def save_validation(
        self,
        source_date: str,
        outcome_date: str,
        market_as_of: str,
        outcomes: dict[str, dict[str, Any]],
        flat_threshold: float = 0.3,
        snapshot_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.latest_public_snapshot(source_date, snapshot_id=snapshot_id)
        if not snapshot:
            raise ValueError(f"No archived sentiment snapshot for {source_date}")
        result = evaluate_market_alignment(
            snapshot.get("prediction") or {}, outcomes, flat_threshold=flat_threshold
        )
        validation = {
            "sourceDate": source_date,
            "outcomeDate": outcome_date,
            "marketAsOf": market_as_of,
            "flatThresholdPct": flat_threshold,
            "predictionSnapshotId": snapshot.get("snapshotId"),
            "predictionCapturedAt": snapshot.get("capturedAt"),
            **(metadata or {}),
            **result,
        }
        date_root = self.root / source_date
        mode = str(validation.get("comparisonMode") or "validation")
        validation_id = re.sub(r"[^0-9A-Za-z_-]+", "", f"{mode}-{market_as_of}")
        path = date_root / "validations" / f"{validation_id}.json"
        self._write_json_atomic(path, validation)

        manifest_path = date_root / "manifest.json"
        manifest = self._read_json(manifest_path, {"sourceDate": source_date, "snapshots": []})
        validations = manifest.setdefault("validations", [])
        summary = {
            "outcomeDate": outcome_date,
            "marketAsOf": market_as_of,
            "accuracyPct": result["overall"]["accuracyPct"],
            "evaluable": result["overall"]["evaluable"],
            "comparisonMode": validation.get("comparisonMode"),
            "predictionSnapshotId": snapshot.get("snapshotId"),
            "path": path.relative_to(date_root).as_posix(),
        }
        existing = next(
            (
                index for index, item in enumerate(validations)
                if item.get("path") == summary["path"]
            ),
            None,
        )
        if existing is None:
            validations.append(summary)
        else:
            validations[existing] = summary
        validations.sort(key=lambda item: str(item.get("marketAsOf") or ""))
        self._write_json_atomic(manifest_path, manifest)
        return validation

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    @staticmethod
    def _write_json_atomic(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


def evaluate_market_alignment(
    prediction: dict[str, Any],
    outcomes: dict[str, dict[str, Any]],
    flat_threshold: float = 0.3,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for entity_type in ("sectors", "stocks"):
        for item in prediction.get(entity_type) or []:
            key = str(item.get("code") or item.get("name") or "")
            sentiment = str(item.get("sentiment") or "")
            outcome = outcomes.get(key) or outcomes.get(str(item.get("name") or ""))
            change = outcome.get("changePct") if outcome else None
            status = "missing"
            correct: bool | None = None
            direction = "bull" if sentiment.endswith("看多") else "bear" if sentiment.endswith("看空") else None
            if change is not None and direction:
                if abs(float(change)) < flat_threshold:
                    status = "flat"
                else:
                    correct = (direction == "bull" and float(change) > 0) or (
                        direction == "bear" and float(change) < 0
                    )
                    status = "match" if correct else "miss"
            elif sentiment == "中性":
                status = "neutral"
            rows.append(
                {
                    "entityType": entity_type[:-1],
                    "name": item.get("name"),
                    "code": item.get("code"),
                    "sentiment": sentiment,
                    "sentimentScore": item.get("score"),
                    "signalTier": item.get("signalTier") or (
                        "qualified" if sentiment in {"看多", "看空"} else
                        "preliminary" if direction else "none"
                    ),
                    "posts": item.get("posts", 0),
                    "changePct": change,
                    "outcomeName": outcome.get("name") if outcome else None,
                    "quoteAt": outcome.get("quoteAt") if outcome else None,
                    "marketSource": outcome.get("source") if outcome else None,
                    "status": status,
                    "correct": correct,
                }
            )

    def aggregate(values: list[dict[str, Any]]) -> dict[str, Any]:
        evaluable = [row for row in values if row["status"] in {"match", "miss"}]
        matches = sum(row["status"] == "match" for row in evaluable)
        return {
            "entities": len(values),
            "evaluable": len(evaluable),
            "matches": matches,
            "misses": len(evaluable) - matches,
            "accuracyPct": round(matches / len(evaluable) * 100, 1) if evaluable else None,
            "flat": sum(row["status"] == "flat" for row in values),
            "neutral": sum(row["status"] == "neutral" for row in values),
            "missing": sum(row["status"] == "missing" for row in values),
        }

    return {
        "overall": aggregate(rows),
        "byEntityType": {
            entity_type: aggregate([row for row in rows if row["entityType"] == entity_type])
            for entity_type in ("sector", "stock")
        },
        "rows": rows,
    }
