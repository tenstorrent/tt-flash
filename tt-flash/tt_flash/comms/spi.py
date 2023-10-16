from __future__ import annotations

import math, struct, yaml, tabulate
from typing import Optional

from pyluwen import PciChip as Chip

from tt_flash import utility
from tt_flash.error import TTError

#Default for both RX and TX buffer depth is 8
RX_BUFFER_DEPTH=8
TX_BUFFER_DEPTH=8
PAGE_SIZE=256
SECTOR_SIZE=4*1024

SPI_CNTL_CLK_DISABLE=  (0x1<<8)
SPI_CNTL_SPI_DISABLE=  (0x0<<0)
SPI_CNTL_SPI_ENABLE=   (0x1<<0)

SPI_WR_EN_CMD=         (0x06)
SPI_WR_DS_CMD=         (0x04)
SPI_WR_ER_CMD=         (0x20)
SPI_RD_CMD=            (0x03)
SPI_FAST_RD_CMD=       (0x0B)
SPI_WR_CMD=            (0x02)
SPI_RD_STATUS_CMD=     (0x05)
SPI_RD_STATUS3_CMD=    (0x15)
SPI_WR_STATUS_CMD=     (0x01)
SPI_CHIP_ERASE_CMD=    (0xC7)


def SPI_CTRL0_SPI_SCPH(SCPH):           return ((SCPH << 6) & 0x1)
SPI_CTRL0_TMOD_TRANSMIT_AND_RECEIVE=    (0x0 << 8 )
SPI_CTRL0_TMOD_TRANSMIT_ONLY=           (0x1 << 8 )
SPI_CTRL0_TMOD_EEPROM_READ=             (0x3 << 8 )
SPI_CTRL0_SPI_FRF_STANDARD=             (0x0 << 21)
SPI_CTRL0_DFS32_FRAME_08BITS=           (0x7 << 16)
SPI_CTRL0_DFS32_FRAME_32BITS=           (0x1f << 16)

def SPI_CTRL1_NDF(FRAME_COUNT):         return (((FRAME_COUNT) << 0)&0xffff)

SPI_SSIENR_ENABLE=                      (0x1)
SPI_SSIENR_DISABLE=                     (0x0)
def SPI_SER_SLAVE_ENABLE(SLAVE_ID):     return ((0x1<<(SLAVE_ID)))
def SPI_SER_SLAVE_DISABLE(SLAVE_ID):    return ((0x0<<(SLAVE_ID)))
def SPI_BAUDR_SCKDV(SSI_CLK_DIV):       return (((SSI_CLK_DIV) << 0)&0xffff)

SPI_SR_RFNE=            (0x1 << 3)
SPI_SR_TFE=             (0x1 << 2)
SPI_SR_BUSY=            (0x1 << 0)

def dw_apb_spi_init(chip, clock_div, slave_id):
    reg = chip.AXI.read32("ARC_RESET.GPIO2_PAD_TRIEN_CNTL")
    reg |= 1 << 2 # Enable tristate for SPI data in PAD
    reg &= ~(1 << 5) # Disable tristate for SPI chip select PAD
    reg &= ~(1 << 6) # Disable tristate for SPI clock PAD
    chip.AXI.write32("ARC_RESET.GPIO2_PAD_TRIEN_CNTL", reg)

    chip.AXI.write32("ARC_RESET.GPIO2_PAD_DRV_CNTL", 0xffffffff)

    # Enable RX for all SPI PADS
    reg = chip.AXI.read32("ARC_RESET.GPIO2_PAD_RXEN_CNTL")
    reg |= (0x3f << 1) # PADs 1 to 6 are used for SPI quad SCPH support
    chip.AXI.write32("ARC_RESET.GPIO2_PAD_RXEN_CNTL", reg)
    chip.AXI.write32("ARC_RESET.SPI_CNTL", SPI_CNTL_SPI_ENABLE)

    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_DISABLE)
    chip.AXI.write32("ARC_SPI.SPI_CTRLR0", SPI_CTRL0_TMOD_EEPROM_READ | SPI_CTRL0_SPI_FRF_STANDARD | SPI_CTRL0_DFS32_FRAME_08BITS | SPI_CTRL0_SPI_SCPH(0x1))
    chip.AXI.write32("ARC_SPI.SPI_SER", 0)
    chip.AXI.write32("ARC_SPI.SPI_BAUDR", SPI_BAUDR_SCKDV(clock_div))
    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_ENABLE)

