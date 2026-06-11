#!/usr/bin/env bash
# One-time setup: makes `chatbot` runnable from anywhere after a git pull/clone.
# Symlinks the repo's launcher into ~/.local/bin (added to PATH if missing).
set -e
DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

chmod +x "$DIR/chatbot"
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/chatbot" "$HOME/.local/bin/chatbot"
echo "Linked: chatbot -> $DIR/chatbot"

# Ensure ~/.local/bin is on PATH (covers bash and zsh).
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;  # already on PATH
  *)
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      [ -f "$rc" ] || continue
      grep -q '.local/bin' "$rc" || \
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
    done
    echo "Added ~/.local/bin to PATH (open a new terminal or 'source' your shell rc)."
    ;;
esac

echo "Done. Run:  chatbot"
