#!/usr/bin/env python3
"""Pre-push Security Scanner: Blocks git push if any secret, private key, or API token is detected."""

import sys
import re
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 1. Blocked filenames
FORBIDDEN_FILES = [
    re.compile(r"^\.env($|\..*)", re.IGNORECASE),
    re.compile(r".*\.pem$", re.IGNORECASE),
    re.compile(r".*\.key$", re.IGNORECASE),
    re.compile(r".*id_rsa.*", re.IGNORECASE),
    re.compile(r".*keystore.*\.json$", re.IGNORECASE),
]

# 2. Known public constant hashes (event topics, zero addresses, etc.)
WHITELIST_HASHES = {
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef", # Transfer(address,address,uint256)
    "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    "0000000000000000000000000000000000000000000000000000000000000000",
    "1111111111111111111111111111111111111111111111111111111111111111",
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
}

# 3. Sensitive regex patterns
SECRET_PATTERNS = [
    # Explicit private key assignment
    (
        "Private Key Assignment",
        re.compile(r"""(?i)(?:private_key|privkey|secret_key|wallet_key)\s*[:=]\s*["'](0x[a-f0-9]{64}|[a-f0-9]{64})["']"""),
    ),
    # Raw 64-character hex string (EVM 32-byte private key)
    (
        "Potential Raw Private Key (64 hex characters)",
        re.compile(r"""(?:['"])(0x[a-fA-F0-9]{64}|[a-fA-F0-9]{64})(?:['"])"""),
    ),
    # Alchemy / Infura / Quicknode private RPC URL with key
    (
        "Private RPC Provider API Key in URL",
        re.compile(r"""https?://(?:[a-zA-Z0-9-]+\.)*(?:alchemy\.com|infura\.io|quicknode\.pro)/v2/([a-zA-Z0-9_-]{20,})"""),
    ),
    # Telegram bot token
    (
        "Telegram Bot API Token",
        re.compile(r"""\b[0-9]{8,10}:[a-zA-Z0-9_-]{35}\b"""),
    ),
    # AWS Access Key
    (
        "AWS Access Key ID",
        re.compile(r"""\b(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b"""),
    ),
    # Generic API Key / Secret assignments with long entropy
    (
        "Hardcoded Secret / API Token Assignment",
        re.compile(r"""(?i)(?:api_key|apikey|secret|token|auth_token)\s*[:=]\s*["']([a-zA-Z0-9_\-\.]{24,})["']"""),
    ),
]

IGNORED_PATHS = [
    Path("scripts/security_check.py"),
    Path(".env.example"),
    Path("package-lock.json"),
]

def is_whitelisted(val: str) -> bool:
    v_clean = val.lower().removeprefix("0x")
    if v_clean in WHITELIST_HASHES or f"0x{v_clean}" in WHITELIST_HASHES:
        return True
    if "<" in val or "YOUR_" in val.upper() or "EXAMPLE" in val.upper():
        return True
    return False

def redact(val: str) -> str:
    if len(val) <= 8:
        return "****"
    return val[:4] + "..." + val[-4:]

def scan_content(file_path: Path, content: str):
    findings = []
    lines = content.splitlines()
    for idx, line in enumerate(lines, 1):
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("#") or line_clean.startswith("//"):
            continue
            
        for name, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(line):
                matched_val = match.group(1) if match.groups() else match.group(0)
                if is_whitelisted(matched_val):
                    continue
                # Double check for raw hex strings: ignore if it's in an ABI or transaction hash
                if "Raw Private Key" in name:
                    # check if context contains topic, tx_hash, or hash
                    lower_line = line.lower()
                    if any(k in lower_line for k in ["topic", "tx_hash", "tx", "hash", "keccak", "signature", "block"]):
                        continue
                findings.append({
                    "file": str(file_path),
                    "line": idx,
                    "type": name,
                    "snippet": line_clean[:100],
                    "match": redact(matched_val),
                })
    return findings

def get_files_to_check():
    files = set()
    try:
        # 1. Check staged files
        res = subprocess.run(["git", "diff", "--cached", "--name-only"], capture_output=True, text=True, cwd=ROOT)
        for f in res.stdout.splitlines():
            if f.strip():
                files.add(f.strip())
        # 2. Check unstaged modified files
        res_m = subprocess.run(["git", "diff", "--name-only"], capture_output=True, text=True, cwd=ROOT)
        for f in res_m.stdout.splitlines():
            if f.strip():
                files.add(f.strip())
        # 3. Check commits being pushed ahead of upstream
        res2 = subprocess.run(["git", "diff", "@{u}..HEAD", "--name-only"], capture_output=True, text=True, cwd=ROOT)
        for f in res2.stdout.splitlines():
            if f.strip():
                files.add(f.strip())
        # 4. Check untracked files (safety check)
        res3 = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ROOT)
        for line in res3.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                files.add(parts[1].strip())
    except Exception:
        pass
        
    # If no changes or explicit --all requested, scan all tracked files
    if not files or "--all" in sys.argv:
        try:
            res = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=ROOT)
            for f in res.stdout.splitlines():
                if f.strip():
                    files.add(f.strip())
        except Exception:
            pass
            
    return sorted(list(files))

def main():
    print("\033[1;36m🔍 Running Pre-Push Security & Secret Scan...\033[0m")
    files = get_files_to_check()
    violations = []
    
    for f_str in files:
        p = Path(f_str)
        if any(p == ign for ign in IGNORED_PATHS):
            continue
            
        # Check forbidden filename
        for fb in FORBIDDEN_FILES:
            if fb.match(p.name):
                violations.append({
                    "file": f_str,
                    "line": 1,
                    "type": "Forbidden Sensitive File Name",
                    "snippet": f"File '{f_str}' must NOT be tracked by git!",
                    "match": f_str,
                })
                break
                
        full_path = ROOT / p
        if not full_path.exists() or not full_path.is_file():
            continue
            
        try:
            # Skip binary files
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            findings = scan_content(p, content)
            violations.extend(findings)
        except Exception as e:
            pass

    if violations:
        print("\n\033[1;31m========================================================================\033[0m")
        print("\033[1;31m🚨 SECURITY ALERT: SENSITIVE CREDENTIALS DETECTED IN COMMITS!\033[0m")
        print("\033[1;31m   Git push has been automatically ABORTED to protect your account.\033[0m")
        print("\033[1;31m========================================================================\033[0m")
        for v in violations:
            print(f"\n  ❌ \033[1;33m{v['type']}\033[0m in \033[1;37m{v['file']}:{v['line']}\033[0m")
            print(f"     Redacted value: \033[1;31m{v['match']}\033[0m")
            print(f"     Line preview  : {v['snippet']}")
        print("\n\033[1;36m💡 How to fix:\033[0m")
        print("  1. Remove sensitive keys/tokens from code/files.")
        print("  2. Put secrets in \033[1m.env\033[0m (which is ignored by .gitignore).")
        print("  3. Run \033[1mgit commit --amend\033[0m to remove them from commit history.")
        sys.exit(1)
    else:
        print("\033[1;32m✅ Security Check Passed: No sensitive keys or credentials detected!\033[0m")
        sys.exit(0)

if __name__ == "__main__":
    main()
