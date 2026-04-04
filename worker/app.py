import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import requests
from redis import Redis


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


REDIS_URL = os.environ["REDIS_URL"]
JOB_QUEUE_KEY = os.getenv("JOB_QUEUE_KEY", "openclaw:jobs")
JOB_STATUS_PREFIX = os.getenv("JOB_STATUS_PREFIX", "openclaw:job:")
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace/jobs"))
OPENCLAW_RUNNER = os.getenv("OPENCLAW_RUNNER", "/app/run_openclaw.sh")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GIT_AUTHOR_NAME = os.getenv("GIT_AUTHOR_NAME", "OpenClaw Bot")
GIT_AUTHOR_EMAIL = os.getenv("GIT_AUTHOR_EMAIL", "openclaw-bot@users.noreply.github.com")

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


def parse_github_owner_repo(repo_url: str) -> Tuple[str, str]:
    """Extract owner and repo name from GitHub URL (HTTPS or SSH)."""
    # SSH: git@github.com:owner/repo.git
    m = re.match(r"git@github\.com:([^/]+)/([^/.]+?)(?:\.git)?$", repo_url)
    if m:
        return m.group(1), m.group(2)
    # HTTPS: https://github.com/owner/repo.git
    m = re.match(r"https?://github\.com/([^/]+)/([^/.]+?)(?:\.git)?$", repo_url)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"Cannot parse GitHub owner/repo from: {repo_url}")


