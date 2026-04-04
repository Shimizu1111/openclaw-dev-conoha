import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone

import requests as http_requests
from fastapi import FastAPI, Header, HTTPException, Request
from redis import Redis

app = FastAPI()

REDIS_URL = os.environ["REDIS_URL"]
JOB_QUEUE_KEY = os.getenv("JOB_QUEUE_KEY", "openclaw:jobs")
JOB_STATUS_PREFIX = os.getenv("JOB_STATUS_PREFIX", "openclaw:job:")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
TRIGGER_KEYWORD = os.getenv("OPENCLAW_TRIGGER", "@openclaw")

ALLOWED_REPOS_KEY = "openclaw:allowed_repos"

_INITIAL_ALLOWED_REPOS = {
    repo.strip()
    for repo in os.getenv("ALLOWED_REPOS", "").split(",")
    if repo.strip()
}

redis_client = Redis.from_url(REDIS_URL, decode_responses=True)

# Seed allowed repos into Redis set (additive, idempotent)
for _repo in _INITIAL_ALLOWED_REPOS:
    redis_client.sadd(ALLOWED_REPOS_KEY, _repo)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_signature(payload_body: bytes, signature: str | None) -> None:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not GITHUB_WEBHOOK_SECRET:
        return  # skip verification if no secret configured
    if not signature:
        raise HTTPException(status_code=403, detail="Missing signature")
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")


def fetch_pr_head_branch(owner: str, repo: str, pr_number: int) -> str:
    """Get the head branch name of a PR via GitHub API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    resp = http_requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch PR info: {resp.status_code}",
        )
    return resp.json()["head"]["ref"]


def job_key(job_id: str) -> str:
    return f"{JOB_STATUS_PREFIX}{job_id}"


def enqueue_job(payload: dict) -> None:
    redis_client.hset(
        job_key(payload["job_id"]),
        mapping={
            "status": "queued",
            "repo": payload["repo"],
            "branch": payload.get("branch", ""),
            "task": payload["task"],
            "requested_by": payload["requested_by"],
            "created_at": payload["created_at"],
            "updated_at": payload["created_at"],
        },
    )
    redis_client.rpush(JOB_QUEUE_KEY, json.dumps(payload))


@app.post("/github/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(None),
    x_hub_signature_256: str = Header(None),
):
    body = await request.body()
    verify_signature(body, x_hub_signature_256)

    # Only handle issue_comment events (PR comments are delivered this way)
    if x_github_event != "issue_comment":
        return {"status": "ignored", "reason": "not issue_comment event"}

    payload = json.loads(body)

    # Only new comments
    if payload.get("action") != "created":
        return {"status": "ignored", "reason": "not a new comment"}

    # Only PR comments (not regular issue comments)
    if not payload.get("issue", {}).get("pull_request"):
        return {"status": "ignored", "reason": "not a PR comment"}

    comment_body = payload["comment"]["body"].strip()

    # Check trigger keyword
    if not comment_body.lower().startswith(TRIGGER_KEYWORD.lower()):
        return {"status": "ignored", "reason": "no trigger keyword"}

    # Extract task from comment (everything after the trigger keyword)
    task = comment_body[len(TRIGGER_KEYWORD) :].strip()
    if not task:
        return {"status": "ignored", "reason": "empty task"}

    # Extract repo info
    repo_full_name = payload["repository"]["full_name"]  # owner/repo
    clone_url = payload["repository"]["clone_url"]  # https://github.com/owner/repo.git
    owner, repo_name = repo_full_name.split("/", 1)
    pr_number = payload["issue"]["number"]
    commenter = payload["comment"]["user"]["login"]

    # Validate repo is allowed
    if not redis_client.sismember(ALLOWED_REPOS_KEY, clone_url):
        return {"status": "rejected", "reason": "repo not allowed"}

    # Fetch the PR's head branch
    head_branch = fetch_pr_head_branch(owner, repo_name, pr_number)

    job_id = str(uuid.uuid4())
    job_payload = {
        "job_id": job_id,
        "type": "pr_comment",
        "repo": clone_url,
        "branch": head_branch,
        "task": task,
        "requested_by": f"github:{commenter}",
        "created_at": utc_now(),
        "pr_number": pr_number,
        "pr_owner": owner,
        "pr_repo_name": repo_name,
    }

    enqueue_job(job_payload)

    return {"status": "queued", "job_id": job_id}


@app.get("/health")
async def health():
    return {"status": "ok"}
