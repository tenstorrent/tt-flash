# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Dict, Union


class ExtractedFwData:
    pass


class SpiTable:
    pass


class Checksum:
    pass


class FwVersion:
    pass


ExtractedFwData = Dict[
    str, Union[Dict[str, FwVersion], Dict[str, SpiTable], Dict[str, Checksum]]
]


def extract_fw_versions(
    device: Device, image: bytes, mapping: dict
) -> tuple[bool, ExtractedFwData]:
    fw_matching = True

    curr_addr = 0
    image_data = {}
    for line in image.decode("utf-8").splitlines():
        line = line.strip()
        if line.startswith("@"):
            curr_addr = int(line.lstrip("@").strip())
        else:
            data = b16decode(line)

            curr_stop = curr_addr + len(data)

            if not isinstance(data, bytes):
                data = bytes(data)

            in_spi = device.spi_read(curr_addr, len(data))

            if data != in_spi:
                fw_matching = False

            image_data[(curr_addr, curr_addr + len(data))] = data

            curr_addr = curr_stop

    for top_level, value in mapping.items():
        for param, param_value in value.items():
            spi_data = chip.spi_read(
                param_value["start"], param_value["end"] - param_value["start"]
            )

            file_data = bytearray()
            for (start_addr, end_addr), data in image_data.items():
                start_inside = (
                    param_value["start"] >= start_addr
                    and param_value["start"] < end_addr
                )
                end_inside = (
                    param_value["end"] > start_addr and param_value["end"] <= end_addr
                )
                if param_value["start"] < start_addr:
                    continue
                elif start_inside or end_inside:
                    start_addr = max(param_value["start"], start_addr)
                    start_addr = min(end_addr, start_addr)

                    end_addr = max(param_value["end"], start_addr)
                    end_addr = min(end_addr, start_addr)

                    file_data += data[
                        param_value["start"]
                        - start_addr : param_value["end"]
                        - end_addr
                    ]


