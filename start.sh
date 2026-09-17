#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"
mkdir -p data "${OPENSHELF_DOWNLOADS:-./downloads}"
OPENSHELF_UID=${OPENSHELF_UID:-$(id -u)}
OPENSHELF_GID=${OPENSHELF_GID:-$(id -g)}
export OPENSHELF_UID OPENSHELF_GID
exec docker compose up --build -d
