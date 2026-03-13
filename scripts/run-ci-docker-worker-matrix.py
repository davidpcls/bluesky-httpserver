#!/usr/bin/env python3
"""Run bluesky-httpserver CI-style checks using a docker client-worker model.

This script is designed to mirror the project's GitHub Actions workflows locally,
with special focus on accelerating the pytest matrix by chunking tests and
dispatching chunks to worker containers.

What it runs by default:
1. Style checks (black/isort/flake8/pre-commit)
2. Docs build check
3. Unit tests for Python 3.10/3.11/3.12/3.13 using worker containers

Notes:
- The unit-test step follows `.github/workflows/testing.yml` dependency setup.
- Shared service containers (Redis, LDAP) are started once and reused.
- Unit tests are chunked by test file and distributed across workers per version.
"""

from __future__ import annotations

import argparse
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

DEFAULT_PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13"]
DEFAULT_WORKERS_PER_VERSION = 2


@dataclass
class ChunkResult:
    python_version: str
    worker_name: str
    chunk_index: int
    command: str
    returncode: int
    log_path: Path


def run(
    command: Sequence[str], *, cwd: Path, env: dict | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, env=env, check=check, text=True)


def shell(
    command: str, *, cwd: Path, env: dict | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, env=env, check=check, text=True, shell=True)


def chunk_items(items: List[str], chunks: int) -> List[List[str]]:
    if chunks <= 0:
        return [items]
    if not items:
        return []
    chunks = max(1, min(chunks, len(items)))
    chunk_size = math.ceil(len(items) / chunks)
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def docker_cmd(*parts: str) -> List[str]:
    return ["docker", *parts]


def ensure_docker_available(repo_root: Path) -> None:
    run(["docker", "info"], cwd=repo_root)


def discover_test_files(repo_root: Path) -> List[str]:
    test_dir = repo_root / "bluesky_httpserver" / "tests"
    files = sorted(
        str(path.relative_to(repo_root)) for path in test_dir.glob("test_*.py")
    )
    return files


def start_redis_container(repo_root: Path, name: str) -> None:
    run(docker_cmd("rm", "-f", name), cwd=repo_root, check=False)
    run(
        docker_cmd(
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            "host",
            "redis:7",
        ),
        cwd=repo_root,
    )


def start_ldap_compose(repo_root: Path) -> None:
    compose_file = (
        repo_root
        / "continuous_integration"
        / "docker-configs"
        / "ldap-docker-compose.yml"
    )
    env = os.environ.copy()
    env["LDAP_COMPOSE_FILE"] = str(compose_file)
    env["LDAP_COMPOSE_PROJECT"] = "bhs-ci-ldap"
    env["LDAP_HOST"] = "127.0.0.1"
    env["LDAP_PORT"] = "1389"
    run(
        [
            "bash",
            str(repo_root / "continuous_integration" / "scripts" / "start_LDAP.sh"),
        ],
        cwd=repo_root,
        env=env,
    )


def stop_ldap_compose(repo_root: Path) -> None:
    compose_file = (
        repo_root
        / "continuous_integration"
        / "docker-configs"
        / "ldap-docker-compose.yml"
    )
    run(
        [
            "docker",
            "compose",
            "-p",
            "bhs-ci-ldap",
            "-f",
            str(compose_file),
            "down",
            "-v",
        ],
        cwd=repo_root,
        check=False,
    )


def make_worker_name(python_version: str, index: int) -> str:
    v = python_version.replace(".", "")
    return f"bhs-ci-py{v}-worker{index}"


def start_worker_container(
    repo_root: Path, python_version: str, worker_name: str
) -> None:
    run(docker_cmd("rm", "-f", worker_name), cwd=repo_root, check=False)
    run(
        docker_cmd(
            "run",
            "-d",
            "--rm",
            "--name",
            worker_name,
            "--network",
            "host",
            "-v",
            f"{repo_root}:/workspace",
            "-w",
            "/workspace",
            f"python:{python_version}",
            "bash",
            "-lc",
            "sleep infinity",
        ),
        cwd=repo_root,
    )


