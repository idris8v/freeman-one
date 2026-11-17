"""Offline release gates; tiny disposable files, no application or providers."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.check_assets import audit


class PublicAssets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.static = self.root / "app/web/static"
        self.static.mkdir(parents=True)
        self.image = b"fixture original image bytes"
        (self.static / "logo.png").write_bytes(self.image)
        (self.static / "index.html").write_text(
            '<link rel="stylesheet" href="/assets/main.css?v=1"><img src="/assets/logo.png">'
        )
        (self.static / "main.css").write_text("body{color:black}")
        manifest = {
            "version": 1,
            "required_images": {"app/web/static/logo.png": self.info(self.image)},
            "entrypoints": ["index.html"],
        }
        (self.root / "app/web/public-assets.json").write_text(json.dumps(manifest))
        styles = self.root / "app/web/styles"
        styles.mkdir()
        (styles / "manifest.json").write_text(
            json.dumps({"main.css": self.info((self.static / "main.css").read_bytes())})
        )

    @staticmethod
    def info(raw):
        return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def kinds(self, git_only=False):
        return {e["kind"] for e in audit(self.root, git_only=git_only)["errors"]}

    def css(self, content):
        raw = content.encode()
        (self.static / "main.css").write_bytes(raw)
        (self.root / "app/web/styles/manifest.json").write_text(
            json.dumps({"main.css": self.info(raw)})
        )

    def test_complete_filesystem_passes(self):
        self.assertEqual(self.kinds(), set())

    def test_missing_unreferenced_required_image_fails(self):
        (self.static / "index.html").write_text("<p>no image reference</p>")
        (self.static / "logo.png").unlink()
        self.assertIn("missing_asset", self.kinds())

    def test_modified_original_image_is_not_silently_accepted(self):
        (self.static / "logo.png").write_bytes(b"different image")
        self.assertIn("image_integrity", self.kinds())

    def test_html_relative_assets_queries_and_srcset(self):
        (self.static / "index.html").write_text(
            '<script src="missing.js?v=2"></script><img srcset="logo.png 1x, missing.png 2x">'
        )
        missing = {
            e["path"]
            for e in audit(self.root)["errors"]
            if e["kind"] == "missing_asset"
        }
        self.assertEqual(
            missing, {"app/web/static/missing.js", "app/web/static/missing.png"}
        )

    def test_dynamic_javascript_literal_reference_is_checked(self):
        (self.static / "app.js").write_text('const photo = "/assets/dynamic.png";')
        self.assertIn("missing_asset", self.kinds())

    def test_css_relative_url_is_checked(self):
        self.css('body{background:url("missing.png?v=3")}')
        self.assertIn("missing_asset", self.kinds())

    def test_generated_css_hash_is_checked(self):
        (self.static / "main.css").write_text("changed output")
        self.assertIn("css_integrity", self.kinds())

    def test_case_mismatch_fails_on_developer_filesystems(self):
        (self.static / "logo.png").rename(self.static / "Logo.png")
        self.assertIn("missing_asset", self.kinds())

    def test_external_embedded_and_runtime_urls_are_not_git_assets(self):
        (self.static / "index.html").write_text(
            '<img src="https://example.test/remote.png"><img src="data:image/png;base64,AAA"><img src="/api/avatar/1"><script src="//example.test/sdk.js"></script>'
        )
        self.css("body{background:url(data:image/png;base64,AAA)}")
        self.assertEqual(self.kinds(), set())

    def test_static_path_escape_is_rejected(self):
        self.css('body{background:url("../../../outside.png")}')
        self.assertIn("asset_outside_static", self.kinds())

    def test_local_only_assets_cannot_mask_clean_git_failure(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        self.assertEqual(self.kinds(git_only=True), set())
        subprocess.run(
            ["git", "-C", str(self.root), "rm", "--cached", "app/web/static/logo.png"],
            check=True,
            stdout=subprocess.PIPE,
        )
        self.assertEqual(self.kinds(), set())
        self.assertIn("not_in_git", self.kinds(git_only=True))

    def test_missing_manifest_fails_closed(self):
        (self.root / "app/web/public-assets.json").unlink()
        self.assertIn("missing_manifest", self.kinds())


if __name__ == "__main__":
    unittest.main()
