# tt-flash

This is a utility to flash firmware blobs to tenstorrent devices.

Flash firmware on all devices on a system using one command:

```
tt-flash <firmware bundle file path>
```

## Official Repository

[https://github.com/tenstorrent/tt-flash](https://github.com/tenstorrent/tt-flash)

## Getting started
### Install Rust (if you don't already have it)
If Rust isn't already installed on your system, you can install it through either of the following methods:

#### Using Distribution packages (preferred)
* **Fedora / EL9:** <br/> `sudo dnf install cargo`
* **Ubuntu / Debian:** <br/> `sudo apt install cargo`
#### Using Rustup
```
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
```

### User installation
tt-flash is available on pypi and can be installed using pip.

```
pip install tt-flash
```

#### (Optional) Virtual environment

If you aren't doing
this as a system-level install, a virtual environment is recommended.

```
python -m venv .venv
source .venv/bin/activate
```


### Developer installation
#### Clone the repository
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
Use the `-h` argument to print the help text.

```
$ tt-flash -h

usage: tt-flash [-h] [-v] [--sys-config SYS_CONFIG] [--no-color] [--no-tty] {flash,verify} ...

options:
  -h, --help            show this help message and exit
  -v, --version         show program's version number and exit
  --sys-config SYS_CONFIG
                        Path to the pre generated sys-config json
  --no-color            Disable the colorful output
  --no-tty              Force disable the tty command output

command:
  {flash,verify}
    flash               Flash firmware to Tenstorrent devices on the system. Run tt-flash flash -h for further command-specific help.
    verify              Verify the contents of the SPI. Will display the currently running and flashed bundle version of the fw and checksum the fw against either what was flashed previously according
                        the the file system state, or a given fw bundle. In the case where a fw bundle or flash record are not provided the program will search known locations that the flash record
                        may have been written to and exit with an error if it cannot be found or read. Run tt-flash verify -h for further command-specific help.
```

```
$ tt-flash flash -h

usage: tt-flash flash [-h] [--sys-config SYS_CONFIG] [--fw-tar FW_TAR] [--skip-missing-fw] [--force] [--no-reset] [fwbundle]

positional arguments:
  fwbundle              Path to the firmware bundle

options:
  -h, --help            show this help message and exit
  --sys-config SYS_CONFIG
                        Path to the pre generated sys-config json
  --fw-tar FW_TAR       Path to the firmware tarball (deprecated)
  --skip-missing-fw     If the fw packages doesn't contain the fw for a detected board, continue flashing
  --force               Force update the ROM
  --no-reset            Do not reset devices at the end of flash
```

## Typical usage
```
tt-flash <firmware bundle file path goes here>
```

### Firmware files
Firmware files are licensed and distributed independently, as tt-flash solely acts as a utility to update devices with provided firmware images. You can find firmware bundles in a seperate repo at [https://github.com/tenstorrent/tt-firmware](https://github.com/tenstorrent/tt-firmware).

### Example output

This is an example of what you can expect to see when you flash a device.

```
$ tt-flash ~/tt-firmware/latest.fwbundle

Stage: SETUP
        Searching for default sys-config path
        Checking /etc/tenstorrent/config.json: not found
        Checking ~/.config/tenstorrent/config.json: not found

        Could not find config in default search locations, if you need it, either pass it in explicitly or generate one
        Warning: continuing without sys-config, galaxy systems will not be reset
Stage: DETECT
Stage: FLASH
        Sub Stage: VERIFY
                Verifying fw-package can be flashed: complete
                Verifying Blackhole[0] can be flashed
        Stage: FLASH
                Sub Stage FLASH Step 1: Blackhole[0]
                        ROM version is: (18, 10, 0, 0). tt-flash version is: (18, 12, 0, 0)
                        FW bundle version > ROM version. ROM will now be updated.
                Sub Stage FLASH Step 2: Blackhole[0] {p150a}
                        Writing new firmware... (this may take up to 1 minute)
                        Writing new firmware... SUCCESS
                        Verifying flashed firmware... (this may also take up to 1 minute)
                        Firmware verification... SUCCESS
Stage: RESET
 Starting PCI link reset on BH devices at PCI indices: 0 
 Waiting for up to 60 seconds for asic to come back after reset
 Config space reset completed for device 0 
 Finishing PCI link reset on BH devices at PCI indices: 0 
FLASH SUCCESS
```

## Supported products

tt-flash can be used to flash Wormhole and Blackhole products. The last version that supported flashing Grayskull products was [v3.4.7](https://github.com/tenstorrent/tt-flash/releases/tag/v3.4.7).

## License

Apache 2.0 - https://www.apache.org/licenses/LICENSE-2.0.txt
