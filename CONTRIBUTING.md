# Contributing to Subscription Hub

Thanks for helping make this plugin better! Here's how to contribute the
right way — the same rules that keep every good open-source repo healthy.

## Ground rules

- **One change per PR.** Small, focused PRs review faster and merge sooner.
- **No secrets, ever.** Real tokens, API keys, or OAuth credentials never
  belong in code, commits, or issues. If you find one in the repo, open a
  security advisory (see `SECURITY.md`) — don't post it in an issue.
- **Tests are the law of the land.** Add a test with every fix or feature.
- **Match the existing style.** Plain ESM + `jsx()` calls in the desktop
  pane, typed Python in the backend.

## Getting started

```bash
# Fork the repo on GitHub, then:
git clone git@github.com:<your-username>/subscription-hub.git
cd subscription-hub
git checkout -b feat/your-change

# Install locally to test (see README "Install")
./install.sh

# Verify syntax before committing
python3 -m py_compile dashboard/plugin_api.py
node --check desktop/plugin.js
```

## What to work on

Check the [issues](https://github.com/DECRUX9812/subscription-hub/issues)
tab. Good first issues are labeled `good first issue`. Ideas:

- New provider support (add to `PROVIDERS` in `dashboard/plugin_api.py`)
- Pet mascot variants / more moods (`desktop/plugin.js`)
- Better sparkline / history views
- Documentation improvements

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add Perplexity provider
fix: handle empty quota response
docs: clarify config.json options
test: cover token refresh path
```

## Pull request checklist

- [ ] Branch off `main`, named `feat/...` or `fix/...`
- [ ] One logical change
- [ ] Syntax checks pass (`py_compile`, `node --check`)
- [ ] Tests added/updated and passing
- [ ] No secrets or local state committed (check `git status`)
- [ ] README updated if behavior changed

## Reporting issues

- **Bugs:** include Hermes version, plugin version, OS, and what you expected
  vs what happened. Screenshots help a lot.
- **Feature requests:** describe the problem you're solving, not just the
  feature name. "I want X" → "When I do Y, Z is hard because…"
- **Security issues:** use the private advisory flow — see `SECURITY.md`.

## Code of conduct

Everyone participating is expected to follow our
[Code of Conduct](CODE_OF_CONDUCT.md). Be kind, be specific, assume good
intent.
