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

# Seed inicial do gh-pages (copiado 1x, somente se gh-pages não existir)
TEMPLATE_GHPAGES_SEED_DIR = Path(".github/templates/site")

WORKDIR_BASE = Path("_work")

EVENT_SITE = os.environ.get("DISPATCH_SITE", "site-template-updated")
EVENT_README = os.environ.get("DISPATCH_README", "readme-template-updated")


# =========================
# LOGGING (mínimo)
# =========================
def log(repo_full: str, msg: str) -> None:
    print(f"{repo_full}: {msg}", flush=True)

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

def enable_pages_once(org: str, repo: str) -> str:
    """
    Só habilita pages se ainda não existir.
    Se já existir, não altera nada.
    Returns: "enabled" | "noop"
    """
    current = get_pages(org, repo)
    if current is not None:
        return "noop"

    payload = {"source": {"branch": "gh-pages", "path": "/"}}
    r_post = gh("POST", f"{API}/repos/{org}/{repo}/pages", json=payload)
    if r_post.status_code in (201, 204):
        return "enabled"

    # fallback PUT (idempotência)
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
    Garante que existe main local baseada na origin/main.
    Se repo estiver vazio (sem origin/main), cria main orphan.
    """
    # tenta origin/main
    branches = out(["git", "branch", "-r"], cwd=repo_dir).splitlines()
    has_origin_main = any(b.strip() == "origin/main" for b in branches)

    if has_origin_main:
        run(["git", "checkout", "-B", "main", "origin/main"], cwd=repo_dir)
        run(["git", "reset", "--hard", "origin/main"], cwd=repo_dir)
    else:
        # cria gh-pages orphan de forma inequívoca
        run(["git", "checkout", "--orphan", "gh-pages"], cwd=repo_dir)

        # GARANTE que o HEAD está em gh-pages
        run(["git", "symbolic-ref", "HEAD", "refs/heads/gh-pages"], cwd=repo_dir)

        # limpa tudo
        subprocess.run(["git", "rm", "-rf", "."], cwd=str(repo_dir), check=False)
        run(["git", "clean", "-fd"], cwd=repo_dir)

        # copia seed
        run(["rsync", "-a", "--delete", f"{TEMPLATE_GHPAGES_SEED_DIR}/", f"{repo_dir}/"])

        # commit EXPLICITAMENTE na gh-pages
        run(["git", "add", "."], cwd=repo_dir)
        run(["git", "commit", "-m", "chore: seed gh-pages from central template"], cwd=repo_dir)

        # push explícito
        run(["git", "push", "-u", "origin", "gh-pages"], cwd=repo_dir)

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

def remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    r = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    return bool((r.stdout or "").strip())

def seed_gh_pages_once(repo_dir: Path) -> bool:
    """
    Se gh-pages já existe -> NOOP (False)
    Se não existe -> cria orphan gh-pages, copia seed, commit, push (True)
    """
    if remote_branch_exists(repo_dir, "gh-pages"):
        return False

    if not TEMPLATE_GHPAGES_SEED_DIR.exists():
        raise RuntimeError(f"gh-pages seed dir not found: {TEMPLATE_GHPAGES_SEED_DIR}")

    # cria gh-pages orphan
    run(["git", "checkout", "--orphan", "gh-pages"], cwd=repo_dir)
    # remove tracked files (se houver)
    subprocess.run(["git", "rm", "-rf", "."], cwd=str(repo_dir), check=False)
    run(["git", "clean", "-fd"], cwd=repo_dir)

    # copia seed -> raiz do repo
    run(["rsync", "-a", "--delete", f"{TEMPLATE_GHPAGES_SEED_DIR}/", f"{repo_dir}/"])

    run(["git", "add", "."], cwd=repo_dir)
    run(["git", "commit", "-m", "chore: seed gh-pages from central template"], cwd=repo_dir)
    run(["git", "push", "-u", "origin", "gh-pages"], cwd=repo_dir)
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

    # 4) gh-pages: seed only if missing
    try:
        seeded = seed_gh_pages_once(repo_dir)
        if seeded:
            log(repo_full, "Criou gh-pages + seed do template (1x)")
    except Exception as e:
        err(repo_full, "seed_gh_pages", str(e))
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