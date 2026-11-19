import os
import platform
import re
import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

PINNED_ZELLIJ = "efd8fd5a89a20c07a111d248ad7fce53848d2c18"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return
        libc, libc_version = platform.libc_ver()
        if platform.system() != "Linux" or platform.machine() != "x86_64" or libc != "glibc":
            raise RuntimeError("Release wheels must be built on glibc Linux x86-64")
        glibc = libc_version.replace(".", "_")

        self.work = tempfile.TemporaryDirectory(prefix="tdnext-zellij-build-")
        work = Path(self.work.name)
        source = os.environ.get("TDNEXT_ZELLIJ_SOURCE")
        if source is None:
            source = str(work / "zellij")
            subprocess.run(
                ["git", "clone", "--no-checkout", "https://github.com/zellij-org/zellij.git", source],
                check=True,
            )
            subprocess.run(["git", "-C", source, "checkout", "--detach", PINNED_ZELLIJ], check=True)
        binary = work / "tdnext"
        root = Path(self.root)
        openssl_lib = subprocess.check_output(["pkg-config", "--variable=libdir", "openssl"], text=True).strip()
        openssl_include = subprocess.check_output(["pkg-config", "--variable=includedir", "openssl"], text=True).strip()
        subprocess.run(
            ["bash", str(root / "scripts/build-zellij-core-navigation.sh"), source, str(binary)],
            env={**os.environ, "OPENSSL_STATIC": "1", "OPENSSL_LIB_DIR": openssl_lib,
                 "OPENSSL_INCLUDE_DIR": openssl_include},
            check=True,
        )
        readelf_env = {**os.environ, "LC_ALL": "C"}
        dynamic = subprocess.check_output(["readelf", "-d", str(binary)], text=True, env=readelf_env)
        needed = set(re.findall(r"\[(.+?\.so(?:\.\d+)*)\]", dynamic))
        allowed = {
            "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", "libgcc_s.so.1", "libz.so.1",
        }
        if "libc.so.6" not in needed or needed - allowed:
            raise RuntimeError(f"Non-manylinux shared libraries in Zellij: {sorted(needed - allowed)}")
        versions = subprocess.check_output(["readelf", "--version-info", str(binary)], text=True, env=readelf_env)
        required = set(re.findall(r"Name: (GLIBC_\S+)", versions))
        if not required:
            raise RuntimeError("Zellij has no readable glibc symbol versions")
        baseline = tuple(int(part) for part in libc_version.split("."))
        for name in required:
            parts = name.removeprefix("GLIBC_").split(".")
            if not all(part.isdigit() for part in parts) or tuple(map(int, parts)) > baseline:
                raise RuntimeError(f"Zellij requires {name}, incompatible with manylinux_{glibc}")
        build_data["force_include"][str(binary)] = "tdnext/_native/tdnext"
        build_data["force_include"][str(root / "patches/LICENSE.zellij-0.45.1")] = (
            "tdnext/_native/LICENSE.zellij-0.45.1"
        )
        openssl_license = os.environ.get("TDNEXT_OPENSSL_LICENSE", "/usr/share/doc/libssl-dev/copyright")
        apache_license = os.environ.get("TDNEXT_APACHE_LICENSE", "/usr/share/common-licenses/Apache-2.0")
        build_data["force_include"][openssl_license] = "tdnext/_native/LICENSE.openssl"
        build_data["force_include"][apache_license] = "tdnext/_native/LICENSE.apache-2.0"
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-manylinux_{glibc}_x86_64"

    def finalize(self, version, build_data, artifact_path):
        if version != "editable":
            self.work.cleanup()
