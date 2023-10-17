from datetime import date
from pathlib import Path
from setuptools import setup, find_packages
import subprocess
import tempfile

def get_version():
    today = date.today()
    today_date = today.strftime("%Y-%m-%d")
    result = subprocess.run(["git", "rev-parse", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0:
        return "%s-%s" % (today_date, result.stdout[0:16].decode("ascii"))
    raise RuntimeError("Must be run from inside git repo!")

ignore_path = Path("tt_flash/.ignored")
ignore_path.mkdir(exist_ok=True, parents=True)
version_file = ignore_path.joinpath("version.txt")
with open(version_file, "w") as f:
    f.write(get_version())

setup(
    maintainer='drosen',
    maintainer_email='drosen@tenstorrent.com',
    name='tt_flash',
    version="0.1.0",
    url='http://tenstorrent.com',
    license='TODO: License',
    long_description="",
    packages=find_packages(),
    package_data={
        "tt_flash": [
            ".ignored/version.txt",
            "data/*/*.yaml"
        ],
    },
    include_package_data=True,
    setup_requires=['wheel'],
    install_requires=[
        "pyyaml",
        "tabulate",
        "pyluwen",
    ],
    entry_points={
        "console_scripts": [
            "tt-flash = tt_flash.main:main"
        ]
    }
)
