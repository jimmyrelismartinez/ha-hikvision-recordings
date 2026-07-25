#!/usr/bin/with-contenv bashio
# Supervisor has already written /data/options.json from the add-on configuration.
# The app reads it directly, so nothing secret needs to pass through the environment.

LOG_LEVEL=$(bashio::config 'log_level')

# uvicorn only accepts critical|error|warning|info|debug|trace — no "fatal".
# config.yaml's schema no longer offers "fatal", but Supervisor persists
# whatever was saved in /data/options.json, so an add-on that was configured
# with "fatal" before this fix keeps that value across an update. Translate
# it defensively, and fall back to "info" for anything else unexpected
# rather than ever handing uvicorn an empty --log-level.
case "${LOG_LEVEL}" in
  fatal) LOG_LEVEL="critical" ;;
  trace|debug|info|warning|error|critical) ;;
  *) LOG_LEVEL="info" ;;
esac

bashio::log.info "Starting DVR Recordings on :8099 (ingress) …"

# Bind 0.0.0.0: Supervisor's ingress proxy connects from 172.30.32.2.
# Binding 127.0.0.1 here would make the panel return 502.
exec python3 -m uvicorn hikvision_recordings.app.main:app \
  --host 0.0.0.0 \
  --port 8099 \
  --log-level "${LOG_LEVEL}" \
  --no-access-log
