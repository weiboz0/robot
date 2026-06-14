#!/usr/bin/env bash
# One-time setup: makes `chatbot` runnable from anywhere after a git pull/clone.
# Symlinks the repo's launcher into ~/.local/bin (added to PATH if missing).
set -e
DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

mkdir -p "$HOME/.local/bin"
for cmd in chatbot roverctl; do
  chmod +x "$DIR/$cmd"
  ln -sf "$DIR/$cmd" "$HOME/.local/bin/$cmd"
  echo "Linked: $cmd -> $DIR/$cmd"
done

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

echo "Done. Run:  chatbot   (or:  roverctl  to launch the rover controller;"
echo "            it needs the rovercontrol-arm64 binary — see docs/plans/002)"
