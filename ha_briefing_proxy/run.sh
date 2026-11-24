#!/usr/bin/with-contenv bashio
set -e

# Map add-on config -> env vars expected by app.py
export OPENAI_API_KEY="$(bashio::config 'openai_api_key')"
export MODEL_NAME="$(bashio::config 'openai_model')"
export OPENAI_MODEL="$(bashio::config 'openai_model')"   # keeps backward compatibility
export HA_WEBHOOK_URL="$(bashio::config 'ha_webhook_url')"
export NHL_TEAM="$(bashio::config 'nhl_team')"

# Existing installations may not have saved the new option yet.
# A saved webhook URL alone must not enable a second playback path.
export SEND_WEBHOOK="false"
if bashio::config.has_value 'send_webhook' && bashio::config.true 'send_webhook'; then
  export SEND_WEBHOOK="true"
fi

# Hard fail early if key missing (prevents confusing "has_key:false")
if [ -z "${OPENAI_API_KEY}" ]; then
  bashio::log.error "OPENAI_API_KEY is empty. Set 'openai_api_key' in the add-on configuration."
  exit 1
fi

bashio::log.info "Starting HA Briefing Proxy 1.1.0 on port 8000 (text only)..."
exec uvicorn app:app --host 0.0.0.0 --port 8000
