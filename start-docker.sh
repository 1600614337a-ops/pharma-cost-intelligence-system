#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
docker compose up --build -d
printf '%s\n' '正在启动，请稍后访问 http://127.0.0.1:8080'