def dw_apb_spi_disable(chip):
    chip.AXI.write32("ARC_RESET.SPI_CNTL", SPI_CNTL_CLK_DISABLE | SPI_CNTL_SPI_DISABLE) # Disable SPI controller PAD override, turn off SPI clock

def dw_apb_spi_read_status(chip, register):
    # Write slave address

    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_DISABLE)
    chip.AXI.write32("ARC_SPI.SPI_CTRLR0", SPI_CTRL0_TMOD_EEPROM_READ | SPI_CTRL0_SPI_FRF_STANDARD | SPI_CTRL0_DFS32_FRAME_08BITS | SPI_CTRL0_SPI_SCPH(0x1))
    chip.AXI.write32("ARC_SPI.SPI_CTRLR1", SPI_CTRL1_NDF(0))
    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_ENABLE)

    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    # Write Status Register to Read
    chip.AXI.write32("ARC_SPI.SPI_DR", register & 0xff)
    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_ENABLE(0))

    # Read Value
    while True:
        if chip.AXI.read32("ARC_SPI.SPI_SR") & SPI_SR_RFNE != 0: # Poll until rx fifo is not empty
            break
    read_buf = chip.AXI.read32("ARC_SPI.SPI_DR") & 0xff

    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))
    return read_buf

def dw_apb_spi_read(chip, addr, frame_count):
    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_DISABLE)
    chip.AXI.write32("ARC_SPI.SPI_CTRLR0", SPI_CTRL0_TMOD_EEPROM_READ | SPI_CTRL0_SPI_FRF_STANDARD | SPI_CTRL0_DFS32_FRAME_08BITS | SPI_CTRL0_SPI_SCPH(0x1))
    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_ENABLE)

    read_buf = []
    while frame_count > 0:
        # Write slave address
        frames = min(RX_BUFFER_DEPTH, frame_count)
        chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_DISABLE)

        # Frames to Read
        chip.AXI.write32("ARC_SPI.SPI_CTRLR1", SPI_CTRL1_NDF(frames - 1))

        chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_ENABLE)
        chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

        # Write Address to Read
        chip.AXI.write32("ARC_SPI.SPI_DR", SPI_RD_CMD & 0xff)
        chip.AXI.write32("ARC_SPI.SPI_DR", (addr >> 16) & 0xff)
        chip.AXI.write32("ARC_SPI.SPI_DR", (addr >> 8) & 0xff)
        chip.AXI.write32("ARC_SPI.SPI_DR", (addr >> 0) & 0xff)

        chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_ENABLE(0))

        #Read Frames
        for n in range(frames):
            while True:
                spi_status = chip.AXI.read32("ARC_SPI.SPI_SR")
                if (0 != (spi_status & SPI_SR_RFNE)): # Poll until rx fifo is not empty
                    break
            read_buf.append(chip.AXI.read32("ARC_SPI.SPI_DR") & 0xff)

        frame_count -= frames
        addr += frames
        chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    return read_buf

