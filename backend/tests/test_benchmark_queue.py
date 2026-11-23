"""Pruebas del protocolo observable del medidor PFI-140."""

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark-queue.py"


def invoke(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def module():
    spec = importlib.util.spec_from_file_location("benchmark_queue", SCRIPT)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    return benchmark


def test_rejects_unsafe_target_without_touching_queue(tmp_path):
    output = tmp_path / "result.json"
    result = invoke("--api", "https://production.example", "--output", str(output))
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "Destino aislado requerido; no se modificó la cola.\n"
    assert not output.exists()


def test_unsafe_target_never_enters_measurement(monkeypatch, capsys):
    benchmark = module()
    called = []
    monkeypatch.setattr(benchmark, 'measure', lambda *args: called.append(args))
    monkeypatch.setenv('TEST_DATABASE_URL', 'postgresql+psycopg://localhost/pfi140_staging')
    assert benchmark.main(['--api', 'https://production.example',
                           '--confirm-isolated', 'pfi93-staging']) == 2
    assert called == []
    assert capsys.readouterr() == ('', 'Destino aislado requerido; no se modificó la cola.\n')


def test_single_concurrency_runs_exactly_three_trials():
    benchmark = module()
    assert benchmark.trial_plan(False, 1) == [1, 1, 1]
    assert benchmark.trial_plan(False, 2) == [2, 2, 2]


def test_accepts_private_database_bound_to_confirmed_railway_staging(monkeypatch):
    benchmark = module()
    args = SimpleNamespace(
        api="https://api-pfi140-pfi140-staging.up.railway.app",
        confirm_isolated="staging",
        worker_pid=1,
        api_pid=None,
    )
    private_url = "postgresql://user:password@postgres.railway.internal:5432/railway"
    monkeypatch.setenv("TEST_DATABASE_URL", private_url)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    assert benchmark.validate_target(args) == private_url

    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    with pytest.raises(ValueError, match="Destino aislado requerido"):
        benchmark.validate_target(args)


def test_previous_local_isolated_target_remains_accepted(monkeypatch):
    benchmark = module()
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://localhost/pfi140_staging")
    args = SimpleNamespace(
        api="http://localhost:8000", confirm_isolated="pfi140-staging",
        worker_pid=1, api_pid=None,
    )
    assert benchmark.validate_target(args) == "postgresql://localhost/pfi140_staging"


@pytest.mark.parametrize(
    ("database_url", "api"),
    [
        ("postgresql://u:p@postgres.railway.internal", "https://api.staging.example"),
        ("postgresql://u:p@postgres.railway.internal/railway", "http://localhost:8000"),
        ("postgresql://u:p@postgres.railway.internal/railway", "https://127.0.0.1"),
    ],
)
def test_railway_target_requires_database_path_and_staging_api(
    monkeypatch, database_url, api
):
    benchmark = module()
    monkeypatch.setenv("TEST_DATABASE_URL", database_url)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    args = SimpleNamespace(
        api=api, confirm_isolated="staging", worker_pid=1, api_pid=None
    )
    with pytest.raises(
        ValueError,
        match=r"^Destino aislado requerido; no se modificó la cola\.$",
    ):
        benchmark.validate_target(args)


@pytest.mark.parametrize(
    ("db", "environment", "confirmation", "api"),
    [
        ("postgresql://u:p@postgres.railway.internal/pfi140_staging",
         "production", "staging", "https://api.staging.example"),
        ("postgresql://u:p@postgres.railway.internal/staging",
         "staging", "pfi93-staging", "https://api.staging.example"),
        ("postgresql://u:p@postgres.railway.app/staging",
         "staging", "staging", "https://api.staging.example"),
        ("postgresql://u:p@postgres.railway.internal/",
         "staging", "staging", "https://api.staging.example"),
        ("postgresql://u:p@postgres.railway.internal/railway",
         "staging", "staging", ""),
        ("postgresql://u:p@postgres.railway.internal/railway",
         "pfi140-staging", "pfi140-staging", "https://api.staging.example"),
    ],
)
def test_railway_mismatches_stop_before_measurement_and_preserve_output(
    monkeypatch, tmp_path, capsys, db, environment, confirmation, api
):
    benchmark = module()
    monkeypatch.setenv("TEST_DATABASE_URL", db)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", environment)
    calls = []
    monkeypatch.setattr(benchmark, "measure", lambda *args: calls.append(args))
    monkeypatch.setattr(benchmark, "load_batch", lambda *args: [])
    output = tmp_path / "evidence.json"

    assert benchmark.main([
        "--api", api, "--manifest", "unused", "--output", str(output),
        "--worker-pid", "1", "--confirm-isolated", confirmation,
        "--concurrency", "1",
    ]) == 2
    assert calls == []
    assert not output.exists()
    assert capsys.readouterr() == (
        "", "Destino aislado requerido; no se modificó la cola.\n"
    )


def test_public_named_database_preserves_existing_output(monkeypatch, tmp_path, capsys):
    benchmark = module()
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql://u:p@postgres.railway.app/pfi140_staging"
    )
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    calls = []
    monkeypatch.setattr(benchmark, "measure", lambda *args: calls.append(args))
    output = tmp_path / "evidence.json"
    output.write_bytes(b"existing evidence\n")

    assert benchmark.main([
        "--api", "https://api.staging.example",
        "--manifest", "unused", "--output", str(output),
        "--worker-pid", "1", "--confirm-isolated", "staging",
        "--concurrency", "1",
    ]) == 2
    assert calls == []
    assert output.read_bytes() == b"existing evidence\n"
    assert capsys.readouterr() == (
        "", "Destino aislado requerido; no se modificó la cola.\n"
    )


