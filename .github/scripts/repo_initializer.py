#!/usr/bin/env python3
import os
import sys
import time
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import requests
import yaml

# ============================================================
# CONFIG
# ============================================================

API = "https://api.github.com"

GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not GH_TOKEN:
    print("[ERROR] missing GH_TOKEN (or GITHUB_TOKEN)", file=sys.stderr)
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

WAIT_TIMEOUT_S = float(os.environ.get("WAIT_TIMEOUT_S", "10.0"))
WAIT_SLEEP_S = float(os.environ.get("WAIT_SLEEP_S", "0.5"))  # pedido: curto

GIT_USER_NAME = os.environ.get("GIT_USER_NAME", "github-actions[bot]")
GIT_USER_EMAIL = os.environ.get("GIT_USER_EMAIL", "github-actions[bot]@users.noreply.github.com")

# IMPORTANTÍSSIMO:
# - Se o workflow já configura auth via extraheader, manter False (default).
# - Se você remover a etapa de auth do workflow, pode setar USE_TOKEN_IN_URL=true.
USE_TOKEN_IN_URL = os.environ.get("USE_TOKEN_IN_URL", "false").lower() == "true"

# ============================================================
# LOGGING
# ============================================================

def log(repo: str, msg: str) -> None:
    print(f"[INIT] {repo}: {msg}", flush=True)

def err(repo: str, step: str, msg: str) -> None:
    print(f"[ERROR] {repo} step={step}: {msg}", file=sys.stderr, flush=True)

# ============================================================
# HTTP / GITHUB
# ============================================================

def gh(method: str, url: str, **kw) -> requests.Response:
    return requests.request(method, url, headers=HEADERS, timeout=30, **kw)

def repo_exists(org: str, repo: str) -> bool:
    r = gh("GET", f"{API}/repos/{org}/{repo}")
    if r.status_code in (200, 404):
        return r.status_code == 200
    raise RuntimeError(f"repo_exists unexpected {r.status_code}: {r.text}")

def create_repo(org: str, repo: str, desc: str, private: bool) -> None:
    payload = {
        "name": repo,
        "description": desc,
        "private": private,
        "auto_init": True,   # garante README.md e main existente
        "has_issues": False,
        "has_projects": False,
        "has_wiki": False,
    }
    r = gh("POST", f"{API}/orgs/{org}/repos", json=payload)
    if r.status_code in (201, 422):
        return
    raise RuntimeError(f"create_repo failed {r.status_code}: {r.text}")

def branch_exists_api(org: str, repo: str, branch: str) -> bool:
    r = gh("GET", f"{API}/repos/{org}/{repo}/branches/{branch}")
    if r.status_code in (200, 404):
        return r.status_code == 200
    raise RuntimeError(f"branch_exists_api unexpected {r.status_code}: {r.text}")

def get_pages(org: str, repo: str) -> Optional[dict]:
    r = gh("GET", f"{API}/repos/{org}/{repo}/pages")
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None
    raise RuntimeError(f"get_pages unexpected {r.status_code}: {r.text}")

def enable_pages_once(org: str, repo: str) -> str:
    """
    Configura Pages para publicar de gh-pages:/.
    Retorna: "enabled" | "noop" | "wait"
    """
    payload = {"source": {"branch": "gh-pages", "path": "/"}}

    current = get_pages(org, repo)
    if current is not None:
        src = (current.get("source") or {})
        if src.get("branch") == "gh-pages" and src.get("path") == "/":
            return "noop"
        # existe, mas diferente -> tenta PUT
        r_put = gh("PUT", f"{API}/repos/{org}/{repo}/pages", json=payload)
        if r_put.status_code in (200, 201, 204):
            return "enabled"
        raise RuntimeError(f"pages PUT failed {r_put.status_code}: {r_put.text}")

    # não existe pages ainda -> tenta POST
    r_post = gh("POST", f"{API}/repos/{org}/{repo}/pages", json=payload)
    if r_post.status_code in (201, 204):
        return "enabled"

    # 422 aqui tipicamente é "branch/path ainda não existe"
    if r_post.status_code == 422:
        return "wait"

    # fallback PUT
    r_put = gh("PUT", f"{API}/repos/{org}/{repo}/pages", json=payload)
    if r_put.status_code in (200, 201, 204):
        return "enabled"

    raise RuntimeError(f"enable_pages failed post={r_post.status_code} put={r_put.status_code}: {r_put.text}")

def dispatch(org: str, repo: str, event: str) -> None:
    r = gh("POST", f"{API}/repos/{org}/{repo}/dispatches", json={"event_type": event})
    if r.status_code != 204:
        raise RuntimeError(f"dispatch failed {r.status_code}: {r.text}")

# ============================================================
# GIT
# ============================================================

def run(cmd: List[str], cwd: Optional[Path] = None) -> None:
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None)

def out(cmd: List[str], cwd: Optional[Path] = None) -> str:
    return subprocess.check_output(cmd, cwd=str(cwd) if cwd else None).decode().strip()

def git_has_changes(repo_dir: Path) -> bool:
    return bool(out(["git", "status", "--porcelain"], cwd=repo_dir))

def git_config_identity(repo_dir: Path) -> None:
    run(["git", "config", "user.name", GIT_USER_NAME], cwd=repo_dir)
    run(["git", "config", "user.email", GIT_USER_EMAIL], cwd=repo_dir)

def git_mark_safe(repo_dir: Path) -> None:
    # runners às vezes exigem safe.directory
    try:
        run(["git", "config", "--global", "--add", "safe.directory", str(repo_dir.resolve())])
    except Exception:
        pass

