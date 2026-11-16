"""SQLite index for run history; JSON files and traces remain the full evidence."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "artifacts" / "webprobe_history.sqlite3"
DATASET_SUITES = {
    "search_v3.jsonl": "search",
    "feedback_v3.jsonl": "form",
    "cart_v1.jsonl": "cart",
    "login_v1.jsonl": "login",
}


@dataclass(frozen=True)
class RunRecord:
    """One searchable row describing a single run or a whole evaluation suite."""

    run_id: str
    run_type: str
    suite: str
    policy: str
    dataset: str | None
    case_count: int
    task_success_count: int
    task_success_rate: float
    agent_conclusion: str | None
    run_status: str | None
    task: str | None
    created_at: str
    artifact_path: Path
    evidence_path: Path


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the small local database and create its table/index on first use."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            run_type TEXT NOT NULL CHECK (run_type IN ('single', 'eval')),
            suite TEXT NOT NULL,
            policy TEXT NOT NULL,
            dataset TEXT,
            case_count INTEGER NOT NULL CHECK (case_count >= 1),
            task_success_count INTEGER NOT NULL CHECK (task_success_count >= 0),
            task_success_rate REAL NOT NULL CHECK (task_success_rate >= 0 AND task_success_rate <= 1),
            agent_conclusion TEXT,
            run_status TEXT,
            task TEXT,
            created_at TEXT NOT NULL,
            artifact_path TEXT NOT NULL UNIQUE,
            evidence_path TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_type_suite_created "
        "ON runs (run_type, suite, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_dataset_created "
        "ON runs (dataset, created_at DESC)"
    )
    connection.commit()
    return connection


def _stable_run_id(run_type: str, suite: str, artifact_path: Path) -> str:
    identity = f"{run_type}|{suite}|{artifact_path.resolve()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _created_at(evidence_path: Path) -> str:
    timestamp = datetime.fromtimestamp(evidence_path.stat().st_mtime, timezone.utc)
    return timestamp.isoformat(timespec="seconds")


def _write_record(db_path: Path, record: RunRecord) -> None:
    connection = _connect(db_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, run_type, suite, policy, dataset, case_count,
                    task_success_count, task_success_rate, agent_conclusion,
                    run_status, task, created_at, artifact_path, evidence_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    policy = excluded.policy,
                    dataset = excluded.dataset,
                    case_count = excluded.case_count,
                    task_success_count = excluded.task_success_count,
                    task_success_rate = excluded.task_success_rate,
                    agent_conclusion = excluded.agent_conclusion,
                    run_status = excluded.run_status,
                    task = excluded.task,
                    created_at = excluded.created_at,
                    evidence_path = excluded.evidence_path
                """,
                (
                    record.run_id, record.run_type, record.suite, record.policy,
                    record.dataset, record.case_count, record.task_success_count,
                    record.task_success_rate, record.agent_conclusion,
                    record.run_status, record.task, record.created_at,
                    str(record.artifact_path), str(record.evidence_path),
                ),
            )
    finally:
        connection.close()


def index_single_result(
    result_path: Path, suite: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> RunRecord:
    """Index one report JSON. Credentials and full browser observations are not copied."""
    if not result_path.is_file():
        raise FileNotFoundError(result_path)
    data = json.loads(result_path.read_text(encoding="utf-8"))
    if suite is None:
        suite = next((
            name for name, field in (
                ("search", "search_assertion"), ("form", "form_assertion"),
                ("cart", "cart_assertion"), ("login", "login_assertion"),
            ) if field in data
        ), "unknown")
    success = data.get("task_success") is True
    artifact_path = result_path.parent.resolve()
    record = RunRecord(
        run_id=_stable_run_id("single", suite, artifact_path),
        run_type="single", suite=suite,
        policy=str(data.get("policy") or "unknown"), dataset=None,
        case_count=1, task_success_count=int(success),
        task_success_rate=float(success),
        agent_conclusion=data.get("agent_conclusion"),
        run_status=data.get("run_status"), task=data.get("task"),
        created_at=_created_at(result_path), artifact_path=artifact_path,
        evidence_path=result_path.resolve(),
    )
    _write_record(db_path, record)
    return record


def index_eval_summary(
    summary_path: Path, db_path: Path = DEFAULT_DB_PATH,
) -> RunRecord:
    """Index a suite summary while leaving per-case traces in their JSONL files."""
    if not summary_path.is_file():
        raise FileNotFoundError(summary_path)
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    dataset = str(data.get("dataset") or "unknown")
    suite = DATASET_SUITES.get(dataset, "unknown")
    case_count = max(1, int(data.get("case_count") or 1))
    success_count = int(data.get("task_success_count") or 0)
    artifact_path = summary_path.parent.resolve()
    record = RunRecord(
        run_id=_stable_run_id("eval", suite, artifact_path),
        run_type="eval", suite=suite,
        policy=str(data.get("policy") or "unknown"), dataset=dataset,
        case_count=case_count, task_success_count=success_count,
        task_success_rate=float(data.get("task_success_rate") or 0.0),
        agent_conclusion=None, run_status=str(data.get("evaluation_scope") or "full_suite"),
        task=None, created_at=_created_at(summary_path),
        artifact_path=artifact_path, evidence_path=summary_path.resolve(),
    )
    _write_record(db_path, record)
    return record


def list_runs(
    *, run_type: str | None = None, suite: str | None = None,
    dataset: str | None = None, limit: int = 100,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[RunRecord]:
    """Return newest indexed runs, optionally filtered for a report or dataset page."""
    if limit < 1 or limit > 1_000:
        raise ValueError("limit 必须在 1 到 1000 之间")
    connection = _connect(db_path)
    try:
        conditions: list[str] = []
        values: list[object] = []
        for column, value in (("run_type", run_type), ("suite", suite), ("dataset", dataset)):
            if value is not None:
                conditions.append(f"{column} = ?")
                values.append(value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = connection.execute(
            f"SELECT * FROM runs {where} ORDER BY created_at DESC, run_id DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [
            RunRecord(
                run_id=row["run_id"], run_type=row["run_type"], suite=row["suite"],
                policy=row["policy"], dataset=row["dataset"], case_count=row["case_count"],
                task_success_count=row["task_success_count"],
                task_success_rate=row["task_success_rate"],
                agent_conclusion=row["agent_conclusion"], run_status=row["run_status"],
                task=row["task"], created_at=row["created_at"],
                artifact_path=Path(row["artifact_path"]),
                evidence_path=Path(row["evidence_path"]),
            )
            for row in rows
        ]
    finally:
        connection.close()
