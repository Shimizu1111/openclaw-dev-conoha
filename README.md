hello world2

ConoHa VPS 上に OpenClaw 実行基盤を置き、Discord から開発依頼を送れるようにするための MVP 構成です。

## Overview

このリポジトリは次の 3 要素で構成します。

- `bot`: Discord Slash Command を受け付け、Redis にジョブを投入する
- `worker`: Redis からジョブを受け取り、対象リポジトリを clone して OpenClaw を実行する
- `redis`: ジョブキューとジョブ状態管理

MVP では次の Slash Command を想定しています。

- `/dev-task`: 開発依頼を投入
- `/job-status`: ジョブ状態を確認

## Architecture

```text
Discord
  -> discord-bot
  -> Redis queue
  -> worker
  -> git clone / workspace
  -> OpenClaw runner
  -> Redis status
  -> Discord /job-status
```

## Directory Layout

```text
.
├── bot/
│   ├── app.py
│   ├── Dockerfile
│   └── requirements.txt
├── worker/
│   ├── app.py
│   ├── Dockerfile
│   ├── requirements.txt
│   └── run_openclaw.sh
├── docker-compose.yml
└── .env.example
```

## Prerequisites

- ConoHa VPS
- Docker
- Docker Compose Plugin
- Discord Application / Bot Token
- GitHub token
- OpenClaw を実行できる CLI または runner script

## Environment Variables

`.env.example` を `.env` にコピーして設定します。

重要な項目:

- `DISCORD_BOT_TOKEN`
- `DISCORD_GUILD_ID`
- `ALLOWED_REPOS`
- `GITHUB_TOKEN`
- `OPENCLAW_RUNNER`
- `CODEX_HOST_CONFIG_DIR`

`ALLOWED_REPOS` は Discord 経由で扱って良いリポジトリを制限します。

例:

```env
ALLOWED_REPOS=https://github.com/your-org/openclaw-dev-conoha.git,https://github.com/your-org/another-repo.git
```

MVP では HTTPS URL と `GITHUB_TOKEN` を使う方が簡単です。SSH を使う場合は worker コンテナへ SSH 鍵を別途 mount してください。

## Run Locally

```bash
cp .env.example .env
docker compose up --build -d
```

初回はホスト側で `codex login` を済ませておきます。

## Discord Commands

### `/dev-task`

- `repo`: 許可済みリポジトリ
- `task`: OpenClaw に渡す依頼内容
- `branch`: 作業対象ブランチ。省略時は `main`

### `/job-status`

- `job_id`: ジョブ ID

## OpenClaw Integration

`worker/run_openclaw.sh` を OpenClaw 実行用の薄いラッパとして使います。
現在は `codex exec` を呼び出す実装にしてあります。

このスクリプトには次の引数が渡されます。

1. `repo_dir`
2. `task_file`
3. `job_id`

`task_file` には OpenClaw へ渡す依頼文が保存されています。

`worker` コンテナではホストの Codex 設定ディレクトリを `/root/.codex` に mount して認証情報を参照します。

## ConoHa VPS Setup

### 1. Firewall

公開するのは原則として次だけにします。

- `22/tcp`: SSH
- `80/tcp`: 任意
- `443/tcp`: 任意

Redis や内部 API は外に公開しません。

### 2. Docker Install

Ubuntu 系の例:

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### 3. Deploy

```bash
git clone <this-repo>
cd openclaw-dev-conoha
cp .env.example .env
docker compose up --build -d
```

## Recommended Next Steps

- `worker/run_openclaw.sh` を実際の OpenClaw CLI に接続する
- GitHub PR 作成処理を worker に追加する
- Discord の特定ロールだけコマンド実行可能にする
- 実行ログを永続化する
- ドメインを付与して HTTPS を有効にする

<!-- webhook smoke test -->
