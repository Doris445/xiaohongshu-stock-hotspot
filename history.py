from __future__ import annotations

import hashlib
import json
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
            "rank", "name", "posts", "comments", "score", "sentiment", "keywords", "topStocks"
        )
        stock_fields = (
            "rank", "name", "code", "sector", "posts", "comments", "score", "sentiment"
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

    def latest_public_snapshot(self, source_date: str) -> dict[str, Any] | None:
        date_root = self.root / source_date
        manifest = self._read_json(date_root / "manifest.json", {})
        snapshots = manifest.get("snapshots") or []
        if not snapshots:
            return None
        latest = snapshots[-1]
        payload = self._read_json(date_root / str(latest.get("path") or ""), {})
        if not payload:
            return None
        # Raw posts remain on disk and are never served through the history API.
        return {
            key: value
            for key, value in payload.items()
            if key != "posts"
        } | {"validations": manifest.get("validations") or []}

    def save_validation(
        self,
        source_date: str,
        outcome_date: str,
        market_as_of: str,
        outcomes: dict[str, dict[str, float]],
        flat_threshold: float = 0.3,
    ) -> dict[str, Any]:
        snapshot = self.latest_public_snapshot(source_date)
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
            **result,
        }
        date_root = self.root / source_date
        validation_id = market_as_of.replace(":", "").replace("+", "_")
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
            "path": path.relative_to(date_root).as_posix(),
        }
        if not any(item.get("marketAsOf") == market_as_of for item in validations):
            validations.append(summary)
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
    outcomes: dict[str, dict[str, float]],
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
            if change is not None and sentiment in {"看多", "看空"}:
                if abs(float(change)) < flat_threshold:
                    status = "flat"
                else:
                    correct = (sentiment == "看多" and float(change) > 0) or (
                        sentiment == "看空" and float(change) < 0
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
                    "posts": item.get("posts", 0),
                    "changePct": change,
                    "status": status,
                    "correct": correct,
                }
            )

    evaluable = [row for row in rows if row["status"] in {"match", "miss"}]
    matches = sum(row["status"] == "match" for row in evaluable)
    accuracy = round(matches / len(evaluable) * 100, 1) if evaluable else None
    return {
        "overall": {
            "entities": len(rows),
            "evaluable": len(evaluable),
            "matches": matches,
            "misses": len(evaluable) - matches,
            "accuracyPct": accuracy,
            "flat": sum(row["status"] == "flat" for row in rows),
            "neutral": sum(row["status"] == "neutral" for row in rows),
            "missing": sum(row["status"] == "missing" for row in rows),
        },
        "rows": rows,
    }
