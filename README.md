# HA Briefing Proxy 1.1.0 — ElevenLabs migration

This version generates and stores briefing **text**. Home Assistant's ElevenLabs
Custom TTS integration turns that text into speech using your saved `Test1`
profile. The proxy continues using your configured OpenAI model to write the
briefing, including weather, calendar events, the Oilers recap, and a fun ending.

## What changed

- Removed OpenAI speech generation and FFmpeg conversion.
- `POST /briefing` now returns the completed briefing text and its timestamp.
- The same record is atomically saved to `/data/latest_briefing.json` and returned
  by `GET /briefing/latest`.
- Every response includes `character_count`, including spaces and punctuation.
- `audio_url` is always null, including when reading a record saved by the old app.
- `/briefing_audio` now returns HTTP 410 explaining that Home Assistant generates
  the audio. Previously generated MP3 files are not deleted.
- Webhook delivery is optional and off by default, independently of any saved
  webhook URL. This gives the Generate script one playback path.
- The original joke/rap history stays in `/data/briefing_endings.json`.
- A failed calendar no longer tells the writing model to omit events from every
  other calendar. The prompt permits the supplied events from working calendars.
- Text-generation errors retain the original weather-only fallback behavior.
  The response reports `generation_mode: fallback` and a short warning code.
- A missing text-generation key returns HTTP 503. A failed latest-file save
  returns HTTP 500 and leaves the prior complete file intact.
- The writing request runs asynchronously, with a 45-second overall deadline,
  so the HTTP handler can normally finish within the HA REST command's 60 seconds.

## Update the existing local app/add-on

1. Make a backup of the existing HA Briefing Proxy app and its data.
2. Copy the updated `app.py`, `config.json`, `run.sh`, and `Dockerfile` into the
   same local `ha_briefing_proxy` source folder used for the existing installation.
   The package also includes the existing `requirements.txt` and `build.yaml`.
3. In Home Assistant, open the app/add-on store and select **Check for updates**.
   Update HA Briefing Proxy to **1.1.0**. For a local installation that exposes
   **Rebuild**, rebuild the image from the updated source and start the app.
   Updating/rebuilding the existing installation preserves its `/data` and options.
4. On its Configuration tab, keep your OpenAI key and writing-model choice.
   Set `send_webhook: false` and retain `nhl_team: EDM`.
5. Open `http://192.168.68.180:8000/health`. It should report `version: 1.1.0`,
   `audio_generation: home_assistant`, and `send_webhook: false`.

The slug remains `ha_briefing_proxy`. A restart alone does not copy changed source
files into an already-built Docker image. Update/rebuild the existing app.

The old `tts_model`, `tts_voice`, and `nabu_casa_url` options are accepted as
optional legacy fields so saved configurations still validate. Version 1.1.0
does not read them. ElevenLabs credentials, voice ID, and model live in the
Home Assistant ElevenLabs integration's `Test1` profile.

### Writing-model retirement

The uploaded app defaults to `gpt-5-mini`; this update preserves that configurable
choice. OpenAI lists **December 11, 2026** as its shutdown date and identifies
`gpt-5.6-terra` as the replacement. Set `openai_model` to a supported model before
that date. The documented replacement accepts Chat Completions, which this app
uses. Pricing and output may differ from the existing model.

If your installed app has a different saved `openai_model`, that saved setting
takes precedence over the source default; `/health` shows the effective model.

## Home Assistant scripts

The `home_assistant` folder contains:

- `morning_briefing_generate.yaml`: paste into the existing Generate script's
  **Edit in YAML** editor.
- `morning_briefing_speak.yaml`: paste into the existing Speak script's
  **Edit in YAML** editor.
- `scripts.yaml`: the same two entries for users who edit `/config/scripts.yaml`.
  Merge these entries into the existing file, preserving other scripts.
- `configuration_fragment.yaml`: the REST command and REST sensor configuration.
  Merge it into the corresponding existing configuration sections. The added
  sensor attributes expose the character count and fallback information.

Keep the existing script entity IDs used by your automations. The generator calls
`script.morning_briefing_speak`. The speech action uses:

```yaml
target:
  entity_id: tts.elevenlabs_custom_tts
data:
  media_player_entity_id: media_player.living_room_speaker
  options:
    voice_profile: "Test1"
```

Confirm these entity IDs against your ElevenLabs test action. Select your intended
Turbo model in `Test1`; a subscription does not select the model in the profile.

**Use these revised scripts in place of the earlier timestamp-polling version.**
Generate now validates and speaks the text in its own successful POST response.
It does not require a previous saved briefing, so it works on a fresh installation.
The REST sensor is refreshed for dashboards and later replay. A sensor refresh
failure does not substitute old text for the POST result.

Running Speak alone deliberately replays the latest saved text. Calling it with
`briefing_text` speaks that supplied text instead. Routine TTS caching is enabled.

## Test the installed system

First run a short speech test from Home Assistant's Actions tool:

```yaml
action: script.morning_briefing_speak
data:
  briefing_text: "Good morning, Matt. This is the updated ElevenLabs briefing test."
```

Then run your Generate script. A successful full run collects calendars, posts
weather/events, receives a completed briefing, refreshes the sensor, and speaks
the response through `Test1`. Inspect its trace and the proxy log if it stops.

Do not enable the legacy webhook unless another workflow needs it. If enabled,
the webhook receives the same completed record after the direct response. Its
failure is logged without discarding the generated briefing. An old webhook
automation that also speaks can duplicate the Generate script's announcement.

## API response

`POST /briefing` accepts the same `weather` and `events` fields as version 1.0.0.
Its successful JSON response has this shape (values below are illustrative):

```json
{
  "status": "ok",
  "text": "Good morning, everyone.",
  "audio_url": null,
  "updated_at": "2026-11-11T19:00:00+00:00",
  "character_count": 23,
  "generation_mode": "ai",
  "warning": null
}
```

`GET /briefing/latest` returns the same record without `status`. Before the first
successful generation, it returns empty text and a null timestamp. Character
count describes the exact text; provider billing can have other factors.

## Verification and limits

The package includes `tests/test_app.py`, a network-isolated regression suite
using the app's pinned dependencies. It mocks OpenAI generation, Oilers retrieval,
and webhook delivery; it does not spend API credits. Run it from this directory:

```bash
python -m unittest discover -s tests -v
```

The accompanying YAML was parsed and its calendar, response-validation, and
speech-text templates checked locally. This is not an execution inside Home
Assistant. Docker/Supervisor installation, live API access, the quality of model
output, and speaker playback still need the local checks above.

## References checked November 11, 2026

- [Home Assistant local app updates](https://contributors.home-assistant.io/apps/first-app/)
- [Home Assistant REST command responses](https://www.home-assistant.io/integrations/rest_command/)
- [ElevenLabs Custom TTS profiles](https://github.com/loryanstrant/HA-ElevenLabs-Custom-TTS)
- [OpenAI deprecations](https://developers.openai.com/api/docs/deprecations)
- [GPT-5.6 Terra model](https://developers.openai.com/api/docs/models/gpt-5.6-terra)
