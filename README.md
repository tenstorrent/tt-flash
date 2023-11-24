# tt-flash

This is a utility to flash firmware blobs to tenstorrent devices, currently only Grayskull boards are supported, detected Wormhole devices will be skipped.

## Getting started

### To Build from git:

Install and source rust to build the luwen library

```
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
```

#### Optional
```
pip -m venv venv
source venv/bin/activate
```
#### Required
```
pip install .
```

or for users who would like to edit the code without re-building

```
pip install --editable .
```

### Help text 
```
usage: tt-flash [-h] [-v] [--force] [--fw-tar FW_TAR] 

options:
  -h, --help            show this help message and exit
  -v, --version         show program's version number and exit
  --force               Force update the ROM
  --fw-tar FW_TAR       Path to the firmware tarball
```

### Typical usage
```
tt-flash --fw-tar <firmware tar file path goes here>
```

### Firmware files
Firmware files are licensed and distributed independently of tt-flash, as this is just the system to deal with the actual flashing of the images.  For Tenstorrent firmware images please see the Tenstorrent website for a pointer on where these would be, or look in the Tenstorrent Github organizations for the appropriate repository.

## License

Apache 2.0 - https://www.apache.org/licenses/LICENSE-2.0.txt
