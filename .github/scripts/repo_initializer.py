#!/usr/bin/env python3
import os
import sys
import time
import yaml
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional
import requests

# ============================================================
# CONFIG
# ============================================================

API = "https://api.github.com"

GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not GH_TOKEN:
    print("[ERROR] missing GH_TOKEN", file=sys.stderr)
    sys.exit(2)

HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

REGISTRY_FILE = Path(".github/registry/repos.yml")
WORKDIR_BASE = Path("_work")

TEMPLATE_REPO_PATH = Path(".github/templates/repo")

DISPATCH_SITE = os.environ.get("DISPATCH_SITE", "site-template-updated")
DISPATCH_README = os.environ.get("DISPATCH_README", "readme-template-updated")

WAIT_TIMEOUT_S = 10.0        # tempo máximo total
WAIT_SLEEP_S = 0.5           # 🔴 espera curta pedida por você

# ============================================================
# LOGGING
# ============================================================

def log(repo: str, msg: str):
    print(f"[INIT] {repo}: {msg}", flush=True)

def err(repo: str, step: str, msg: str):
    print(f"[ERROR] {repo} step={step}: {msg}", file=sys.stderr, flush=True)

# ============================================================
# HTTP / GITHUB
# ============================================================

def gh(method: str, url: str, **kw) -> requests.Response:
    return requests.request(method, url, headers=HEADERS, timeout=30, **kw)

def repo_exists(org: str, repo: str) -> bool:
    return gh("GET", f"{API}/repos/{org}/{repo}").status_code == 200

def create_repo(org: str, repo: str, desc: str, private: bool):
    payload = {
        "name": repo,
        "description": desc,
        "private": private,
        "auto_init": True,
        "has_issues": False,
        "has_projects": False,
        "has_wiki": False,
    }
    r = gh("POST", f"{API}/orgs/{org}/repos", json=payload)
    if r.status_code not in (201, 422):
        raise RuntimeError(r.text)

def enable_pages_once(org: str, repo: str):
    payload = {"source": {"branch": "gh-pages", "path": "/"}}

    r = gh("GET", f"{API}/repos/{org}/{repo}/pages")
    if r.status_code == 200:
        src = (r.json().get("source") or {})
        if src.get("branch") == "gh-pages":
            return "noop"

    r = gh("POST", f"{API}/repos/{org}/{repo}/pages", json=payload)
    if r.status_code in (201, 204):
        return "enabled"

    if r.status_code == 422:
        return "wait"

    r = gh("PUT", f"{API}/repos/{org}/{repo}/pages", json=payload)
    if r.status_code in (200, 201, 204):
        return "enabled"

    raise RuntimeError(f"enable_pages failed {r.status_code} {r.text}")

def dispatch(org: str, repo: str, event: str):
    r = gh("POST", f"{API}/repos/{org}/{repo}/dispatches", json={"event_type": event})
    if r.status_code != 204:
        raise RuntimeError(r.text)

# ============================================================
# GIT
# ============================================================

def run(cmd: List[str], cwd: Optional[Path] = None):
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None)

def out(cmd: List[str], cwd: Optional[Path] = None) -> str:
    return subprocess.check_output(cmd, cwd=str(cwd) if cwd else None).decode().strip()

def remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    r = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    return bool(r.stdout.strip())

def ensure_repo_clone(org: str, repo: str) -> Path:
    d = WORKDIR_BASE / f"{org}__{repo}"
    if d.exists():
        shutil.rmtree(d)

    run(["git", "clone", f"https://x-access-token:{GH_TOKEN}@github.com/{org}/{repo}.git", str(d)])
    return d

def ensure_main_and_workflows(repo_dir: Path) -> bool:
    run(["git", "checkout", "-B", "main"], cwd=repo_dir)

    run(["rsync", "-a", "--delete",
         f"{TEMPLATE_REPO_PATH}/", f"{repo_dir}/"])

    if not out(["git", "status", "--porcelain"], cwd=repo_dir):
        return False

    run(["git", "add", "."], cwd=repo_dir)
    run(["git", "commit", "-m", "chore: sync repo infra"], cwd=repo_dir)
    run(["git", "push", "-u", "origin", "main"], cwd=repo_dir)
    return True

# ============================================================
# WAIT gh-pages (0.5s polling)
# ============================================================

def wait_for_gh_pages(repo_dir: Path) -> bool:
    deadline = time.time() + WAIT_TIMEOUT_S
    while time.time() < deadline:
        if remote_branch_exists(repo_dir, "gh-pages"):
            return True
        time.sleep(WAIT_SLEEP_S)
    return False

# ============================================================
# CORE
# ============================================================

def process_repo(entry: Dict):
    org = entry.get("org", "").strip()
    repo = entry.get("name", "").strip()
    desc = entry.get("description", "").strip()
    private = bool(entry.get("private", False))
    full = f"{org}/{repo}"

    if not org or not repo:
        return

    log(full, "start")

    if not repo_exists(org, repo):
        create_repo(org, repo, desc, private)
        log(full, "repo created")

    repo_dir = ensure_repo_clone(org, repo)

    changed = ensure_main_and_workflows(repo_dir)
    if changed:
        log(full, "main updated (push triggered workflow)")
    else:
        dispatch(org, repo, DISPATCH_SITE)
        log(full, "dispatch site (no push)")
        dispatch(org, repo, DISPATCH_README)

    if not wait_for_gh_pages(repo_dir):
        log(full, "gh-pages not yet visible; Pages will be enabled later")
        return

    st = enable_pages_once(org, repo)
    if st == "enabled":
        log(full, "Pages enabled (gh-pages:/)")
    else:
        log(full, "Pages already enabled")

def main():
    data = yaml.safe_load(REGISTRY_FILE.read_text()) or {}
    for entry in data.get("repos", []):
        try:
            process_repo(entry)
        except Exception as e:
            err(f"{entry.get('org')}/{entry.get('name')}", "process", str(e))

if __name__ == "__main__":
    main()