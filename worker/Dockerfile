# SPDX-License-Identifier: GPL-3.0-or-later
# Build context: repository root (see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_FILE=/config/config.yml \
    STATE_DIR=/state \
    BACKUP_DIR=/backups

# cron for scheduling, git + ca-certificates for diagnostics, tini as PID 1.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY scripts/ /app/scripts/
COPY tests/ /app/tests/
COPY worker/entrypoint.sh /entrypoint.sh
COPY worker/crontab /app/crontab.template

RUN chmod 0755 /entrypoint.sh /app/scripts/restore-drill.sh \
    && mkdir -p /state /backups /config

VOLUME ["/state", "/backups"]

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
