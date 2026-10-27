"""Showcase check: runs before every commit, before every push and in CI.

This repository is public. It may hold only the README (architecture diagram, safety design, tech stack), this
check, its hooks, its CI workflow, a licence and .gitignore. The check refuses:
- any file outside that allowlist (so no strategy file, data file, setting or result can be added);
- any key-shaped string or email address, in any file;
- in the README: any digit, number word, money or percent sign, or all-capital token outside a short allowlist
  (so no parameter, holding or performance figure can be written into it). The one exception is a single line that
  matches the test-count template exactly; GitHub Actions in the private repository writes it.
Messages name the file, rule and line, never the matched text. Standard library only.

Usage:
  python3 tools/check.py --staged       pre-commit: the staged files
  python3 tools/check.py --pre-push     pre-push: every commit being pushed (git passes the refs on stdin)
  python3 tools/check.py --history      CI: every commit reachable from HEAD
  python3 tools/check.py --dir PATH     a plain folder
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ALLOWED_FILES = frozenset({"README.md", "LICENSE", ".gitignore", ".githooks/pre-commit", ".githooks/pre-push",
                           "tools/check.py", ".github/workflows/check.yml"})
PROSE_FILES = frozenset({"README.md"})
MAX_BYTES = 1_000_000
ZERO_SHA = "0" * 40

_TOKEN = r"[A-Za-z0-9_\-]"
SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github_fine_grained_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("anthropic_token", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}")),
    ("healthchecks_ping_url", re.compile(r"hc-ping\.com/[A-Za-z0-9_\-]{10,}")),
    ("key_id", re.compile(r"\b(?:PK|AK|CK|AKIA)[A-Z0-9]{16,30}\b")),
    ("email_address", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")),
]
# Any long run mixing lower case, upper case and digits: the shape of most API keys and secrets.
LONG_TOKEN = re.compile(rf"(?<!{_TOKEN})(?=[A-Za-z0-9_\-]*[a-z])(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[0-9])"
                        rf"{_TOKEN}{{32,}}(?!{_TOKEN})")
ASSIGNED = re.compile(r"(?i)\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|KEY_ID)[A-Z0-9_]*\s*[:=]\s*['\"]?"
                      r"([A-Za-z0-9/+_\-]{16,})")

# README rules. Only "SHA-256" may carry digits, plus the single test-count line that GitHub Actions in the private
# repository writes under the marker; every other figure is refused.
DIGITS_ALLOWED = re.compile(r"\bSHA-256\b")
TEST_COUNT_MARKER = "<!-- test-count: written by GitHub Actions in the private repository; do not edit -->"
TEST_COUNT_TEMPLATE = "**Tests:** {n} automated tests pass in the private repository, counted by its GitHub Actions run."
TEST_COUNT_LINE = re.compile("^" + re.escape(TEST_COUNT_TEMPLATE).replace(re.escape("{n}"), "([1-9][0-9]{0,5})") + "$")
NUMBER_WORDS = re.compile(
    r"(?i)\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|"
    r"billion|dozen|half|halves|quarter|third|percent|percentage|per cent|basis points?|bps?|double|triple|twice)\b")
MONEY_OR_PERCENT = re.compile(r"[%£$€¥‰]")
CAPITAL_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Z]{1,5}(?![A-Za-z0-9_])")
CAPITALS_ALLOWED = frozenset({"A", "I", "API", "REST", "SEC", "EDGAR", "XBRL", "UTC", "UK", "US", "SHA",
                              "CSV", "YAML", "JSON", "HTTP", "HTTPS", "CLI", "LLM", "AI", "TDD", "OS", "PR", "MIT", "TD"})


def _scan_secrets(text: str) -> list[tuple[str, int]]:
    hits = []
    for n, line in enumerate(text.splitlines(), start=1):
        rules = [name for name, rx in SECRET_PATTERNS if rx.search(line)]
        rules += ["assigned_secret" for m in ASSIGNED.finditer(line) if _mixed(m.group(1))][:1]
        if not rules and LONG_TOKEN.search(line):
            rules.append("long_token")
        hits += [(rule, n) for rule in rules]
    return hits


def _mixed(value: str) -> bool:
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def _scan_prose(text: str) -> list[tuple[str, int]]:
    hits = []
    count_lines = 0
    for n, line in enumerate(text.splitlines(), start=1):
        if TEST_COUNT_LINE.match(line):
            count_lines += 1
            if count_lines > 1:
                hits.append(("test_count_repeated", n))
            continue                                   # an exact match holds nothing but the count
        if any(c.isdigit() for c in DIGITS_ALLOWED.sub("", line)):
            hits.append(("digit", n))
        if NUMBER_WORDS.search(line):
            hits.append(("number_word", n))
        if MONEY_OR_PERCENT.search(line):
            hits.append(("percent_or_money", n))
        if any(t not in CAPITALS_ALLOWED for t in CAPITAL_TOKEN.findall(line)):
            hits.append(("capital_token", n))
    return hits


def check_files(files: dict[str, bytes]) -> list[str]:
    """Problems for a set of files (repo-relative posix path -> content); an empty list means clean."""
    problems = [] if "README.md" in files else ["README.md: missing"]
    for path in sorted(files):
        content = files[path]
        if path not in ALLOWED_FILES:
            problems.append(f"{path}: not_allowed")
            continue
        if len(content) > MAX_BYTES:
            problems.append(f"{path}: too_large")
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{path}: not_utf8")
            continue
        hits = _scan_secrets(text) + (_scan_prose(text) if path in PROSE_FILES else [])
        problems += [f"{path}: {rule} (line {n})" for rule, n in hits]
    return problems


# --- reading files from git or a folder -----------------------------------------------------------------------------

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True)


def _git_out(*args: str) -> str:
    result = _git(*args)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode()


def staged_files() -> dict[str, bytes]:
    names = [n for n in _git_out("ls-files", "-z").split("\0") if n]
    return {n: _git("show", f":{n}").stdout for n in names}


def commit_files(sha: str) -> dict[str, bytes]:
    names = [n for n in _git_out("ls-tree", "-r", "-z", "--name-only", sha).split("\0") if n]
    return {n: _git("show", f"{sha}:{n}").stdout for n in names}


def commits_to_push(lines: list[str]) -> list[str]:
    commits: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO_SHA:
            continue                                   # malformed, or a branch deletion
        local, remote = parts[1], parts[3]
        result = _git("rev-list", local, f"^{remote}") if remote != ZERO_SHA else None
        if result is None or result.returncode != 0:   # new branch, or the remote commit is not here
            result = _git("rev-list", local, "--not", "--remotes")
        commits += [c for c in result.stdout.decode().split() if c not in commits]
    return commits


IGNORED_IN_FOLDERS = frozenset({".git", "__pycache__", ".DS_Store"})   # never committed (see .gitignore)


def folder_files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and not IGNORED_IN_FOLDERS.intersection(p.relative_to(root).parts)}


def _check_commits(commits: list[str]) -> list[str]:
    problems: list[str] = []
    for sha in commits:
        problems += [f"commit {sha[:12]}: {p}" for p in check_files(commit_files(sha))]
    return problems


def main(argv: list[str]) -> int:
    if argv[:1] == ["--staged"]:
        problems = check_files(staged_files())
    elif argv[:1] == ["--pre-push"]:
        problems = _check_commits(commits_to_push(sys.stdin.read().splitlines()))
    elif argv[:1] == ["--history"]:
        problems = _check_commits(_git_out("rev-list", "HEAD").split())
    elif argv[:1] == ["--dir"] and len(argv) == 2:
        problems = check_files(folder_files(Path(argv[1])))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    if problems:
        print("showcase check: blocked", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
