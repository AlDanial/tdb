"""Pure-function tests for the OCaml value decoder (no lldb needed)."""

import struct

from tdb.adapters.ocaml.lldb_formatters import describe_value


class FakeMemory:
    """addr -> bytes store with OCaml block layout helpers (64-bit)."""

    def __init__(self):
        self.mem: dict[int, bytes] = {}

    def read(self, addr: int, size: int) -> bytes | None:
        blob = self.mem.get(addr)
        if blob is None or len(blob) < size:
            return None
        return blob[:size]

    def add_block(self, addr: int, tag: int, fields: list[int]) -> int:
        """Lay out header at addr-8 and fields at addr. Returns the value
        word (the pointer, which is even)."""
        header = (len(fields) << 10) | tag
        self.mem[addr - 8] = struct.pack("<Q", header)
        for i, f in enumerate(fields):
            self.mem[addr + 8 * i] = struct.pack("<Q", f)
        return addr

    def add_string(self, addr: int, s: bytes) -> int:
        nwords = (len(s) // 8) + 1
        data = s + b"\x00" * (nwords * 8 - len(s) - 1)
        padding = nwords * 8 - len(s) - 1
        data += bytes([padding])
        self.mem[addr - 8] = struct.pack("<Q", (nwords << 10) | 252)
        for i in range(nwords):
            self.mem[addr + 8 * i] = data[8 * i : 8 * i + 8]
        return addr


def test_immediate_int():
    summary, children = describe_value(2 * 21 + 1, lambda a, s: None)
    assert "21" in summary
    assert children == []


def test_string_block():
    m = FakeMemory()
    v = m.add_string(0x1000, b"hello")
    summary, children = describe_value(v, m.read)
    assert '"hello"' in summary
    assert children == []


def test_float_block():
    m = FakeMemory()
    m.mem[0x2000 - 8] = struct.pack("<Q", (1 << 10) | 253)
    m.mem[0x2000] = struct.pack("<d", 3.5)
    summary, _ = describe_value(0x2000, m.read)
    assert "3.5" in summary


def test_structured_block_with_children():
    m = FakeMemory()
    inner = m.add_string(0x3000, b"hi")
    v = m.add_block(0x4000, 0, [2 * 7 + 1, inner])
    summary, children = describe_value(v, m.read)
    assert "block(tag=0, size=2)" in summary
    assert children == [("[0]", 15), ("[1]", inner)]


def test_closure_and_custom_tags():
    m = FakeMemory()
    fn = m.add_block(0x5000, 247, [0x9999, 3])
    summary, children = describe_value(fn, m.read)
    assert "fun" in summary and children == []
    cu = m.add_block(0x6000, 255, [0x1234])
    summary, _ = describe_value(cu, m.read)
    assert "custom" in summary


def test_unreadable_pointer_degrades():
    summary, children = describe_value(0x7000, lambda a, s: None)
    assert "0x7000" in summary  # falls back to the raw pointer
    assert children == []


def test_forward_chain_truncation():
    """Regression test: self-referential Forward block must not infinite-loop."""
    m = FakeMemory()
    # Create a Forward block (tag 250) at 0x8000 that points to itself
    addr = 0x8000
    m.mem[addr - 8] = struct.pack("<Q", (1 << 10) | 250)  # Forward block with 1 field
    m.mem[addr] = struct.pack("<Q", addr)  # points to itself
    summary, children = describe_value(addr, m.read)
    # Must complete without RecursionError and mention forward/truncation
    assert "forward" in summary.lower() or "truncated" in summary.lower()
    assert children == []