def extract_fw_versions(
    device: Device, image: dict, mapping: dict
) -> tuple[bool, ExtractedFwData]:
    output: ExtractedFwData = {}
    matching = True
    for name, info in FIRMWARES.items():
        versions = []
        versions.append(name)
        output[name] = {}

        if info.file is not None:
            data = read_file_to_bytes(device.chip.chip_name, info.file)
        else:
            data = None

        fw_data: dict[str, FwVersion] = {}

        # Right now the galaxy module doesn't have any way of getting the M3 fw versions.
        # So we are just skipping them until that changes.

        found_versions: List[Optional[FwVersion]] = []
        if info.mem_version is not None:
            if device.board_type not in [
                BoardType.GALAXY,
                BoardType.NEBULA_CB,
            ] or name not in ["M3_APP_FW", "M3_BL_FW"]:
                version = info.mem_version(device.chip)
                found_versions.append(version)
                fw_data["running"] = version

        if info.spi_version is not None:
            if device.board_type not in [
                BoardType.GALAXY,
                BoardType.NEBULA_CB,
            ] or name not in ["M3_APP_FW", "M3_BL_FW"]:
                version = info.spi_version(device.chip, data)
                found_versions.append(version)
                fw_data["spi"] = version

        have_flash = None
        if info.flash_version is not None and data is not None:
            if device.board_type not in [
                BoardType.GALAXY,
                BoardType.NEBULA_CB,
            ] or name not in ["M3_APP_FW", "M3_BL_FW"]:
                have_flash = info.flash_version(data)
                found_versions.append(have_flash)
                fw_data["flash"] = have_flash

        output[name] = fw_data

        if have_flash is not None:
            transform_fw_version(have_flash, found_versions, False)
            if any(
                v.matching != MatchDegree.FULL_MATCH
                for v in found_versions
                if v is not None and v.matching is not None
            ):
                matching = False

        def extract_params(info: ParamConfig) -> tuple[bool, dict[str, Checksum]]:
            param_output = {}

            row_matching = True
            if info.file is not None:
                data = read_file_to_bytes(device.chip.chip_name, info.file)

                if info.mem_version is not None:
                    checksum = info.mem_version(device.chip, len(data))
                    if checksum is not None:
                        param_output["running"] = Checksum(checksum)

                if info.spi_version is not None:
                    checksum = info.spi_version(device.chip, len(data))
                    if checksum is not None:
                        param_output["spi"] = Checksum(checksum)

                if info.flash_version is not None:
                    checksum = info.flash_version(data)
                    if checksum is not None:
                        param_output["flash"] = Checksum(checksum)

                if "flash" in param_output:
                    for k, v in param_output.items():
                        if k == "flash":
                            continue
                        if v != param_output["flash"]:
                            row_matching = False
                            break

            return (row_matching, param_output)

        if device.board_type == BoardType.GALAXY:
            param_table_matching, params = extract_params(ETH_PARAM_TABLES["GALAXY"])
            if not param_table_matching:
                matching = False
            output["GALAXY_ETH_PARAM_TABLE"] = params
        elif device.board_type == BoardType.NEBULA_CB:
            param_table_matching, params = extract_params(ETH_PARAM_TABLES["NEBULA_CB"])
            if not param_table_matching:
                matching = False
            output["NEBULA_CB_ETH_PARAM_TABLE"] = params
        elif device.board_type == BoardType.NEBULA_X1:
            param_table_matching, params = extract_params(ETH_PARAM_TABLES["NEBULA_X1"])
            if not param_table_matching:
                matching = False
            output["NEBULA_X1_ETH_PARAM_TABLE"] = params
        elif device.board_type == BoardType.NEBULA_X2:
            param_table_matching, params = extract_params(
                ETH_PARAM_TABLES["NEBULA_X2_LEFT"]
            )
            if not param_table_matching:
                matching = False
            output["NEBULA_X2_LEFT_ETH_PARAM_TABLE"] = params
            param_table_matching, params = extract_params(
                ETH_PARAM_TABLES["NEBULA_X2_RIGHT"]
            )
            if not param_table_matching:
                matching = False
            output["NEBULA_X2_RIGHT_ETH_PARAM_TABLE"] = params
        else:
            raise NotImplementedError(f"Unknown board type {device.board_type}")

        def extract_spi_table(
            info: SPIConfig, prefix: Path
        ) -> tuple[bool, dict[str, SpiTable]]:
            spi_output: dict[str, SpiTable] = {}

            row_matching = True
            if info._file is not None:
                data = read_data_file_to_bytes(
                    device.chip.chip_name, str(info.get_file(prefix))
                )

                bin_file = yaml_load(
                    f"{utility.package_root_path()}/data/{device.chip.chip_name}/{info.bin_file}"
                )["HEADER"]["data"]
                mask_file = yaml_load(
                    f"{utility.package_root_path()}/data/{device.chip.chip_name}/{info.mask_file}"
                )["HEADER"]

                def populate_table_output(
                    spi_output: dict[str, SpiTable],
                    table: Optional[SpiTableInfo],
                    table_name: str,
                ):
                    if table is not None:
                        matched_table = {}
                        for name, entry in table.entries.items():
                            (value, expected_width) = bytes_to_type(
                                entry.value, entry.type, entry.size
                            )
                            matched_table[name] = SpiEntry(
                                value=value,
                                width=expected_width,
                                bytes=entry.value,
                                matching=None,
                                default=False,
                                skip=False,
                                view=entry.view,
                                check=entry.check,
                            )
                        spi_output[table_name] = SpiTable(
                            checksum=table.checksum, entries=matched_table
                        )

                if info.mem_table is not None:
                    table = info.mem_table(bin_file, mask_file, device.chip, len(data))
                    populate_table_output(spi_output, table, "running")

                if info.spi_table is not None:
                    table = info.spi_table(bin_file, mask_file, device.chip, len(data))
                    populate_table_output(spi_output, table, "spi")

                if info.flash_table is not None:
                    table = info.flash_table(bin_file, mask_file, data)
                    populate_table_output(spi_output, table, "flash")

                if "flash" in spi_output:
                    flash_output = spi_output["flash"]
                    for k, v in spi_output.items():
                        if k == "flash":
                            continue
                        elif k == "spi":
                            for table_name, table_value in v.entries.items():
                                flash_value = flash_output.entries[table_name]
                                if table_value.bytes != flash_value.bytes:
                                    table_value.matching = SpiMatch.NO_MATCH
                                    row_matching = False
                                else:
                                    table_value.matching = SpiMatch.MATCH
                        elif k == "running":
                            for table_name, table_value in v.entries.items():
                                flash_value = flash_output.entries[table_name]
                                if table_value.check == CheckType.EQ:
                                    check_passed = (
                                        table_value.value == flash_value.value
                                    )
                                elif table_value.check == CheckType.LE:
                                    check_passed = (
                                        flash_value.value <= table_value.value
                                    )

                                if not check_passed:
                                    default_flash = all(
                                        b == 0xFF for b in flash_value.bytes
                                    )
                                    if "spi" in spi_output:
                                        spi_value = spi_output["spi"].entries[
                                            table_name
                                        ]
                                        default_spi = all(
                                            b == 0xFF for b in spi_value.bytes
                                        )
                                    else:
                                        default_spi = True

                                    if default_spi and default_flash:
                                        flash_value.default = True
                                        if "spi" in spi_output:
                                            spi_value = spi_output["spi"].entries[
                                                table_name
                                            ]
                                            spi_value.default = True
                                        table_value.matching = None
                                    else:
                                        table_value.matching = SpiMatch.NO_MATCH
                                        row_matching = False
                                else:
                                    default_flash = all(
                                        b == 0xFF for b in flash_value.bytes
                                    )
                                    if default_flash:
                                        table_value.default = True
                                    if table_value.value != flash_value.value:
                                        table_value.matching = SpiMatch.VALID_NON_MATCH
                                    else:
                                        table_value.matching = SpiMatch.MATCH
                        else:
                            raise Exception(f"Found unexpected SPI Table Type {k}")

            return (row_matching, spi_output)

        if device.board_type == BoardType.GALAXY:
            param_table_matching, params = extract_spi_table(
                SPI_PARAM_TABLE["GALAXY"], spi_table_prefix
            )
            if not param_table_matching:
                matching = False
            output["GALAXY_SPI_TABLE"] = params
        elif device.board_type == BoardType.NEBULA_CB:
            param_table_matching, params = extract_spi_table(
                SPI_PARAM_TABLE["NEBULA_CB"], spi_table_prefix
            )
            if not param_table_matching:
                matching = False
            output["NEBULA_CB_SPI_TABLE"] = params
        elif device.board_type == BoardType.NEBULA_X1:
            param_table_matching, params = extract_spi_table(
                SPI_PARAM_TABLE["NEBULA_X1"], spi_table_prefix
            )
            if not param_table_matching:
                matching = False
            output["NEBULA_X1_SPI_TABLE"] = params
        elif device.board_type == BoardType.NEBULA_X2:
            param_table_matching, params = extract_spi_table(
                SPI_PARAM_TABLE["NEBULA_X2"], spi_table_prefix
            )
            if not param_table_matching:
                matching = False
            if device.is_remote:
                output["NEBULA_X2_RIGHT_SPI_TABLE"] = params
            else:
                output["NEBULA_X2_LEFT_SPI_TABLE"] = params
        else:
            raise NotImplementedError(f"Unknown board type {device.board_type}")

    return matching, output