def exec_in_worker(
    repo_root: Path, worker_name: str, command: str, *, log_path: Path
) -> int:
    full_cmd = ["docker", "exec", worker_name, "bash", "-lc", command]
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.run(
            full_cmd,
            cwd=repo_root,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
    return process.returncode


def bootstrap_worker(repo_root: Path, worker_name: str) -> None:
    # Mirrors `.github/workflows/testing.yml` install strategy as closely as possible.
    install_cmd = " && ".join(
        [
            "python -m pip install --upgrade pip setuptools numpy",
            "pip install git+https://github.com/bluesky/bluesky-queueserver.git",
            "pip install git+https://github.com/bluesky/bluesky-queueserver-api.git",
            "pip install .",
            "pip install -r requirements-dev.txt",
            "pip list",
        ]
    )
    code = exec_in_worker(
        repo_root,
        worker_name,
        install_cmd,
        log_path=repo_root / ".ci-artifacts" / f"{worker_name}-bootstrap.log",
    )
    if code != 0:
        raise RuntimeError(f"Bootstrap failed for worker {worker_name}")


def run_style_and_docs(repo_root: Path, python_version: str) -> None:
    worker_name = f"bhs-ci-style-docs-py{python_version.replace('.', '')}"
    start_worker_container(repo_root, python_version, worker_name)
    try:
        bootstrap_worker(repo_root, worker_name)
        steps = [
            ("black", "black . --check"),
            ("isort", "isort . -c"),
            ("flake8", "flake8"),
            ("pre-commit", "pre-commit run --all-files"),
            ("docs", "make -C docs/ html"),
        ]
        for label, cmd in steps:
            log_path = repo_root / ".ci-artifacts" / f"{worker_name}-{label}.log"
            code = exec_in_worker(repo_root, worker_name, cmd, log_path=log_path)
            if code != 0:
                raise RuntimeError(f"Step '{label}' failed. See {log_path}")
    finally:
        run(docker_cmd("rm", "-f", worker_name), cwd=repo_root, check=False)


def run_test_matrix(
    repo_root: Path,
    python_versions: Iterable[str],
    workers_per_version: int,
    include_pattern: str | None,
    chunks_per_version: int | None,
    tests_per_chunk: int | None,
) -> list[ChunkResult]:
    artifacts_dir = repo_root / ".ci-artifacts"
    test_files = discover_test_files(repo_root)
    if include_pattern:
        test_files = [f for f in test_files if include_pattern in f]

    if not test_files:
        raise RuntimeError("No test files discovered.")

    all_results: list[ChunkResult] = []

    for python_version in python_versions:
        print(f"\n=== Python {python_version}: preparing workers ===", flush=True)
        workers = [
            make_worker_name(python_version, i + 1) for i in range(workers_per_version)
        ]

        for worker in workers:
            start_worker_container(repo_root, python_version, worker)
        try:
            for worker in workers:
                print(f"Bootstrapping {worker} ...", flush=True)
                bootstrap_worker(repo_root, worker)

            if tests_per_chunk and tests_per_chunk > 0:
                chunks = [
                    test_files[i : i + tests_per_chunk]
                    for i in range(0, len(test_files), tests_per_chunk)
                ]
            else:
                n_chunks = (
                    chunks_per_version
                    if (chunks_per_version and chunks_per_version > 0)
                    else workers_per_version * 4
                )
                chunks = chunk_items(test_files, n_chunks)
            work_queue: queue.Queue[tuple[int, list[str]]] = queue.Queue()
            for idx, chunk in enumerate(chunks):
                work_queue.put((idx, chunk))

            results_lock = threading.Lock()

            def worker_loop(worker_name: str) -> None:
                while True:
                    try:
                        chunk_index, chunk = work_queue.get_nowait()
                    except queue.Empty:
                        return
                    chunk_args = " ".join(chunk)
                    command = (
                        "QSERVER_TEST_LDAP_HOST=localhost "
                        "QSERVER_TEST_LDAP_PORT=1389 "
                        f"coverage run -m pytest -vv {chunk_args}"
                    )
                    log_path = (
                        artifacts_dir / f"{worker_name}-chunk{chunk_index:03d}.log"
                    )
                    rc = exec_in_worker(
                        repo_root, worker_name, command, log_path=log_path
                    )
                    with results_lock:
                        all_results.append(
                            ChunkResult(
                                python_version=python_version,
                                worker_name=worker_name,
                                chunk_index=chunk_index,
                                command=command,
                                returncode=rc,
                                log_path=log_path,
                            )
                        )
                    work_queue.task_done()

            threads = [
                threading.Thread(target=worker_loop, args=(worker,), daemon=True)
                for worker in workers
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Per-version coverage summary (best effort, non-fatal)
            for worker in workers:
                exec_in_worker(
                    repo_root,
                    worker,
                    "coverage report -m || true",
                    log_path=artifacts_dir / f"{worker}-coverage-report.log",
                )
        finally:
            for worker in workers:
                run(docker_cmd("rm", "-f", worker), cwd=repo_root, check=False)

    return all_results


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python-versions",
        nargs="+",
        default=DEFAULT_PYTHON_VERSIONS,
        help="Python versions to test (default mirrors .github/workflows/testing.yml)",
    )
    parser.add_argument(
        "--workers-per-version",
        type=int,
        default=DEFAULT_WORKERS_PER_VERSION,
        help="Worker containers per Python version",
    )
    parser.add_argument(
        "--chunks-per-version",
        type=int,
        default=None,
        help="Total pytest chunks per Python version (default: workers_per_version * 4)",
    )
    parser.add_argument(
        "--tests-per-chunk",
        type=int,
        default=None,
        help="Number of test files per chunk (overrides --chunks-per-version)",
    )
    parser.add_argument(
        "--include-pattern",
        default=None,
        help="Only run test files containing this substring",
    )
    parser.add_argument(
        "--skip-style-docs",
        action="store_true",
        help="Skip style/docs checks and run only the test matrix",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="Keep .ci-artifacts from previous runs (default clears first)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    artifacts_dir = repo_root / ".ci-artifacts"

    ensure_docker_available(repo_root)

    if artifacts_dir.exists() and not args.keep_artifacts:
        shutil.rmtree(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    redis_name = "bhs-ci-redis"
    start_redis_container(repo_root, redis_name)
    start_ldap_compose(repo_root)

    try:
        if not args.skip_style_docs:
            print(
                "\n=== Running style/docs checks (CI parity for non-matrix workflows) ===",
                flush=True,
            )
            run_style_and_docs(repo_root, "3.12")

        print("\n=== Running chunked pytest matrix ===", flush=True)
        results = run_test_matrix(
            repo_root,
            python_versions=args.python_versions,
            workers_per_version=args.workers_per_version,
            include_pattern=args.include_pattern,
            chunks_per_version=args.chunks_per_version,
            tests_per_chunk=args.tests_per_chunk,
        )

        failed = [r for r in results if r.returncode != 0]
        print("\n=== Summary ===", flush=True)
        print(f"Total chunks run: {len(results)}", flush=True)
        print(f"Failed chunks: {len(failed)}", flush=True)
        if failed:
            for r in failed:
                print(
                    f"[FAIL] py={r.python_version} worker={r.worker_name} chunk={r.chunk_index} "
                    f"log={r.log_path}",
                    flush=True,
                )
            return 1
        print("All CI-equivalent checks passed.", flush=True)
        return 0
    finally:
        stop_ldap_compose(repo_root)
        run(docker_cmd("rm", "-f", redis_name), cwd=repo_root, check=False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