def clone_repo(repo: str, destination: Path) -> None:
    """Clone repo default branch. Use HTTPS + token auth when GITHUB_TOKEN is set."""
    clone_target = repo
    if GITHUB_TOKEN:
        owner, name = parse_github_owner_repo(repo)
        clone_target = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{owner}/{name}.git"

    result = run_command(
        ["git", "clone", clone_target, str(destination)]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git clone failed")


def detect_default_branch(repo_dir: Path) -> str:
    """Detect the default branch name from the cloned repo."""
    result = run_command(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_dir)
    if result.returncode == 0:
        # e.g. "refs/remotes/origin/main" -> "main"
        return result.stdout.strip().split("/")[-1]
    return "main"


def collect_git_summary(repo_dir: Path) -> str:
    result = run_command(["git", "status", "--short"], cwd=repo_dir)
    return result.stdout.strip() or "No tracked changes."


def collect_git_diff_detail(repo_dir: Path) -> str:
    """Collect detailed diff of all changes (staged + unstaged + untracked)."""
    # Stage everything so we can get a unified diff
    run_command(["git", "add", "-A"], cwd=repo_dir)
    result = run_command(["git", "diff", "--cached", "--stat"], cwd=repo_dir)
    stat = result.stdout.strip()
    result = run_command(["git", "diff", "--cached"], cwd=repo_dir)
    diff = result.stdout.strip()
    return stat, diff


def collect_file_change_summary(repo_dir: Path) -> str:
    """Return a human-readable summary of changed files with their change type."""
    result = run_command(["git", "diff", "--cached", "--name-status"], cwd=repo_dir)
    if not result.stdout.strip():
        return "No file changes detected."
    lines = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status_code, filepath = parts
            status_map = {
                "A": "Added",
                "M": "Modified",
                "D": "Deleted",
                "R": "Renamed",
                "C": "Copied",
            }
            status_label = status_map.get(status_code[0], status_code)
            lines.append(f"- **{status_label}**: `{filepath}`")
    return "\n".join(lines) if lines else "No file changes detected."


def git_commit_and_push(repo_dir: Path, branch_name: str, commit_message: str) -> None:
    """Configure git, commit all changes, and push to the new branch."""
    run_command(["git", "config", "user.name", GIT_AUTHOR_NAME], cwd=repo_dir)
    run_command(["git", "config", "user.email", GIT_AUTHOR_EMAIL], cwd=repo_dir)

    result = run_command(["git", "checkout", "-b", branch_name], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create branch {branch_name}: {result.stderr.strip()}")

    # Files are already staged by collect_git_diff_detail
    result = run_command(["git", "commit", "-m", commit_message], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git commit failed: {result.stderr.strip()}")

    result = run_command(["git", "push", "-u", "origin", branch_name], cwd=repo_dir)
    if result.returncode != 0:
        raise RuntimeError(f"git push failed: {result.stderr.strip()}")


def create_pull_request(
    owner: str,
    repo_name: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> str:
    """Create a GitHub Pull Request via the API. Returns the PR URL."""
    url = f"https://api.github.com/repos/{owner}/{repo_name}/pulls"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
    }
    resp = requests.post(url, headers=headers, json=data, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"GitHub PR creation failed ({resp.status_code}): {resp.text}"
        )
    return resp.json()["html_url"]


def build_pr_body(
    payload: dict,
    file_change_summary: str,
    diff_stat: str,
    diff: str,
) -> str:
    """Build a detailed PR description."""
    task = payload["task"]
    requested_by = payload["requested_by"]
    job_id = payload["job_id"]
    branch = payload["branch"]
    created_at = payload.get("created_at", "N/A")

    # Truncate diff if too large for PR body (GitHub limit ~65536 chars)
    max_diff_len = 50000
    diff_display = diff
    if len(diff) > max_diff_len:
        diff_display = diff[:max_diff_len] + "\n\n... (diff truncated due to size)"

    return f"""## Overview

This PR was automatically generated by **OpenClaw** based on a task requested via Discord.

## Task Description

> {task}

## Request Details

| Field | Value |
|-------|-------|
| **Job ID** | `{job_id}` |
| **Requested by** | {requested_by} |
| **Base branch** | `{branch}` |
| **Requested at** | {created_at} |

## Changes

### Files Changed

{file_change_summary}

### Diff Stats

```
{diff_stat}
```

### Full Diff

<details>
<summary>Click to expand full diff</summary>

```diff
{diff_display}
```

</details>

---
*Generated by OpenClaw Bot*
"""


def build_commit_message(payload: dict, file_change_summary: str, diff_stat: str) -> str:
    """Build a detailed commit message."""
    task = payload["task"]
    job_id = payload["job_id"]
    requested_by = payload["requested_by"]

    return f"""openclaw: {task[:72]}

Task: {task}

Job ID: {job_id}
Requested by: {requested_by}

Changes:
{file_change_summary}

Stats:
{diff_stat}
"""


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
        clone_repo(payload["repo"], repo_dir)
        task_file = write_task_file(job_dir, payload)

        store_status(job_id, {"status": "executing"})
        result = run_command([OPENCLAW_RUNNER, str(repo_dir), str(task_file), job_id], cwd=job_dir)

        if result.returncode != 0:
            git_summary = collect_git_summary(repo_dir)
            store_status(
                job_id,
                {
                    "status": "failed",
                    "error": result.stderr.strip() or result.stdout.strip() or "OpenClaw runner failed",
                    "result_summary": git_summary,
                },
            )
            return

        # Collect detailed change information
        git_summary = collect_git_summary(repo_dir)
        diff_stat, diff = collect_git_diff_detail(repo_dir)
        file_change_summary = collect_file_change_summary(repo_dir)

        # If there are no changes, mark as completed without PR
        if not diff_stat:
            stdout = result.stdout.strip().splitlines()
            summary = stdout[-1] if stdout else "Runner completed."
            store_status(
                job_id,
                {
                    "status": "completed",
                    "result_summary": f"{summary}\nNo file changes to push.",
                },
            )
            return

        # Push changes and create PR
        store_status(job_id, {"status": "pushing"})
        branch_name = f"openclaw/{job_id[:8]}"
        base_branch = detect_default_branch(repo_dir)
        owner, repo_name = parse_github_owner_repo(payload["repo"])

        commit_message = build_commit_message(payload, file_change_summary, diff_stat)
        git_commit_and_push(repo_dir, branch_name, commit_message)

        store_status(job_id, {"status": "creating_pr"})
        pr_title = f"openclaw: {payload['task'][:60]}"
        pr_body = build_pr_body(payload, file_change_summary, diff_stat, diff)
        pr_url = create_pull_request(owner, repo_name, branch_name, base_branch, pr_title, pr_body)

        stdout = result.stdout.strip().splitlines()
        summary = stdout[-1] if stdout else "Runner completed."
        store_status(
            job_id,
            {
                "status": "completed",
                "result_summary": f"{summary}\n{git_summary}",
                "pr_url": pr_url,
                "branch": branch_name,
            },
        )
        print(f"Job {job_id}: PR created at {pr_url}")

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
