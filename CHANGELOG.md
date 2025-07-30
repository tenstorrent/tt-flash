# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 3.4.0 - 30/07/25

- Bump pyyaml 6.0.1 -> 6.0.2
- Improve error message formatting
- No longer have to use --force for flashing BH cards

## 3.3.5 - 03/07/25

- Bump luwen 0.7.3 -> 0.7.5

## 3.3.4 - 02/07/25

- Bump tt-tools-common 1.4.16 -> 1.4.17
- Bump luwen 0.6.4 -> 0.7.3

## 3.3.3 - 05/06/2025

- Bumped tt-tools-common version to fix driver version check for compatability with tt-kmd 2.0.0

## 3.3.2 - 14/05/2025

- Bump tt-tools-common version to latest

## 3.2.0 - 12/03/2025

### Updated

- luwen version bump to bring inline with tt-smi; provides stability fixes

## 3.1.3 - 06/03/2025

### Added

- luwen version bump to include bh arc init checks

## 3.1.2 - 28/02/2025

### Added

- Support for more BH cards: p100a, p150, and p150c

## 3.1.1 - 06/01/2025

### Updated

- Bumped luwen version to accomodate Maturin updates

## 3.1.0 - 29/10/2024

### Added

- Support for flashing the BH tt-boot-fs file format
- Bumped luwen version to 0.4.6 to allow resets when chip is inaccessible

## 3.0.2 - 17/10/2024

### Fixed
- Unbound variable when exception is thrown when getting current fw-version

## 3.0.1 - 16/10/2024

### Changed
- Bumped luwen version to 0.4.5 to resolve false positives on bad chip detection

## 3.0.0 - 23/08/2024

- NO BREAKING CHANGES! Major version bump to signify new generation of product.
- Added support for p100

## 2.2.0 - 19/07/2024

### Updated
- Added support for an alternative spi flash configuration via a new version of luwen

## 2.0.8 - 14/05/2024

### Updated
- Bumped luwen (0.3.8) and tt_tools_common (1.4.3) lib versions

## 2.0.1 - 2.0.7
- Dependency updates

## 2.0.0
- WH flash release

## 1.0.0

- GS flash release
