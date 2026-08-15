#!/usr/bin/env bash
# Subscription Hub — installer for Hermes Desktop.
set -euo pipefail

HUB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins"
DESKTOP_DIR="${HERMES_HOME:-$HOME/.hermes}/desktop-plugins"

echo "🚀 Installing Subscription Hub…"
mkdir -p "$PLUGINS_DIR" "$DESKTOP_DIR"

# 1. Backend plugin
rm -rf "$PLUGINS_DIR/subscription-hub"
cp -r "$HUB_DIR" "$PLUGINS_DIR/subscription-hub"

# 2. Desktop pane (pretty part)
rm -rf "$DESKTOP_DIR/subscription-hub"
mkdir -p "$DESKTOP_DIR/subscription-hub"
cp "$HUB_DIR/desktop/plugin.js" "$DESKTOP_DIR/subscription-hub/plugin.js"

# 3. Enable backend
if command -v hermes >/dev/null 2>&1; then
  hermes plugins enable subscription-hub --allow-tool-override || true
else
  echo "⚠️  hermes CLI not found — enable manually: hermes plugins enable subscription-hub --allow-tool-override"
fi

echo "✅ Subscription Hub installed."
echo "   Restart the desktop app (or open a new chat) to mount the backend."
echo "   Pane:  Subscriptions (right dock)"
echo "   Chip:  ◇ NN% in the status bar"
