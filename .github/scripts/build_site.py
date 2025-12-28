#!/usr/bin/env python3
import argparse, os, shutil, json, html
from pathlib import Path
import subprocess
from datetime import datetime
import sys
import re, json, html
import yaml
import html


def _load_yaml_file(p: Path) -> dict:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def _merge_dicts(base: dict, extra: dict) -> dict:
    out = dict(base or {})
    for k, v in (extra or {}).items():
        out[k] = v
    return out

def load_config(cfg_path: Path) -> dict:
    """
    Se cfg_path for arquivo: lê.
    Se cfg_path for diretório: lê todos *.yml/*.yaml (recursivo) e mescla.
    Depois aplica a regra do HERO_URL (BASE+SUBDIR+FILE).
    """
    if not cfg_path or not cfg_path.exists():
        return {}

    data = {}
    if cfg_path.is_file():
        data = _load_yaml_file(cfg_path)
    else:
        files = []
        for pat in ("*.yml", "*.yaml"):
            files.extend(sorted(cfg_path.rglob(pat)))
        for f in files:
            data = _merge_dicts(data, _load_yaml_file(f))

    if not data.get("HERO_URL"):
        base = (data.get("ASSETS_BASE") or "").rstrip("/")
        sub  = (data.get("ASSETS_SUBDIR") or "").strip("/")
        fil  = (data.get("HERO_FILE") or "").lstrip("/")
        if base and fil:
            data["HERO_URL"] = "/".join(p for p in [base, sub, fil] if p)

    return data

# ============================= Helpers =============================

IGNORE_DIRS = {
    ".git", ".github", ".venv", "venv", "__pycache__",
    "node_modules", ".script", "site"
}

def copy_tree(src_dir: Path, dst_dir: Path):
    """
    Copia todo o conteúdo de src_dir para dst_dir (se existir).
    Mantém estrutura, sobrescreve arquivos.
    """
    if not src_dir.exists():
        return
    for root, dirs, files in os.walk(src_dir):
        rel = Path(root).relative_to(src_dir)
        out_root = dst_dir / rel
        out_root.mkdir(parents=True, exist_ok=True)
        for f in files:
            src_f = Path(root) / f
            dst_f = out_root / f
            shutil.copy2(src_f, dst_f)

def load_template_index(template_dir: Path) -> str:
    """
    Lê template/index.html. Lança erro claro se não existir.
    """
    index_path = template_dir / "index.html"
    if not index_path.exists():
        raise FileNotFoundError(f"Template missing: {index_path}")
    return index_path.read_text(encoding="utf-8")

def render_index(index_src: str, title: str, nb_count: int, tree: dict) -> str:
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    safe_json = json.dumps(tree, ensure_ascii=False).replace("</", "<\\/")  # evita fechar <script>

    rep = {
        r"\{\{\s*TITLE\s*\}\}": html.escape(title),
        r"\{\{\s*TIMESTAMP\s*\}\}": timestamp,
        r"\{\{\s*NBCOUNT\s*\}\}": str(nb_count),
        r"\{\{\s*TREE_JSON\s*\}\}": safe_json,
    }
    out = index_src
    for pattern, value in rep.items():
        out = re.sub(pattern, lambda m, v=value: v, out)  # <— literal
    return out

# ====================== Núcleo de varredura/build ======================

#     return root, nb_count
import sys, subprocess
from pathlib import Path

def collect_tree(src: Path, out: Path, execute: bool):
    """
    Varre src; converte apenas .ipynb -> .html em out.
    - Arquivos que não sejam .ipynb são ignorados.
    - Diretórios sem nenhum notebook são removidos da árvore.
    """
    nb_count = 0
    root = {"type": "dir", "name": src.name, "path": "", "children": []}
    dir_map = {str(src.resolve()): root}

    for path in sorted(src.rglob("*")):
        if out in path.parents or path == out:
            continue
        rel_parts = path.relative_to(src).parts
        if not rel_parts:
            continue

        # Garante nós de diretório
        cur = src
        parent_node = root
        for i, p in enumerate(rel_parts[:-1] if not path.is_dir() else rel_parts):
            cur = cur / p
            key = str(cur.resolve())
            if key not in dir_map:
                node = {"type": "dir", "name": p, "path": str(Path(*rel_parts[: i + 1])), "children": []}
                parent_node["children"].append(node)
                dir_map[key] = node
            parent_node = dir_map[key]

        # Se for diretório, só garante hierarquia
        if path.is_dir():
            continue

        # Se não for .ipynb → ignora
        if path.suffix.lower() != ".ipynb":
            continue

        # Converte notebook
        rel = path.relative_to(src)
        file_node = {"type": "file", "name": rel.name, "path": str(rel)}
        nb_count += 1

        out_html = (out / rel).with_suffix(".html")
        out_html.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable, "-m", "nbconvert",
            "--to", "html",
            "--template=classic",
            "--HTMLExporter.embed_images=True",
            "--TagRemovePreprocessor.enabled=True",
            "--TagRemovePreprocessor.remove_input_tags=hide-input",
            "--output", out_html.name,
            "--output-dir", str(out_html.parent),
            str(path),
        ]
        
        if execute:
            cmd.append("--execute")
        subprocess.run(cmd, check=True)

        file_node["nb_html"] = str(out_html.relative_to(out)).replace(os.sep, "/")

        parent_key = str(path.parent.resolve())
        dir_map[parent_key]["children"].append(file_node)

    # --- remove diretórios vazios ---
    def prune_empty_dirs(node):
        if node["type"] == "file":
            return node, True
        new_children = []
        has_ipynb = False
        for ch in node.get("children", []):
            pruned, child_has_ipynb = prune_empty_dirs(ch)
            if pruned:
                new_children.append(pruned)
            has_ipynb = has_ipynb or child_has_ipynb
        node["children"] = new_children
        return (node if has_ipynb else None), has_ipynb

    root, _ = prune_empty_dirs(root)
    if root is None:
        root = {"type": "dir", "name": src.name, "path": "", "children": []}

    return root, nb_count