def test_fixed_trials_are_three_for_one_and_two():
    assert module().trial_plan(False) == [1, 1, 1, 2, 2, 2]
    assert module().trial_plan(True) == [1, 1, 1, 2, 2, 2, 3, 3, 3]


def test_cli_routes_only_selected_limit_and_preserves_default(monkeypatch, tmp_path):
    benchmark = module()
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://localhost/pfi140_staging")
    monkeypatch.setattr(benchmark, "load_batch", lambda path: [])
    plans = []

    def measure(args, batch, db):
        plan = benchmark.trial_plan(args.include_three, args.concurrency)
        plans.append(plan)
        return {"trials": [{"concurrency": c} for c in plan]}

    monkeypatch.setattr(benchmark, "measure", measure)
    for index, (options, expected) in enumerate((
        ([], [1, 1, 1, 2, 2, 2]),
        (["--concurrency", "1"], [1, 1, 1]),
        (["--concurrency", "2"], [2, 2, 2]),
        (["--include-three"], [1, 1, 1, 2, 2, 2, 3, 3, 3]),
    )):
        output = tmp_path / f"result-{index}.json"
        assert benchmark.main([
            "--api", "https://api.staging.example",
            "--worker-pid", "123",
            "--confirm-isolated", "pfi93-staging",
            "--manifest", str(tmp_path / "unused.json"),
            "--output", str(output), *options,
        ]) == 0
        assert plans[-1] == expected
        assert [trial["concurrency"] for trial in
                json.loads(output.read_text())["trials"]] == expected


def test_intervals_prove_overlap_and_cap_with_persisted_attempts():
    benchmark = module()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    attempts = [SimpleNamespace(started_at=base + timedelta(seconds=start),
                                finished_at=base + timedelta(seconds=end), outcome="ok")
                for start, end in ((0, 4), (1, 3), (4, 5))]
    assert benchmark.interval_evidence(attempts, 2) == {
        "peak_active": 2, "overlap_seconds": 2.0, "limit_respected": True,
        "complete_intervals": 3,
    }
    assert benchmark.interval_evidence(attempts, 1)["limit_respected"] is False


def test_summary_reports_latency_capacity_errors_and_aggregate_resources():
    benchmark = module()
    result = benchmark.summarize_trial(
        2, [1.0, 2.0], [0.1, 0.2], ["ok", "error"], 4.0,
        {"peak_active": 2, "overlap_seconds": 1.0, "limit_respected": True,
         "complete_intervals": 2},
        [(10.0, 100), (25.0, 200)],
    )
    assert result["resolution_seconds"] == {"median": 1.5, "p95": 1.95}
    assert result["api_seconds"] == {"median": 0.15, "p95": 0.195}
    assert result["capacity_jobs_per_second"] == 0.25
    assert result["errors"] == 1
    assert result["resources"] == {"peak_tree_cpu_percent": 25.0,
                                    "peak_tree_rss_bytes": 200}
    assert result["limit_respected"] is True
    assert "payload" not in json.dumps(result)