def dw_apb_spi_write(chip, addr, val):

    # Write slave address
    spi_status=0
    n=0
    frame_count=len(val)

    while frame_count > 0:

        #Buffer_Depth - x for x frames needed to send command and address
        #Prevent writing across pages in a single command
        frames=min(TX_BUFFER_DEPTH - 4, frame_count, PAGE_SIZE - (addr%PAGE_SIZE))
        chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_DISABLE)
        chip.AXI.write32("ARC_SPI.SPI_CTRLR0", SPI_CTRL0_TMOD_TRANSMIT_ONLY | SPI_CTRL0_SPI_FRF_STANDARD | SPI_CTRL0_DFS32_FRAME_08BITS | SPI_CTRL0_SPI_SCPH(0x1))
        chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_ENABLE)
        chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

        #Write Enable
        chip.AXI.write32("ARC_SPI.SPI_DR", SPI_WR_EN_CMD&0xff)
        chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_ENABLE(0))

        # Add some delay to make sure enable above propogates
        while True:
            spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
            if (SPI_SR_TFE == (spi_status&SPI_SR_TFE)):
                break
        while True:
            spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
            if (SPI_SR_BUSY != (spi_status&SPI_SR_BUSY)):
                break
        chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

        #Write Address and Values
        chip.AXI.write32("ARC_SPI.SPI_DR", SPI_WR_CMD&0xff)
        chip.AXI.write32("ARC_SPI.SPI_DR", (addr>>16)&0xff)
        chip.AXI.write32("ARC_SPI.SPI_DR", (addr>>8)&0xff)
        chip.AXI.write32("ARC_SPI.SPI_DR", (addr>>0)&0xff)
        for j in range(frames):
            chip.AXI.write32("ARC_SPI.SPI_DR", val[n]&0xff)
            n+=1

        chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_ENABLE(0))

        # Add some delay to make sure enable above propogates
        while True:
            spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
            if (SPI_SR_TFE == (spi_status&SPI_SR_TFE)):
                break

        while True:
            spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
            if (SPI_SR_BUSY != (spi_status&SPI_SR_BUSY)):
                break

        chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))
        addr+=frames
        frame_count-=frames

        #Wait for write to complete
        while True:
            busy=dw_apb_spi_read_status(chip, SPI_RD_STATUS_CMD) & 0x1
            if busy !=0x1:
                break

def dw_apb_spi_erase(chip, addr):

    # Write slave address
    spi_status=0
    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_DISABLE)
    chip.AXI.write32("ARC_SPI.SPI_CTRLR0", SPI_CTRL0_TMOD_TRANSMIT_ONLY | SPI_CTRL0_SPI_FRF_STANDARD | SPI_CTRL0_DFS32_FRAME_08BITS | SPI_CTRL0_SPI_SCPH(0x1))
    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_ENABLE)
    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    #Write Enable
    chip.AXI.write32("ARC_SPI.SPI_DR", SPI_WR_EN_CMD&0xff)
    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_ENABLE(0))

    # Add some delay to make sure enable above propogates
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_TFE == (spi_status&SPI_SR_TFE)):
            break
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_BUSY != (spi_status&SPI_SR_BUSY)):
            break

    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    #Write Sector to Erase
    chip.AXI.write32("ARC_SPI.SPI_DR", SPI_WR_ER_CMD&0xff)
    chip.AXI.write32("ARC_SPI.SPI_DR", (addr>>16)&0xff)
    chip.AXI.write32("ARC_SPI.SPI_DR", (addr>>8)&0xff)
    chip.AXI.write32("ARC_SPI.SPI_DR", (addr>>0)&0xff)
    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_ENABLE(0))

    # Add some delay to make sure enable above propogates
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_TFE == (spi_status&SPI_SR_TFE)):
            break
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_BUSY != (spi_status&SPI_SR_BUSY)):
            break

    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    #Wait for erase to complete
    while True:
        busy=dw_apb_spi_read_status(chip, SPI_RD_STATUS_CMD) & 0x1
        if busy !=0x1:
            break

def dw_apb_spi_full_erase(chip):

    # Write slave address
    spi_status=0
    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_DISABLE)
    chip.AXI.write32("ARC_SPI.SPI_CTRLR0", SPI_CTRL0_TMOD_TRANSMIT_ONLY | SPI_CTRL0_SPI_FRF_STANDARD | SPI_CTRL0_DFS32_FRAME_08BITS | SPI_CTRL0_SPI_SCPH(0x1))
    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_ENABLE)
    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    #Write Enable
    chip.AXI.write32("ARC_SPI.SPI_DR", SPI_WR_EN_CMD&0xff)
    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_ENABLE(0))

    # Add some delay to make sure enable above propogates
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_TFE == (spi_status&SPI_SR_TFE)):
            break
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_BUSY != (spi_status&SPI_SR_BUSY)):
            break

    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    #Write Sector to Erase
    chip.AXI.write32("ARC_SPI.SPI_DR", SPI_CHIP_ERASE_CMD &0xff)
    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_ENABLE(0))

    # Add some delay to make sure enable above propogates
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_TFE == (spi_status&SPI_SR_TFE)):
            break
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_BUSY != (spi_status&SPI_SR_BUSY)):
            break

    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    #Wait for erase to complete
    while True:
        busy=dw_apb_spi_read_status(chip, SPI_RD_STATUS_CMD) & 0x1
        if busy !=0x1:
            break