def ensure_repo_clone(org: str, repo: str) -> Path:
    d = WORKDIR_BASE / f"{org}__{repo}"
    if d.exists():
        shutil.rmtree(d)
    d.parent.mkdir(parents=True, exist_ok=True)

    clean_url = f"https://github.com/{org}/{repo}.git"
    authed_url = f"https://x-access-token:{GH_TOKEN}@github.com/{org}/{repo}.git"

    # CLONE:
    # - default: clean_url (auth vem do workflow extraheader)
    # - fallback: token no URL se USE_TOKEN_IN_URL=true
    clone_url = authed_url if USE_TOKEN_IN_URL else clean_url
    run(["git", "clone", "--no-tags", clone_url, str(d)])

    # garante origin conforme estratégia
    run(["git", "remote", "set-url", "origin", authed_url if USE_TOKEN_IN_URL else clean_url], cwd=d)
    run(["git", "fetch", "origin", "--prune"], cwd=d)

    git_mark_safe(d)
    git_config_identity(d)

    return d

def ensure_main_checkout(repo_dir: Path) -> None:
    """
    Garante checkout em main com base em origin/main quando existir.
    """
    # origin/main existe?
    remotes = out(["git", "branch", "-r"], cwd=repo_dir).splitlines()
    has_origin_main = any(r.strip() == "origin/main" for r in remotes)

    if has_origin_main:
        run(["git", "checkout", "-B", "main", "origin/main"], cwd=repo_dir)
        run(["git", "reset", "--hard", "origin/main"], cwd=repo_dir)
        run(["git", "clean", "-ffd"], cwd=repo_dir)
        return

    # se por algum motivo não apareceu nos remotes (timing), tenta checkout main normal
    try:
        run(["git", "checkout", "main"], cwd=repo_dir)
        return
    except Exception:
        pass

    # fallback: cria main a partir do HEAD atual (não orphan, sem destruir)
    run(["git", "checkout", "-b", "main"], cwd=repo_dir)

def ensure_main_and_template(repo_dir: Path) -> bool:
    """
    Sincroniza TEMPLATE_REPO_PATH para o repo (com delete).
    Commit/push se houver diff.
    Retorna True se push ocorreu.
    """
    if not TEMPLATE_REPO_PATH.exists():
        raise RuntimeError(f"missing TEMPLATE_REPO_PATH: {TEMPLATE_REPO_PATH}")

    ensure_main_checkout(repo_dir)

    run(["rsync", "-a", "--delete", f"{TEMPLATE_REPO_PATH}/", f"{repo_dir}/"])

    if not git_has_changes(repo_dir):
        return False

    run(["git", "add", "."], cwd=repo_dir)
    run(["git", "commit", "-m", "chore: sync repo infra"], cwd=repo_dir)
    run(["git", "push", "-u", "origin", "main"], cwd=repo_dir)
    return True

# ============================================================
# WAIT + ENABLE PAGES (0.5s polling)
# ============================================================

def wait_for_branch_api(org: str, repo: str, branch: str) -> bool:
    deadline = time.time() + WAIT_TIMEOUT_S
    while time.time() < deadline:
        if branch_exists_api(org, repo, branch):
            return True
        time.sleep(WAIT_SLEEP_S)
    return False

def enable_pages_with_wait(org: str, repo: str) -> str:
    """
    Tenta habilitar Pages, mas aguarda:
    - gh-pages existir
    - e (se necessário) o backend do Pages aceitar (422 -> wait)
    Retorna "enabled" | "noop"
    """
    if not wait_for_branch_api(org, repo, "gh-pages"):
        return "wait_branch"

    deadline = time.time() + WAIT_TIMEOUT_S
    while time.time() < deadline:
        st = enable_pages_once(org, repo)
        if st in ("enabled", "noop"):
            return st
        # st == "wait"
        time.sleep(WAIT_SLEEP_S)

    return "wait_pages"

# ============================================================
# CORE
# ============================================================

def process_repo(entry: Dict) -> None:
    org = (entry.get("org") or "").strip()
    repo = (entry.get("name") or "").strip()
    desc = (entry.get("description") or "").strip()
    private = bool(entry.get("private", False))
    full = f"{org}/{repo}"

    if not org or not repo:
        return

    log(full, "start")

    if not repo_exists(org, repo):
        create_repo(org, repo, desc, private)
        log(full, "repo created")

    repo_dir = ensure_repo_clone(org, repo)

    pushed = ensure_main_and_template(repo_dir)
    if pushed:
        log(full, "main updated (push triggered workflows)")
    else:
        # sem diff => força build por dispatch
        dispatch(org, repo, DISPATCH_SITE)
        log(full, "dispatch site (no push)")
        dispatch(org, repo, DISPATCH_README)
        log(full, "dispatch readme (no push)")

    st = enable_pages_with_wait(org, repo)
    if st == "enabled":
        log(full, "Pages enabled (gh-pages:/)")
    elif st == "noop":
        log(full, "Pages already enabled")
    elif st == "wait_branch":
        log(full, "gh-pages not visible yet (timeout); Pages will be enabled later")
    else:
        log(full, "Pages still returning 422 (timeout); try again shortly")

def main() -> None:
    if not REGISTRY_FILE.exists():
        print(f"[ERROR] registry not found: {REGISTRY_FILE}", file=sys.stderr)
        sys.exit(2)

    data = yaml.safe_load(REGISTRY_FILE.read_text(encoding="utf-8")) or {}
    repos = data.get("repos", [])
    if not isinstance(repos, list):
        print("[ERROR] registry.repos must be a list", file=sys.stderr)
        sys.exit(2)

    for entry in repos:
        full = f"{(entry.get('org') or '').strip()}/{(entry.get('name') or '').strip()}"
        try:
            process_repo(entry)
        except Exception as e:
            err(full, "process", str(e))

if __name__ == "__main__":
    main()