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

export LOG_LEVEL

bashio::log.info "Starting DVR Recordings (ingress :8099, optional links :8100) …"

# One process, two uvicorn.Server instances — see hikvision_recordings.app.serve.
# Bind 0.0.0.0 inside serve.py: Supervisor's ingress proxy connects from 172.30.32.2.
exec python3 -m hikvision_recordings.app.serve
