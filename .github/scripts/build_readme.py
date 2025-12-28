#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re
import sys
import shutil
from datetime import datetime
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
        raise FileNotFoundError(f"cfg/placeholders não encontrado: {p}")

    # NOVO: arquivo -> usa direto (modo registry/_cfg)
    if p.is_file():
        return load_yaml_file(p)

    patterns = ("*.yml", "*.yaml")
    files: list[Path] = []

    if recursive:
        for pat in patterns:
            files.extend(sorted(p.rglob(pat)))
    else:
        for pat in patterns:
            files.extend(sorted(p.glob(pat)))

    cfg: dict = {}
    for f in files:
        cfg = merge_dicts(cfg, load_yaml_file(f))

    print("[DBG] cfg files:")
    for f in files:
        print("  -", f)
    return cfg


def ensure_defaults(cfg: dict) -> dict:
    cfg = dict(cfg)

    # paths
    cfg.setdefault("ASSETS_DIR", ".github/readme")
    cfg.setdefault("README_OUT", "README.md")

    # textos
    cfg.setdefault("REPO_TAGLINE", "lectures • notebooks • references")
    cfg.setdefault("CTA_TEXT", cfg.get("BANNER_ACCESS_CTA", "Access the site →"))

    # defaults de paleta (se o template não setar)
    cfg.setdefault("BG_1", "#0b1220")
    cfg.setdefault("BG_2", "#111827")
    cfg.setdefault("TEXT_MAIN", "#e5e7eb")
    cfg.setdefault("TEXT_MUTED", "#9ca3af")
    cfg.setdefault("ACCENT", "#93c5fd")
    cfg.setdefault("CARD_RADIUS", "18")

    # tema
    cfg.setdefault("THEME", "")         # ex: board, coding
    cfg.setdefault("THEME_ASSET", "")   # preenchido depois

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
    central_readme: Path | None = None
    cfg_path: Path | None = None

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--repo":
            repo_root = Path(argv[i + 1]).resolve()
            i += 2
            continue
        if a == "--central":
            central_readme = Path(argv[i + 1]).resolve()
            i += 2
            continue
        # NOVO: --cfg é o nome “novo”, mas mantém compatibilidade com os antigos
        if a in ("--cfg", "--repo-cfg", "--placeholders", "--placeholders-path"):
            cfg_path = Path(argv[i + 1]).resolve()
            i += 2
            continue
        i += 1

    if central_readme is None:
        raise SystemExit("Faltou --central <path para templates do readme no central>")

    if cfg_path is None:
        # default antigo: diretório padrão (fallback)
        cfg_path = repo_root / ".github" / "scripts"

    return repo_root, central_readme, cfg_path


def inject_svg_build_attr(svg_text: str, cfg: dict) -> str:
    """
    Injeta data-build="{{TIMESTAMP}}" no <svg ...>
    Se já existir, não duplica.
    """
    ts = cfg.get("TIMESTAMP", "")
    if not ts:
        return svg_text

    if "data-build=" in svg_text:
        return svg_text

    return re.sub(r"<svg\b", f'<svg data-build="{ts}"', svg_text, count=1)


def main():
    repo_root, central_readme, cfg_path = parse_args(sys.argv[1:])

    # NOVO: se for arquivo, usa como truth; se for diretório, mantém comportamento antigo
    cfg = load_placeholders(cfg_path, recursive=True)
    cfg = ensure_defaults(cfg)
    cfg["TIMESTAMP"] = datetime.utcnow().isoformat() + "Z"

    assets_dir = repo_root / cfg["ASSETS_DIR"]
    assets_dir.mkdir(parents=True, exist_ok=True)

    # 1) THEME -> copia asset escolhido e injeta placeholder THEME_ASSET
    theme = (cfg.get("THEME") or "").strip()
    if theme:
        src = _pick_theme_asset(central_readme, theme)
        if src:
            out_name = f"theme{src.suffix.lower()}"
            shutil.copy2(src, assets_dir / out_name)
            cfg["THEME_ASSET"] = f"{cfg['ASSETS_DIR'].rstrip('/')}/{out_name}"
        else:
            cfg.setdefault("THEME_ASSET", "")
    else:
        cfg.setdefault("THEME_ASSET", "")

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
        raw = tpath.read_text(encoding="utf-8")
        rendered = render_text(raw, cfg)
        rendered = inject_svg_build_attr(rendered, cfg)
        (assets_dir / outname).write_text(rendered, encoding="utf-8")

    # 3) gerar README.md
    readme_template_path = central_readme / "README.template.md"
    if not readme_template_path.exists():
        raise FileNotFoundError(f"README.template.md não encontrado em: {readme_template_path}")

    readme_template = readme_template_path.read_text(encoding="utf-8")
    rendered_readme = render_text(readme_template, cfg)
    (repo_root / cfg["README_OUT"]).write_text(rendered_readme, encoding="utf-8")

    print("[OK] README e SVGs gerados.")
    print(f"     README: {repo_root / cfg['README_OUT']}")
    print(f"     Assets: {assets_dir}")
    print(f"     CFG: {cfg_path}")
    print(f"     THEME: {theme} -> {cfg.get('THEME_ASSET','') or '(none)'}")


if __name__ == "__main__":
    main()