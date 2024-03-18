# tt-flash

This is a utility to flash firmware blobs to tenstorrent devices, currently only Grayskull boards are supported, detected Wormhole devices will be skipped.

## Official Repository

[https://github.com/tenstorrent/tt-flash](https://github.com/tenstorrent/tt-flash)

## Getting started

### Regardless of install path (straight pip or git clone for development)
#### Install Rust (if you don't already have it)
Note: This is only needed if you haven't already installed luwen
##### Using Distribution packages (preferred)
* **Fedora / EL9:** <br/> `sudo dnf install cargo`
* **Ubuntu / Debian:** <br/> `sudo apt install cargo`
##### Using Rustup 
```
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
```

#### (Optional) Virtual environment
Virtual environments allow Python to create a self-contained area where it can
install things and deal with it's own library pathing.  If you aren't doing
this as a system level install a virtual environment is recommended.

```
pip -m venv venv
source venv/bin/activate
```

### To install without git (non-development):

If you just want to install tt-flash, and not do any development, you can just
use pip (with or without a virtual environment) to install a public version.

```
pip install git+https://github.com/tenstorrent/tt-flash.git
```

***Note:*** if you fork the repository please fill in the appropriate accessible URL above to install tt-flash

tt-flash is now installed

### To Build from git (development):

#### Clone the repository

If you haven't already cloned the repository you are reading this from clone 
it now, this will vary depending on where this repository is at.  The official
public repository is at https://github.com/tenstorrent/tt-flash 

```
git clone https://github.com/tenstorrent/tt-flash.git
cd tt-flash
```

#### Building the repository

```
pip install .
```

or for users who would like to edit the code without re-building

```
pip install --editable .
```

### Help text 
```
usage: tt-flash [-h] [-v] [--force] [--fw-tar FW_BUNDLE] 

options:
  -h, --help            show this help message and exit
  -v, --version         show program's version number and exit
  --force               Force update the ROM
  --fw-tar FW_BUNDLE       Path to the firmware bundle
```

## Typical usage
```
tt-flash --fw-tar <firmware bundle file path goes here>
```

### Grayskull Note:
If you are using a Grayskull based card, the card itself does not have an on-board reset mechanism, and you will need to reboot to have the new firmware apply.

### Firmware files
Firmware files are licensed and distributed independently of tt-flash, as this is just the system to deal with the actual flashing of the images.

| Product Line | Firmware repository |
| --- | --- |
| Grayskull | [https://github.com/tenstorrent/tt-firmware-gs](https://github.com/tenstorrent/tt-firmware-gs)

## License

Apache 2.0 - https://www.apache.org/licenses/LICENSE-2.0.txt