def fw_version_to_table(data: ExtractedFwData, verbose: bool) -> Table:
    table = Table()

    table.add_column("FW Name")
    table.add_column("Running")
    table.add_column("SPI")
    table.add_column("To Flash")

    table_keys = ["running", "spi", "flash"]

    for name, info in sorted(data.items()):
        for_vis = []
        for key in table_keys:
            if key in info:
                for_vis.append(info[key])
            else:
                for_vis.append(None)

        if len(for_vis) > 0:
            if any(isinstance(v, FwVersion) for v in for_vis):
                for_vis = cast(List[Optional[FwVersion]], for_vis)
                for_vis = transform_fw_version(for_vis[-1], for_vis, not verbose)
                for_vis = [str(v) if v is not None else None for v in for_vis]
            elif any(isinstance(v, SpiTable) for v in for_vis):
                for_vis = cast(List[Optional[SpiTable]], for_vis)

                if verbose:
                    output = []
                    for vis in for_vis:
                        if vis is not None:
                            inner_table = Table()
                            inner_table.add_column("Name")
                            inner_table.add_column("Value")

                            for n, v in vis.entries.items():
                                if v.skip:
                                    continue

                                pretty_values = []
                                if all(b == 0xFF for b in v.bytes):
                                    pretty_values.append("<NOT SET>")
                                else:
                                    for value in v.value:
                                        if isinstance(value, int):
                                            if len(v.value) == 1:
                                                if v.view == ViewType.DEC:
                                                    pretty_values.append(str(value))
                                                elif v.view == ViewType.HEX:
                                                    pretty_values.append(
                                                        f"{value:0{v.width * 2}X}"
                                                    )
                                                else:
                                                    raise NotImplementedError(
                                                        f"Unknown view type {v.view}"
                                                    )
                                            else:
                                                pretty_values.append(
                                                    f"{value:0{v.width * 2}X}"
                                                )
                                        else:
                                            pretty_values.append(str(value))
                                value = " ".join(pretty_values)
                                if v.matching is None:
                                    inner_table.add_row(n, str(value))
                                elif v.matching == SpiMatch.MATCH:
                                    inner_table.add_row(n, f"[green]{value}")
                                elif v.matching == SpiMatch.NO_MATCH:
                                    inner_table.add_row(n, f"[red]{value}")
                                elif v.matching == SpiMatch.VALID_NON_MATCH:
                                    inner_table.add_row(n, f"[orange]{value}")
                                else:
                                    raise NotImplementedError(
                                        f"No handler for {type(v.matching)}"
                                    )

                        else:
                            inner_table = None
                        output.append(inner_table)
                    for_vis = output
                else:
                    output = []
                    for vis in for_vis:
                        if vis is not None:
                            all_match = True
                            any_match = False
                            error = []
                            warn = []
                            for n, v in vis.entries.items():
                                if v.matching is None:
                                    continue
                                any_match = True
                                if v.matching == SpiMatch.NO_MATCH:
                                    all_match = False
                                    error.append(n.replace("_", " "))
                                elif v.matching == SpiMatch.VALID_NON_MATCH:
                                    warn.append(n.replace("_", " "))
                            if any_match:
                                if all_match:
                                    if len(warn) > 0:
                                        if len(warn) == 1:
                                            inner_table = f"[green]All valid (no problems), but an expected mismatch was found for {', '.join(warn)}"
                                        else:
                                            inner_table = f"[green]All valid (no problems), but expected mismatches were found for {', '.join(warn)}"
                                    else:
                                        inner_table = "[green]All matching"
                                else:
                                    inner_table = (
                                        f"[red]Mismatch found for {', '.join(error)}"
                                    )
                            else:
                                inner_table = "All matching"
                        else:
                            inner_table = None
                        output.append(inner_table)
                    for_vis = output

            elif any(isinstance(v, Checksum) for v in for_vis):
                for_vis = cast(List[Optional[str]], for_vis)
                for_vis = transform_checksum(for_vis[-1], for_vis)
                for_vis = [str(v) if v is not None else None for v in for_vis]

        table.add_row(name, *["-" if i is None else i for i in for_vis])

    return table


def fw_version_to_tables(
    devices: list[Device], spi_prefix: Path, verbose: bool
) -> list[FwVersionTable]:
    tables: list[FwVersionTable] = []
    for i, dev in tqdm(
        enumerate(devices), total=len(devices), desc="Reading FW Versions"
    ):
        matching, data = extract_fw_versions(dev, spi_prefix)

        table = fw_version_to_table(data, verbose)
        table.title = f"FW Versions for device {i}"
        tables.append(FwVersionTable(table=table, matching=matching))

    return tables
