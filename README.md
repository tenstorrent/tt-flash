# tt-flash

This is a utility to flash firmware blobs to tenstorrent devices, currently only Grayskull boards are supported, detected Wormhole devices will be skipped.

## Official Repository

[https://github.com/tenstorrent/tt-flash](https://github.com/tenstorrent/tt-flash)

## Getting started

### To Build from git:

#### Install Rust
```sh
# Install rust to build the luwen library
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
```

#### Clone and Build

```sh
# Clone tt-flash repo
git clone https://github.com/tenstorrent/tt-flash.git tt-flash
cd tt-flash

# Optional: Setup a Python virtual environment
python3 -m venv venv
source venv/bin/activate

# Install tt-flash
pip install .
# or for users who would like to edit the code without re-building
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

#### Grayskull Note:
If you are using a Grayskull based card, the card itself does not have an on-board reset mechanism, and you will need to reboot to have the new firmware apply.

### Firmware files
Firmware files are licensed and distributed independently of tt-flash, as this is just the system to deal with the actual flashing of the images.

| Product Line | Firmware repository |
| --- | --- |
| Grayskull | [https://github.com/tenstorrent/tt-firmware-gs](https://github.com/tenstorrent/tt-firmware-gs)

## License

Apache 2.0 - https://www.apache.org/licenses/LICENSE-2.0.txt
