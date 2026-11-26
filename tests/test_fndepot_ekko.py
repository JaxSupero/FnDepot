import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import gen_fnpack as G
import nas_sync as N


class EkkoIndexTests(unittest.TestCase):
    def test_minimum_os_version_is_emitted_on_release(self):
        spec = G.app_spec("ekko-studio")
        rel = {
            "version": "0.8.31-1",
            "changelog": "release notes",
            "updated_at": "2026-11-25T02:11:52Z",
            "packages": {"x86": {"download_url": "https://example.test/a.fpk",
                                   "sha256": "a" * 64, "size": 10}},
        }
        doc = G.build([(spec, [rel])], "JaxSupero")
        errs, _ = G.validate(doc)
        self.assertEqual(errs, [])
        self.assertEqual(doc["apps"]["ekko-studio"]["releases"]["0.8.31-1"]["os_min_version"], "0.9.27")

    def run_build_index(self, deployed):
        spec = {"app_name": "demo", "keep_versions": 2, "arch_key": "x86"}
        rels = [
            {"version": "2.0", "packages": {"x86": {"size": 20, "sha256": "b" * 64},
                                                  "arm": {"size": 21, "sha256": "c" * 64}}},
            {"version": "1.0", "packages": {"x86": {"size": 10, "sha256": "d" * 64},
                                                  "arm": {"size": 11, "sha256": "f" * 64}}},
        ]
        seen = {}
        def stabilize(items, settle_minutes, only_versions):
            seen["only_versions"] = only_versions
            return items, []
        with patch.object(N.G, "APP_SPECS", [spec]), \
             patch.object(N.G, "FETCH_EXTRA", 0), \
             patch.object(N.G, "SETTLE_MINUTES", 0), \
             patch.object(N.G, "fetch_releases", return_value=rels), \
             patch.object(N.G, "stabilize", side_effect=stabilize), \
             patch.object(N.G, "build", return_value={"schema_version": "2"}), \
             patch.object(N.G, "validate", return_value=([], [])):
            N.build_index(deployed=deployed)
        return seen["only_versions"]

    def test_no_deployed_index_checks_latest_only(self):
        self.assertEqual(self.run_build_index(None), {"2.0"})

    def test_unchanged_nested_package_metadata_skips_old_versions(self):
        deployed = {"apps": {"demo": {"releases": {
            "2.0": {"packages": {"x86": {"size": 20, "sha256": "b" * 64},
                                   "arm": {"size": 21, "sha256": "c" * 64}}},
            "1.0": {"packages": {"x86": {"size": 10, "sha256": "d" * 64},
                                    "arm": {"size": 11, "sha256": "f" * 64}}},
        }}}}
        self.assertEqual(self.run_build_index(deployed), {"2.0"})

    def test_arm_package_change_also_triggers_version_check(self):
        deployed = {"apps": {"demo": {"releases": {
            "2.0": {"packages": {"x86": {"size": 20, "sha256": "b" * 64},
                                   "arm": {"size": 21, "sha256": "c" * 64}}},
            "1.0": {"packages": {"x86": {"size": 10, "sha256": "d" * 64},
                                    "arm": {"size": 12, "sha256": "g" * 64}}},
        }}}}
        self.assertEqual(self.run_build_index(deployed), {"2.0", "1.0"})


if __name__ == "__main__":
    unittest.main()
