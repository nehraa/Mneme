#!/bin/bash
# Auto-reload MNEME dev server to pick up newly-indexed chunks
# Called every 5 min by cron
curl -s -m 5 -X POST "http://localhost:8080/dev/reload" \
  -H "X-Mneme-Key: local-dev-key" > /tmp/mneme_reload.log 2>&1