def dw_apb_spi_smart_write(chip, addr, value: bytes):
    val = list(value)

    # val.reverse()
    read=dw_apb_spi_read(chip, addr, len(val))

    # Write if already erased
    if read.count(0xff) == len(read):
        dw_apb_spi_write(chip, addr, val)

    # Copy and rewrite sector if different value already written
    elif read != val:
        sector_start = addr // SECTOR_SIZE
        num_sectors = (addr + len(val) - 1) // SECTOR_SIZE - sector_start + 1

        #Copy sector(s)
        data=dw_apb_spi_read(chip, sector_start * SECTOR_SIZE, num_sectors * SECTOR_SIZE)

        #Replace values
        data[addr % SECTOR_SIZE : addr % SECTOR_SIZE + len(val)] = val

        #Erase sector(s)
        for i in range(num_sectors):
            dw_apb_spi_erase(chip, (sector_start + i) * SECTOR_SIZE)

        #Write sector(s)
        dw_apb_spi_write(chip, sector_start*SECTOR_SIZE, data)

def dw_apb_spi_lock(chip, block_protect):
    # Write slave address
    spi_status=0
    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_DISABLE)
    chip.AXI.write32("ARC_SPI.SPI_CTRLR0", SPI_CTRL0_TMOD_TRANSMIT_ONLY | SPI_CTRL0_SPI_FRF_STANDARD | SPI_CTRL0_DFS32_FRAME_08BITS | SPI_CTRL0_SPI_SCPH(0x1))
    chip.AXI.write32("ARC_SPI.SPI_SSIENR", SPI_SSIENR_ENABLE)
    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    #Write Enable
    chip.AXI.write32("ARC_SPI.SPI_DR", SPI_WR_EN_CMD&0xff)
    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_ENABLE(0))

    # Add some delay to make sure enable above propogates
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_TFE == (spi_status&SPI_SR_TFE)):
            break
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_BUSY != (spi_status&SPI_SR_BUSY)):
            break

    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    # Write Sectors to Lock
    chip.AXI.write32("ARC_SPI.SPI_DR", SPI_WR_STATUS_CMD&0xff)
    if block_protect < 5:
        chip.AXI.write32("ARC_SPI.SPI_DR", (0x3 << 5) | ((block_protect << 2)))
    else:
        chip.AXI.write32("ARC_SPI.SPI_DR", (0x1 << 5) | (((block_protect-5) << 2)))
    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_ENABLE(0))

    # Add some delay to make sure enable above propogates
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_TFE == (spi_status&SPI_SR_TFE)):
            break
    while True:
        spi_status=chip.AXI.read32("ARC_SPI.SPI_SR")
        if (SPI_SR_BUSY != (spi_status&SPI_SR_BUSY)):
            break

    chip.AXI.write32("ARC_SPI.SPI_SER", SPI_SER_SLAVE_DISABLE(0))

    #Wait for lock operation to complete
    while True:
        busy=dw_apb_spi_read_status(chip, SPI_RD_STATUS_CMD) & 0x1
        if busy !=0x1:
            break

