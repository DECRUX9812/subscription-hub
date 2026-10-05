# Subscription Hub

**Hermes Desktop plugin for subscription quotas, usage history, provider connections, and proxy health.**

Works with **Google Antigravity, OpenCode Go, Claude, Codex, Kimi, Grok, Vertex**, or anything your local proxy can reach. A small pet stays in the pane as a status indicator; it is not required for the quota features.

## Features

- **Pet status indicator**: green when quota is high, amber when it is getting low, red when it is panic time, asleep when there is no data.
- **Animated quota bars**: gradient fill and live "resets in 2h 12m" countdowns.
- **Connect any provider in one click**: OAuth, API key, or file import. No terminal spelunking.
- **Usage history sparklines**: the last 24h trend at a glance.
- **Proxy health strip**: up/down, model count, latency.
- **Status-bar chip**: the current remaining percentage with a state dot. Click opens the hub.

## Demo

![pet moods](assets/pets.png)

*Pet moods, left to right: happy, fine, worried, panicked, sleeping. Animated in the app: bob, tilt, shake, zzz.*

## Install

```bash
# One command (installs backend + desktop pane + enables the plugin)
git clone https://github.com/DECRUX9812/subscription-hub.git
cd subscription-hub
./install.sh

# Restart Hermes Desktop (or open a new chat; a fresh backend mounts routes)
```

**Manual install** (if you prefer):

```bash
mkdir -p ~/.hermes/plugins ~/.hermes/desktop-plugins
git clone https://github.com/DECRUX9812/subscription-hub.git ~/.hermes/plugins/subscription-hub
cp -r ~/.hermes/plugins/subscription-hub/desktop ~/.hermes/desktop-plugins/subscription-hub
hermes plugins enable subscription-hub --allow-tool-override
```

Then look for:

- **Status bar**: the remaining-percent chip
- **Right dock**: the "Subscriptions" pane
- **Command palette**: "Subscription Hub"

## Configure

Everything is optional; it works out of the box with a default CLIProxyAPI Antigravity account. Overrides live in `~/.hermes/plugins/subscription-hub/config.json`:

```json
{
  "auth_dir": "/path/to/auth-dir",
  "selected_auth_file": "antigravity-x.json",
  "hosts": ["daily-cloudcode-pa.googleapis.com"],
  "load_hosts": ["cloudcode-pa.googleapis.com"],
  "client_id": "…", "client_secret": "…",
  "token": "ya29…",
  "models": ["gemini-3.1-pro-high"],
  "low_threshold": 0.1,
  "refresh_interval_seconds": 60,
  "proxy_bin": "/path/to/cli-proxy-api",
  "proxy_config": "/path/to/cliproxyapi/config.yaml"
}
```

Env overrides (highest precedence): `CPA_QUOTA_AUTH_DIR`, `CPA_QUOTA_HOSTS`, `CPA_QUOTA_TOKEN`, `CPA_QUOTA_PROXY_BIN`, `CPA_QUOTA_PROXY_CONFIG`, `CPA_QUOTA_CLIENT_ID`, `CPA_QUOTA_CLIENT_SECRET`.

**Security:** OAuth tokens are read from the proxy's auth dir, never written by this plugin. API keys come from your Hermes `.env`. `config.json`, `history.json`, and `.env` are all gitignored. Never commit credentials.

## How it works

| File | Role |
|---|---|
| `dashboard/plugin_api.py` | FastAPI backend: quota, tier, history, providers, connect flows |
| `desktop/plugin.js` | Desktop pane + status-bar chip (plain ESM, `jsx()` calls) |
| `plugin.yaml` | Hermes plugin registry metadata |
| `install.sh` | One-shot installer |

**Backend endpoints:** `/quota`, `/providers`, `/connect`, `/connect/status`, `/connect/cancel`, `/history`, `/status`, `/accounts`, `/config`, `/health`.

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for ground rules, [SECURITY.md](SECURITY.md) for reporting vulnerabilities, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards.

**Good first ideas:** new providers (add to `PROVIDERS`), pet variants, better sparklines, docs.

## License

MIT. Go build something with it.
