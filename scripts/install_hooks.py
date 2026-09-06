#!/usr/bin/env python3
"""Installer for local Git security hooks."""

import subprocess
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def install():
    hooks_dir = ROOT / ".githooks"
    pre_push = hooks_dir / "pre-push"
    
    if not pre_push.exists():
        print("Error: .githooks/pre-push not found!")
        return False
        
    os.chmod(pre_push, 0o755)
    
    # Configure git core.hooksPath
    res = subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=ROOT)
    if res.returncode == 0:
        print("\033[1;32m✅ Git pre-push hook installed successfully!\033[0m")
        print("   Any 'git push' will now automatically scan for private keys, tokens, and secrets.")
        return True
    else:
        print("\033[1;31m❌ Failed to configure git hooks path.\033[0m")
        return False

if __name__ == "__main__":
    install()
