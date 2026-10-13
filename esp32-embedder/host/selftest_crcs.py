"""Write the known-good output vector CRCs into firmware/full/main/selftest.h from a board's log.

Run the firmware's test flow on a board known to be healthy (bit-identical to a single-core run),
log it with serial_log.py, then:

    python selftest_crcs.py board.log [../firmware/full/main/selftest.h]

It takes every `selftest N: vector crc XXXXXXXX` line; tests 0..4 are mxtest.bin's, 5.. the long
texts. Re-record whenever the kernels' arithmetic changes.
"""
import re
import sys
from pathlib import Path


def main():
    log = Path(sys.argv[1]).read_text(errors='replace')
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).parent.parent / 'firmware/full/main/selftest.h')
    crcs = {int(i): c for i, c in re.findall(r'selftest (\d+): vector crc ([0-9a-f]{8})', log)}
    src = dst.read_text()
    n = int(re.search(r'#define N_SELFTEST (\d+)', src).group(1))
    missing = [i for i in range(n) if i not in crcs]
    if missing:
        sys.exit(f'no crc in the log for test(s) {missing}')
    table = ', '.join(f'0x{crcs[i]}' for i in range(n))
    dst.write_text(re.sub(r'SELFTEST_CRC\[N_SELFTEST\] = \{[^}]*\}', f'SELFTEST_CRC[N_SELFTEST] = {{ {table} }}', src))
    print(f'wrote {n} crcs to {dst}')


if __name__ == '__main__':
    main()
