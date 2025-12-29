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

# Poll curto pedido por você
WAIT_SLEEP_S = float(os.environ.get("WAIT_SLEEP_S", "0.5"))

# Timeout total (recomendo >= 60 para dar tempo do workflow criar gh-pages)
WAIT_TIMEOUT_S = float(os.environ.get("WAIT_TIMEOUT_S", "60.0"))

# Se seu workflow já faz auth via extraheader, deixe false (default).
# Se rodar fora do workflow, use: USE_TOKEN_IN_URL=true
USE_TOKEN_IN_URL = os.environ.get("USE_TOKEN_IN_URL", "false").lower() == "true"

# Se quiser sempre disparar dispatch mesmo após push:
ALWAYS_DISPATCH = os.environ.get("ALWAYS_DISPATCH", "false").lower() == "true"


# ============================================================
# LOGGING
# ============================================================

def log(repo: str, msg: str) -> None:
    print(f"[INIT] {repo}: {msg}", flush=True)

def err(repo: str, step: str, msg: str) -> None:
    print(f"[ERROR] {repo} step={step}: {msg}", file=sys.stderr, flush=True)


# ============================================================
# HTTP / GitHub
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
        "auto_init": True,          # garante origin/main existir
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
    Returns: "enabled" | "noop" | "wait"
    - wait = ainda não dá (tipicamente branch não existe -> 422)
    """
    payload = {"source": {"branch": "gh-pages", "path": "/"}}

    current = get_pages(org, repo)
    if current is not None:
        src = (current.get("source") or {})
        if src.get("branch") == "gh-pages" and src.get("path") == "/":
            return "noop"
        # existe mas está diferente -> tenta atualizar
        r_put = gh("PUT", f"{API}/repos/{org}/{repo}/pages", json=payload)
        if r_put.status_code in (200, 201, 204):
            return "enabled"
        raise RuntimeError(f"pages PUT failed {r_put.status_code}: {r_put.text}")

    # não existe Pages ainda -> tenta criar
    r_post = gh("POST", f"{API}/repos/{org}/{repo}/pages", json=payload)
    if r_post.status_code in (201, 204):
        return "enabled"
    if r_post.status_code == 422:
        return "wait"

    # fallback PUT (às vezes funciona como idempotente)
    r_put = gh("PUT", f"{API}/repos/{org}/{repo}/pages", json=payload)
    if r_put.status_code in (200, 201, 204):
        return "enabled"

    raise RuntimeError(f"enable_pages failed post={r_post.status_code} put={r_put.status_code}: {r_put.text}")

def dispatch(org: str, repo: str, event: str) -> None:
    r = gh("POST", f"{API}/repos/{org}/{repo}/dispatches", json={"event_type": event})
    if r.status_code != 204:
        raise RuntimeError(f"dispatch failed {r.status_code}: {r.text}")


# ============================================================
# GIT helpers (IMPORTANT: do not double-auth)
# ============================================================

def run(cmd: List[str], cwd: Optional[Path] = None) -> None:
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None)

def out(cmd: List[str], cwd: Optional[Path] = None) -> str:
    return subprocess.check_output(cmd, cwd=str(cwd) if cwd else None).decode().strip()

def ensure_repo_clone(org: str, repo: str) -> Path:
    d = WORKDIR_BASE / f"{org}__{repo}"
    if d.exists():
        shutil.rmtree(d)
    d.parent.mkdir(parents=True, exist_ok=True)

    clean_url = f"https://github.com/{org}/{repo}.git"
    token_url = f"https://x-access-token:{GH_TOKEN}@github.com/{org}/{repo}.git"

    clone_url = token_url if USE_TOKEN_IN_URL else clean_url
    run(["git", "clone", "--no-tags", clone_url, str(d)])

    # origin: se você usa extraheader no workflow, mantenha clean_url para evitar duplicate header.
    run(["git", "remote", "set-url", "origin", token_url if USE_TOKEN_IN_URL else clean_url], cwd=d)
    run(["git", "fetch", "origin", "--prune"], cwd=d)

    # identidade local (não conflita com extraheader)
    run(["git", "config", "user.name", os.environ.get("GIT_USER_NAME", "github-actions[bot]")], cwd=d)
    run(["git", "config", "user.email", os.environ.get("GIT_USER_EMAIL", "github-actions[bot]@users.noreply.github.com")], cwd=d)

    return d

def ensure_main_checkout(repo_dir: Path) -> None:
    # repo criado com auto_init => origin/main existe
    run(["git", "checkout", "-B", "main", "origin/main"], cwd=repo_dir)
    run(["git", "reset", "--hard", "origin/main"], cwd=repo_dir)
    run(["git", "clean", "-ffd"], cwd=repo_dir)

def sync_template_into_main(repo_dir: Path) -> bool:
    """
    Copia TUDO que está em TEMPLATE_REPO_PATH para a raiz do repo alvo.
    Commit/push se houver diff.
    """
    if not TEMPLATE_REPO_PATH.exists():
        raise RuntimeError(f"missing template path: {TEMPLATE_REPO_PATH}")

    ensure_main_checkout(repo_dir)

    # copia template
    run(["rsync", "-a", "--delete", f"{TEMPLATE_REPO_PATH}/", f"{repo_dir}/"])

    if not out(["git", "status", "--porcelain"], cwd=repo_dir):
        return False

    run(["git", "add", "."], cwd=repo_dir)
    run(["git", "commit", "-m", "chore: sync repo template (infra + content)"], cwd=repo_dir)
    run(["git", "push", "-u", "origin", "main"], cwd=repo_dir)
    return True


# ============================================================
# Wait orchestration (gh-pages created by workflow)
# ============================================================

def wait_for_branch(org: str, repo: str, branch: str) -> bool:
    deadline = time.time() + WAIT_TIMEOUT_S
    while time.time() < deadline:
        if branch_exists_api(org, repo, branch):
            return True
        time.sleep(WAIT_SLEEP_S)
    return False

def enable_pages_with_wait(org: str, repo: str) -> str:
    """
    Espera gh-pages existir, depois tenta habilitar Pages com retries.
    Returns: "enabled" | "noop" | "timeout_branch" | "timeout_pages"
    """
    if not wait_for_branch(org, repo, "gh-pages"):
        return "timeout_branch"

    deadline = time.time() + WAIT_TIMEOUT_S
    while time.time() < deadline:
        st = enable_pages_once(org, repo)
        if st in ("enabled", "noop"):
            return st
        time.sleep(WAIT_SLEEP_S)
    return "timeout_pages"


# ============================================================
# CORE
# ============================================================

def process_repo(entry: Dict) -> None:
    org = (entry.get("org") or "").strip()
    repo = (entry.get("name") or "").strip()
    desc = (entry.get("description") or entry.get("title") or "").strip()
    private = bool(entry.get("private", False))
    full = f"{org}/{repo}"

    if not org or not repo:
        return

    log(full, "start")

    if not repo_exists(org, repo):
        create_repo(org, repo, desc, private)
        log(full, "repo created")

    repo_dir = ensure_repo_clone(org, repo)

    pushed = sync_template_into_main(repo_dir)
    if pushed:
        log(full, "main updated (push should trigger update-site workflow)")
    else:
        log(full, "no diff in main (template already in sync)")

    # Opcional: dispatch manual (útil quando não houve push/diff, ou para forçar rebuild)
    if ALWAYS_DISPATCH or (not pushed):
        dispatch(org, repo, DISPATCH_SITE)
        log(full, f"dispatched {DISPATCH_SITE}")
        dispatch(org, repo, DISPATCH_README)
        log(full, f"dispatched {DISPATCH_README}")

    # Agora: aguardar gh-pages ser criado pelo workflow e então habilitar Pages
    st = enable_pages_with_wait(org, repo)
    if st == "enabled":
        log(full, "Pages enabled (gh-pages:/)")
    elif st == "noop":
        log(full, "Pages already enabled")
    elif st == "timeout_branch":
        log(full, "timeout: gh-pages not visible yet. Re-run initializer soon.")
    else:
        log(full, "timeout: Pages still returning 422/wait. Re-run initializer soon.")


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