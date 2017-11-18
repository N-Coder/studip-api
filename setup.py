#!/usr/bin/env python3

import os
import re

from setuptools import setup

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.realpath(__file__)))

    with open("studip_api/__init__.py", "r") as file:
        version = re.search('^__version__\s*=\s*"(.*)"', file.read(), re.M).group(1)

    with open("README.md", "rb") as f:
        long_descr = f.read().decode("utf-8")

    setup(
        name="studip-api",
        packages=["studip_api"],
        include_package_data=True,
        install_requires=[
            "more_itertools",
            "attrs",
            "asyncio",
            "aiofiles",
            "aiohttp",
            "beautifulsoup4",
            "lxml",
            "cached_property",
        ],
        version=version,
        description="Python API for courses and files available through the Stud.IP University Access Portal",
        long_description=long_descr,
        author="Fabian Knorr, Jonas Pöhler, Simon Fink",
        url="https://github.com/N-Coder/studip-api"
    )
