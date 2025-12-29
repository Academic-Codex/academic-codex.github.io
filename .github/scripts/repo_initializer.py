#!/usr/bin/env python3
import os
import sys
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import requests
import yaml

API = "https://api.github.com"

GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not GH_TOKEN:
    print("[ERROR] missing GH_TOKEN", file=sys.stderr)
    sys.exit(2)

HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}

# ====== Central repo files/dirs ======
REGISTRY_FILE = Path(".github/registry/repos.yml")

TEMPLATE_WORKFLOWS_DIR = Path(".github/templates/repo/.github/workflows")
TEMPLATE_GITIGNORE_FILE = Path(".github/templates/repo/.gitignore")

WORKDIR_BASE = Path("_work")

EVENT_SITE = os.environ.get("DISPATCH_SITE", "site-template-updated")
EVENT_README = os.environ.get("DISPATCH_README", "readme-template-updated")


# =========================
# LOGGING (mínimo)
# =========================
def log(repo_full: str, msg: str) -> None:
    print(f"[ACTION] {repo_full}: {msg}", flush=True)

def err(repo_full: str, step: str, msg: str) -> None:
    print(f"[ERROR] {repo_full} step={step} {msg}", file=sys.stderr, flush=True)


# =========================
# HTTP (GitHub)
# =========================
def gh(method: str, url: str, **kw) -> requests.Response:
    return requests.request(method, url, headers=HEADERS, timeout=60, **kw)

def repo_exists(org: str, repo: str) -> bool:
    r = gh("GET", f"{API}/repos/{org}/{repo}")
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise RuntimeError(f"repo_exists unexpected: {r.status_code} {r.text}")

def create_repo(org: str, repo: str, desc: str = "", private: bool = False) -> bool:
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

def enable_pages_once(org: str, repo: str) -> str:
    """
    Garante Pages apontando para gh-pages:/ .
    Se já estiver ok, noop.
    Returns: "enabled" | "noop"
    """
    payload = {"source": {"branch": "gh-pages", "path": "/"}}

    current = get_pages(org, repo)
    if current is not None:
        src = (current.get("source") or {})
        if src.get("branch") == "gh-pages" and src.get("path") == "/":
            return "noop"
        # se existe mas está diferente, atualiza
        r_put = gh("PUT", f"{API}/repos/{org}/{repo}/pages", json=payload)
        if r_put.status_code in (200, 201, 204):
            return "enabled"
        raise RuntimeError(f"enable_pages update failed: {r_put.status_code} {r_put.text}")

    # não existe ainda: cria
    r_post = gh("POST", f"{API}/repos/{org}/{repo}/pages", json=payload)
    if r_post.status_code in (201, 204):
        return "enabled"

    # fallback PUT
    r_put = gh("PUT", f"{API}/repos/{org}/{repo}/pages", json=payload)
    if r_put.status_code in (200, 201, 204):
        return "enabled"

    raise RuntimeError(f"enable_pages failed: post={r_post.status_code} put={r_put.status_code}")

def dispatch(org: str, repo: str, event_type: str) -> None:
    body = {"event_type": event_type}
    r = gh("POST", f"{API}/repos/{org}/{repo}/dispatches", json=body)
    if r.status_code != 204:
        raise RuntimeError(f"dispatch failed: {r.status_code} {r.text}")


# =========================
# GIT helpers
# =========================
def run(cmd: List[str], cwd: Optional[Path] = None) -> None:
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None)

def out(cmd: List[str], cwd: Optional[Path] = None) -> str:
    return subprocess.check_output(cmd, cwd=str(cwd) if cwd else None).decode().strip()

def git_has_changes(repo_dir: Path) -> bool:
    return bool(out(["git", "status", "--porcelain"], cwd=repo_dir))

def ensure_local_repo(org: str, repo: str) -> Path:
    """
    Garante clone local em _work/org__repo.
    Sempre seta origin com token pra push.
    """
    repo_dir = WORKDIR_BASE / f"{org}__{repo}"
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    remote_url = f"https://github.com/{org}/{repo}.git"
    authed_url = f"https://x-access-token:{GH_TOKEN}@github.com/{org}/{repo}.git"

    if not repo_dir.exists():
        run(["git", "clone", "--no-tags", remote_url, str(repo_dir)])
    else:
        if not (repo_dir / ".git").exists():
            raise RuntimeError(f"{repo_dir} exists but is not a git repo")

    run(["git", "remote", "set-url", "origin", authed_url], cwd=repo_dir)
    run(["git", "fetch", "origin", "--prune"], cwd=repo_dir)

    return repo_dir

