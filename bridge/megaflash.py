"""Flash the Arduino Mega 2560 over USB, without avrdude.

The Mega's bootloader speaks STK500v2: reset it (toggle DTR), sign on, then
load-address / program-page commands, and read everything back to verify.
Only the program memory is written: the EEPROM (the learned setup and the
active profile) stays as it is.

    flash(port_path, hex_path, progress=lambda pct: ...)
"""
import os
import struct
import sys
import time

try:
    import serial  # pyserial (Windows needs it; Mac/Linux work without)
except ImportError:
    serial = None

BAUD = 115200
PAGE = 256          # ATmega2560 flash page
FLASH = 256 * 1024
MESSAGE_START, TOKEN = 0x1B, 0x0E
CMD_SIGN_ON, CMD_LOAD_ADDRESS = 0x01, 0x06
CMD_ENTER_PROGMODE, CMD_LEAVE_PROGMODE = 0x10, 0x11
CMD_PROGRAM_FLASH, CMD_READ_FLASH = 0x13, 0x14
STATUS_OK = 0x00


class FlashError(Exception):
    pass


def read_hex(path):
    """Intel HEX -> (start address, bytes). Gaps are 0xFF."""
    mem = {}
    base = 0
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if not line.startswith(":"):
                raise FlashError(f"{os.path.basename(path)} line {n}: not an Intel HEX file")
            raw = bytes.fromhex(line[1:])
            count, addr, kind = raw[0], struct.unpack(">H", raw[1:3])[0], raw[3]
            data = raw[4:4 + count]
            if (sum(raw) & 0xFF) != 0 or len(data) != count:
                raise FlashError(f"{os.path.basename(path)} line {n}: damaged (checksum)")
            if kind == 0:
                for k, b in enumerate(data):
                    mem[base + addr + k] = b
            elif kind == 1:
                break
            elif kind == 2:
                base = struct.unpack(">H", data)[0] << 4
            elif kind == 4:
                base = struct.unpack(">H", data)[0] << 16
    if not mem:
        raise FlashError(f"{os.path.basename(path)} has no data")
    lo, hi = min(mem), max(mem) + 1
    if hi > FLASH - 8 * 1024:  # the top 8 KB is the bootloader
        raise FlashError("That firmware is too big for the Mega (it would overwrite the bootloader)")
    lo -= lo % PAGE
    img = bytearray(b"\xff" * (hi - lo))
    for a, b in mem.items():
        img[a - lo] = b
    return lo, bytes(img)


class Link:
    """The serial port at the bootloader's speed, with DTR control."""

    def __init__(self, path):
        self.path = path
        if serial:
            self.s = serial.Serial(path, BAUD, timeout=0.2)
        else:
            import termios
            self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
            a = termios.tcgetattr(self.fd)
            a[0] = a[1] = a[3] = 0
            a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            a[4] = a[5] = termios.B115200
            a[6][termios.VMIN] = 0
            a[6][termios.VTIME] = 2
            termios.tcsetattr(self.fd, termios.TCSANOW, a)

    def dtr(self, on):
        try:
            if serial:
                self.s.dtr = on
                self.s.rts = on
            else:
                import fcntl
                import termios
                bits = struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS)
                fcntl.ioctl(self.fd, termios.TIOCMBIS if on else termios.TIOCMBIC, bits)
        except (OSError, ValueError):
            pass  # not a real modem line (tests)

    def write(self, data):
        if serial:
            self.s.write(data)
        else:
            os.write(self.fd, data)

    def read(self, n, timeout):
        end, out = time.time() + timeout, b""
        while len(out) < n and time.time() < end:
            chunk = self.s.read(n - len(out)) if serial else os.read(self.fd, n - len(out))
            out += chunk
        return out

    def flush_input(self):
        if serial:
            self.s.reset_input_buffer()
        else:
            import termios
            termios.tcflush(self.fd, termios.TCIFLUSH)

    def close(self):
        try:
            self.s.close() if serial else os.close(self.fd)
        except OSError:
            pass


