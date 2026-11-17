#!/usr/bin/env python3
"""Validate shipped public assets without .env, database or external services.

Use --git-only before release; filesystem mode also runs inside Docker builds.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ASSET_LITERAL = re.compile(
    r"(?<![\w:/])/assets/([\w.%/-]+\.(?:png|webp|svg|jpe?g|gif|css|js|woff2?))", re.I
)
CSS_URL = re.compile(r"url\(\s*['\"]?([^)'\"\s]+)", re.I)


class AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        keys = {
            "script": ("src",),
            "link": ("href",),
            "img": ("src", "srcset"),
            "source": ("src", "srcset"),
            "image": ("href", "xlink:href"),
        }.get(tag, ())
        for key in keys:
            value = attrs.get(key)
            if value:
                if key == "srcset" and not value.startswith("data:"):
                    self.links.extend(
                        part.strip().split()[0]
                        for part in value.split(",")
                        if part.strip()
                    )
                else:
                    self.links.append(value)


def file_exact(path, root):
    """Detect Linux case mismatches even on a case-insensitive developer disk."""
    if not path.is_file():
        return False
    cursor = root
    for part in path.relative_to(root).parts:
        if part not in {entry.name for entry in cursor.iterdir()}:
            return False
        cursor /= part
    return True


def audit(root, *, git_only=False):
    root = Path(root).resolve()
    static = root / "app/web/static"
    manifest_path = root / "app/web/public-assets.json"
    errors, required, references = [], set(), {}

    def error(kind, path, source=None):
        item = {"kind": kind, "path": str(path)}
        if source:
            item["source"] = str(source)
        if item not in errors:
            errors.append(item)

    def local_reference(value, source):
        url = urlsplit(value)
        if url.scheme or url.netloc or not url.path or value.startswith("#"):
            return
        path = unquote(url.path)
        if path.startswith("/assets/"):
            target = static / path[8:]
        elif path.startswith("/"):
            return  # Application routes/uploads are not public build inputs.
        else:
            target = source.parent / path
        target = target.resolve()
        if not target.is_relative_to(static):
            error("asset_outside_static", value, source.relative_to(root))
            return
        name = target.relative_to(root).as_posix()
        required.add(name)
        references.setdefault(name, set()).add(source.relative_to(root).as_posix())

    if not manifest_path.is_file():
        return {
            "errors": [
                {"kind": "missing_manifest", "path": "app/web/public-assets.json"}
            ],
            "required": [],
        }
    try:
        manifest = json.loads(manifest_path.read_text())
        if manifest["version"] != 1:
            raise ValueError("unsupported manifest")
        for name, info in manifest["required_images"].items():
            path = root / name
            if not path.resolve().is_relative_to(static):
                raise ValueError("invalid manifest path")
            required.add(name)
            if path.is_file():
                raw = path.read_bytes()
                if (
                    len(raw) != info["bytes"]
                    or hashlib.sha256(raw).hexdigest() != info["sha256"]
                ):
                    error("image_integrity", name)
        required.update("app/web/static/" + name for name in manifest["entrypoints"])
    except (KeyError, ValueError, TypeError):
        error("invalid_manifest", "app/web/public-assets.json")

    for source in sorted((root / "app").rglob("*")):
        if (
            source.suffix not in {".py", ".html", ".js", ".css", ".svg"}
            or source.is_symlink()
        ):
            continue
        content = source.read_text(errors="strict")
        for match in ASSET_LITERAL.finditer(content):
            local_reference("/assets/" + match[1], source)
        if source.is_relative_to(static):
            if source.suffix in {".html", ".svg"}:
                parser = AssetLinks()
                parser.feed(content)
                for value in parser.links:
                    local_reference(value, source)
            elif source.suffix == ".css":
                for match in CSS_URL.finditer(content):
                    local_reference(match[1], source)

    # The production image ships these generated CSS files, without Node.
    css_manifest = root / "app/web/styles/manifest.json"
    try:
        for name, info in json.loads(css_manifest.read_text()).items():
            path = static / name
            required.add(path.relative_to(root).as_posix())
            if path.is_file():
                raw = path.read_bytes()
                if (
                    len(raw) != info["bytes"]
                    or hashlib.sha256(raw).hexdigest() != info["sha256"]
                ):
                    error("css_integrity", path.relative_to(root))
    except (OSError, ValueError, KeyError, TypeError):
        error("invalid_css_manifest", "app/web/styles/manifest.json")

    for name in sorted(required):
        path = root / name
        if not file_exact(path, root):
            error(
                "missing_asset",
                name,
                next(iter(sorted(references.get(name, ()))), None),
            )
        elif path.is_symlink() or not path.resolve().is_relative_to(static):
            error("unsafe_asset_path", name)
    if git_only:
        output = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached"], cwd=root
        )
        tracked = {name.decode() for name in output.split(b"\0") if name}
        # Local-only files must never make a release appear complete.
        public = {
            p.relative_to(root).as_posix()
            for p in static.rglob("*")
            if p.is_file()
            and not any(part.startswith(".") for part in p.relative_to(static).parts)
        }
        for name in sorted(required | public | {"app/web/public-assets.json"}):
            if name not in tracked:
                error("not_in_git", name)
    return {
        "mode": "git" if git_only else "filesystem",
        "required": sorted(required),
        "references": {
            name: sorted(items) for name, items in sorted(references.items())
        },
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--git-only", action="store_true")
    args = parser.parse_args()
    result = audit(args.root, git_only=args.git_only)
    print(
        json.dumps(
            {
                "mode": result.get("mode"),
                "required_assets": len(result["required"]),
                "errors": result["errors"],
            },
            sort_keys=True,
        )
    )
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