# Parsing certain registers into readable format
def parse_reg(register, data):
    if all(value == "f" for value in data):
        lprint(f"{register}: Reading all f's - \33[31mSPI ROM for {register} may not have been programmed!\33[0m")
        return

    output = ""
    if register == "BOARD_INFO" or register == "VRM_CHECKSUM":
        output = data

    elif register == "ASIC_ID":
        asic_id = ""
        # Pairs of 2 characters, skipping the '0x' at start
        for byte in (data[i:i+2] for i in range(0, len(data), 2)):
            if byte != "ff" and byte != "00": asic_id += byte
        output = bytes.fromhex(asic_id).decode('utf-8')

    elif register == "DATE_PROGRAMMED":
        output = data[0:4]+"-"+data[4:6]+"-"+data[6:8]+" (YYYY-MM-DD)"

    elif register == "VF_SLOPE" or register == "VF_OFFSET": # Requires IEEE floating point conversion
        reg_val = int(data, 16)
        sign = reg_val >> 31
        exp = ((reg_val >> 23) & 0xff) - 127 - 23 # subtract extra 23 as we will left shift mantissa by 23 bits to avoid computing decimals
        mantissa = 0x800000 | reg_val & 0x7fffff # mantissa is generally 1.mantissa, but here we essentually left shift it 23 bits

        output = (-1)**sign * 2**exp * mantissa

    elif register == "SPARE": # Print out a warning message as well - this shouldn't be programmed
        output = "Not all f's"
        utility.WARN(f"SPARE regs should be just padding - please verify what's in here!")

    elif "FW" in register or register == "TELEMETRY" or register == "MAPPING_TABLE": # Just quickly tell if it's programmed or not
        output = "Programmed - version " + data[0:16]

    else:
        output = int(data, 16)

    utility.PRINT_GREEN(f"{register}: {output}")

def lprint(val):
    print(val)

from dataclasses import dataclass

