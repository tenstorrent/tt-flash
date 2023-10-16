import sys
from pathlib import Path
from glob import glob

def get_luwen_path(root):
    pyversion = f"{sys.version_info.major}{sys.version_info.minor}"

    max_version = None
    for file in glob(f"{root}/pyluwen-*cp{pyversion}-*.whl"):
        file = Path(file)
        version = file.name.split("-")[1]
        version = tuple(map(int, version.split(".")))

        if max_version is None or version > max_version:
            max_version = version

    if max_version is None:
        raise RuntimeError(f"Could not find pyluwen wheel for python version {pyversion}.")

    possible_values = glob(f"{root}/pyluwen-{'.'.join(map(str, max_version))}*cp{pyversion}-*.whl")

    if len(possible_values) == 0:
        raise RuntimeError(f"Could not find pyluwen wheel for python version {pyversion}.")
    elif len(possible_values) > 1:
        raise RuntimeError(f"Found multiple pyluwen wheels for python version {pyversion} ({possible_values}).")

    return possible_values[0]

print(get_luwen_path(Path(__file__).parent.parent.joinpath("pyluwen/whl")), end="")
