# Sendblue for AstrBot

A standalone AstrBot plugin for direct iMessage/SMS text conversations through a
Sendblue line. Install it from GitHub; no AstrBot core patch is required.

The plugin registers a normal platform adapter and uses AstrBot's event queue,
agent runner, sessions and reply path. It owns a small HTTP webhook listener;
Sendblue credentials stay in plugin configuration. It does not register model tools
or change AstrBot's command permissions.

## Requirements

- AstrBot **4.29.0-beta.1 or newer, below 5.0**. The tested upstream revision is
  [`42972e9`](https://github.com/AstrBotDevs/AstrBot/commit/42972e932a64f01d96e2e0708579b0f79dd6f792).
- A configured AstrBot model provider. Messaging credentials do not provide LLM access.
- A Sendblue account/line with **receive webhooks and API replies enabled**, an
  allowed recipient, and a public HTTPS endpoint forwarding to the plugin listener.

**Plan caveat:** Sendblue's [pricing page](https://www.sendblue.com/pricing) says the
free sandbox has no webhooks/outbound messaging, while its
[quickstart](https://docs.sendblue.com/getting-started/quickstart/) documents free
shared-line sends to verified contacts. These public descriptions conflict. Do not
assume a free account can run this webhook integration: confirm the account's actual
webhook/reply entitlement first. No live account or handset has been used to validate
this plugin. Local integration tests use synthetic credentials and an HTTP receiver.

## Install and configure

1. In AstrBot, open **Extensions → Plugins → Install**, select installation from a
   GitHub repository URL, and enter:

   ```text
   https://github.com/lookevink/astrbot_plugin_sendblue
   ```

   Alternatively, clone that URL into `AstrBot/data/plugins/astrbot_plugin_sendblue`,
   install `requirements.txt` with AstrBot's Python environment, and restart AstrBot.
   Install only one copy. Installing with empty settings opens no network listener.
2. Open the plugin's configuration and fill in:

   | Setting | Value |
   | --- | --- |
   | API key ID | Sendblue API key ID |
   | API secret key | Sendblue API secret |
   | Webhook shared secret | A fresh random value, e.g. `openssl rand -hex 32` |
   | Assigned Sendblue line | Assigned `+E164` line, not your personal phone |
   | Allowed sender phones | A list of explicit personal `+E164` numbers |

   Empty allowlists deny everyone. `*` explicitly admits any sender. Provider contact
   verification and AstrBot's own user/command permissions are separate checks.
   Secrets are masked in the WebUI, **not encrypted on disk**: protect the files in
   `data/config`, keep backups private, and never commit credentials.
3. Save plugin settings. Open **Bots**, create a **Sendblue iMessage / SMS** adapter,
   and choose a unique bot ID. The template is disabled by default. The listener
   defaults to `127.0.0.1:6198`; enable the bot only after configuration is complete.
   One configured Sendblue account/line is shared by this plugin's adapters; use one
   bot for that line to avoid duplicate receive registrations.
4. Forward public HTTPS **`/sendblue/webhook`** to that listener. For a local
   prototype, `ngrok http 6198` provides an HTTPS origin; append `/sendblue/webhook`.
   A permanent reverse proxy can forward the same path. Expose this listener, not
   the dashboard, and retain an upstream 64 KiB request-body limit as defense in depth.

   With Docker, set the bot's `listen_host` to `0.0.0.0` inside the container and
   publish `127.0.0.1:6198:6198` on the host, then point the host tunnel/proxy at 6198.
   A tunnel in another container can target the AstrBot container's port directly.
5. Add a Sendblue **receive** webhook with that HTTPS URL, the same webhook secret,
   and the assigned line filter. The API uses `POST /api/account/webhooks` with
   `sb-api-key-id` and `sb-api-secret-key` headers:

   ```json
   {"webhooks":{"receive":[{"url":"https://YOUR_HOST/sendblue/webhook","secret":"YOUR_RANDOM_SECRET","sendblue_numbers":["+ASSIGNED_LINE"]}]}}
   ```

   POST appends registrations; inspect existing entries first. Do not use a replace
   operation that removes other integrations' webhooks. Sendblue sends the secret
   in the `sb-signing-secret` header; this is a shared-secret check, not HMAC.
6. Ensure AstrBot has a model provider and allows the intended sender/session.
   Text the assigned line from the allowed phone: `Remember cobalt`. Wait for a
   reply **on the handset**, then ask `What word did I ask you to remember?`.
   Confirm the second reply and conversation history. `QUEUED` only proves provider
   acceptance; it does not prove delivery to a phone.

### Creating a Sendblue account

If you need credentials, the documented CLI phone-verification flow is:

```sh
npx --yes @sendblue/cli@0.10.0 setup --phone +YOUR_PERSONAL_NUMBER
npx --yes @sendblue/cli@0.10.0 setup --check
```

Send the displayed verification phrase from your own phone; exit code 3 on the
check means verification is pending. `~/.sendblue/credentials.json` contains
`apiKey`, `apiSecret`, and `assignedNumber`. Do not paste that file into chat.
For another contact, run `npx --yes @sendblue/cli@0.10.0 add-contact +RECIPIENT`
and have the recipient text the assigned line once. Then verify webhook/reply
availability for that account as described in Requirements.

## Updating, reloading and removal

Saving plugin settings, reloading, or disabling the plugin stops its active
listeners and closes HTTP clients. After reloading/re-enabling the plugin, toggle
the Sendblue bot off and on (or restart AstrBot) to bind the listener with the new
settings. Bot configuration and history remain intact. This explicit restart also
prevents a plugin installation with missing credentials from starting the channel.

To remove: disable the bot, uninstall the plugin, and remove only its Sendblue
receive webhook and proxy/tunnel route. AstrBot's uninstall options determine
whether plugin settings are retained. Clear credentials if no longer needed.
The process-local receive queue and duplicate cache are not a durable inbox;
messages already queued before shutdown follow AstrBot's normal queue lifecycle.

### Moving from the core-adapter PR

1. Disable the old Sendblue bot and its webhook before changing installations.
2. Return AstrBot to an upstream version without PR #10404's core adapter. Do not
   load both implementations: their platform IDs intentionally collide and fail
   closed rather than registering two receivers.
3. Install this plugin, copy the existing credentials/allowlist into plugin settings,
   and recreate the bot with the **same bot ID** if you need the same session origin.
4. Change the receive webhook from `/api/platform/webhook/<uuid>` to
   `/sendblue/webhook`, verify the line filter, and perform the two-turn handset check.
   Remove the old credentials from the old bot config after migration.

## Behavior and troubleshooting

- Direct text only. Groups, echoes and delivery-status callbacks do not start turns.
  Media becomes a text-only notice; arbitrary media URLs are never fetched.
- Wrong secret: HTTP 401. Oversized body: 413. A full queue/listener: 503. Slow body:
  408. Wrong line/sender or ignored event: 200 without a queued message.
- The listener bounds request bodies to 64 KiB, concurrent handlers to 16, body
  processing to 10 seconds, queue admission to 128, and duplicate handles to 4096.
  Duplicate suppression is process-local and resets on restart.
- Replies split into 2,000-character chunks. There are no automatic retries of
  non-idempotent send POSTs. If acceptance is ambiguous or a later chunk fails,
  inspect Sendblue history before retrying manually; earlier chunks may have sent.
- The plugin does not acknowledge delivery receipts or guarantee exactly-once sends.
- On startup errors, verify all plugin fields and check whether port 6198 is in use.
  Do not share logs/configuration containing real credentials or phone numbers.

## Development and evidence

The tests load a copied plugin through the **real AstrBot PluginManager** on
unmodified upstream core, then use the real PlatformManager, local HTTP ingress,
normal message events, a scripted provider with the native agent runner, and a
local Sendblue-compatible HTTP receiver. They cover two turns in the same session,
proactive delivery, authorization/body/queue guards, ambiguous sends, blank install,
reload, disable/re-enable, and uninstall. Conversation context is supplied by the
test harness; this is not a full deployed scheduler, browser, real LLM or handset test.

Use an AstrBot checkout and its installed development environment:

```sh
ASTRBOT_TEST_HOST=/absolute/path/to/AstrBot \
  /absolute/path/to/AstrBot/.venv/bin/python -m pytest tests -q
/absolute/path/to/AstrBot/.venv/bin/ruff check .
/absolute/path/to/AstrBot/.venv/bin/ruff format --check .
```

CI pins the tested AstrBot commit for reproducibility. No Sendblue credentials are
needed. Tests use isolated temporary AstrBot data/config directories.

## License and origin

AGPL-3.0, matching [AstrBot](https://github.com/AstrBotDevs/AstrBot). Adapted from
[the original core-adapter proposal](https://github.com/AstrBotDevs/AstrBot/pull/10404)
and AstrBot's documented plugin platform-adapter API. This standalone packaging
follows the contributor review recommending a plugin. AI-assisted; independent
human review and live handset verification remain outstanding.
