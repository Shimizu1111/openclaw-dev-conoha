import json
import os
import re
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
from redis import Redis


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


REDIS_URL = os.environ["REDIS_URL"]
JOB_QUEUE_KEY = os.getenv("JOB_QUEUE_KEY", "openclaw:jobs")
JOB_STATUS_PREFIX = os.getenv("JOB_STATUS_PREFIX", "openclaw:job:")
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
DEFAULT_BRANCH = os.getenv("DEFAULT_BRANCH", "main")
SYNC_COMMANDS = os.getenv("SYNC_COMMANDS", "true").lower() == "true"

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


CHAT_CHANNEL_IDS = {
    int(ch.strip())
    for ch in os.getenv("CHAT_CHANNEL_IDS", "").split(",")
    if ch.strip()
}
CHAT_DEFAULT_REPO = os.getenv("CHAT_DEFAULT_REPO", "")
CHAT_POLL_INTERVAL = int(os.getenv("CHAT_POLL_INTERVAL", "3"))


class OpenClawBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        guild = discord.Object(id=DISCORD_GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        if SYNC_COMMANDS:
            await self.tree.sync(guild=guild)


bot = OpenClawBot()


def job_key(job_id: str) -> str:
    return f"{JOB_STATUS_PREFIX}{job_id}"


def enqueue_job(payload: dict) -> None:
    redis_client.rpush(JOB_QUEUE_KEY, json.dumps(payload))


def store_status(job_id: str, fields: dict) -> None:
    redis_client.hset(job_key(job_id), mapping=fields)


@bot.tree.command(
    name="dev-task",
    description="Queue a development request for OpenClaw.",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
@app_commands.describe(
    repo="Allowed repository URL",
    task="Development task to send to OpenClaw",
    branch="Target branch",
)
async def dev_task(
    interaction: discord.Interaction,
    repo: str,
    task: str,
    branch: str = DEFAULT_BRANCH,
) -> None:
    await interaction.response.defer(ephemeral=True)

    if not redis_client.sismember(ALLOWED_REPOS_KEY, repo):
        allowed = "\n".join(sorted(redis_client.smembers(ALLOWED_REPOS_KEY))) or "No repositories configured."
        await interaction.followup.send(
            f"`repo` is not allowed.\nAllowed repos:\n{allowed}",
            ephemeral=True,
        )
        return

    job_id = str(uuid.uuid4())
    payload = {
        "job_id": job_id,
        "repo": repo,
        "branch": branch,
        "task": task,
        "requested_by": str(interaction.user),
        "requested_by_id": str(interaction.user.id),
        "created_at": utc_now(),
    }

    store_status(
        job_id,
        {
            "status": "queued",
            "repo": repo,
            "branch": branch,
            "task": task,
            "requested_by": str(interaction.user),
            "created_at": payload["created_at"],
            "updated_at": payload["created_at"],
        },
    )
    enqueue_job(payload)

    await interaction.followup.send(
        "\n".join(
            [
                f"Queued job `{job_id}`",
                f"Repo: `{repo}`",
                f"Branch: `{branch}`",
                f"Task: {task}",
            ]
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="job-status",
    description="Check an OpenClaw job status.",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
@app_commands.describe(job_id="Job ID returned by /dev-task")
async def job_status(interaction: discord.Interaction, job_id: str) -> None:
    await interaction.response.defer(ephemeral=True)

    status = redis_client.hgetall(job_key(job_id))
    if not status:
        await interaction.followup.send(f"Job `{job_id}` was not found.", ephemeral=True)
        return

    lines = [
        f"Job: `{job_id}`",
        f"Status: `{status.get('status', 'unknown')}`",
        f"Repo: `{status.get('repo', '-')}`",
        f"Branch: `{status.get('branch', '-')}`",
        f"Requested by: `{status.get('requested_by', '-')}`",
        f"Updated at: `{status.get('updated_at', '-')}`",
    ]

    if status.get("repo_url"):
        lines.append(f"Repo: {status['repo_url']}")
    if status.get("pr_url"):
        lines.append(f"PR: {status['pr_url']}")
    if status.get("result_summary"):
        lines.append(f"Summary: {status['result_summary']}")
    if status.get("error"):
        lines.append(f"Error: {status['error']}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


GITHUB_DEFAULT_ORG = os.getenv("GITHUB_DEFAULT_ORG", "")

REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


@bot.tree.command(
    name="create-project",
    description="Create a new GitHub repository and optionally run an initial task.",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
@app_commands.describe(
    name="Repository name (e.g., my-new-project)",
    description="Short description for the repository",
    task="Optional initial development task for OpenClaw to execute",
    private="Whether the repo should be private (default: False)",
    org="GitHub org or user (defaults to GITHUB_DEFAULT_ORG)",
)
async def create_project(
    interaction: discord.Interaction,
    name: str,
    description: str = "",
    task: str = "",
    private: bool = False,
    org: str = "",
) -> None:
    await interaction.response.defer(ephemeral=True)

    if not REPO_NAME_RE.match(name):
        await interaction.followup.send(
            "Invalid repo name. Use only letters, numbers, hyphens, dots, and underscores.",
            ephemeral=True,
        )
        return

    target_org = org or GITHUB_DEFAULT_ORG
    if not target_org:
        await interaction.followup.send(
            "No GitHub org/user specified. Set `GITHUB_DEFAULT_ORG` env var or provide `org` parameter.",
            ephemeral=True,
        )
        return

    job_id = str(uuid.uuid4())
    payload = {
        "job_id": job_id,
        "type": "create_project",
        "name": name,
        "description": description,
        "task": task,
        "private": private,
        "org": target_org,
        "requested_by": str(interaction.user),
        "requested_by_id": str(interaction.user.id),
        "created_at": utc_now(),
    }

    store_status(
        job_id,
        {
            "status": "queued",
            "repo": f"(new) {target_org}/{name}",
            "task": task or "(create repo only)",
            "requested_by": str(interaction.user),
            "created_at": payload["created_at"],
            "updated_at": payload["created_at"],
        },
    )
    enqueue_job(payload)

    lines = [
        f"Queued project creation `{job_id}`",
        f"Repo: `{target_org}/{name}`",
        f"Private: `{private}`",
    ]
    if task:
        lines.append(f"Initial task: {task}")
    else:
        lines.append("No initial task — repo will be created empty.")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="add-reference",
    description="Add a cross-repo reference to a project's openclaw.yml.",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
@app_commands.describe(
    repo="Target repository URL (must be allowed)",
    ref_repo="Referenced repository URL (must be allowed)",
    ref_path="Path within referenced repo (e.g., src/components)",
    mount_as="Where to mount in target repo (e.g., ./vendor/shared-components)",
)
async def add_reference(
    interaction: discord.Interaction,
    repo: str,
    ref_repo: str,
    ref_path: str,
    mount_as: str,
) -> None:
    await interaction.response.defer(ephemeral=True)

    if not redis_client.sismember(ALLOWED_REPOS_KEY, repo):
        await interaction.followup.send(f"Target repo `{repo}` is not in the allowed list.", ephemeral=True)
        return

    if not redis_client.sismember(ALLOWED_REPOS_KEY, ref_repo):
        await interaction.followup.send(f"Referenced repo `{ref_repo}` is not in the allowed list.", ephemeral=True)
        return

    job_id = str(uuid.uuid4())
    payload = {
        "job_id": job_id,
        "type": "add_reference",
        "repo": repo,
        "ref_repo": ref_repo,
        "ref_path": ref_path,
        "mount_as": mount_as,
        "requested_by": str(interaction.user),
        "requested_by_id": str(interaction.user.id),
        "created_at": utc_now(),
    }

    store_status(
        job_id,
        {
            "status": "queued",
            "repo": repo,
            "task": f"Add reference: {ref_repo} ({ref_path}) -> {mount_as}",
            "requested_by": str(interaction.user),
            "created_at": payload["created_at"],
            "updated_at": payload["created_at"],
        },
    )
    enqueue_job(payload)

    await interaction.followup.send(
        "\n".join([
            f"Queued reference addition `{job_id}`",
            f"Target: `{repo}`",
            f"Reference: `{ref_repo}` path `{ref_path}`",
            f"Mount as: `{mount_as}`",
        ]),
        ephemeral=True,
    )


@bot.tree.command(
    name="list-repos",
    description="List all allowed repositories.",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
async def list_repos(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    repos = sorted(redis_client.smembers(ALLOWED_REPOS_KEY))
    if repos:
        lines = [f"**Allowed repositories ({len(repos)}):**"]
        for r in repos:
            lines.append(f"- `{r}`")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    else:
        await interaction.followup.send("No repositories configured.", ephemeral=True)


PROJECTS_KEY = "openclaw:projects"


@bot.tree.command(
    name="list-projects",
    description="List all registered projects and their directories.",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
async def list_projects(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    projects = redis_client.hgetall(PROJECTS_KEY)
    if projects:
        lines = [f"**Projects ({len(projects)}):**"]
        for name, path in sorted(projects.items()):
            lines.append(f"- **{name}**: `{path}`")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    else:
        await interaction.followup.send("No projects registered. Use `/register-project` to add one.", ephemeral=True)


@bot.tree.command(
    name="register-project",
    description="Register a project name and its directory path.",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
@app_commands.describe(
    name="Project name (e.g., openclaw-dev-conoha)",
    path="Directory path on the server (e.g., /root/apps/openclaw-dev-conoha)",
)
async def register_project(interaction: discord.Interaction, name: str, path: str) -> None:
    await interaction.response.defer(ephemeral=True)
    redis_client.hset(PROJECTS_KEY, name, path)
    await interaction.followup.send(f"Registered project **{name}** at `{path}`", ephemeral=True)


@bot.tree.command(
    name="unregister-project",
    description="Remove a project from the list.",
    guild=discord.Object(id=DISCORD_GUILD_ID),
)
@app_commands.describe(name="Project name to remove")
async def unregister_project(interaction: discord.Interaction, name: str) -> None:
    await interaction.response.defer(ephemeral=True)
    removed = redis_client.hdel(PROJECTS_KEY, name)
    if removed:
        await interaction.followup.send(f"Removed project **{name}**.", ephemeral=True)
    else:
        await interaction.followup.send(f"Project **{name}** not found.", ephemeral=True)


@bot.event
async def on_ready() -> None:
    print(f"Discord bot connected as {bot.user}")
    if CHAT_CHANNEL_IDS:
        print(f"Chat channels: {CHAT_CHANNEL_IDS}")


# ---------------------------------------------------------------------------
# Chat mode: messages in designated channels are forwarded to codex
# ---------------------------------------------------------------------------

import asyncio


def _split_message(text: str, limit: int = 1990) -> list[str]:
    """Split text into chunks that fit within Discord's 2000-char limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to split at a newline
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


async def _poll_and_reply(channel, message, job_id: str) -> None:
    """Poll Redis for job completion, then reply with the result."""
    key = job_key(job_id)
    try:
        while True:
            await asyncio.sleep(CHAT_POLL_INTERVAL)
            status = redis_client.hgetall(key)
            if not status:
                break
            job_status = status.get("status", "")
            if job_status in ("completed", "failed"):
                break

        status = redis_client.hgetall(key)
        if not status:
            await message.reply("Job not found.")
            return

        if status.get("status") == "failed":
            error = status.get("error", "Unknown error")
            await message.reply(f"Error: {error}")
            return

        # Send the chat response
        response = status.get("chat_response", "")
        if not response:
            response = status.get("result_summary", "No response.")

        chunks = _split_message(response)
        for chunk in chunks:
            await message.reply(chunk)

    except Exception as exc:
        await message.reply(f"Error polling result: {exc}")


def _detect_repo_from_message(content: str) -> str | None:
    """Try to find a matching allowed repo URL from keywords in the message.

    Checks against:
    1. Allowed repos in Redis (matches repo name portion of the URL)
    2. Registered projects in Redis (matches project name)
    """
    lower = content.lower()

    # Check allowed repos: extract repo name from URL and match
    allowed = redis_client.smembers(ALLOWED_REPOS_KEY)
    for repo_url in allowed:
        # Extract repo name from URL like https://github.com/owner/repo-name.git
        parts = repo_url.rstrip("/").rstrip(".git").split("/")
        if parts:
            repo_name = parts[-1].lower()
            if repo_name in lower:
                return repo_url

    # Check registered projects: project name -> look up if an allowed repo matches
    projects = redis_client.hgetall(PROJECTS_KEY)
    for proj_name, proj_path in projects.items():
        if proj_name.lower() in lower:
            # See if there's an allowed repo matching this project name
            for repo_url in allowed:
                if proj_name.lower() in repo_url.lower():
                    return repo_url

    return None


def _try_local_answer(content: str) -> str | None:
    """Check if the question can be answered from Redis data (projects, repos)."""
    lower = content.lower()

    # Project path queries
    path_keywords = ["path", "パス", "どこ", "ディレクトリ", "フォルダ", "場所"]
    if any(kw in lower for kw in path_keywords):
        projects = redis_client.hgetall(PROJECTS_KEY)
        matched = []
        for name, url in projects.items():
            if name.lower() in lower:
                matched.append((name, url))
        if matched:
            lines = []
            for name, url in matched:
                lines.append(f"**{name}**: {url}")
            return "\n".join(lines)

    # List all projects
    list_keywords = ["一覧", "リスト", "list", "全部", "すべて", "プロジェクト"]
    if sum(1 for kw in list_keywords if kw in lower) >= 2:
        projects = redis_client.hgetall(PROJECTS_KEY)
        if projects:
            lines = [f"**Projects ({len(projects)}):**"]
            for name, url in sorted(projects.items()):
                lines.append(f"- **{name}**: {url}")
            return "\n".join(lines)

    return None


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore own messages
    if message.author == bot.user:
        return

    # Only respond in designated chat channels
    if not CHAT_CHANNEL_IDS or message.channel.id not in CHAT_CHANNEL_IDS:
        return

    # Ignore empty messages
    content = message.content.strip()
    if not content:
        return

    # Try to answer from local data (projects, repos) before calling codex
    answer = _try_local_answer(content)
    if answer:
        await message.reply(answer)
        return

    # Check if a repo is specified: "repo:URL message" or use default
    repo = CHAT_DEFAULT_REPO
    if content.lower().startswith("repo:"):
        parts = content.split(None, 1)
        repo = parts[0][5:]  # strip "repo:" prefix
        content = parts[1] if len(parts) > 1 else ""
        if not content:
            await message.reply("Please provide a message after the repo URL.")
            return

    # Auto-detect repo name from message if not explicitly specified
    if not repo:
        detected = _detect_repo_from_message(content)
        if detected:
            repo = detected

    # Show typing indicator
    async with message.channel.typing():
        job_id = str(uuid.uuid4())
        payload = {
            "job_id": job_id,
            "type": "chat",
            "task": content,
            "requested_by": str(message.author),
            "requested_by_id": str(message.author.id),
            "created_at": utc_now(),
        }
        if repo:
            payload["repo"] = repo

        store_status(
            job_id,
            {
                "status": "queued",
                "repo": repo or "(no repo)",
                "task": content,
                "requested_by": str(message.author),
                "created_at": payload["created_at"],
                "updated_at": payload["created_at"],
            },
        )
        enqueue_job(payload)

    # Poll in background and reply when done
    bot.loop.create_task(_poll_and_reply(message.channel, message, job_id))


bot.run(DISCORD_BOT_TOKEN)
