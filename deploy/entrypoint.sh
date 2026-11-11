#!/bin/sh
# Seed the cache volume on first start (never overwrites existing files), then run the app.
set -e
mkdir -p /app/data/.cache
cp -n /app/data/.cache_seed/* /app/data/.cache/ 2>/dev/null || true
exec "$@"
