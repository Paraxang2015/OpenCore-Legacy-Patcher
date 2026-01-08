"""
metallib_handler.py: Library for handling Metal libraries
FIXED: Prevent MetalLib from loading during installer/recovery
"""

import logging
import requests
import subprocess
import packaging.version

from typing import cast
from pathlib import Path

from . import network_handler, subprocess_wrapper
from .. import constants
from ..datasets import os_data


METALLIB_INSTALL_PATH: str = "/Library/Application Support/Dortania/MetallibSupportPkg"
METALLIB_API_LINK: str = "https://dortania.github.io/MetallibSupportPkg/manifest.json"

METALLIB_ASSET_LIST: list = None


class MetalLibraryObject:

    def __init__(
        self,
        global_constants: constants.Constants,
        host_build: str,
        host_version: str,
        ignore_installed: bool = False,
        passive: bool = False
    ) -> None:

        self.constants: constants.Constants = global_constants

        self.host_build: str = host_build
        self.host_version: str = host_version

        self.passive: bool = passive
        self.ignore_installed: bool = ignore_installed

        self.metallib_already_installed: bool = False
        self.metallib_installed_path: str = ""

        self.metallib_url: str = ""
        self.metallib_url_build: str = ""
        self.metallib_url_version: str = ""
        self.metallib_url_is_exactly_match: bool = False

        self.metallib_closest_match_url: str = ""
        self.metallib_closest_match_url_build: str = ""
        self.metallib_closest_match_url_version: str = ""

        self.success: bool = False
        self.error_msg: str = ""

        # 🔴 CRITICAL FIX:
        # Never resolve MetalLib during installer or recovery
        if self.constants.installer_environment or self.constants.recovery_environment:
            logging.info("Installer/Recovery environment detected, skipping MetallibSupportPkg")
            self.success = True
            return

        self._get_latest_metallib()


    def _get_remote_metallibs(self) -> dict:
        """
        Get the MetallibSupportPkg list from the API
        """

        global METALLIB_ASSET_LIST

        if METALLIB_ASSET_LIST:
            return METALLIB_ASSET_LIST

        logging.info("Pulling metallib list from MetallibSupportPkg API")

        try:
            results = network_handler.NetworkUtilities().get(
                METALLIB_API_LINK,
                headers={"User-Agent": f"OCLP/{self.constants.patcher_version}"},
                timeout=5
            )
        except (requests.exceptions.Timeout,
                requests.exceptions.TooManyRedirects,
                requests.exceptions.ConnectionError):
            logging.warning("Could not contact MetallibSupportPkg API")
            return None

        if results.status_code != 200:
            logging.warning("Could not fetch Metallib list")
            return None

        METALLIB_ASSET_LIST = results.json()
        return METALLIB_ASSET_LIST


    def _get_latest_metallib(self) -> None:
        """
        Resolve correct MetallibSupportPkg
        """

        parsed_version = cast(
            packaging.version.Version,
            packaging.version.parse(self.host_version)
        )

        # Metallib only required for Sequoia / Tahoe+
        if os_data.os_conversion.os_to_kernel(str(parsed_version.major)) < os_data.os_data.sequoia:
            logging.info("MetallibSupportPkg not required for this OS")
            self.success = True
            return

        self.metallib_installed_path = self._local_metallib_installed()
        if self.metallib_installed_path:
            logging.info(f"Metallib already installed ({Path(self.metallib_installed_path).name})")
            self.metallib_already_installed = True
            self.success = True
            return

        remote_metallib_version = self._get_remote_metallibs()
        if remote_metallib_version is None:
            self.error_msg = "Failed to fetch Metallib manifest"
            logging.warning(self.error_msg)
            return

        # Exact build match first
        for metallib in remote_metallib_version:
            if metallib["build"] == self.host_build:
                self.metallib_url = metallib["url"]
                self.metallib_url_build = metallib["build"]
                self.metallib_url_version = metallib["version"]
                self.metallib_url_is_exactly_match = True
                break

        # Closest match (POST-INSTALL ONLY)
        if not self.metallib_url:
            for metallib in remote_metallib_version:
                metallib_version = cast(
                    packaging.version.Version,
                    packaging.version.parse(metallib["version"])
                )

                if metallib_version > parsed_version:
                    continue
                if metallib_version.major != parsed_version.major:
                    continue

                self.metallib_closest_match_url = metallib["url"]
                self.metallib_closest_match_url_build = metallib["build"]
                self.metallib_closest_match_url_version = metallib["version"]
                break

            if not self.metallib_closest_match_url:
                self.error_msg = f"No suitable metallib found for {self.host_build}"
                logging.warning(self.error_msg)
                return

            self.metallib_url = self.metallib_closest_match_url
            self.metallib_url_build = self.metallib_closest_match_url_build
            self.metallib_url_version = self.metallib_closest_match_url_version

        logging.info("Recommended Metallib:")
        logging.info(f"  Build: {self.metallib_url_build}")
        logging.info(f"  Version: {self.metallib_url_version}")
        logging.info(f"  URL: {self.metallib_url}")

        self.success = True


    def _local_metallib_installed(self, match: str = None, check_version: bool = False) -> str:
        """
        Check if MetallibSupportPkg is already installed
        """

        if self.ignore_installed:
            return None

        path = Path(METALLIB_INSTALL_PATH)
        if not path.exists():
            return None

        for metallib_folder in path.iterdir():
            if not metallib_folder.is_dir():
                continue
            if check_version:
                if match and match not in metallib_folder.name:
                    continue
            else:
                if match and not metallib_folder.name.endswith(f"-{match}"):
                    continue
            return str(metallib_folder)

        return None


    def retrieve_download(self, override_path: str = "") -> network_handler.DownloadObject:
        """
        Retrieve download object
        """

        if self.metallib_already_installed or not self.metallib_url:
            return None

        download_path = (
            self.constants.metallib_download_path
            if not override_path
            else Path(override_path)
        )

        return network_handler.DownloadObject(self.metallib_url, download_path)


    def install_metallib(self, metallib: str = None) -> bool:
        """
        Install MetallibSupportPkg (POST-INSTALL ONLY)
        """

        if self.passive:
            logging.info("Passive mode, skipping metallib installation")
            return True

        if not self.success or self.metallib_already_installed:
            return True

        result = subprocess_wrapper.run_as_root(
            [
                "/usr/sbin/installer",
                "-pkg",
                metallib if metallib else self.constants.metallib_download_path,
                "-target",
                "/"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        if result.returncode != 0:
            subprocess_wrapper.log(result)
            return False

        return True
