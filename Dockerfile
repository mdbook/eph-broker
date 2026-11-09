FROM python:3.12-slim

WORKDIR /opt/eph-broker
# Copy runtime code only. Never bake a checkout's site config, prompts or credentials into an image.
COPY *.py ./
COPY adapters/ ./adapters/
COPY trackers/ ./trackers/
COPY config/demo/broker.example.json ./config/demo/broker.example.json

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/eph-broker \
    BROKER_SITE_ENV=/dev/null \
    BROKER_CONFIG=config/demo/broker.example.json \
    EVENTS_LOG=/var/lib/eph-broker/events.jsonl \
    WORKTREES_ROOT=/tmp/eph-broker-worktrees

# COPY preserves checkout modes, including files created with umask 077.
RUN chmod -R a+rX /opt/eph-broker \
    && mkdir -p /var/lib/eph-broker && chown 10001:10001 /var/lib/eph-broker
USER 10001:10001
CMD ["python", "app.py"]
