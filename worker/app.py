import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from redis import Redis


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


REDIS_URL = os.environ["REDIS_URL"]
JOB_QUEUE_KEY = os.getenv("JOB_QUEUE_KEY", "openclaw:jobs")
JOB_STATUS_PREFIX = os.getenv("JOB_STATUS_PREFIX", "openclaw:job:")
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace/jobs"))
OPENCLAW_RUNNER = os.getenv("OPENCLAW_RUNNER", "/app/run_openclaw.sh")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

redis_client = Redis.from_url(REDIS_URL, decode_responses=True)


def job_key(job_id: str) -> str:
    return f"{JOB_STATUS_PREFIX}{job_id}"


def store_status(job_id: str, fields: dict) -> None:
    fields["updated_at"] = utc_now()
    redis_client.hset(job_key(job_id), mapping=fields)


def run_command(args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )


def clone_repo(repo: str, branch: str, destination: Path) -> None:
    clone_target = repo
    if repo.startswith("https://") and GITHUB_TOKEN:
        clone_target = repo.replace("https://", f"https://x-access-token:{GITHUB_TOKEN}@")

    result = run_command(
        ["git", "clone", "--depth", "1", "--branch", branch, clone_target, str(destination)]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git clone failed")


def collect_git_summary(repo_dir: Path) -> str:
    result = run_command(["git", "status", "--short"], cwd=repo_dir)
    return result.stdout.strip() or "No tracked changes."


def write_task_file(job_dir: Path, payload: dict) -> Path:
    task_file = job_dir / "task.txt"
    task_file.write_text(
        "\n".join(
            [
                f"Job ID: {payload['job_id']}",
                f"Repository: {payload['repo']}",
                f"Branch: {payload['branch']}",
                f"Requested By: {payload['requested_by']}",
                "",
                "Task:",
                payload["task"],
                "",
                "Instructions:",
                "- Work only inside this repository.",
                "- Make the requested code changes.",
                "- Leave a clean diff for review.",
            ]
        ),
        encoding="utf-8",
    )
    return task_file


def execute_job(payload: dict) -> None:
    job_id = payload["job_id"]
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    job_dir = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=WORKSPACE_ROOT))
    repo_dir = job_dir / "repo"

    try:
        store_status(job_id, {"status": "running"})
        clone_repo(payload["repo"], payload["branch"], repo_dir)
        task_file = write_task_file(job_dir, payload)

        store_status(job_id, {"status": "executing"})
        result = run_command([OPENCLAW_RUNNER, str(repo_dir), str(task_file), job_id], cwd=job_dir)
        git_summary = collect_git_summary(repo_dir)

        if result.returncode != 0:
            store_status(
                job_id,
                {
                    "status": "failed",
                    "error": result.stderr.strip() or result.stdout.strip() or "OpenClaw runner failed",
                    "result_summary": git_summary,
                },
            )
            return

        stdout = result.stdout.strip().splitlines()
        summary = stdout[-1] if stdout else "Runner completed."
        store_status(
            job_id,
            {
                "status": "completed",
                "result_summary": f"{summary}\n{git_summary}",
            },
        )
    except Exception as exc:
        store_status(job_id, {"status": "failed", "error": str(exc)})
    finally:
        keep_workspace = os.getenv("KEEP_WORKSPACE", "true").lower() == "true"
        if not keep_workspace and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)


def main() -> None:
    print("Worker started.")
    while True:
        item = redis_client.blpop(JOB_QUEUE_KEY, timeout=0)
        if not item:
            continue
        _, raw_payload = item
        payload = json.loads(raw_payload)
        execute_job(payload)


if __name__ == "__main__":
    main()
