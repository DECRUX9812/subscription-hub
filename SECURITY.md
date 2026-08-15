# Security Policy

## Reporting a vulnerability

**Do NOT open a public issue for security problems.** If you find a
credential leak, an injection vector, or anything that could let someone
access data they shouldn't:

1. Go to **Security → Report a vulnerability** on the GitHub repo
   (or email the maintainers privately).
2. Include: what you found, how to reproduce it, and its impact.
3. We'll acknowledge within 48 hours and work on a fix before disclosure.

## What's in scope

- The backend (`dashboard/plugin_api.py`) — any path that reads auth files,
  proxies requests, or executes commands.
- The desktop pane (`desktop/plugin.js`) — any place user/provider-supplied
  data is rendered (XSS surface).
- The connect flow — OAuth URL handling and subprocess spawning.

## Out of scope / by design

- The plugin reads OAuth tokens from the **proxy's auth dir**
  (`~/.cli-proxy-api/`) — it never writes them. That dir is your machine's
  secret store; protect it.
- API keys come from your Hermes `.env` — never from committed files.

## Safe defaults

- Never commit `config.json`, `history.json`, `.env`, `*.auth.json`, or
  `antigravity-*.json` (all gitignored).
- OAuth client credentials in the code are the **public** CLIProxyAPI
  defaults — overridable via env/config for your own deployments.
