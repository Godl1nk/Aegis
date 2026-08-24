"""Dangerous command approval — detection, prompting, and per-session state.

Ported from Hermes (tools/approval.py). This module is the single source of
truth for the dangerous command system:
- Pattern detection (DANGEROUS_PATTERNS, detect_dangerous_command)
- Unconditional hardline blocklist (HARDLINE_PATTERNS)
- Per-session approval state (thread-safe, keyed by session id)
- Async approval gate (SSE approval_request event + /api/approvals resolve)
- Permanent allowlist persistence (data/command_allowlist.json)

The detection layer — patterns, normalization, and the quote-aware
deobfuscation variants — is copied verbatim from Hermes; only the
Hermes-specific config/gateway targets are swapped for their Aegis
equivalents (settings.json / auth.json / uvicorn self-termination).
"""

from __future__ import annotations

import asyncio
import fnmatch
import functools
import json
import logging
import os
import re
import shlex
import tempfile
import threading
import unicodedata
import uuid
from typing import Optional

from src.ansi_strip import strip_ansi
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

# Freeze YOLO mode at module import time. Reading os.environ on every call
# would allow anything running inside the process to set this variable and
# instantly bypass all approval checks — a prompt-injection escalation path.
def _is_truthy(v: str) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")

_YOLO_MODE_FROZEN: bool = _is_truthy(os.getenv("AEGIS_YOLO_MODE", ""))

_ALLOWLIST_FILE = os.path.join(DATA_DIR, "command_allowlist.json")

# =========================================================================
# Sensitive write targets
# =========================================================================
# Sensitive write targets that should trigger approval even when referenced
# via shell expansions like $HOME, or by the resolved absolute home path.
# The resolved-absolute form is folded into the ~/ patterns at detection
# time by _normalize_command_for_detection().

_SSH_SENSITIVE_PATH = r'(?:~|\$home|\$\{home\})/\.ssh(?:/|$)'
# Aegis security-policy files: settings.json holds command_approval_mode and
# the deny list, auth.json holds credentials, command_allowlist.json IS the
# permanent-approval allowlist — a write to any of them lets the agent flip
# its own approval gate off. Gate sed -i / tee / > / cp targeting them so the
# route-level protections aren't unpaired theater.
_AEGIS_CONFIG_PATH = (
    r'(?:(?:/|\.{1,2}/)?(?:[^\s/"\'`]+/)*'
    r'(?:settings\.json|auth\.json|command_allowlist\.json))'
)
_PROJECT_ENV_PATH = r'(?:(?:/|\.{1,2}/)?(?:[^\s/"\'`]+/)*\.env(?:\.[^/\s"\'`]+)*)'
_PROJECT_CONFIG_PATH = r'(?:(?:/|\.{1,2}/)?(?:[^\s/"\'`]+/)*config\.yaml)'
_SHELL_RC_FILES = (
    r'(?:~|\$home|\$\{home\})/\.'
    r'(?:bashrc|zshrc|profile|bash_profile|zprofile)\b'
)
_CREDENTIAL_FILES = (
    r'(?:~|\$home|\$\{home\})/\.'
    r'(?:netrc|pgpass|npmrc|pypirc)\b'
)
# macOS: /etc, /var, /tmp, /home are symlinks to /private/{etc,var,tmp,home}.
# A command written to target /private/etc/sudoers works identically to
# /etc/sudoers on macOS but bypasses a plain "/etc/" pattern check. Match
# both forms.
_MACOS_PRIVATE_SYSTEM_PATH = r'/private/(?:etc|var|tmp|home)/'
_SYSTEM_CONFIG_PATH = (
    rf'(?:/etc/|{_MACOS_PRIVATE_SYSTEM_PATH})'
)
_SENSITIVE_WRITE_TARGET = (
    rf'(?:{_SYSTEM_CONFIG_PATH}|/dev/sd|'
    rf'{_SSH_SENSITIVE_PATH}|'
    rf'{_AEGIS_CONFIG_PATH}|'
    rf'{_SHELL_RC_FILES}|'
    rf'{_CREDENTIAL_FILES})'
)
_USER_SENSITIVE_WRITE_TARGET = (
    rf'(?:{_SSH_SENSITIVE_PATH}|'
    rf'{_SHELL_RC_FILES}|'
    rf'{_CREDENTIAL_FILES})'
)
_PROJECT_SENSITIVE_WRITE_TARGET = rf'(?:{_PROJECT_ENV_PATH}|{_PROJECT_CONFIG_PATH})'
# Anchor for the cp/mv/install rule, where the sensitive path is only a write
# target when it is the LAST argument (the destination). Requiring end-of-line
# (or a command separator) keeps `cp config.yaml backup.yaml` — config.yaml as
# the SOURCE — out of the deny.
_COMMAND_TAIL = r'(?:\s*(?:&&|\|\||;).*)?$'
# Boundary for stream-write rules (`>`/`>>` redirection and `tee`), where the
# sensitive path is ALWAYS a write target no matter what follows it. We only
# need the path token to END at a shell word boundary — whitespace, a quote, a
# command separator, a redirection operator, or end-of-line.
#
# `#` is deliberately NOT a boundary char: a real trailing comment always has
# whitespace before the `#` (already covered by `\s`), whereas a `#` glued to
# the path is part of the filename. `echo x > .env#backup` writes to the
# distinct file `.env#backup`, not `.env`, so it must stay OUT of the deny.
_WRITE_TARGET_BOUNDARY = r'(?=[\s;&|<>"\']|$)'

# =========================================================================
# Hardline (unconditional) blocklist
# =========================================================================
#
# Commands so catastrophic they should NEVER run via the agent, regardless
# of yolo or command_approval_mode=off.  This is a floor below yolo: opting
# into yolo is the user trusting the agent with their files and services,
# not trusting it to wipe the disk or power the box off.
#
# The list is deliberately tiny — only things with no recovery path:
# filesystem destruction rooted at /, raw block device overwrites, kernel
# shutdown/reboot, and denial-of-service commands that take the host down.
# Recoverable-but-costly operations (git reset --hard, rm -rf /tmp/x,
# chmod -R 777, curl|sh) stay in DANGEROUS_PATTERNS where yolo can pass
# them through — that's what yolo is for.

# Regex fragment matching the *start* of a command (i.e. positions where
# a shell would begin parsing a new command).  Used by shutdown/reboot
# patterns so they don't fire on "echo reboot" or "grep 'shutdown' log".
_CMDPOS = (
    r'(?:^|[;&|\n`]|\$\()'         # start position
    r'\s*'                          # optional whitespace
    r'(?:sudo\s+(?:-[^\s]+\s+)*)?'  # optional sudo with flags
    r'(?:env\s+(?:\w+=\S*\s+)*)?'   # optional env with VAR=VAL pairs
    r'(?:(?:exec|nohup|setsid|time)\s+)*'  # optional wrapper commands
    r'\s*'
)


