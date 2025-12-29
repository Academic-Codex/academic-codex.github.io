#!/usr/bin/env python3
import os
import subprocess
import requests
import yaml
from pathlib import Path
from typing import Optional, List

# =========================================================
# CONFIG
# =========================================================

API = "https://api.github.com"
GH_TOKEN = os.environ["GH_TOKEN"]

HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}

CENTRAL_WORKFLOWS_DIR = Path(".github/workflows")
REGISTRY_FILE = Path(".github/registry/repos.yml")
WORKDIR_BASE = Path("_work")

# =========================================================
# HELPERS
# =========================================================

def log(*a):
    print("[init]", *a, flush=True)


def gh(method: str, url: str, **kw) -> requests.Response:
    return requests.request(method, url, headers=HEADERS, **kw)


def run(cmd: List[str], cwd: Optional[Path] = None) -> None:
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None)


def git_has_changes(path: Path) -> bool:
    out = subprocess.check_output(
        ["git", "status", "--porcelain"],
        cwd=str(path)
    ).decode().strip()
    return bool(out)

# =========================================================
# GITHUB PRIMITIVES (IDEMPOTENTES)
# =========================================================

def create_repo(org: str, repo: str, desc: str = "", private: bool = False) -> None:
    payload = {
        "name": repo,
        "description": desc,
        "private": private,
        "auto_init": False,
        "has_issues": False,
        "has_projects": False,
        "has_wiki": False,
    }

    r = gh("POST", f"{API}/orgs/{org}/repos", json=payload)

    if r.status_code == 201:
        log(f"created repo {org}/{repo}")
        return

    if r.status_code == 422:
        log(f"repo already exists {org}/{repo}")
        return

    raise RuntimeError(f"create_repo failed {org}/{repo}: {r.status_code} {r.text}")


def enable_pages(org: str, repo: str) -> None:
    payload = {"source": {"branch": "gh-pages", "path": "/"}}

    r_post = gh("POST", f"{API}/repos/{org}/{repo}/pages", json=payload)
    r_put  = gh("PUT",  f"{API}/repos/{org}/{repo}/pages", json=payload)

    if r_post.status_code in (201, 204) or r_put.status_code in (200, 201, 204):
        log(f"pages configured {org}/{repo}")
        return

    r_get = gh("GET", f"{API}/repos/{org}/{repo}/pages")
    if r_get.status_code == 200:
        log(f"pages already enabled {org}/{repo}")
        return

    raise RuntimeError(
        f"enable_pages failed {org}/{repo}: "
        f"post={r_post.status_code} put={r_put.status_code}"
    )


def dispatch(org: str, repo: str, event: str) -> None:
    body = {"event_type": event}

    r = gh("POST", f"{API}/repos/{org}/{repo}/dispatches", json=body)

    if r.status_code != 204:
        raise RuntimeError(
            f"dispatch failed {org}/{repo} {event}: {r.status_code} {r.text}"
        )

    log(f"dispatched {event} -> {org}/{repo}")

# =========================================================
# CORE LOGIC
# =========================================================

def sync_workflows(repo_dir: Path) -> None:
    run(["git", "checkout", "main"], cwd=repo_dir)

    run([
        "rsync", "-a", "--delete",
        f"{CENTRAL_WORKFLOWS_DIR}/",
        f"{repo_dir}/.github/workflows/"
    ])

    if git_has_changes(repo_dir):
        run(["git", "add", ".github/workflows"], cwd=repo_dir)
        run(["git", "commit", "-m", "chore: sync repo infra"], cwd=repo_dir)
        run(["git", "push", "origin", "main"], cwd=repo_dir)
        log("infra committed + pushed")
    else:
        log("no infra changes to commit")


def process_repo(entry: dict) -> None:
    org  = entry["org"]
    repo = entry["name"]
    desc = entry.get("title", "")

    log(f"--- {org}/{repo} ---")

    create_repo(org, repo, desc)

    repo_dir = WORKDIR_BASE / f"{org}__{repo}"
    repo_dir.parent.mkdir(exist_ok=True)

    if repo_dir.exists():
        log("repo exists locally → reuse")
        run(["git", "fetch", "origin"], cwd=repo_dir)
    else:
        run([
            "git", "clone",
            f"https://github.com/{org}/{repo}.git",
            str(repo_dir)
        ])

    sync_workflows(repo_dir)

    enable_pages(org, repo)

    dispatch(org, repo, "site-template-updated")
    dispatch(org, repo, "readme-template-updated")

# =========================================================
# MAIN
# =========================================================

def main():
    data = yaml.safe_load(REGISTRY_FILE.read_text(encoding="utf-8"))
    repos = data.get("repos", [])

    if not isinstance(repos, list):
        raise RuntimeError("registry.repos must be a list")

    for entry in repos:
        process_repo(entry)


if __name__ == "__main__":
    main()