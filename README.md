# tt-flash



## Getting started

### To Build from git:

- Requirements
    - luwen library (specifically pyluwen)
        - pre-installed
        - rust compiler and ability to compile rust

#### Optional
```
pip -m venv venv
source venv/bin/activate
```
#### Required
```
pip install -r requirements.txt
pip install .
```

### Help text (may not be up to date)
```
usage: tt-flash [-h] [-v] [--interface INTERFACE] [--force] [--fw-tar FW_TAR] [--read] [--configure] [--fw-only] [--skip-voltage-change] [--external]

options:
  -h, --help            show this help message and exit
  -v, --version         show program's version number and exit
  --interface INTERFACE
                        For multi-card systems "all" (default) iterates through all cards. "pci:0", "pci:1", etc. acts on a single card
  --force               Force update the ROM
  --fw-tar FW_TAR       Path to the firmware tarball
  --read                Prints a summary of the SPI contents
  --configure           Flashes the spi
  --fw-only             Flashes only the fw
  --skip-voltage-change
                        Skips voltage switching for SPI programming
  --external            Run the external version when T6PY_RELEASE=0. External is default when T6PY_RELEASE=1.
```

### Typical usage
```
tt-flash --fw-tar <firmware tar file path goes here>
```

### Firmware files
Firmware files are licensed and distributed independently of tt-flash, as this is just the system to deal with the actual flashing of the images.  For Tenstorrent firmware images please see the Tenstorrent website for a pointer on where these would be, or look in the Tenstorrent Github organizations for the appropriate repository.

## License

Apache 2.0 - https://www.apache.org/licenses/LICENSE-2.0.txt
