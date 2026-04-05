#!/usr/bin/env bash
set -eu

REPO_DIR="${1:?repo_dir is required}"
TASK_FILE="${2:?task_file is required}"
JOB_ID="${3:?job_id is required}"
CODEX_SANDBOX="${CODEX_SANDBOX:-danger-full-access}"
CODEX_MODEL="${CODEX_MODEL:-}"
CODEX_PROFILE="${CODEX_PROFILE:-}"

echo "Starting OpenClaw runner for job ${JOB_ID}"
echo "Repository: ${REPO_DIR}"
echo "Task file: ${TASK_FILE}"

cd "${REPO_DIR}"

ARGS=(exec -s "${CODEX_SANDBOX}" -C "${REPO_DIR}" -)

if [ -n "${CODEX_MODEL}" ]; then
  ARGS=(-m "${CODEX_MODEL}" "${ARGS[@]}")
fi

if [ -n "${CODEX_PROFILE}" ]; then
  ARGS=(-p "${CODEX_PROFILE}" "${ARGS[@]}")
fi

echo "Launching Codex CLI"
codex "${ARGS[@]}" < "${TASK_FILE}"
