#!/usr/bin/with-contenv bashio
# Supervisor has already written /data/options.json from the add-on configuration.
# The app reads it directly, so nothing secret needs to pass through the environment.

LOG_LEVEL=$(bashio::config 'log_level')
bashio::log.info "Starting DVR Recordings on :8099 (ingress) …"

# Bind 0.0.0.0: Supervisor's ingress proxy connects from 172.30.32.2.
# Binding 127.0.0.1 here would make the panel return 502.
exec python3 -m uvicorn hikvision_recordings.app.main:app \
  --host 0.0.0.0 \
  --port 8099 \
  --log-level "${LOG_LEVEL}" \
  --no-access-log
