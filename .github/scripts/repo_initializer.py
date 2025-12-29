#!/usr/bin/env python3
import os
import sys
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import requests
import yaml

# =========================
# CONFIG (central repo)
# =========================
API = "https://api.github.com"

GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not GH_TOKEN:
    print("[ERROR] missing GH_TOKEN", file=sys.stderr)
    sys.exit(2)

HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}

# Registry no repo central (este repo onde o workflow roda)
REGISTRY_FILE = Path(".github/registry/repos.yml")

# ✅ TEMPLATE CORRETO: workflows a serem injetados nos repos-alvo
# Ajuste aqui se o seu template estiver em outro path.
TEMPLATE_WORKFLOWS_DIR = Path(".github/templates/repo/.github/workflows")

WORKDIR_BASE = Path("_work")

# Dispatch events (nomes exatos que seus listeners esperam)
EVENT_SITE = "site-template-updated"
EVENT_README = "readme-template-updated"

FORCE = (os.environ.get("FORCE", "false").lower() == "true")

# =========================
# LOGGING (mínimo)
# =========================
def log(repo_full: str, msg: str) -> None:
    print(f"{repo_full}: {msg}", flush=True)

def err(repo_full: str, step: str, msg: str) -> None:
    print(f"[ERROR] {repo_full} step={step} {msg}", file=sys.stderr, flush=True)

# =========================
# HTTP
# =========================
def gh(method: str, url: str, **kw) -> requests.Response:
    return requests.request(method, url, headers=HEADERS, **kw)

def repo_exists(org: str, repo: str) -> bool:
    r = gh("GET", f"{API}/repos/{org}/{repo}")
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise RuntimeError(f"repo_exists unexpected: {r.status_code} {r.text}")

def create_repo(org: str, repo: str, desc: str = "", private: bool = False) -> bool:
    """
    Returns True if created, False if already existed.
    """
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
        return True
    if r.status_code == 422:
        return False
    raise RuntimeError(f"create_repo failed: {r.status_code} {r.text}")

def get_pages(org: str, repo: str) -> Optional[dict]:
    r = gh("GET", f"{API}/repos/{org}/{repo}/pages")
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None
    raise RuntimeError(f"get_pages unexpected: {r.status_code} {r.text}")

def ensure_pages(org: str, repo: str, branch: str = "gh-pages", path: str = "/") -> str:
    """
    Returns:
      - "enabled" if it enabled pages
      - "updated" if it changed config
      - "noop" if already ok
    """
    desired = {"source": {"branch": branch, "path": path}}

    current = get_pages(org, repo)
    if current is None:
        r = gh("POST", f"{API}/repos/{org}/{repo}/pages", json=desired)
        if r.status_code in (201, 204):
            return "enabled"
        # fallback PUT
        r2 = gh("PUT", f"{API}/repos/{org}/{repo}/pages", json=desired)
        if r2.status_code in (200, 201, 204):
            return "enabled"
        raise RuntimeError(f"enable_pages failed: post={r.status_code} put={r2.status_code}")

    cur_source = (current.get("source") or {})
    cur_branch = cur_source.get("branch")
    cur_path = cur_source.get("path")

    if (cur_branch == branch and cur_path == path) and not FORCE:
        return "noop"

    r = gh("PUT", f"{API}/repos/{org}/{repo}/pages", json=desired)
    if r.status_code in (200, 201, 204):
        return "updated"
    raise RuntimeError(f"update_pages failed: {r.status_code} {r.text}")

def dispatch(org: str, repo: str, event_type: str) -> None:
    body = {"event_type": event_type}
    r = gh("POST", f"{API}/repos/{org}/{repo}/dispatches", json=body)
    if r.status_code != 204:
        raise RuntimeError(f"dispatch failed: {r.status_code} {r.text}")

# =========================
# GIT HELPERS
# =========================
def run(cmd: List[str], cwd: Optional[Path] = None) -> None:
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None)

def out(cmd: List[str], cwd: Optional[Path] = None) -> str:
    return subprocess.check_output(cmd, cwd=str(cwd) if cwd else None).decode().strip()

