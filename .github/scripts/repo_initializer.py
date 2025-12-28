#!/usr/bin/env python3
import os
import time
import yaml
import shutil
import subprocess
from typing import Any, Dict, List, Optional
import requests

API = "https://api.github.com"
TOKEN = os.environ.get("GH_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("Missing GH_TOKEN")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
MODE = os.environ.get("MODE", "all")           # all | one
ONE_ORG = os.environ.get("ONE_ORG", "academic-codex")
ONE_REPO = os.environ.get("ONE_REPO", "")
FORCE = os.environ.get("FORCE", "false").lower() == "true"

# Quando verdadeiro, dispara repo_dispatch no final
DISPATCH = os.environ.get("DISPATCH", "true").lower() == "true"

REGISTRY_PATH = ".github/registry/repos.yml"

# Template de infra (apenas main). NÃO existe mais TEMPLATE_SITE_PATH aqui.
TEMPLATE_REPO_PATH = ".github/templates/repo"

# tags do repository_dispatch (as mesmas que você mostrou no print)
DISPATCH_SITE = os.environ.get("DISPATCH_SITE", "site-template-updated")
DISPATCH_README = os.environ.get("DISPATCH_README", "readme-template-updated")

# Identidade padrão para commits no runner (pode sobrescrever via env)
GIT_USER_NAME = os.environ.get("GIT_USER_NAME", "github-actions[bot]")
GIT_USER_EMAIL = os.environ.get("GIT_USER_EMAIL", "github-actions[bot]@users.noreply.github.com")

def log(*a):  # noqa
    print("[init]", *a, flush=True)

def gh(method: str, url: str, **kwargs) -> requests.Response:
    r = requests.request(method, url, headers=HEADERS, timeout=60, **kwargs)
    if r.status_code == 403 and "rate limit" in (r.text or "").lower():
        reset = r.headers.get("X-RateLimit-Reset")
        if reset:
            wait = max(0, int(reset) - int(time.time()) + 5)
            log(f"rate-limit: sleeping {wait}s")
            time.sleep(wait)
            r = requests.request(method, url, headers=HEADERS, timeout=60, **kwargs)
    return r

def repo_exists(org: str, repo: str) -> bool:
    r = gh("GET", f"{API}/repos/{org}/{repo}")
    return r.status_code == 200

def create_repo(org: str, repo: str, desc: str, private: bool) -> None:
    payload = {
        "name": repo,
        "description": desc or "",
        "private": bool(private),
        "has_issues": False,
        "has_projects": False,
        "has_wiki": False,
        "auto_init": False,
    }
    if DRY_RUN:
        log(f"DRY_RUN create repo {org}/{repo}")
        return
    r = gh("POST", f"{API}/orgs/{org}/repos", json=payload)
    if r.status_code == 201:
        log(f"created {org}/{repo}")
        return
    if r.status_code == 422:
        log(f"repo already exists {org}/{repo}")
        return
    raise RuntimeError(f"create_repo failed {org}/{repo}: {r.status_code} {r.text}")

def list_branches(org: str, repo: str) -> List[str]:
    out: List[str] = []
    page = 1
    while True:
        r = gh("GET", f"{API}/repos/{org}/{repo}/branches", params={"per_page": 100, "page": page})
        if r.status_code != 200:
            raise RuntimeError(f"list_branches failed {org}/{repo}: {r.status_code} {r.text}")
        batch = r.json()
        if not batch:
            break
        out.extend([b["name"] for b in batch if "name" in b])
        page += 1
    return out

def repo_is_empty(org: str, repo: str) -> bool:
    # info -> size + default_branch heurística
    r = gh("GET", f"{API}/repos/{org}/{repo}")
    if r.status_code != 200:
        raise RuntimeError(f"repo info failed {org}/{repo}: {r.status_code} {r.text}")
    info = r.json()
    if info.get("size", 0) == 0 and not info.get("default_branch"):
        return True

    # commits -> 409 = empty
    rr = gh("GET", f"{API}/repos/{org}/{repo}/commits", params={"per_page": 1})
    if rr.status_code == 409:
        return True
    if rr.status_code == 200 and rr.json() == []:
        return True
    return False

def enable_pages(org: str, repo: str) -> None:
    payload = {"source": {"branch": "gh-pages", "path": "/"}}
    if DRY_RUN:
        log(f"DRY_RUN enable pages {org}/{repo} -> gh-pages:/")
        return

    # idempotente: tenta criar e atualizar
    r1 = gh("POST", f"{API}/repos/{org}/{repo}/pages", json=payload)
    r2 = gh("PUT", f"{API}/repos/{org}/{repo}/pages", json=payload)

    if r1.status_code in (201, 204) or r2.status_code in (200, 201, 204):
        log(f"pages configured {org}/{repo} -> gh-pages:/")
        return

    # já configurado?
    r3 = gh("GET", f"{API}/repos/{org}/{repo}/pages")
    if r3.status_code == 200:
        log(f"pages already configured {org}/{repo}")
        return

    raise RuntimeError(f"enable_pages failed {org}/{repo}: post={r1.status_code} put={r2.status_code} {r2.text}")

def dispatch(org: str, repo: str, event_type: str, payload: Optional[dict] = None) -> None:
    body = {"event_type": event_type, "client_payload": payload or {}}
    if DRY_RUN:
        log(f"DRY_RUN dispatch {org}/{repo} {event_type} payload={body['client_payload']}")
        return
    r = gh("POST", f"{API}/repos/{org}/{repo}/dispatches", json=body)
    if r.status_code != 204:
        raise RuntimeError(f"dispatch failed {org}/{repo}: {r.status_code} {r.text}")
    log(f"dispatched {event_type} -> {org}/{repo}")

def run(cmd: List[str], cwd: Optional[str] = None) -> None:
    if DRY_RUN:
        log("DRY_RUN", " ".join(cmd))
        return
    subprocess.check_call(cmd, cwd=cwd)

def rsync(src: str, dst: str, delete: bool = False, ignore_existing: bool = False) -> None:
    cmd = ["rsync", "-av"]
    if delete:
        cmd.append("--delete")
    if ignore_existing:
        cmd.append("--ignore-existing")
    cmd += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
    run(cmd)

def clone_repo(org: str, repo: str, dest: str) -> None:
    if os.path.exists(dest):
        shutil.rmtree(dest)
    url = f"https://x-access-token:{TOKEN}@github.com/{org}/{repo}.git"
    run(["git", "clone", url, dest])

def git_has_changes(workdir: str) -> bool:
    if DRY_RUN:
        return True
    out = subprocess.check_output(["git", "status", "--porcelain"], cwd=workdir).decode().strip()
    return bool(out)

def git_config_identity(workdir: str) -> None:
    """
    Corrige: 'Author identity unknown' / 'empty ident name'.
    Configura user.name/email LOCALMENTE no repo clonado e marca safe.directory.
    """
    if DRY_RUN:
        log(f"DRY_RUN git_config_identity in {workdir}")
        return

    absdir = os.path.abspath(workdir)

    # safe.directory (alguns runners exigem)
    try:
        subprocess.check_call(["git", "config", "--global", "--add", "safe.directory", absdir])
    except Exception:
        pass

    # identidade local do repo (não depende do runner)
    subprocess.check_call(["git", "config", "user.name", GIT_USER_NAME], cwd=workdir)
    subprocess.check_call(["git", "config", "user.email", GIT_USER_EMAIL], cwd=workdir)

def ensure_main_initialized(org: str, repo: str, workdir: str) -> None:
    """
    - Se repo vazio: cria branch main, injeta TEMPLATE_REPO_PATH (infra), commita e push.
    - Se repo não vazio: garante infra de forma não-destrutiva (ignore_existing=True), commita só se houver diff.
    """
    if repo_is_empty(org, repo):
        log(f"{org}/{repo} is empty -> initializing main")
        run(["git", "checkout", "-b", "main"], cwd=workdir)

        rsync(TEMPLATE_REPO_PATH, workdir, ignore_existing=False)

        run(["git", "add", "."], cwd=workdir)

        # <<< correção essencial: identidade antes do commit
        git_config_identity(workdir)

        run(["git", "commit", "-m", "chore: initialize repository"], cwd=workdir)
        run(["git", "push", "-u", "origin", "main"], cwd=workdir)
        return

    log(f"{org}/{repo} not empty -> ensuring infra in main (non-destructive)")
    run(["git", "fetch", "origin"], cwd=workdir)

    checked_out = False
    for br in ("main", "master"):
        try:
            run(["git", "checkout", br], cwd=workdir)
            checked_out = True
            break
        except Exception:
            continue
    if not checked_out:
        run(["git", "checkout", "-B", "main", "origin/main"], cwd=workdir)

    rsync(TEMPLATE_REPO_PATH, workdir, ignore_existing=True)
    run(["git", "add", "."], cwd=workdir)

    if git_has_changes(workdir):
        # <<< correção essencial: identidade antes do commit
        git_config_identity(workdir)

        run(["git", "commit", "-m", "chore: sync repo infra"], cwd=workdir)
        try:
            run(["git", "push"], cwd=workdir)
        except Exception:
            pass
    else:
        log("no infra changes to commit")

def should_skip(org: str, repo: str) -> bool:
    """
    - Skip só se gh-pages já existe e FORCE=false.
    """
    try:
        branches = list_branches(org, repo)
        if "gh-pages" in branches and not FORCE:
            log(f"{org}/{repo} gh-pages exists -> skip (FORCE=false)")
            return True
    except Exception:
        pass
    return False

def load_registry() -> List[Dict[str, Any]]:
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    repos = data.get("repos")
    if not isinstance(repos, list):
        raise SystemExit(f"registry must have key 'repos' as a list in {REGISTRY_PATH}")
    return repos

def process_one(item: Dict[str, Any]) -> None:
    org = (item.get("org") or "").strip()
    repo = (item.get("name") or "").strip()
    desc = (item.get("description") or "").strip()
    private = bool(item.get("private", False))

    if not org or not repo:
        log("skip invalid registry item:", item)
        return

    log(f"--- {org}/{repo} ---")

    if not repo_exists(org, repo):
        log("repo missing -> create")
        create_repo(org, repo, desc, private)

    if should_skip(org, repo):
        return

    workdir = f"_work/{org}__{repo}"
    os.makedirs("_work", exist_ok=True)

    clone_repo(org, repo, workdir)

    # garante infra/workflows em main
    ensure_main_initialized(org, repo, workdir)

    # configura pages para gh-pages (idempotente)
    enable_pages(org, repo)

    # dispara rebuilds (o workflow cria/atualiza gh-pages)
    if DISPATCH:
        dispatch(org, repo, DISPATCH_SITE, {"reason": "initializer"})
        dispatch(org, repo, DISPATCH_README, {"reason": "initializer"})

def main():
    if MODE == "one":
        if not ONE_ORG or not ONE_REPO:
            raise SystemExit("MODE=one requires ONE_ORG and ONE_REPO")
        process_one({"org": ONE_ORG, "name": ONE_REPO})
        return

    repos = load_registry()
    for item in repos:
        try:
            process_one(item)
        except Exception as e:
            log("ERROR:", e)

if __name__ == "__main__":
    main()