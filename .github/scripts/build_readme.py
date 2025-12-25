#!/usr/bin/env python3
from pathlib import Path
import re, sys, shutil
import yaml

def load_yaml(p: Path) -> dict:
    if not p.exists():
        raise FileNotFoundError(f"repo.yml não encontrado: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}

def ensure_defaults(cfg: dict) -> dict:
    cfg = dict(cfg)
    cfg.setdefault("ASSETS_DIR", ".github/readme")
    cfg.setdefault("README_OUT", "README.md")
    cfg.setdefault("REPO_TAGLINE", "lectures • notebooks • references")
    cfg.setdefault("BANNER_ACCESS_TITLE", "Explore the full course website")
    cfg.setdefault("BANNER_ACCESS_SUBTITLE", "lectures • notebooks • references • interactive material")
    cfg.setdefault("BANNER_ACCESS_CTA", "Access the site →")
    return cfg

_TOKEN = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")

def render_text(template: str, cfg: dict) -> str:
    def repl(m):
        key = m.group(1)
        return str(cfg.get(key, ""))
    return _TOKEN.sub(repl, template)

def main():
    """
    Uso:
      build_readme.py --repo . --central ./central/.github/readme
    Rodando dentro do repo destino.
    """
    args = sys.argv[1:]
    repo_root = Path(".").resolve()
    central_readme = None
    for i, a in enumerate(args):
        if a == "--repo":
            repo_root = Path(args[i+1]).resolve()
        if a == "--central":
            central_readme = Path(args[i+1]).resolve()

    if central_readme is None:
        raise SystemExit("Faltou --central <path para .github/readme do central>")

    cfg = ensure_defaults(load_yaml(repo_root / ".github" / "scripts" / "repo.yml"))
    assets_dir = repo_root / cfg["ASSETS_DIR"]
    assets_dir.mkdir(parents=True, exist_ok=True)

    # 1) copiar assets estáticos (ex: her.webp) se existir no central
    for static_name in ["her.webp"]:
        src = central_readme / static_name
        if src.exists():
            shutil.copy2(src, assets_dir / static_name)

    # 2) gerar SVGs a partir de templates
    svg_templates = {
        "hero.template.svg": "hero.svg",
        "access-site.template.svg": "access-site.svg",
        "repo-card.template.svg": "repo-card.svg",
    }
    for tname, outname in svg_templates.items():
        tpath = central_readme / tname
        if not tpath.exists():
            continue
        rendered = render_text(tpath.read_text(encoding="utf-8"), cfg)
        (assets_dir / outname).write_text(rendered, encoding="utf-8")

    # 3) gerar README.md
    readme_template = (central_readme / "README.template.md").read_text(encoding="utf-8")
    rendered_readme = render_text(readme_template, cfg)
    (repo_root / cfg["README_OUT"]).write_text(rendered_readme, encoding="utf-8")

    print("[OK] README e SVGs gerados.")
    print(f"     README: {repo_root / cfg['README_OUT']}")
    print(f"     Assets: {assets_dir}")

if __name__ == "__main__":
    main()