def build_static_site(src: Path, out: Path, template_dir: Path, title: str, execute: bool, cfg: dict | None):
    tree, nb_count = collect_tree(src, out, execute)

    # === NOVO: carregar refs do repo e colocar no cfg para render_tokens ===
    refs = load_references(src)  # assumes references.yml no root do repo (src)
    refs_html = render_references_html(refs)
    cfg = dict(cfg or {})
    cfg["REFERENCIAS"] = refs_html
    
    tree, nb_count = collect_tree(src, out, execute)

    out.mkdir(parents=True, exist_ok=True)
    pages = [
        ("index.html", False),     # Home
        ("software.html", True),   # Software (com árvore)
        ("publications.html", False),
        ("research.html", False),
    ]
    for fname, needs_tree in pages:
        page_path = template_dir / fname
        if not page_path.exists():
            continue
        src_html = page_path.read_text(encoding="utf-8")
        html_doc = render_tokens(src_html, title, nb_count, tree if needs_tree else None, cfg)
        (out / fname).write_text(html_doc, encoding="utf-8")

    copy_tree(template_dir / "css", out / "css")
    copy_tree(template_dir / "assets", out / "assets")
    copy_tree(template_dir / "js", out / "js")

    return nb_count

# ================================ CLI ================================

def render_tokens(src: str, title: str, nb_count: int, tree: dict | None, cfg: dict | None):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # básicos
    rep = {
        r"\{\{\s*TITLE\s*\}\}": html.escape(title),
        r"\{\{\s*TIMESTAMP\s*\}\}": ts,
        r"\{\{\s*NBCOUNT\s*\}\}": str(nb_count),
    }

    # placeholders vindos do YAML (substituição literal, permitindo HTML)
    if cfg:
        for k, v in cfg.items():
            if v is None:
                continue
            pat = rf"\{{\{{\s*{re.escape(k)}\s*\}}\}}"
            rep[pat] = str(v)

    out = src
    for pat, val in rep.items():
        out = re.sub(pat, lambda m, v=val: v, out)

    if tree is not None:
        safe_json = json.dumps(tree, ensure_ascii=False).replace("</", "<\\/")
        out = re.sub(r"\{\{\s*TREE_JSON\s*\}\}", lambda m, v=safe_json: v, out)

    return out

def load_references(repo_path: Path) -> list[dict]:
    p = repo_path / "references.yml"
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        refs = data.get("references", [])
        return refs if isinstance(refs, list) else []
    except Exception as e:
        print(f"[warn] references.yml inválido em {repo_path}: {e}")
        return []

def render_references_html(refs: list[dict]) -> str:
    if not refs:
        return '<p class="muted"><em>No references provided yet.</em></p>'

    items = []
    for r in refs:
        title = html.escape(str(r.get("title", "")).strip() or "Untitled")
        author = html.escape(str(r.get("author", "")).strip())
        year = html.escape(str(r.get("year", "")).strip())
        note = html.escape(str(r.get("note", "")).strip())
        url = str(r.get("url", "")).strip()

        # monta linha “bonita” e mono
        parts = [f"<strong>{title}</strong>"]
        meta = " — ".join([p for p in [author, year] if p])
        if meta:
            parts.append(f"<div class='ref-meta'>{meta}</div>")
        if note:
            parts.append(f"<div class='ref-note'><em>{note}</em></div>")

        if url:
            safe_url = html.escape(url, quote=True)
            parts.append(f"<div class='ref-link'><a href='{safe_url}' target='_blank' rel='noreferrer'>link</a></div>")

        items.append("<li class='ref-item'>" + "\n".join(parts) + "</li>")

    return "<ul class='ref-list'>\n" + "\n".join(items) + "\n</ul>"
    
def main():
    ap = argparse.ArgumentParser(
        description="Gera um site estático a partir de notebooks .ipynb usando nbconvert e um template externo."
    )
    ap.add_argument("--src", type=str, required=True)
    ap.add_argument("--out", type=str, required=True)
    ap.add_argument("--template", type=str, required=True)
    ap.add_argument("--title", type=str, default=None)
    ap.add_argument("--execute", type=str, default="false")
    ap.add_argument("--cfg", type=str, default=None)  # <-- ADICIONE ISTO
    args = ap.parse_args()

    src = Path(args.src).resolve()
    out = Path(args.out).resolve()
    template_dir = Path(args.template).resolve()
    execute = args.execute.lower() == "true"
    title = args.title or f"Notebooks Tree — {src.name}"
    cfg = load_config(Path(args.cfg)) if args.cfg else {}   # <-- E ISTO

    nb_count = build_static_site(src, out, template_dir, title, execute, cfg)  # <-- E ISTO
    print(f"[OK] Gerado em {out} • notebooks convertidos: {nb_count}")

if __name__ == "__main__":
    main()
