# openclaw-dev-conoha

Discord から開発タスクを投げると、ConoHa VPS 上で Codex (OpenClaw runner) が実行され、GitHub に PR を作成するパイプライン。

## アーキテクチャ

```
Discord ─> bot (Slash Command / Chat) ─> Redis queue ─> worker ─> Codex 実行 ─> git push & PR 作成
                                                                      ↑
GitHub Webhook (PR comment @openclaw) ─> webhook (FastAPI:9000) ──────┘
```

## サービス構成 (docker-compose.yml)

| サービス | 役割 | 備考 |
|---------|------|------|
| `redis` | ジョブキュー & 状態管理 | Redis 7 Alpine |
| `bot` | Discord Bot (slash commands + chat) | discord.py |
| `worker` | ジョブ実行 (clone → Codex → push → PR) | privileged, SSHキーマウント |
| `webhook` | GitHub PR コメントからジョブ投入 | FastAPI, port 9000 |

## 主要ファイル

- `bot/app.py` — Discord Bot 本体。Slash Commands (`/dev-task`, `/job-status`, `/create-project`, `/add-reference`, `/claude-mobile`, `/ai-secretary` 等) と Chat モード
- `worker/app.py` — ジョブ実行ループ。clone → Codex → diff → push → PR 作成。`openclaw.yml` によるクロスリポジトリ参照にも対応
- `webhook/app.py` — GitHub Webhook 受信 (PR コメントで `@openclaw` トリガー)
- `worker/run_openclaw.sh` — Codex 実行ラッパースクリプト

## デプロイ先

ConoHa VPS `private-n8n-conoha` にデプロイ済み。

- SSH: `ssh private-n8n-conoha` (in ~/.ssh/config)
- VPS上のパス: `/root/apps/openclaw-dev-conoha`
- デプロイ手順: `git push` → VPS で `cd /root/apps/openclaw-dev-conoha && git pull && docker compose build && docker compose up -d`

## 環境変数 (.env)

`DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `ALLOWED_REPOS`, `GITHUB_TOKEN`, `REDIS_URL`, `OPENCLAW_RUNNER`, `CODEX_HOST_CONFIG_DIR`, `GITHUB_DEFAULT_ORG`, `GITHUB_WEBHOOK_SECRET`, `CHAT_CHANNEL_IDS`, `CHAT_DEFAULT_REPO` 等。

## 開発ルール

- VPS への操作は `ssh private-n8n-conoha` で接続して行う
- コミットメッセージは日本語
