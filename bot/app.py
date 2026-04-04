import json
import os
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

ALLOWED_REPOS = {
    repo.strip()
    for repo in os.getenv("ALLOWED_REPOS", "").split(",")
    if repo.strip()
}

redis_client = Redis.from_url(REDIS_URL, decode_responses=True)


class OpenClawBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
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

    if repo not in ALLOWED_REPOS:
        allowed = "\n".join(sorted(ALLOWED_REPOS)) or "No repositories configured."
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

    if status.get("pr_url"):
        lines.append(f"PR: {status['pr_url']}")
    if status.get("result_summary"):
        lines.append(f"Summary: {status['result_summary']}")
    if status.get("error"):
        lines.append(f"Error: {status['error']}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.event
async def on_ready() -> None:
    print(f"Discord bot connected as {bot.user}")


bot.run(DISCORD_BOT_TOKEN)
