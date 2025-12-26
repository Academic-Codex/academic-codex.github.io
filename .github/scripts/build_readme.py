#!/usr/bin/env python3
from pathlib import Path
import re, sys, shutil
import yaml

def load_yaml_file(p: Path) -> dict:
    if not p.exists():
        raise FileNotFoundError(f"YAML não encontrado: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}

def merge_dicts(base: dict, extra: dict) -> dict:
    """Merge raso: chaves em extra sobrescrevem base."""
    out = dict(base or {})
    for k, v in (extra or {}).items():
        out[k] = v
    return out

def load_placeholders(path_or_dir: Path, recursive: bool = True) -> dict:
    """
    Se for arquivo: carrega ele.
    Se for diretório: carrega todos *.yml/*.yaml e mescla em ordem lexicográfica.
    """
    p = path_or_dir
    if not p.exists():
        raise FileNotFoundError(f"placeholders não encontrado: {p}")

    if p.is_file():
        return load_yaml_file(p)

    patterns = ("*.yml", "*.yaml")
    files = []
    if recursive:
        for pat in patterns:
            files.extend(sorted(p.rglob(pat)))
    else:
        for pat in patterns:
            files.extend(sorted(p.glob(pat)))

    cfg = {}
    for f in files:
        cfg = merge_dicts(cfg, load_yaml_file(f))
    return cfg

def ensure_defaults(cfg: dict) -> dict:
    cfg = dict(cfg)
    cfg.setdefault("ASSETS_DIR", ".github/readme")
    cfg.setdefault("README_OUT", "README.md")
    cfg.setdefault("REPO_TAGLINE", "lectures • notebooks • references")
    cfg.setdefault("CTA_TEXT", cfg.get("BANNER_ACCESS_CTA", "Access the site →"))

    # defaults de paleta
    cfg.setdefault("BG_1", "#000000")
    cfg.setdefault("BG_2", "#000000")
    cfg.setdefault("TEXT_MAIN", "#303030")
    cfg.setdefault("TEXT_MUTED", "#555555")
    cfg.setdefault("ACCENT", "#222222")
    cfg.setdefault("CARD_RADIUS", "30")

    # tema
    cfg.setdefault("THEME", "")  # ex: board, coding
    cfg.setdefault("THEME_ASSET", "")  # vamos preencher depois
    return cfg

def _pick_theme_asset(central_readme: Path, theme: str) -> Path | None:
    """
    Procura central/.github/templates/readme/assets/<theme>.(webp|gif|png|jpg|jpeg)
    Retorna Path ou None.
    """
    if not theme:
        return None
    assets_dir = central_readme / "assets"
    exts = (".webp", ".gif", ".png", ".jpg", ".jpeg")
    for ext in exts:
        p = assets_dir / f"{theme}{ext}"
        if p.exists():
            return p
    return None


_TOKEN = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")

def render_text(template: str, cfg: dict) -> str:
    return _TOKEN.sub(lambda m: str(cfg.get(m.group(1), "")), template)

def parse_args(argv):
    repo_root = Path(".").resolve()
    central_readme = None
    repo_cfg = None

    it = iter(range(len(argv)))
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--repo":
            repo_root = Path(argv[i+1]).resolve(); i += 2; continue
        if a == "--central":
            central_readme = Path(argv[i+1]).resolve(); i += 2; continue
        if a in ("--repo-cfg", "--placeholders", "--placeholders-path"):
            repo_cfg = Path(argv[i+1]).resolve(); i += 2; continue
        i += 1

    if central_readme is None:
        raise SystemExit("Faltou --central <path para templates do readme no central>")
    if repo_cfg is None:
        # default: diretório padrão
        repo_cfg = repo_root / ".github" / "scripts"

    return repo_root, central_readme, repo_cfg

def main():
    repo_root, central_readme, repo_cfg = parse_args(sys.argv[1:])
    cfg = load_placeholders(repo_cfg, recursive=True)
    cfg = ensure_defaults(cfg)

    assets_dir = repo_root / cfg["ASSETS_DIR"]
    assets_dir.mkdir(parents=True, exist_ok=True)

    # 1) THEME -> copia asset escolhido (webp ou gif etc.) e injeta placeholder
    theme = str(cfg.get("THEME", "")).strip()
    theme_src = _pick_theme_asset(central_readme, theme)

    if theme_src is not None:
        # salva como theme.<ext> no repo (não força .png)
        dst_name = f"theme{theme_src.suffix.lower()}"
        dst = assets_dir / dst_name
        shutil.copy2(theme_src, dst)

        # path relativo que você usa nos templates
        cfg["THEME_ASSET"] = f"{cfg['ASSETS_DIR'].rstrip('/')}/{dst_name}".replace("\\", "/")
    else:
        # fallback legado: her.webp (se existir)
        legacy = central_readme / "her.webp"
        if legacy.exists():
            shutil.copy2(legacy, assets_dir / "her.webp")
            cfg["THEME_ASSET"] = f"{cfg['ASSETS_DIR'].rstrip('/')}/her.webp".replace("\\", "/")

    # 2) gerar SVGs
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
    print(f"     Placeholders: {repo_cfg}")
    print(f"     THEME: {theme} -> {cfg.get('THEME_ASSET','') or '(none)'}")

if __name__ == "__main__":
    main()