def ensure_main_checkout(repo_dir: Path) -> None:
    """
    Garante que existe um branch local `main`.
    - Se existir `origin/main`, recria `main` a partir dele.
    - Se NÃO existir (repo recém-criado), cria `main` como orphan limpo.
    """

    # verifica se existe origin/main
    remotes = out(["git", "branch", "-r"], cwd=repo_dir).splitlines()
    has_origin_main = any(r.strip() == "origin/main" for r in remotes)

    if has_origin_main:
        # força main a refletir exatamente origin/main
        run(["git", "checkout", "-B", "main", "origin/main"], cwd=repo_dir)
        run(["git", "reset", "--hard", "origin/main"], cwd=repo_dir)
        run(["git", "clean", "-ffd"], cwd=repo_dir)
    else:
        # repo vazio: cria main orphan, HEAD inequívoco
        run(["git", "checkout", "--orphan", "main"], cwd=repo_dir)
        run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repo_dir)

        # limpa tudo (não depende de git rm)
        run(["git", "reset", "--hard"], cwd=repo_dir)
        run(["git", "clean", "-ffd"], cwd=repo_dir)

def sync_workflows_and_gitignore(repo_dir: Path) -> bool:
    """
    REPLACE sempre:
      - .github/workflows/
      - .gitignore
    Commit/push se tiver diff.
    Return True se commitou.
    """
    if not TEMPLATE_WORKFLOWS_DIR.exists():
        raise RuntimeError(f"template workflows dir not found: {TEMPLATE_WORKFLOWS_DIR}")
    if not TEMPLATE_GITIGNORE_FILE.exists():
        raise RuntimeError(f"template gitignore not found: {TEMPLATE_GITIGNORE_FILE}")

    ensure_main_checkout(repo_dir)

    # Workflows REPLACE
    target_wf = repo_dir / ".github" / "workflows"
    target_wf.parent.mkdir(parents=True, exist_ok=True)
    run(["rsync", "-a", "--delete", f"{TEMPLATE_WORKFLOWS_DIR}/", f"{target_wf}/"])

    # .gitignore REPLACE
    run(["cp", "-f", str(TEMPLATE_GITIGNORE_FILE), str(repo_dir / ".gitignore")])

    if not git_has_changes(repo_dir):
        return False

    run(["git", "add", ".github/workflows", ".gitignore"], cwd=repo_dir)
    # commit mesmo em repo recém-criado
    run(["git", "commit", "-m", "chore: sync repo infra (workflows + gitignore)"], cwd=repo_dir)
    run(["git", "push", "-u", "origin", "main"], cwd=repo_dir)
    return True


# =========================
# CORE
# =========================
def process_repo(entry: Dict) -> None:
    org = (entry.get("org") or "").strip()
    repo = (entry.get("name") or "").strip()
    repo_full = f"{org}/{repo}"

    if not org or not repo:
        err(repo_full, "registry", "missing org/name")
        return

    private = bool(entry.get("private", False))
    desc = (entry.get("description") or entry.get("title") or "").strip()

    # 1) create repo if missing
    try:
        if not repo_exists(org, repo):
            created = create_repo(org, repo, desc, private)
            if created:
                log(repo_full, "Criou repo")
    except Exception as e:
        err(repo_full, "create_repo", str(e))
        return

    # 2) clone/fetch local
    try:
        repo_dir = ensure_local_repo(org, repo)
    except Exception as e:
        err(repo_full, "clone_fetch", str(e))
        return

    # 3) main: REPLACE workflows + gitignore, commit if diff
    try:
        changed = sync_workflows_and_gitignore(repo_dir)
        if changed:
            log(repo_full, "Atualizou main (workflows + gitignore)")
    except Exception as e:
        err(repo_full, "sync_main", str(e))
        return

    # 5) pages: enable only if not exists
    try:
        st = enable_pages_once(org, repo)
        if st == "enabled":
            log(repo_full, "Ativou Pages (gh-pages /)")
    except Exception as e:
        err(repo_full, "pages_enable", str(e))
        return

    # 6) dispatch (sempre)
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
    if not REGISTRY_FILE.exists():
        print(f"[ERROR] registry not found: {REGISTRY_FILE}", file=sys.stderr)
        return 2

    data = yaml.safe_load(REGISTRY_FILE.read_text(encoding="utf-8")) or {}
    repos = data.get("repos", [])
    if not isinstance(repos, list):
        print("[ERROR] registry.repos must be a list", file=sys.stderr)
        return 2

    # iteração explícita e clara
    for entry in repos:
        print(f"-----------------------------Processando--------------------------------")
        print("--------------------------------------------------------------------------")
        process_repo(entry)
        print("--------------------------------FIM-----------------------------------------")
        print("--------------------------------------------------------------------------")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())