def ensure_local_repo(org: str, repo: str) -> Path:
    """
    Ensures _work/org__repo exists as a git clone.
    Always sets origin URL with token for non-interactive pushes.
    """
    repo_dir = WORKDIR_BASE / f"{org}__{repo}"
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    remote_url = f"https://github.com/{org}/{repo}.git"
    authed_url = f"https://x-access-token:{GH_TOKEN}@github.com/{org}/{repo}.git"

    if not repo_dir.exists():
        run(["git", "clone", "--no-tags", "--depth", "1", remote_url, str(repo_dir)])
    else:
        # make sure it's a repo
        if not (repo_dir / ".git").exists():
            raise RuntimeError(f"{repo_dir} exists but is not a git repo")

    # ensure auth remote for push
    run(["git", "remote", "set-url", "origin", authed_url], cwd=repo_dir)

    # hard sync main
    run(["git", "fetch", "origin", "main", "--prune"], cwd=repo_dir)
    run(["git", "checkout", "-B", "main", "origin/main"], cwd=repo_dir)
    run(["git", "reset", "--hard", "origin/main"], cwd=repo_dir)
    run(["git", "clean", "-fd"], cwd=repo_dir)

    return repo_dir

def git_has_changes(repo_dir: Path) -> bool:
    return bool(out(["git", "status", "--porcelain"], cwd=repo_dir))

def sync_workflows(repo_dir: Path) -> bool:
    """
    Overwrite .github/workflows with TEMPLATE_WORKFLOWS_DIR contents.
    Returns True if committed/pushed, False if no change.
    """
    if not TEMPLATE_WORKFLOWS_DIR.exists():
        raise RuntimeError(f"template workflows dir not found: {TEMPLATE_WORKFLOWS_DIR}")

    target_dir = repo_dir / ".github" / "workflows"
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    # Always overwrite to match template exactly
    run([
        "rsync", "-a", "--delete",
        f"{TEMPLATE_WORKFLOWS_DIR}/",
        f"{target_dir}/"
    ])

    if not git_has_changes(repo_dir):
        return False

    run(["git", "add", ".github/workflows"], cwd=repo_dir)
    run(["git", "commit", "-m", "chore: sync repo infra (workflows)"], cwd=repo_dir)
    run(["git", "push", "origin", "main"], cwd=repo_dir)
    return True

# =========================
# CORE LOOP (claro)
# =========================
def process_repo(entry: Dict) -> None:
    org = entry["org"]
    repo = entry["name"]
    repo_full = f"{org}/{repo}"
    desc = entry.get("title", "")

    # 1) Repo (create if missing)
    try:
        if not repo_exists(org, repo):
            created = create_repo(org, repo, desc)
            if created:
                log(repo_full, "Criou repo")
    except Exception as e:
        err(repo_full, "create_repo", str(e))
        return

    # 2) Local checkout + sync workflows (overwrite always)
    try:
        repo_dir = ensure_local_repo(org, repo)
        changed = sync_workflows(repo_dir)
        if changed:
            log(repo_full, "Sobrescreveu workflows + push main")
    except Exception as e:
        err(repo_full, "sync_workflows", str(e))
        return

    # 3) Pages (enable/update only if needed)
    try:
        status = ensure_pages(org, repo, branch="gh-pages", path="/")
        if status == "enabled":
            log(repo_full, "Ativou Pages (gh-pages /)")
        elif status == "updated":
            log(repo_full, "Atualizou config do Pages (gh-pages /)")
        # noop => não loga nada
    except Exception as e:
        err(repo_full, "ensure_pages", str(e))
        return

    # 4) Dispatch (sempre)
    try:
        dispatch(org, repo, EVENT_SITE)
        log(repo_full, "Chamando dispatch reconstrução site")
    except Exception as e:
        err(repo_full, "dispatch_site", str(e))
        return

    try:
        dispatch(org, repo, EVENT_README)
        log(repo_full, "Chamando dispatch reconstrução readme")
    except Exception as e:
        err(repo_full, "dispatch_readme", str(e))
        return


def main() -> int:
    # sanity checks (silencioso; erra só se realmente faltar)
    if not REGISTRY_FILE.exists():
        print(f"[ERROR] registry not found: {REGISTRY_FILE}", file=sys.stderr)
        return 2

    data = yaml.safe_load(REGISTRY_FILE.read_text(encoding="utf-8")) or {}
    repos = data.get("repos", [])
    if not isinstance(repos, list):
        print("[ERROR] registry.repos must be a list", file=sys.stderr)
        return 2

    # ITERAÇÃO CLARA
    for entry in repos:
        process_repo(entry)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())