# Destructive-path argument matcher for the rm hardline rules.
#
# The path token in `rm -rf /` is almost always written quoted in real
# shells — `rm -rf "/"`, `rm -rf "$HOME"` — and `${HOME}` is the universal
# brace form. Accept the path either fully wrapped in a matching quote pair
# OR bare with a terminator (whitespace, end-of-string, or a shell
# metacharacter so `$(rm -rf /)` whose `/` is terminated by `)` is caught).
def _hardline_rm_path(path_alt: str, tail: str = r'(?:\s|$|[)`;|&])') -> str:
    return rf'(?:["\'](?:{path_alt})["\']|(?:{path_alt}){tail})'


# Protected system roots whose recursive deletion has no recovery path.
_HARDLINE_SYSTEM_DIRS = (
    r'/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|'
    r'/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*'
)

# `rm` plus its flag group, shared by the three rm hardline rules.
# Anchored to _CMDPOS so the rule fires only when `rm` is an actual command
# word — not when the literal string "rm -rf /" appears as DATA inside
# another command's argument, e.g. `git commit -m "…rm -rf /…"`.
_RM_FLAG_PREFIX = _CMDPOS + r'rm\s+(-[^\s]*\s+)*'

HARDLINE_PATTERNS = [
    # rm recursive targeting the root filesystem or protected roots.
    (_RM_FLAG_PREFIX + _hardline_rm_path(r'/(?:(?:\.\.?)?/)*(?:\.\.?)?\**|/ \*'), "recursive delete of root filesystem"),
    (_RM_FLAG_PREFIX + _hardline_rm_path(_HARDLINE_SYSTEM_DIRS), "recursive delete of system directory"),
    (_RM_FLAG_PREFIX + _hardline_rm_path(r'(?:~|\$\{?HOME\}?)(?:/?|/\*)?'), "recursive delete of home directory"),
    # Filesystem format
    (r'\bmkfs(\.[a-z0-9]+)?\b', "format filesystem (mkfs)"),
    # Raw block device overwrites (dd + redirection)
    (r'\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*', "dd to raw block device"),
    (r'>\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b', "redirect to raw block device"),
    # Fork bomb (classic shell form)
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    # Kill every process on the system
    (r'\bkill\s+(-[^\s]+\s+)*-1\b', "kill all processes"),
    # System shutdown / reboot — anchored to command position so we don't
    # false-positive on "echo reboot" or "grep 'shutdown' logs".
    (_CMDPOS + r'(shutdown|reboot|halt|poweroff)\b', "system shutdown/reboot"),
    (_CMDPOS + r'init\s+[06]\b', "init 0/6 (shutdown/reboot)"),
    (_CMDPOS + r'systemctl\s+(poweroff|reboot|halt|kexec)\b', "systemctl poweroff/reboot"),
    (_CMDPOS + r'telinit\s+[06]\b', "telinit 0/6 (shutdown/reboot)"),
]

# Pre-compiled variant used by the hot-path matcher. Building these at module
# load eliminates the cold-cache re.compile fan-out on the first call.
_RE_FLAGS = re.IGNORECASE | re.DOTALL
HARDLINE_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in HARDLINE_PATTERNS
]


# =========================================================================
# Sudo stdin guard — block password guessing via "sudo -S"
# =========================================================================
# Any explicit "sudo -S" in the command is the LLM piping a guessed password
# via stdin.  This is a brute-force attack vector: the model iterates through
# candidate passwords, inspects sudo's "Sorry, try again" output, and
# refines.  Unconditional block — there is never a legitimate reason for the
# agent to pipe passwords to sudo -S when no password has been configured.
_SUDO_STDIN_RE = re.compile(
    r'(?:^|[;&|`\n]|&&|\|\||\$\()\s*sudo\s+-S\b',
    re.IGNORECASE)


def _check_sudo_stdin_guard(command: str) -> tuple:
    """Detect ``sudo -S`` (stdin password) without configured SUDO_PASSWORD.

    Returns:
        (is_blocked: bool, description: str | None)
    """
    if "SUDO_PASSWORD" in os.environ:
        return (False, None)
    normalized = _normalize_command_for_detection(command).lower()
    if _SUDO_STDIN_RE.search(normalized):
        return (True, "sudo password guessing via stdin (sudo -S)")
    return (False, None)


def detect_hardline_command(command: str) -> tuple:
    """Check if a command matches the unconditional hardline blocklist.

    Returns:
        (is_hardline, description) or (False, None)
    """
    for command_variant in _command_detection_variants(command):
        normalized = command_variant.lower()
        for pattern_re, description in HARDLINE_PATTERNS_COMPILED:
            if pattern_re.search(normalized):
                return (True, description)
    return (False, None)


# =========================================================================
# Dangerous command patterns
# =========================================================================