@dataclass
class Spi:
    chip: Chip
    mapping: dict[str, int]
    initialized: bool = False

    def prepare(self):
        chip = self.chip

        #Set Clock Rate to ~20MHz
        refclk = 27.0

        def arc_pll_1_feedback_divider():
            return (chip.axi_read32(0x1FF20018 + 0x4) & (0xFFFF << 16)) >> 16

        def arc_pll_1_referece_divider():
            return chip.axi_read32(0x1FF20018 + 0x4) & 0xFF

        def arc_pll_1_post_divider():
            return chip.axi_read32(0x1FF20018 + 0x14) & 0xFF

        pll1_fb_div = arc_pll_1_feedback_divider() + 1
        pll1_ref_div = arc_pll_1_referece_divider() >> 2
        if pll1_ref_div == 0: pll1_ref_div = 1
        pll1_vco_f = (refclk / pll1_ref_div) * pll1_fb_div
        def postdiv_val (pd):
            pd = pd + 1
            if pd > 16: pd = pd * 2
            return pd

        ARCCLK = pll1_vco_f / postdiv_val(arc_pll_1_post_divider())
        clock_div = math.ceil(ARCCLK / 20)
        clock_div += clock_div % 2

        dw_apb_spi_init(chip, clock_div, 0)

    def init(self, for_write: Optional[int] = None):
        if not self.initialized:
            self.prepare()
            self.initialized = True

        if for_write is not None:
            status = dw_apb_spi_read_status(self.chip, SPI_RD_STATUS_CMD)
            locked_sectors = ((status & 0x1c) >> 2) + (0 if status & 0x40 else 5)

            # check if sector to write to locked
            if locked_sectors != 0 and for_write//SECTOR_SIZE < pow(2, locked_sectors - 1):
                self.deinit()
                raise TTError(f"lock_spi={locked_sectors}, First {pow(2, locked_sectors - 1)} Sectors of SPI is Locked. Did not write {val} to {write_mapping}")

    def deinit(self):
        if self.initialized:
            dw_apb_spi_disable(self.chip)
            self.initialized = False

    def __enter__(self):
        self.init()

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.deinit()

    def read(self, addr: int, bytes: int, deinit: bool = False):
        self.init()

        output = dw_apb_spi_read(self.chip, addr, bytes)

        if deinit:
            self.deinit()

        return output

    def read_mapping(self, addr):
        addr = self.mapping[addr]['Address']
        b = self.mapping[addr]['ArraySize']

        return self.read(addr, b, deinit=True)

    def write(self, addr: int, value: bytes, deinit: bool = False):
        self.init(for_write=addr)

        dw_apb_spi_smart_write(self.chip, addr, value)

        if deinit:
            self.deinit()

    def write_mapping(self, addr, value):
        int_addr = self.mapping[addr]['Address']
        b = self.mapping[addr]['ArraySize']

        return self.write(int_addr, value.to_bytes(b, byteorder='little'), deinit=True)

    def write_int(self, addr: int, value: int, byte_len: int):
        self.write(addr, value.to_bytes(byte_len, byteorder='little'), deinit=True)

    def write_from_bin_bytes(self, addr: int, file: bytes):
        self.write(addr, file, deinit=True)

    def write_from_bin(self, addr: int, file: str):
        with open(file, "rb") as fp:
            return self.write_from_bin_bytes(addr, fp.read())

    def write_from_hex_bytes(self, addr: int, file: bytes):
        val = []
        fp = file.decode('utf-8').splitlines()
        for line in fp:
            val.extend(list(int(line, 16).to_bytes(4, byteorder='big')))

        self.write(addr, bytes(val), deinit=True)

    def write_from_hex(self, addr: int, file: str):
        with open(file, "rb") as fp:
            return self.write_from_hex_bytes(addr, fp.read())

    def lock_spi(self, block_protect: int):
        if block_protect == 5:
            lprint(f"lock_spi={block_protect} was specified but cannot lock exactly 64KB")
            return
        elif block_protect > 12:
            lprint(f"Specifying region larger than SPI, will lock all 8MB of SPI")
            block_protect = 12
        dw_apb_spi_lock(self.chip, block_protect)

    def status(self):
        self.init()
        status = dw_apb_spi_read_status(self.chip, SPI_RD_STATUS_CMD)
        self.deinit()

        locked_sectors = ((status & 0x1c) >> 2) + (0 if status & 0x40 else 5)
        if locked_sectors == 0:
            utility.PRINT_GREEN(f"lock_spi={locked_sectors}, SPI is Unlocked")
        else:
            utility.WARN(f"lock_spi={locked_sectors}, First {pow(2, locked_sectors - 1)} Sectors of SPI is Locked")

    def check_spi(self, verbosity: int):
        mapping = self.mapping

        debug_dump = verbosity == 3

        self.init()
        status = dw_apb_spi_read_status(self.chip, SPI_RD_STATUS_CMD)
        self.deinit()
        locked_sectors = ((status & 0x1c) >> 2) + (0 if status & 0x40 else 5)
        if locked_sectors == 0:
            utility.PRINT_GREEN(f"lock_spi={locked_sectors}, SPI is Unlocked")
        else:
            utility.WARN(f"lock_spi={locked_sectors}, First {pow(2, locked_sectors - 1)} Sectors of SPI is Locked")

        # Remove 3 entries that are not registers
        del mapping["Regsize"]
        del mapping["Magic_Number_1"]
        del mapping["Magic_Number_2"]
        # Remove HEADER (it's just the amalgamation of everything)
        del mapping["HEADER"]

        if verbosity == 1:
            check_mapping = [ "ARC_L1_FW", "ARC_L2_FW", "ARC_WDG_FW", "DATE_PROGRAMMED", "MAPPING_VERSION", "REPROGRAMMED_COUNT", "BOARD_INFO", "ASIC_ID", "DUAL_RANK", "THM_LIMIT" ]
        elif verbosity == 2 or verbosity == 3:
            check_mapping = [ item for item in mapping ]

        for read_mapping in check_mapping:
            try:
                addr = mapping[read_mapping]['Address']
                b = mapping[read_mapping]['ArraySize']
            except Exception:
                addr = int(read_mapping, 0)
                b = 4

            read = self.read(addr, b, deinit=True)

            if debug_dump:
                addrs = [f'0x{val:06x}' for val in range(addr, addr + b, 4)]
                vals = [f"0x{int.from_bytes(bytes(read[i:min(i + 4, b)]), byteorder='little'):0{min(4, b - i) * 2}x}" for i in range(0, b, 4)]

                lprint(f"\nRead from {read_mapping}: ")
                lprint(tabulate.tabulate(zip(addrs, vals), headers=["Address", "Value"]))

                if all((value=="0xffffffff" or value=="0xffff") for value in vals):
                    utility.PRINT_RED(f"SPI ROM for {read_mapping} may not have been programmed!")
            else:
                vals = f"{int.from_bytes(bytes(read), byteorder='little'):0{b * 2}x}"
                parse_reg(read_mapping,vals)