class Bootloader:
    def __init__(self, link):
        self.link = link
        self.seq = 0

    def cmd(self, body, timeout=1.0):
        """Send one STK500v2 message, return the answer's body (status checked)."""
        self.seq = (self.seq + 1) & 0xFF
        msg = bytes([MESSAGE_START, self.seq, len(body) >> 8, len(body) & 0xFF, TOKEN]) + bytes(body)
        x = 0
        for b in msg:
            x ^= b
        self.link.write(msg + bytes([x]))
        end = time.time() + timeout
        while time.time() < end:  # find the start of the answer
            b = self.link.read(1, end - time.time())
            if b == bytes([MESSAGE_START]):
                break
        else:
            raise FlashError("no answer from the bootloader")
        head = self.link.read(4, timeout)
        if len(head) < 4 or head[0] != self.seq or head[3] != TOKEN:
            raise FlashError("garbled answer from the bootloader")
        size = (head[1] << 8) | head[2]
        rest = self.link.read(size + 1, timeout)
        if len(rest) < size + 1:
            raise FlashError("answer from the bootloader cut short")
        x = MESSAGE_START
        for b in head + rest[:-1]:
            x ^= b
        if x != rest[-1]:
            raise FlashError("damaged answer from the bootloader (checksum)")
        ans = rest[:-1]
        if ans[0] != body[0] or (len(ans) > 1 and ans[1] != STATUS_OK):
            raise FlashError(f"bootloader refused command 0x{body[0]:02x}")
        return ans

    def sign_on(self, tries=10):
        """Right after a reset the bootloader listens for ~1 s."""
        for _ in range(tries):
            self.link.flush_input()
            try:
                ans = self.cmd([CMD_SIGN_ON], timeout=0.25)
                return ans[3:3 + ans[2]].decode(errors="replace") if len(ans) > 3 else "?"
            except FlashError:
                continue
        raise FlashError("the Mega's bootloader didn't answer (is it a Mega 2560? try again, or press its "
                         "reset button just before flashing)")

    def load_address(self, byte_addr):
        word = (byte_addr >> 1) | 0x80000000  # bit 31: above 64 KB (ATmega2560)
        self.cmd([CMD_LOAD_ADDRESS] + list(struct.pack(">I", word)))

    def program_page(self, data):
        n = len(data)
        self.cmd([CMD_PROGRAM_FLASH, n >> 8, n & 0xFF, 0xC1, 10, 0x40, 0x4C, 0x20, 0x00, 0x00] + list(data),
                 timeout=2.0)

    def read_page(self, n):
        ans = self.cmd([CMD_READ_FLASH, n >> 8, n & 0xFF, 0x20])
        return ans[2:2 + n]


def flash(port, hex_path, progress=None, log=print):
    """Write hex_path to the Mega on port and verify it."""
    start, img = read_hex(hex_path)
    pages = [(start + off, img[off:off + PAGE]) for off in range(0, len(img), PAGE)]
    log(f"Flashing {os.path.basename(hex_path)} ({len(img) // 1024} KB) to the Mega on {port}...")
    link = Link(port)
    try:
        link.dtr(False)  # reset: the bootloader starts
        time.sleep(0.25)
        link.dtr(True)
        time.sleep(0.05)
        boot = Bootloader(link)
        log(f"  bootloader: {boot.sign_on()}")
        boot.cmd([CMD_ENTER_PROGMODE, 200, 100, 25, 32, 0, 0x53, 3, 0xAC, 0x53, 0, 0])
        total = len(pages) * 2
        for n, (addr, data) in enumerate(pages):
            boot.load_address(addr)
            boot.program_page(data.ljust(PAGE, b"\xff"))
            if progress:
                progress(n * 100 // total)
        for n, (addr, data) in enumerate(pages):  # read back
            boot.load_address(addr)
            got = boot.read_page(PAGE)
            if got[:len(data)] != data:
                raise FlashError(f"verify failed at 0x{addr:05x}: the Mega has something else there")
            if progress:
                progress((len(pages) + n) * 100 // total)
        boot.cmd([CMD_LEAVE_PROGMODE, 1, 1])
        if progress:
            progress(100)
        log("  written and verified. The Mega restarts with the new firmware.")
    finally:
        link.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: megaflash.py PORT FILE.hex")
    flash(sys.argv[1], sys.argv[2], lambda p: print(f"\r  {p:3d}%", end="", flush=True))