DANGEROUS_PATTERNS = [
    (r'\brm\s+(-[^\s]*\s+)*/', "delete in root path"),
    (r'\brm\s+-[^\s]*r', "recursive delete"),
    (r'\brm\s+--recursive\b', "recursive delete (long flag)"),
    # Windows shell front-ends have destructive built-ins that do not look like
    # Unix `rm`. Gate only when they are executed through cmd/powershell so
    # ordinary prose or filenames containing "del"/"rd" do not trip the guard.
    (r'\bcmd(?:\.exe)?\s+/(?:c|k)\s+.*\b(?:del|erase|rd|rmdir)\b', "Windows cmd destructive delete"),
    # PowerShell/pwsh: the destructive verb runs as the default positional
    # argument, so `powershell Remove-Item ...` needs NO explicit -Command.
    (r'\b(?:powershell|pwsh)(?:\.exe)?\b(?:\s+-\S+)*\s+(?:-(?:command|c)\s+)?["\']?(?:remove-item|rmdir|erase|del|rd|ri|rm)\b', "Windows PowerShell destructive delete"),
    (r'\b(?:powershell|pwsh)(?:\.exe)?\b.*\s-(?:encodedcommand|enc|e)\b', "PowerShell encoded command execution"),
    (r'\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b', "world/other-writable permissions"),
    (r'\bchmod\s+--recursive\b.*(777|666|o\+[rwx]*w|a\+[rwx]*w)', "recursive world/other-writable (long flag)"),
    (r'\bchown\s+(-[^\s]*)?R\s+root', "recursive chown to root"),
    (r'\bchown\s+--recur[a-z]*\b.*root', "recursive chown to root (long flag)"),
    (r'\bmkfs\b', "format filesystem"),
    (r'\bdd\s+.*if=', "disk copy"),
    (r'>\s*/dev/sd', "write to block device"),
    (r'\bDROP\s+(TABLE|DATABASE)\b', "SQL DROP"),
    # Use [^\n]* instead of .* so DOTALL mode does not cause a WHERE clause on the
    # *next* line to satisfy the negative lookahead, silently allowing DELETE without WHERE.
    (r'\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)', "SQL DELETE without WHERE"),
    (r'\bTRUNCATE\s+(TABLE)?\s*\w', "SQL TRUNCATE"),
    (rf'>\s*{_SYSTEM_CONFIG_PATH}', "overwrite system config"),
    (r'\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b', "stop/restart system service"),
    (r'\bkill\s+-9\s+-1\b', "kill all processes"),
    (r'\bpkill\s+-9\b', "force kill processes"),
    # killall with SIGKILL (parallel to pkill -9). Catches -9 / -KILL /
    # -s KILL / -SIGKILL forms, and also `killall -r <regex>` broad sweeps.
    (r'\bkillall\s+(-[^\s]*\s+)*-(9|KILL|SIGKILL)\b', "force kill processes (killall -KILL)"),
    (r'\bkillall\s+(-[^\s]*\s+)*-s\s+(KILL|SIGKILL|9)\b', "force kill processes (killall -s KILL)"),
    (r'\bkillall\s+(-[^\s]*\s+)*-r\b', "kill processes by regex (killall -r)"),
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    # Any shell invocation via -c or combined flags like -lc, -ic, etc.
    (r'\b(bash|sh|zsh|ksh)\s+-[^\s]*c(\s+|$)', "shell command via -c/-lc flag"),
    (r'\b(python[23]?|perl|ruby|node)\s+-[ec]\s+', "script execution via -e/-c flag"),
    (r'\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)', "pipe remote content to shell"),
    (r'\b(bash|sh|zsh|ksh)\s+<\s*<?\s*\(\s*(curl|wget)\b', "execute remote script via process substitution"),
    # Remote content executed via command substitution: eval/source/. $(curl ...)
    (r'(?:\beval\b|\bsource\b|\.)\s*(?:\$\(\s*|`\s*)(?:curl|wget)\b', "execute remote content via command substitution"),
    # Decode-and-execute: encoded/transformed content piped to a shell. Without
    # these, `echo <base64> | base64 -d | bash` silently runs `rm -rf /` or any
    # other command because the raw text carries no dangerous keywords.
    (r'\b(base64|base32|base16)\s+(?:-[dD]|--decode)\b.*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe decoded content to shell (possible command obfuscation)"),
    # xxd reverse hex dump to shell (xxd uses -r for decode, not -d).
    (r'\bxxd\s+-r\b.*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe xxd-decoded content to shell (possible command obfuscation)"),
    # Character transformation via tr piped to shell:
    # `echo 'eq -pe v/' | tr 'eqv' 'rmf' | bash` decodes to `rm -rf /`.
    (r'\becho\b[^|]*\|\s*\btr\b[^|]*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe tr-transformed output to shell (possible command obfuscation)"),
    # openssl decode piped to shell.
    (r'\bopenssl\b.*\b(?:base64|enc)\b[^|]*\s+-[dD]\b[^|]*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe openssl-decoded content to shell (possible command obfuscation)"),
    (rf'\btee\b.*["\']?{_SENSITIVE_WRITE_TARGET}', "overwrite system file via tee"),
    (rf'>>?\s*["\']?{_SENSITIVE_WRITE_TARGET}', "overwrite system file via redirection"),
    (rf'\btee\b.*["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_WRITE_TARGET_BOUNDARY}', "overwrite project env/config via tee"),
    (rf'>>?\s*["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_WRITE_TARGET_BOUNDARY}', "overwrite project env/config via redirection"),
    (r'\bxargs\s+.*\brm\b', "xargs with rm"),
    # find -exec rm / -execdir rm — the -execdir variant runs in the directory
    # of each match with the same semantics.
    (r'\bfind\b.*-exec(?:dir)?\s+(/\S*/)?rm\b', "find -exec/-execdir rm"),
    (r'\bfind\b.*-delete\b', "find -delete"),
    # Docker container lifecycle — any user with docker.sock reachable gives
    # the agent the ability to restart/stop/kill containers without approval.
    (r'\bdocker\s+compose\s+(restart|stop|kill|down)\b', "docker compose restart/stop/kill/down (container lifecycle)"),
    (r'\bdocker\s+(restart|stop|kill)\b', "docker restart/stop/kill (container lifecycle)"),
    # Self-termination protection: prevent agent from killing its own process
    # (the Aegis server runs under uvicorn from app.py).
    (r'\b(pkill|killall)\b.*\b(uvicorn|aegis|app\.py)\b', "kill Aegis server process (self-termination)"),
    # Self-termination via kill + command substitution (pgrep/pidof).
    # The name-based pattern above catches `pkill uvicorn` but not
    # `kill -9 $(pgrep -f uvicorn)` because the substitution is opaque
    # to regex at detection time. Catch the structural pattern instead.
    (r'\bkill\b.*\$\(\s*(pgrep|pidof)\b', "kill process via pgrep/pidof expansion (self-termination)"),
    (r'\bkill\b.*`\s*(pgrep|pidof)\b', "kill process via backtick pgrep/pidof expansion (self-termination)"),
    # File copy/move/edit into sensitive system paths (/etc/ and macOS
    # /private/etc/ mirror).
    (rf'\b(cp|mv|install)\b.*\s{_SYSTEM_CONFIG_PATH}', "copy/move file into system config path"),
    (rf'\b(cp|mv|install)\b.*\s["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_COMMAND_TAIL}', "overwrite project env/config file"),
    # cp/mv/install OVERWRITING a sensitive credential/SSH/shell-rc/Aegis file.
    # Anchor the sensitive target to the command tail so this fires on the
    # DESTINATION (last arg) only — `cp evil ~/.ssh/authorized_keys` is gated,
    # but reading OUT of a sensitive path (`cp ~/.ssh/config /tmp/x`) stays safe.
    (rf'\b(cp|mv|install)\b.*\s["\']?{_SENSITIVE_WRITE_TARGET}[^\s"\']*["\']?{_COMMAND_TAIL}', "copy/move file into sensitive credential/SSH/shell-rc path"),
    # In-place edits mutate the target file directly, bypassing redirection,
    # tee, and copy/move/install coverage. Gate the same user-controlled
    # startup/credential files so `sed -i ... ~/.bashrc` and `perl -i ...
    # ~/.ssh/authorized_keys` cannot silently plant login commands or keys.
    (rf'\bsed\s+-[^\s]*i.*(?:{_USER_SENSITIVE_WRITE_TARGET})[^\s"\']*', "in-place edit of sensitive credential/SSH/shell-rc path"),
    (rf'\bsed\s+--in-place\b.*(?:{_USER_SENSITIVE_WRITE_TARGET})[^\s"\']*', "in-place edit of sensitive credential/SSH/shell-rc path (long flag)"),
    (rf'\b(?:perl|ruby)\b.*(?:^|\s)-[^\s]*i\b.*(?:{_USER_SENSITIVE_WRITE_TARGET})[^\s"\']*', "in-place edit of sensitive credential/SSH/shell-rc path (perl/ruby)"),
    (rf'\bsed\s+-[^\s]*i.*\s{_SYSTEM_CONFIG_PATH}', "in-place edit of system config"),
    (rf'\bsed\s+--in-place\b.*\s{_SYSTEM_CONFIG_PATH}', "in-place edit of system config (long flag)"),
    # In-place edit of an Aegis-managed security file (settings.json,
    # auth.json, command_allowlist.json). sed -i bypasses the redirection/tee
    # patterns above because it mutates the file directly.
    (rf'\bsed\s+-[^\s]*i.*{_AEGIS_CONFIG_PATH}', "in-place edit of Aegis config/auth file"),
    (rf'\bsed\s+--in-place\b.*{_AEGIS_CONFIG_PATH}', "in-place edit of Aegis config/auth file (long flag)"),
    (rf'\b(?:perl|ruby)\b.*(?:^|\s)-[^\s]*i\b.*{_AEGIS_CONFIG_PATH}', "in-place edit of Aegis config/auth file (perl/ruby)"),
    # Script execution via heredoc — bypasses the -e/-c flag patterns above.
    # `python3 << 'EOF'` feeds arbitrary code via stdin without -c/-e flags.
    (r'\b(python[23]?|perl|ruby|node)\s+<<', "script execution via heredoc"),
    # Shell execution via heredoc — `bash <<'EOF' ... EOF` runs arbitrary
    # shell commands without triggering the `bash -c` pattern above.
    (r'\b(bash|sh|zsh|ksh)\s+<<', "shell execution via heredoc"),
    # Git destructive operations that can lose uncommitted work or rewrite
    # shared history. `git reset --hard` accepts any unambiguous long-flag
    # prefix (--h, --ha, --har, --hard) because git's own option parser
    # resolves abbreviated long flags; it does not match `--help`, which git
    # special-cases before mode resolution.
    (r'\bgit\s+reset\s+--h(?:a(?:r(?:d)?)?)?\b', "git reset --hard (destroys uncommitted changes)"),
    (r'\bgit\s+push\b.*--forc[a-z]*\b', "git force push (rewrites remote history)"),
    (r'\bgit\s+push\b.*-f\b', "git force push short flag (rewrites remote history)"),
    (r'\bgit\s+clean\s+-[^\s]*f', "git clean with force (deletes untracked files)"),
    (r'\bgit\s+branch\s+-D\b', "git branch force delete"),
    # `-D` is shorthand for `-d --force`; the long-flag spellings delete an
    # unmerged branch exactly like `-D` does. Match delete+force in either
    # order, bounded to the same command segment (not spanning `;`/`|`/`&`).
    (r'\bgit\s+branch\b[^;|&\n]*?(?:-d\b|--delete\b)[^;|&\n]*?(?:-f\b|--force\b)', "git branch force delete (long flags)"),
    (r'\bgit\s+branch\b[^;|&\n]*?(?:-f\b|--force\b)[^;|&\n]*?(?:-d\b|--delete\b)', "git branch force delete (long flags, force-first)"),
    # Script execution after chmod +x — catches the two-step pattern where
    # a script is first made executable then immediately run.
    (r'\bchmod\s+\+x\b.*[;&|]+\s*\./', "chmod +x followed by immediate execution"),
    # Sudo with stdin / askpass / shell / list-privs flags. An LLM-driven
    # agent has no TTY, so sudo invocations that succeed without human
    # interaction are those reading the password from stdin (-S/--stdin)
    # or via an askpass helper (-A/--askpass). Plain `sudo cmd` (no flag) is
    # TTY-bound and excluded. Lazy `[^;|&\n]*?` allows flag arguments without
    # spanning command separators.
    (r'\bsudo\b[^;|&\n]*?\s+(?:-s\b|--st[a-z]*\b|-a\b|--a[a-z]*\b)',
     "sudo with privilege flag (stdin/askpass/shell/list)"),
    # Combined short-flag form: -nS, -ns, -sa, -las — sudo flags packed
    # into a single -X token. Catches the same threat class.
    (r'\bsudo\b[^;|&\n]*?\s+-[a-z]*[sa][a-z]*\b',
     "sudo with combined-flag privilege escalation"),
]


# Pre-compiled variant (same rationale as HARDLINE_PATTERNS_COMPILED above).
DANGEROUS_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in DANGEROUS_PATTERNS
]


# =========================================================================
# Detection
# =========================================================================

def _normalize_command_for_detection(command: str) -> str:
    """Normalize a command string before dangerous-pattern matching.

    Strips ANSI escape sequences (full ECMA-48), null bytes, and normalizes
    Unicode fullwidth characters so that obfuscation techniques cannot
    bypass the pattern-based detection.
    """
    # Strip all ANSI escape sequences (CSI, OSC, DCS, 8-bit C1, etc.)
    command = strip_ansi(command)
    # Strip null bytes
    command = command.replace('\x00', '')
    # Normalize Unicode (fullwidth Latin, halfwidth Katakana, etc.)
    command = unicodedata.normalize('NFKC', command)
    # Collapse shell line continuations (backslash-newline). The shell removes
    # BOTH characters and joins the tokens, so `rm -rf \<newline>/` executes as
    # `rm -rf /`. This must run BEFORE the generic backslash-escape strip below,
    # whose [^\n] class deliberately skips newlines and would otherwise leave
    # the dangling backslash wedged between tokens — defeating the structured
    # rm/mkfs/dd patterns (notably the HARDLINE root-delete floor).
    command = re.sub(r'\\\r?\n', '', command)
    # Fold absolute home prefixes into their canonical ~/ form so static
    # user-sensitive patterns catch /home/alice/.bashrc and
    # C:\Users\alice\.bashrc the same way they catch ~/.bashrc. This MUST run
    # before the backslash-escape strip below: on Windows the home prefix is
    # separated by backslashes, which that strip would otherwise dissolve.
    command = _rewrite_resolved_user_home(command)
    # Strip shell backslash-escapes: r\m → rm. Prevents \-injection bypass.
    command = re.sub(r'\\([^\n])', r'\1', command)
    # Strip empty-string literals that split tokens: r''m → rm, r"\"m → rm.
    command = re.sub(r"''|\"\"", '', command)
    # Collapse $IFS / ${IFS} word-separator expansions to a literal space.
    # In any POSIX shell IFS defaults to <space><tab><newline>, so
    # `rm${IFS}-rf${IFS}/` is executed as `rm -rf /`. Because the dangerous
    # and hardline patterns anchor on literal whitespace between a command
    # and its arguments, leaving the unexpanded `${IFS}` token in place lets
    # an attacker slip past EVERY pattern — including the hardline floor.
    command = re.sub(r'\$\{IFS\b[^}]*\}|\$IFS\b', ' ', command)
    return command


# Shell metacharacters, quotes, and whitespace that terminate a filesystem
# path token on a command line. Used to bound the path tail we normalize.
_PATH_TOKEN_STOP = r"""\s'"`;|&<>()"""
# One path segment (no separators, no terminators) preceded by a separator.
_PATH_TAIL = r"(?P<tail>(?:[/\\][^/\\" + _PATH_TOKEN_STOP + r"]*)+)"


@functools.lru_cache(maxsize=64)
def _home_prefix_fold_regex(path: str):
    """Compile a regex matching *path* used as an absolute directory prefix.

    The home components are matched with either separator (``/`` or ``\\``)
    between them, followed by the rest of the path token (the ``tail``
    group), so a Windows native path, its forward-slash form, and
    mixed-separator forms all fold. Returns ``None`` for an unset or
    degenerate path — one with fewer than two components below the root — so
    a stray HOME such as ``/`` or ``C:\\`` cannot rewrite unrelated prefixes.
    """
    if not path:
        return None
    components = [c for c in re.split(r"[/\\]+", path) if c]
    if len(components) < 2:
        return None
    body = r"[/\\]+".join(re.escape(c) for c in components)
    return re.compile(r"[/\\]*" + body + _PATH_TAIL)


def _fold_home_prefixes(command: str, paths, replacement: str) -> str:
    """Fold each resolved home *path* prefix in *command* to *replacement*."""
    seen: set = set()
    for path in sorted((p for p in paths if p), key=len, reverse=True):
        if path in seen:
            continue
        seen.add(path)
        pattern = _home_prefix_fold_regex(path)
        if pattern is not None:
            command = pattern.sub(
                lambda m: replacement + m.group("tail").replace("\\", "/"),
                command,
            )
    return command


def _rewrite_resolved_user_home(command: str) -> str:
    """Rewrite the current user's absolute home prefix to ``~/``.

    Resolves the home at detection time — its expanduser form,
    symlink-resolved form, and an explicitly set ``HOME`` — so absolute home
    paths are checked by the same static patterns as tilde and ``$HOME``
    forms. Matches both POSIX and Windows separators.
    """
    try:
        home = os.path.expanduser("~")
        candidates = [
            home,
            os.path.realpath(home),
            os.environ.get("HOME", ""),
        ]
    except Exception:
        return command
    return _fold_home_prefixes(command, candidates, "~")


_PARAM_REPLACEMENT_RE = re.compile(r"\$\{[^}/\s]+/[^}/]*/(?P<replacement>[^}]*)\}")
_PARAM_DEFAULT_RE = re.compile(r"\$\{[^}:}\s]+:-(?P<default>[^}]*)\}")
_SIMPLE_SHELL_LITERAL_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]+$")
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_COMMAND_WRAPPER_WORDS = {
    "sudo",
    "env",
    "exec",
    "nohup",
    "setsid",
    "time",
    "command",
    "builtin",
}
_SUDO_OPTIONS_WITH_ARG = {
    "-c", "--close-from",
    "-g", "--group",
    "-h", "--host",
    "-p", "--prompt",
    "-u", "--user",
}


def _skip_shell_whitespace(command: str, pos: int) -> int:
    while pos < len(command) and command[pos].isspace():
        pos += 1
    return pos


def _scan_dollar_paren_end(command: str, start: int) -> Optional[int]:
    """Return the offset after a balanced ``$(...)`` command substitution."""
    depth = 1
    quote: Optional[str] = None
    i = start + 2
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            i += 2
            continue
        if command.startswith("$(", i):
            depth += 1
            i += 2
            continue
        if ch == ")":
            depth -= 1
            i += 1
            if depth == 0:
                return i
            continue
        i += 1
    return None


def _scan_backtick_end(command: str, start: int) -> Optional[int]:
    i = start + 1
    while i < len(command):
        if command[i] == "\\" and i + 1 < len(command):
            i += 2
            continue
        if command[i] == "`":
            return i + 1
        i += 1
    return None


def _read_shell_word(command: str, pos: int) -> tuple:
    """Read one shell word without executing expansions."""
    start = _skip_shell_whitespace(command, pos)
    i = start
    quote: Optional[str] = None
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            i += 2
            continue
        if command.startswith("$(", i):
            end = _scan_dollar_paren_end(command, i)
            if end is None:
                i += 2
            else:
                i = end
            continue
        if command.startswith("${", i):
            end = command.find("}", i + 2)
            if end == -1:
                i += 2
            else:
                i = end + 1
            continue
        if ch == "`":
            end = _scan_backtick_end(command, i)
            if end is None:
                i += 1
            else:
                i = end
            continue
        if ch.isspace() or ch in ";&|":
            break
        i += 1
    return (start, i, command[start:i])


def _is_simple_shell_literal(value: str) -> bool:
    return bool(value and _SIMPLE_SHELL_LITERAL_RE.fullmatch(value))


def _literal_command_substitution_output(script: str) -> Optional[str]:
    """Resolve tiny literal command substitutions without executing a shell."""
    try:
        tokens = shlex.split(script, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None

    command = tokens[0].lower()
    args = tokens[1:]
    if command == "echo":
        while args and re.fullmatch(r"-[nEe]+", args[0]):
            args = args[1:]
        if len(args) == 1 and _is_simple_shell_literal(args[0]):
            return args[0]
        return None

    if command == "printf":
        if len(args) == 1 and _is_simple_shell_literal(args[0]):
            return args[0]
        if (
            len(args) == 2
            and args[0] == "%s"
            and _is_simple_shell_literal(args[1])
        ):
            return args[1]
    return None


def _replace_simple_command_substitutions(word: str) -> str:
    chars: list = []
    i = 0
    while i < len(word):
        if word.startswith("$(", i):
            end = _scan_dollar_paren_end(word, i)
            if end is not None:
                replacement = _literal_command_substitution_output(word[i + 2:end - 1])
                if replacement is not None:
                    chars.append(replacement)
                    i = end
                    continue
        if word[i] == "`":
            end = _scan_backtick_end(word, i)
            if end is not None:
                replacement = _literal_command_substitution_output(word[i + 1:end - 1])
                if replacement is not None:
                    chars.append(replacement)
                    i = end
                    continue
        chars.append(word[i])
        i += 1
    return "".join(chars)


def _replace_simple_shell_expansions(word: str) -> str:
    word = _replace_simple_command_substitutions(word)
    word = _PARAM_REPLACEMENT_RE.sub(lambda match: match.group("replacement"), word)
    return _PARAM_DEFAULT_RE.sub(lambda match: match.group("default"), word)


def _strip_shell_word_syntax(word: str) -> str:
    chars: list = []
    quote: Optional[str] = None
    i = 0
    while i < len(word):
        ch = word[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(word):
                chars.append(word[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
                i += 1
                continue
            chars.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(word):
            chars.append(word[i + 1])
            i += 2
            continue
        chars.append(ch)
        i += 1
    return "".join(chars)


def _deobfuscate_shell_word_for_detection(word: str) -> str:
    """Approximate how shell syntax can spell a command word.

    This is intentionally narrow and non-executing: it only collapses shell
    quoting/escaping plus simple literal command substitutions that appear in
    the command word itself.
    """
    deobfuscated = word
    for _ in range(2):
        previous = deobfuscated
        deobfuscated = _replace_simple_shell_expansions(deobfuscated)
        deobfuscated = _strip_shell_word_syntax(deobfuscated)
        if deobfuscated == previous:
            break
    return deobfuscated


def _iter_shell_command_starts(command: str):
    starts = [0]
    quote: Optional[str] = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < len(command):
                i += 2
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
            if command.startswith("$(", i):
                starts.append(i + 2)
                i += 2
                continue
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            i += 2
            continue
        if command.startswith("$(", i):
            starts.append(i + 2)
            i += 2
            continue
        # Bare subshell `(cmd)` and brace group `{ cmd; }` openers begin a new
        # command context, just like `;` or `$(`. We only reach this branch
        # OUTSIDE any quote (the quote arms above `continue` first), so a `(`
        # or `{` sitting inside a quoted argument — `--title "block (reboot)"`
        # — never registers a command start.
        if ch in ("(", "{"):
            starts.append(i + 1)
            i += 1
            continue
        if ch == ";":
            starts.append(i + 1)
            i += 1
            continue
        if ch == "&":
            if i + 1 < len(command) and command[i + 1] == "&":
                starts.append(i + 2)
                i += 2
            else:
                starts.append(i + 1)
                i += 1
            continue
        if ch == "|":
            if i + 1 < len(command) and command[i + 1] == "|":
                starts.append(i + 2)
                i += 2
            else:
                starts.append(i + 1)
                i += 1
            continue
        if ch == "\n":
            starts.append(i + 1)
        i += 1

    seen: set = set()
    for start in starts:
        start = _skip_shell_whitespace(command, start)
        if start < len(command) and start not in seen:
            seen.add(start)
            yield start


def _mark_command_starts(command: str) -> str:
    """Insert a newline before each real (quote-aware) command start.

    ``\\n`` is already a ``_CMDPOS`` separator, so this rewrites subshell
    ``(cmd)`` and brace-group ``{ cmd; }`` openers — which the flat pattern
    class deliberately omits — into a form the anchored hardline/dangerous
    patterns recognize, WITHOUT the quoted-prose false positives that adding
    ``(`` / ``{`` to ``_CMDPOS`` would cause.
    """
    offsets = sorted(o for o in _iter_shell_command_starts(command) if o > 0)
    if not offsets:
        return command
    out = command
    for offset in reversed(offsets):
        out = out[:offset] + "\n" + out[offset:]
    return out


def _iter_shell_command_word_spans(command: str):
    """Yield command-position words that may be executable names."""
    for command_start in _iter_shell_command_starts(command):
        pos = command_start
        prefix_words = 0
        skip_wrapper_options = False
        skip_next_wrapper_arg = False
        while prefix_words < 12:
            word_start, word_end, word = _read_shell_word(command, pos)
            if word_start == word_end:
                break
            deobfuscated = _deobfuscate_shell_word_for_detection(word)
            lower_word = deobfuscated.lower()
            if skip_next_wrapper_arg:
                skip_next_wrapper_arg = False
                pos = word_end
                prefix_words += 1
                continue
            if skip_wrapper_options and lower_word.startswith("-"):
                option_name = lower_word.split("=", 1)[0]
                skip_next_wrapper_arg = (
                    "=" not in lower_word
                    and option_name in _SUDO_OPTIONS_WITH_ARG
                )
                pos = word_end
                prefix_words += 1
                continue

            yield (word_start, word_end, word)
            prefix_words += 1

            if lower_word in _COMMAND_WRAPPER_WORDS:
                skip_wrapper_options = lower_word in {"sudo", "env"}
                pos = word_end
                continue
            if _ENV_ASSIGNMENT_RE.fullmatch(deobfuscated):
                skip_wrapper_options = False
                pos = word_end
                continue
            break


def _command_detection_variants(command: str):
    normalized = _normalize_command_for_detection(command)
    seen = {normalized}
    yield normalized
    # Subshell `(cmd)` and brace-group `{ cmd; }` openers put `cmd` at a real
    # command position, but the flat `_CMDPOS`-anchored patterns can't see it.
    # Reconstruct the command with a newline (already a `_CMDPOS` separator)
    # inserted at each command start the QUOTE-AWARE tokenizer found.
    marked = _mark_command_starts(normalized)
    if marked != normalized and marked not in seen:
        seen.add(marked)
        yield marked
    # Shell quoting/escaping can spell a dangerous executable name in pieces
    # (for example r\m or r''m). Keep that deobfuscation scoped to command
    # words so similarly shaped arguments do not become false positives.
    for word_start, word_end, word in _iter_shell_command_word_spans(normalized):
        deobfuscated = _deobfuscate_shell_word_for_detection(word)
        if not deobfuscated or deobfuscated == word:
            continue
        variant = normalized[:word_start] + deobfuscated + normalized[word_end:]
        if variant in seen:
            continue
        seen.add(variant)
        yield variant


def _is_verification_artifact_cleanup(command: str) -> bool:
    """Return whether *command* only removes one Aegis ad-hoc temp script."""
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False
    if len(argv) != 3 or argv[0] != "rm" or argv[1] != "-f":
        return False

    operand = argv[2]
    temp_dir = os.path.realpath(tempfile.gettempdir())
    basename = os.path.basename(operand)
    if operand != os.path.join(temp_dir, basename):
        return False

    target = os.path.realpath(operand)
    if os.path.dirname(target) != temp_dir:
        return False
    return re.fullmatch(r"aegis-(?:verify|ad-hoc)-[A-Za-z0-9_.-]+", basename) is not None


def detect_dangerous_command(command: str) -> tuple:
    """Check if a command matches any dangerous patterns.

    Returns:
        (is_dangerous, pattern_key, description) or (False, None, None)
    """
    if _is_verification_artifact_cleanup(command):
        return (False, None, None)

    for command_variant in _command_detection_variants(command):
        command_lower = command_variant.lower()
        for pattern_re, description in DANGEROUS_PATTERNS_COMPILED:
            if pattern_re.search(command_lower):
                return (True, description, description)
    return (False, None, None)


# =========================================================================
# Per-session approval state (thread-safe)
# =========================================================================

_lock = threading.Lock()
_session_approved: dict = {}       # session_key → set(pattern_key)
_session_yolo: set = set()
_permanent_approved: set = set()
_permanent_loaded = False


def approve_session(session_key: str, pattern_key: str):
    """Approve a pattern for this session only."""
    with _lock:
        _session_approved.setdefault(session_key, set()).add(pattern_key)


def enable_session_yolo(session_key: str) -> None:
    """Enable YOLO bypass for a single session key."""
    if not session_key:
        return
    with _lock:
        _session_yolo.add(session_key)


def disable_session_yolo(session_key: str) -> None:
    """Disable YOLO bypass for a single session key."""
    if not session_key:
        return
    with _lock:
        _session_yolo.discard(session_key)


def clear_session(session_key: str) -> None:
    """Remove all approval and yolo state for a given session."""
    if not session_key:
        return
    with _lock:
        _session_approved.pop(session_key, None)
        _session_yolo.discard(session_key)


def is_session_yolo_enabled(session_key: str) -> bool:
    if not session_key:
        return False
    with _lock:
        return session_key in _session_yolo


def is_approved(session_key: str, pattern_key: str) -> bool:
    """Check if a pattern is approved (session-scoped or permanent)."""
    _ensure_permanent_loaded()
    with _lock:
        if pattern_key in _permanent_approved:
            return True
        return pattern_key in _session_approved.get(session_key, set())


def approve_permanent(pattern_key: str):
    """Add a pattern to the permanent allowlist (and persist it)."""
    _ensure_permanent_loaded()
    with _lock:
        _permanent_approved.add(pattern_key)
        patterns = set(_permanent_approved)
    save_permanent_allowlist(patterns)


_ALLOWLIST_SHELL_OPERATOR_RE = re.compile(r"(?:\n|&&|\|\||[;&|<>`]|\$\()")


def _has_allowlist_shell_operator(command: str) -> bool:
    """Return True when a command is too compound for the allowlist shortcut."""
    return bool(_ALLOWLIST_SHELL_OPERATOR_RE.search(command or ""))


def _command_matches_permanent_allowlist(command: str) -> bool:
    """Return True when the allowlist contains this command or a glob.

    Permanent approvals historically store dangerous-pattern keys such as
    ``recursive delete``. Manual entries may be command text, and may include
    shell-style wildcards like ``podman *``.
    """
    command = (command or "").strip()
    if not command:
        return False
    if _has_allowlist_shell_operator(command):
        return False

    _ensure_permanent_loaded()
    with _lock:
        patterns = tuple(_permanent_approved)

    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        pattern = pattern.strip()
        if not pattern:
            continue
        if command == pattern:
            return True
        if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(command, pattern):
            return True
    return False


# =========================================================================
# Permanent allowlist persistence (data/command_allowlist.json)
# =========================================================================

def _ensure_permanent_loaded() -> None:
    global _permanent_loaded
    if _permanent_loaded:
        return
    patterns = load_permanent_allowlist()
    with _lock:
        _permanent_approved.update(patterns)
    _permanent_loaded = True


def load_permanent_allowlist() -> set:
    """Load permanently allowed command patterns from disk."""
    try:
        with open(_ALLOWLIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {p for p in data if isinstance(p, str) and p.strip()}
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Failed to load permanent allowlist: %s", e)
    return set()


def save_permanent_allowlist(patterns: set):
    """Save permanently allowed command patterns to disk."""
    try:
        from core.atomic_io import atomic_write_json
        atomic_write_json(_ALLOWLIST_FILE, sorted(patterns), indent=2)
    except Exception as e:
        logger.warning("Could not save allowlist: %s", e)


# =========================================================================
# Config (settings.json keys)
# =========================================================================

def _get_approval_mode() -> str:
    """``command_approval_mode``: "manual" (default) or "off"."""
    try:
        from src.settings import get_setting
        mode = str(get_setting("command_approval_mode", "manual") or "manual").lower()
    except Exception:
        return "manual"
    if mode not in ("manual", "off"):
        logger.warning("Unknown command_approval_mode %r — defaulting to 'manual'.", mode)
        return "manual"
    return mode


def _get_approval_timeout() -> int:
    """Read the approval timeout from settings. Defaults to 60 seconds."""
    try:
        from src.settings import get_setting
        return int(get_setting("command_approval_timeout", 60))
    except (ValueError, TypeError, Exception):
        return 60


def _match_user_deny_rule(command: str) -> Optional[str]:
    """Return the matching ``command_approval_deny`` glob, or None.

    A user-defined list of fnmatch globs that block a command
    unconditionally — like the hardline floor, a deny match fires BEFORE the
    yolo / mode=off bypass: "never let the agent run this, even under yolo".

    Matching is case-insensitive and runs over the same normalized /
    deobfuscated command variants the dangerous-pattern detector uses, so
    quoting tricks (``r\\m``, ``git st""atus``) can't sidestep a rule any
    more easily than they sidestep detection.
    """
    try:
        from src.settings import get_setting
        deny_patterns = get_setting("command_approval_deny", None) or []
    except Exception:
        return None
    if not deny_patterns:
        return None
    globs = [p.strip() for p in deny_patterns
             if isinstance(p, str) and p.strip()]
    if not globs:
        return None
    for command_variant in _command_detection_variants(command):
        candidate = command_variant.lower().strip()
        for pattern in globs:
            if fnmatch.fnmatchcase(candidate, pattern.lower()):
                return pattern
    return None


# =========================================================================
# Block-result builders
# =========================================================================

def _user_deny_block_result(pattern: str) -> dict:
    return {
        "approved": False,
        "user_deny": True,
        "message": (
            f"BLOCKED: this command matches the user-defined deny rule "
            f"'{pattern}' (command_approval_deny in settings). It cannot be "
            "executed via the agent — not even with AEGIS_YOLO_MODE or "
            "command_approval_mode=off. Do NOT retry or rephrase this "
            "command; the user has explicitly forbidden it."
        ),
    }


def _hardline_block_result(description: str) -> dict:
    return {
        "approved": False,
        "hardline": True,
        "message": (
            f"BLOCKED (hardline): {description}. "
            "This command is on the unconditional blocklist and cannot "
            "be executed via the agent — not even with AEGIS_YOLO_MODE or "
            "command_approval_mode=off. If you genuinely need to run it, "
            "run it yourself in a terminal outside the agent."
        ),
    }


def _sudo_stdin_block_result(description: str) -> dict:
    return {
        "approved": False,
        "message": (
            f"BLOCKED: {description}. "
            "Do not pipe passwords to 'sudo -S' — this is a brute-force "
            "attack vector. Set SUDO_PASSWORD in your .env file if the "
            "agent needs passwordless sudo, or run the sudo command "
            "manually in your own terminal."
        ),
    }


def _deny_block_result(description: str, *, timed_out: bool) -> dict:
    # Consent contract: silence is NOT consent, and an explicit deny is also
    # a hard halt — both produce a BLOCKED outcome that names the agent's
    # most common evasion paths (retry, rephrase, achieve the same outcome
    # via a different command).
    reason = "timed out without user response" if timed_out else "denied by user"
    timeout_addendum = " Silence is not consent." if timed_out else ""
    return {
        "approved": False,
        "message": (
            f"BLOCKED: Command {reason}. The user has NOT consented to this "
            f"action. Do NOT retry this command, do NOT rephrase it, and do "
            f"NOT attempt the same outcome via a different command. Stop the "
            f"current workflow and wait for the user to respond before "
            f"taking any further destructive or irreversible "
            f"action.{timeout_addendum}"
        ),
        "description": description,
        "outcome": "timeout" if timed_out else "denied",
        "user_consent": False,
    }


# =========================================================================
# Async approval gate (pending registry + resolve endpoint bridge)
# =========================================================================

# approval_id → {"event": asyncio.Event, "choice": str|None, "data": dict}
_pending_approvals: dict = {}


def resolve_approval(
    approval_id: str,
    choice: str,
    *,
    owner: Optional[str] = None,
) -> bool:
    """Resolve a pending approval from the API route.

    choice: 'once' | 'session' | 'always' | 'deny'
    When ``owner`` is provided, the approval must belong to that user.
    Returns False when the approval id is unknown, expired, already resolved,
    or owned by someone else.
    """
    if choice not in ("once", "session", "always", "deny"):
        return False
    entry = _pending_approvals.get(approval_id)
    if entry is None or entry.get("choice") is not None:
        return False
    if owner is not None and entry.get("owner") != owner:
        return False
    entry["choice"] = choice
    entry["event"].set()
    return True


def list_pending_approvals(
    session_id: Optional[str] = None,
    *,
    owner: Optional[str] = None,
) -> list:
    """Pending approval requests (for reconnecting clients)."""
    out = []
    for approval_id, entry in _pending_approvals.items():
        if owner is not None and entry.get("owner") != owner:
            continue
        data = entry.get("data") or {}
        if session_id and data.get("session_id") != session_id:
            continue
        out.append({"approval_id": approval_id, **data})
    return out


async def check_command_guard(
    command: str,
    *,
    session_id: str = "",
    owner: Optional[str] = None,
    env_type: str = "local",
    has_host_access: bool = False,
    emit_event=None,
) -> dict:
    """Run all pre-exec security checks and return a single approval decision.

    This is the main entry point called before executing any agent shell
    command. Mirrors Hermes ``check_all_command_guards``: hardline floor →
    sudo stdin guard → user deny rules → yolo/mode-off bypass → permanent
    allowlist → detection → session approvals → interactive approval.

    ``emit_event`` is an async callable used to push the approval request to
    the user's stream (as an ``approval_request`` SSE event). Without it
    there is no human to ask, so a dangerous command fails CLOSED.

    Returns {"approved": True/False, "message": str or None, ...}.
    """
    # Isolated container backends skip the dangerous-command layer entirely —
    # nothing they run can touch the host. Docker stops skipping once host
    # paths are bind-mounted into the sandbox.
    if env_type == "docker" and not has_host_access:
        return {"approved": True, "message": None}

    # Hardline floor: commands with no recovery path are blocked
    # unconditionally, BEFORE the yolo bypass.
    is_hardline, hardline_desc = detect_hardline_command(command)
    if is_hardline:
        logger.warning("Hardline block: %s (command: %s)", hardline_desc, command[:200])
        return _hardline_block_result(hardline_desc)

    # Sudo stdin guard: unconditional, fires BEFORE the yolo check so even
    # yolo/mode=off cannot bypass it.
    is_sudo_guess, sudo_guess_desc = _check_sudo_stdin_guard(command)
    if is_sudo_guess:
        logger.warning("Sudo stdin guard block: %s (command: %s)",
                       sudo_guess_desc, command[:200])
        return _sudo_stdin_block_result(sudo_guess_desc)

    # User-defined deny rules: like the hardline floor, these fire BEFORE
    # the yolo / mode=off bypass.
    deny_pattern = _match_user_deny_rule(command)
    if deny_pattern is not None:
        logger.warning("User deny rule %r blocked command: %s",
                       deny_pattern, command[:200])
        return _user_deny_block_result(deny_pattern)

    # Yolo or command_approval_mode=off: bypass all approval prompts.
    if (
        _YOLO_MODE_FROZEN
        or is_session_yolo_enabled(session_id)
        or _get_approval_mode() == "off"
    ):
        return {"approved": True, "message": None}

    if _command_matches_permanent_allowlist(command):
        return {"approved": True, "message": None}

    is_dangerous, pattern_key, description = detect_dangerous_command(command)
    if not is_dangerous:
        return {"approved": True, "message": None}

    if is_approved(session_id, pattern_key):
        return {"approved": True, "message": None}

    if emit_event is None:
        # No human channel (scheduled task, background context) — fail closed.
        return {
            "approved": False,
            "message": (
                f"BLOCKED: Command flagged as dangerous ({description}) but "
                "this run has no user present to approve it. Find an "
                "alternative approach that avoids this command."
            ),
            "description": description,
        }

    approval_id = uuid.uuid4().hex
    entry = {
        "event": asyncio.Event(),
        "choice": None,
        # Canonicalize single-user/auth-disabled requests to the same empty
        # owner returned by require_user(), so their approval route can match.
        "owner": owner or "",
        "data": {
            "command": command,
            "pattern_key": pattern_key,
            "description": description,
            "session_id": session_id,
        },
    }
    _pending_approvals[approval_id] = entry
    try:
        await emit_event({
            "approval_request": {
                "approval_id": approval_id,
                "command": command,
                "description": description,
                "pattern_key": pattern_key,
            }
        })
        try:
            await asyncio.wait_for(
                entry["event"].wait(), timeout=_get_approval_timeout()
            )
        except asyncio.TimeoutError:
            pass
    finally:
        _pending_approvals.pop(approval_id, None)

    choice = entry["choice"]
    if choice in ("once", "session", "always"):
        if choice == "session":
            approve_session(session_id, pattern_key)
        elif choice == "always":
            approve_session(session_id, pattern_key)
            approve_permanent(pattern_key)
        return {"approved": True, "message": None,
                "user_approved": True, "description": description}

    return _deny_block_result(description, timed_out=choice is None)
