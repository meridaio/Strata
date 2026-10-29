"""Tests for setup.py's download hardening: --build is remembered and keeps every ready-made binary out, the source
archives and the CUDA repository key are pinned by SHA-256, and the key never goes through a fixed name in /tmp.
No network, no GPU.

    python -m unittest tools.test_setup_build_only
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import setup  # noqa: E402


def no_network(*a, **k):
    raise AssertionError("nothing may be fetched")


class BuildOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        for patcher in (mock.patch.object(setup, "home", lambda: self.home),
                        mock.patch.dict(os.environ, {}, clear=False),
                        mock.patch.object(setup, "say"),
                        mock.patch.object(setup, "_urlopen", no_network),
                        mock.patch.object(setup, "download", no_network),
                        mock.patch.object(setup, "pip_install", no_network),
                        mock.patch.object(setup.subprocess, "run", no_network)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("STRATA_BUILD_ONLY", None)

    def test_remembered_and_forgotten(self):
        self.assertFalse(setup.build_only())
        setup.set_build_only(True)
        self.assertTrue((self.home / setup.BUILD_ONLY_MARK).exists())
        self.assertTrue(setup.build_only())
        setup.set_build_only(False)
        self.assertFalse(setup.build_only())

    def test_the_environment_asks_for_it_too(self):
        os.environ["STRATA_BUILD_ONLY"] = "1"
        self.assertTrue(setup.build_only())

    def test_no_ready_made_binary_is_fetched(self):
        setup.set_build_only(True)
        gpu = {"arch": 89, "index": 0}
        self.assertIsNone(setup.get_prebuilt(setup.PREBUILT_URL, gpu, "gpu"))
        self.assertIsNone(setup.get_prebuilt(setup.PREBUILT_URL, gpu, "gpu", toolkit=12))
        self.assertIsNone(setup.get_prebuilt_hip(setup.PREBUILT_URL, {"arch": "gfx1100"}))
        self.assertIsNone(setup.engine_from_feed(self.home / "engine"))
        self.assertIsNone(setup.get_dsv41_engine(self.home / "engine", False))
        exe = self.home / "strata-dsv41"
        self.assertEqual(setup.upgrade_dsv41_engine(self.home / "engine", exe), exe)
        setup.pip_cuda_libs(13)                            # returns without pip
        setup.pip_cuda_libs(12)

    def test_the_cuda12_move_at_start_compiles(self):
        setup.set_build_only(True)
        with mock.patch.object(setup, "build_engine", return_value=self.home / "engine-cuda12") as build, \
                mock.patch.object(setup, "get_llama_cpp", return_value=self.home / "llama.cpp"):
            eng = setup.get_cuda12_engine(setup.PREBUILT_URL, {"arch": 61, "archs": [61]}, "none", True,
                                          build=setup.build_only())
        self.assertEqual(eng, self.home / "engine-cuda12")
        build.assert_called_once()


class Pins(unittest.TestCase):
    def test_the_llama_cpp_source_is_pinned(self):
        size, sha = setup.file_pin(setup.LLAMA_CPP_ZIP)
        self.assertGreater(size, 0)
        self.assertRegex(sha, r"^[0-9a-f]{64}$")

    def test_every_cuda_repository_setup_adds_has_a_pinned_key(self):
        for osr in ({"ID": "ubuntu", "VERSION_ID": '"22.04"'}, {"ID": "ubuntu", "VERSION_ID": '"24.04"'},
                    {"ID": "debian", "VERSION_ID": '"12"'}, {"ID": "debian", "VERSION_ID": '"13"'}):
            repo = setup.cuda_apt_repo(osr)
            for arch in ("x86_64", "sbsa"):
                with self.subTest(repo=repo, arch=arch):
                    self.assertIn((repo, arch), setup.CUDA_KEYRING_PINS)
                    self.assertIsNotNone(setup.file_pin(setup.CUDA_KEYRING_URL.format(repo=repo, arch=arch)))

    def test_the_key_never_goes_through_a_fixed_name_in_tmp(self):
        src = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertNotIn('Path("/tmp/cuda-keyring.deb")', src)
        body = src[src.index("def install_build_tools("):]
        body = body[:body.index("\ndef ")]
        self.assertIn("mkdtemp", body)
        self.assertLess(body.index("verify_sha256(deb"), body.index('"dpkg", "-i"'))

    def test_the_image_sources_are_pinned(self):
        qi = setup._qimage()
        for pin in (qi.SD_CPP_ZIP_PIN, qi.SD_GGML_ZIP_PIN):
            self.assertGreater(pin[0], 0)
            self.assertRegex(pin[1], r"^[0-9a-f]{64}$")


class Locks(unittest.TestCase):
    def test_a_lock_is_found_for_its_platform_and_python(self):
        with mock.patch.object(setup.platform, "machine", return_value="x86_64"), \
                mock.patch.object(setup.sys, "platform", "linux"), mock.patch.object(setup, "WIN", False), \
                mock.patch.object(setup.sys, "version_info", (3, 12, 0)):
            self.assertEqual(setup.lock_target(), "linux-x86_64")
            self.assertEqual(setup.lock_file("runtime"), setup.LOCKS / "runtime-linux-x86_64-cp312.txt")
        with mock.patch.object(setup.sys, "version_info", (3, 13, 0)):
            self.assertIsNone(setup.lock_file("runtime"))          # no lock for that Python: versions only, said

    def test_a_locked_install_requires_hashes(self):
        lock = setup.LOCKS / "runtime-linux-x86_64-cp312.txt"
        seen = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(setup.sys, "prefix", d), mock.patch.object(setup, "CONTROL", False), \
                mock.patch.object(setup, "run", lambda cmd, **k: seen.append(cmd)), \
                mock.patch.object(setup, "say"), mock.patch.object(setup, "ok"):
            setup.pip_install(["numpy==2.5.3"], "numpy", lock=lock)
        self.assertEqual(len(seen), 1)
        self.assertIn("--require-hashes", seen[0])
        self.assertEqual(seen[0][-2:], ["-r", str(lock)])

    def test_torch_never_comes_from_an_extra_index(self):
        src = (ROOT / "setup.py").read_text(encoding="utf-8")
        body = src[src.index("def ensure_nvfp4_local_packages("):]
        body = body[:body.index("\ndef ")]
        self.assertNotIn('"--extra-index-url"', body)


if __name__ == "__main__":
    unittest.main()
