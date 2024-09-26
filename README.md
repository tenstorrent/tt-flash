# tt-flash

This is a utility to flash firmware blobs to tenstorrent devices.

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
python -m venv venv
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
usage: tt-flash [-h] [-v] [--sys-config SYS_CONFIG] [--no-color] [--no-tty] {flash} ...

optional arguments:
  -h, --help            show this help message and exit
  -v, --version         show program's version number and exit
  --sys-config SYS_CONFIG
                        Path to the pre generated sys-config json
  --no-color            Disable the colorful output
  --no-tty              Force disable the tty command output

command:
  {flash}

usage: tt-flash flash [-h] [--sys-config SYS_CONFIG] --fw-tar FW_TAR [--skip-missing-fw] [--force] [--no-reset]

optional arguments:
  -h, --help            show this help message and exit
  --sys-config SYS_CONFIG
                        Path to the pre generated sys-config json
  --fw-tar FW_TAR       Path to the firmware tarball
  --skip-missing-fw     If the fw packages doesn't contain the fw for a detected board, continue flashing
  --force               Force update the ROM
  --no-reset            Do not reset devices at the end of flash
```

## Typical usage
```
tt-flash flash --fw-tar <firmware bundle file path goes here>
```

### Grayskull Note:
If you are using a Grayskull based card, the card itself does not have an on-board reset mechanism, and you will need to reboot to have the new firmware apply.

### Firmware files
Firmware files are licensed and distributed independently of tt-flash, as this is just the system to deal with the actual flashing of the images. You can find the firmware files in a seperate repo [https://github.com/tenstorrent/tt-firmware](https://github.com/tenstorrent/tt-firmware).

### Example output

This is an example of what you can expect the final output to look like on a system when not needing an update

```
$ tt-flash flash --fw-tar ~/work/build-combine-flash-pkg/fw_pack.tar.gz
Stage: SETUP
        Searching for default sys-config path
        Checking /etc/tenstorrent/config.json: not found
        Checking ~/.config/tenstorrent/config.json: not found

        Could not find config in default search locations, if you need it, either pass it in explicity or generate one
        Warning: continuing without sys-config, galaxy systems will not be reset
Stage: DETECT
Stage: FLASH
        Sub Stage: VERIFY
                Verifying fw-package can be flashed: complete
                Verifying Grayskull[0] can be flashed
                Verifying Wormhole[1] can be flashed
        Stage: FLASH
                Sub Stage FLASH Step 1: Grayskull[0]
                        ROM version is: (80, 9, 0, 0). tt-flash version is: (80, 9, 0, 0)
                        ROM does not need to be updated.
                Sub Stage FLASH Step 1: Wormhole[1]
                        ROM version is: (80, 9, 0, 0). tt-flash version is: (80, 9, 0, 0)
                        ROM does not need to be updated.
FLASH SUCCESS
```

And when forcing an update with `--force`

```
$ tt-flash flash --fw-tar ~/work/build-combine-flash-pkg/fw_pack.tar.gz --force
Stage: SETUP
        Searching for default sys-config path
        Checking /etc/tenstorrent/config.json: not found
        Checking ~/.config/tenstorrent/config.json: not found

        Could not find config in default search locations, if you need it, either pass it in explicity or generate one
        Warning: continuing without sys-config, galaxy systems will not be reset
Stage: DETECT
Stage: FLASH
        Sub Stage: VERIFY
                Verifying fw-package can be flashed: complete
                Verifying Grayskull[0] can be flashed
                Verifying Wormhole[1] can be flashed
        Stage: FLASH
                Sub Stage FLASH Step 1: Grayskull[0]
                        ROM version is: (80, 9, 0, 0). tt-flash version is: (80, 9, 0, 0)
                        Forced ROM update requested. ROM will now be updated.
                Sub Stage FLASH Step 1: Wormhole[1]
                        ROM version is: (80, 9, 0, 0). tt-flash version is: (80, 9, 0, 0)
                        Forced ROM update requested. ROM will now be updated.
                        Board will require reset to complete update, checking if an automatic reset is possible
                                Success: Board can be auto reset; will be triggered if the flash is successful
                Sub Stage FLASH Step 2: Grayskull[0] {e150}
                        Writing new firmware... SUCCESS
                        Firmware verification... SUCCESS
                Sub Stage FLASH Step 2: Wormhole[1] {n300}
                        Writing new firmware... SUCCESS
                        Firmware verification... SUCCESS
                        Initiating local to remote data copy
                Flash and verification for all chips completed, will now wait for for n300 remote copy to complete...
                Remote copy completed
Stage: RESET
 Starting PCI link reset on WH devices at PCI indices: 1
 Finishing PCI link reset on WH devices at PCI indices: 1

FLASH SUCCESS
```

## License

Apache 2.0 - https://www.apache.org/licenses/LICENSE-2.0.txt
