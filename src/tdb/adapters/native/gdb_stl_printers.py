"""GDB-side helper that guarantees libstdc++ pretty-printing.

tdb's gdb adapter sources this file before gdb enters DAP mode
(`gdb -iex "source .../gdb_stl_printers.py" -i dap`). A distro gdb
pretty-prints `std::vector`, `std::map`, ... because gcc installs the
libstdc++ printers next to it and gdb auto-loads them when libstdc++.so
appears; a gdb built from source into its own prefix never finds that
auto-load script, so the same `std::vector<Result>` shows up as
`_M_impl._M_start`/`_M_finish` pointer soup in the Variables view.

This script appends ONE lazy printer, "tdb-stl", to the END of gdb's
global printer list, so anything registered by gdb's auto-load (on the
objfile) or the user's own gdbinit (global, inserted first) still wins.
The first time it is asked about a `std::` value it resolves what to do:

1. Find the real libstdc++ printers (`libstdcxx/v6/printers.py`) that
   gcc ships: an explicit directory from $TDB_GDB_STL_PRINTERS, the
   distro auto-load script matching the loaded libstdc++.so (whose
   `pythondir = ...` line names the directory), directories near that
   objfile or near gdb's own data directory, then the usual distro,
   toolset, Homebrew and MacPorts locations. Found -> delegate every
   lookup to them: the view then matches the distro gdb exactly.
2. Otherwise use the bundled fallback printers below, an MIT-licensed
   subset covering the common containers and smart pointers.

It also guards the printers gdb auto-loads itself. gcc 15's map and set
printers carry a `num_children` that only gdb's DAP layer calls, and
that is broken there (15.2: `def num_children(slf)` -> NameError;
earlier: half the children), so expanding a std::map in a DAP client
fails even on a distro gdb. tdb wraps every libstdc++ printer object it
hands out, and every `libstdc++-v6` printer found on a libstdc++ objfile
when it loads, in a proxy that is not a gdb.ValuePrinter: gdb's DAP
layer then counts children itself. "bundled" instead drops the
auto-loaded printers from that objfile, so the fallback really is used.

$TDB_GDB_STL_PRINTERS: unset/"auto" (search, then fallback), "off"
(register nothing), "bundled" (skip the search; the fallback only),
or a directory holding `libstdcxx/` (use exactly that, else fallback).
The `tdb-stl-printers` gdb command reports which source is in use.

The file is also importable outside gdb (tdb's unit tests exercise the
search logic); everything that touches the gdb module stays behind the
`gdb is not None` guard at the bottom. It must never write to stdout:
in DAP mode gdb's stdout is the protocol channel.
"""

from __future__ import annotations

import glob
import os
import re
import sys

try:  # Importing this module during normal Python packaging must stay harmless.
    import gdb  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only happens outside GDB.
    gdb = None  # type: ignore[assignment]

ENV_VAR = "TDB_GDB_STL_PRINTERS"
PRINTER_NAME = "tdb-stl"
STATUS_COMMAND = "tdb-stl-printers"

# Relative to a candidate python directory.
_PRINTERS_MODULE = os.path.join("libstdcxx", "v6", "printers.py")

# Where distros, Red Hat toolsets, Homebrew and MacPorts put gcc's python
# directory. Globs; checked after anything derived from the session.
FIXED_CANDIDATE_GLOBS = (
    "/usr/share/gcc*/python",
    "/usr/local/share/gcc*/python",
    "/opt/rh/gcc-toolset-*/root/usr/share/gcc*/python",
    "/opt/rh/devtoolset-*/root/usr/share/gcc*/python",
    "/opt/homebrew/Cellar/gcc/*/share/gcc-*/python",
    "/opt/homebrew/Cellar/gcc@*/*/share/gcc-*/python",
    "/usr/local/Cellar/gcc/*/share/gcc-*/python",
    "/usr/local/Cellar/gcc@*/*/share/gcc-*/python",
    "/opt/local/share/gcc*/python",
)

# gdb data directories whose auto-load tree may hold the distro's
# `<libstdc++.so path>-gdb.py` loader, which names the python directory.
AUTO_LOAD_DATA_DIRS = ("/usr/share/gdb", "/usr/local/share/gdb")

_PYTHONDIR_LINE = re.compile(r"^\s*pythondir\s*=\s*['\"]([^'\"]+)['\"]", re.M)


def is_libstdcxx_objfile(path: str) -> bool:
    base = os.path.basename(path)
    return base.startswith("libstdc++.") or base.startswith("libstdc++-")


def pythondir_from_auto_load_script(script_path: str) -> str | None:
    """The `pythondir = '...'` assignment in a distro libstdc++ auto-load
    script, or None when the file is missing or has no such line."""
    try:
        with open(script_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = _PYTHONDIR_LINE.search(text)
    return m.group(1) if m else None


def candidate_dirs(
    env_value: str | None,
    objfile_paths: list[str],
    data_directory: str | None,
    *,
    auto_load_data_dirs: tuple[str, ...] = AUTO_LOAD_DATA_DIRS,
    fixed_globs: tuple[str, ...] = FIXED_CANDIDATE_GLOBS,
) -> list[str]:
    """Ordered, de-duplicated directories that may hold `libstdcxx/`.

    An explicit directory in `env_value` is the only candidate. Otherwise
    the list goes from most to least specific: the distro auto-load
    script for the loaded libstdc++.so, `share/gcc*/python` up to three
    levels above that objfile, the same beside gdb's data directory, then
    the fixed globs.
    """
    mode = (env_value or "").strip()
    if mode and mode.lower() not in ("auto", "bundled", "off"):
        return [mode]
    found: list[str] = []

    def add(path: str) -> None:
        path = os.path.normpath(path)
        if path not in found:
            found.append(path)

    for objfile in objfile_paths:
        if not is_libstdcxx_objfile(objfile):
            continue
        for real in dict.fromkeys((objfile, os.path.realpath(objfile))):
            for datadir in dict.fromkeys(
                (*auto_load_data_dirs, *([data_directory] if data_directory else []))
            ):
                script = os.path.join(datadir, "auto-load") + real + "-gdb.py"
                pythondir = pythondir_from_auto_load_script(script)
                if pythondir:
                    add(pythondir)
        libdir = os.path.dirname(objfile)
        for _ in range(3):
            libdir = os.path.dirname(libdir)
            for hit in sorted(
                glob.glob(os.path.join(libdir, "share", "gcc*", "python"))
            ):
                add(hit)
    if data_directory:
        for hit in sorted(
            glob.glob(os.path.join(data_directory, "..", "gcc*", "python"))
        ):
            add(hit)
    for pattern in fixed_globs:
        for hit in sorted(glob.glob(pattern)):
            add(hit)
    return found


def find_libstdcxx_dir(dirs: list[str]) -> str | None:
    """The first directory in `dirs` holding libstdcxx/v6/printers.py."""
    for d in dirs:
        if os.path.isfile(os.path.join(d, _PRINTERS_MODULE)):
            return d
    return None


def strip_std_template(tag: str) -> str | None:
    """`std::__cxx11::basic_string<char, ...>` -> `basic_string`; None
    when `tag` is not a std:: template specialization."""
    if not tag.startswith("std::"):
        return None
    name = tag[5:]
    for inline_ns in ("__cxx11::", "__debug::"):
        if name.startswith(inline_ns):
            name = name[len(inline_ns) :]
    lt = name.find("<")
    if lt <= 0 or not name.endswith(">"):
        return None
    name = name[:lt]
    return name if "::" not in name else None


# ---------------------------------------------------------------------------
# Everything below needs gdb.
# ---------------------------------------------------------------------------

if gdb is not None:
    import gdb.printing  # noqa: E402
    import gdb.types  # noqa: E402

    def _find_type(orig, name):
        """Look up nested typedef `name` on `orig` or one of its bases, the
        way libstdc++'s own printers do (their containers expose the node
        types tdb needs as member typedefs)."""
        typ = orig.strip_typedefs()
        while True:
            # Type.tag ignores cv-qualifiers (a `const std::map&` local).
            search = f"{typ.tag}::{name}"
            try:
                return gdb.lookup_type(search)
            except RuntimeError:
                pass
            fields = typ.fields()
            if len(fields) and fields[0].is_base_class:
                typ = fields[0].type
            else:
                raise ValueError(f"cannot find type {orig}::{name}")

    def _value_type(container_type):
        """A container's `value_type` typedef (its first template argument
        when the typedef was not emitted)."""
        try:
            valtype = _find_type(container_type, "value_type")
        except ValueError:
            valtype = container_type.strip_typedefs().template_argument(0)
        return valtype.strip_typedefs()

    def _node_type(template, container_type):
        """`std::_List_node<T>` / `std::_Rb_tree_node<T>` for a container
        of T: the node specialization gdb knows from the DWARF of the
        container's own allocations."""
        return gdb.lookup_type(f"{template}<{_value_type(container_type)}>")

    def _is_struct(typ):
        return typ.strip_typedefs().code in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION)

    def _aligned_value(storage, valtype):
        """The T stored in a libstdc++ `__aligned_membuf<T>`/`__aligned_buffer<T>`."""
        return storage.address.cast(valtype.pointer()).dereference()

    def _node_value(node_val):
        """Payload of a `_List_node<T>`, `_Rb_tree_node<T>` or `_Hash_node<T,
        cached>`: `_M_storage` (gcc >= 5) or the older `_M_value_field` /
        `_M_v`."""
        for field in ("_M_storage", "_M_value_field", "_M_v"):
            if gdb.types.has_field(node_val.type, field):
                member = node_val[field]
                if field == "_M_storage":
                    return _aligned_value(member, node_val.type.template_argument(0))
                return member
        raise ValueError("unknown libstdc++ node layout")

    class _StringPrinter:
        def __init__(self, val):
            self.val = val

        def to_string(self):
            ptr = self.val["_M_dataplus"]["_M_p"]
            if gdb.types.has_field(self.val.type, "_M_string_length"):
                length = int(self.val["_M_string_length"])
                return ptr.lazy_string(length=length)
            # Pre-C++11 COW ABI: the length lives in the _Rep header
            # before the characters; the data is NUL-terminated, so read
            # it as a C string.
            return ptr.string()

        def display_hint(self):
            return "string"

    class _VectorPrinter:
        def __init__(self, val):
            self.val = val
            impl = val["_M_impl"]
            self.start = impl["_M_start"]
            self.finish = impl["_M_finish"]
            # vector<bool>: _M_start is a _Bit_iterator struct, not T*.
            self.is_bool = _is_struct(self.start.type) and gdb.types.has_field(
                self.start.type.strip_typedefs(), "_M_p"
            )

        def _length(self):
            if not self.is_bool:
                return int(self.finish - self.start)
            word_bits = self.start["_M_p"].dereference().type.sizeof * 8
            words = int(self.finish["_M_p"] - self.start["_M_p"])
            return (
                words * word_bits
                + int(self.finish["_M_offset"])
                - int(self.start["_M_offset"])
            )

        def to_string(self):
            length = self._length()
            if self.is_bool:
                return f"std::vector<bool> of length {length}"
            capacity = int(self.val["_M_impl"]["_M_end_of_storage"] - self.start)
            return f"std::vector of length {length}, capacity {capacity}"

        def children(self):
            length = self._length()
            if not self.is_bool:
                for i in range(length):
                    yield f"[{i}]", (self.start + i).dereference()
                return
            word_ptr = self.start["_M_p"]
            word_bits = word_ptr.dereference().type.sizeof * 8
            bit = int(self.start["_M_offset"])
            for i in range(length):
                word = int(word_ptr.dereference())
                yield f"[{i}]", bool(word & (1 << bit))
                bit += 1
                if bit == word_bits:
                    bit = 0
                    word_ptr = word_ptr + 1

        def display_hint(self):
            return "array"

    class _DequePrinter:
        def __init__(self, val):
            self.val = val
            self.elttype = val.type.template_argument(0)
            size = self.elttype.sizeof
            self.buffer_size = 512 // size if size < 512 else 1
            impl = val["_M_impl"]
            self.start = impl["_M_start"]
            self.finish = impl["_M_finish"]

        def _length(self):
            if int(self.start["_M_node"]) == int(self.finish["_M_node"]):
                return int(self.finish["_M_cur"] - self.start["_M_cur"])
            nodes = int(self.finish["_M_node"] - self.start["_M_node"]) - 1
            return (
                self.buffer_size * nodes
                + int(self.start["_M_last"] - self.start["_M_cur"])
                + int(self.finish["_M_cur"] - self.finish["_M_first"])
            )

        def to_string(self):
            return f"std::deque with {self._length()} elements"

        def children(self):
            node = self.start["_M_node"]
            cur = self.start["_M_cur"]
            last = self.start["_M_last"]
            end = self.finish["_M_cur"]
            i = 0
            while True:
                if int(cur) == int(end) and int(node) == int(self.finish["_M_node"]):
                    return
                if int(cur) == int(last):
                    node = node + 1
                    cur = node.dereference()
                    last = cur + self.buffer_size
                    continue
                yield f"[{i}]", cur.dereference()
                i += 1
                cur = cur + 1

        def display_hint(self):
            return "array"

    class _ListPrinter:
        def __init__(self, val):
            self.val = val
            self.nodetype = _node_type("std::_List_node", val.type)

        def _size(self):
            node = self.val["_M_impl"]["_M_node"]
            if gdb.types.has_field(node.type, "_M_size"):
                return int(node["_M_size"])
            return None

        def to_string(self):
            size = self._size()
            if size is None:
                return "std::list"
            return f"std::list with {size} elements"

        def children(self):
            head = self.val["_M_impl"]["_M_node"]
            head_addr = int(head.address)
            node = head["_M_next"]
            i = 0
            while int(node) != head_addr:
                yield (
                    f"[{i}]",
                    _node_value(node.cast(self.nodetype.pointer()).dereference()),
                )
                i += 1
                node = node["_M_next"]

        def display_hint(self):
            return "array"

    class _RbTreePrinter:
        """std::map, std::multimap, std::set, std::multiset."""

        def __init__(self, val, kind, is_map):
            self.val = val
            self.kind = kind
            self.is_map = is_map
            self.nodetype = _node_type("std::_Rb_tree_node", val.type)
            self.impl = val["_M_t"]["_M_impl"]

        def to_string(self):
            count = int(self.impl["_M_node_count"])
            return f"std::{self.kind} with {count} element{'s' if count != 1 else ''}"

        def _nodes(self):
            header = self.impl["_M_header"]
            header_addr = int(header.address)
            node = header["_M_left"]  # leftmost
            while int(node) != header_addr and int(node) != 0:
                yield node
                # In-order successor.
                if int(node["_M_right"]) != 0:
                    node = node["_M_right"]
                    while int(node["_M_left"]) != 0:
                        node = node["_M_left"]
                else:
                    parent = node["_M_parent"]
                    while int(node) == int(parent["_M_right"]):
                        node = parent
                        parent = parent["_M_parent"]
                    if int(node["_M_right"]) != int(parent):
                        node = parent

        def children(self):
            # Maps yield key, value as consecutive children (gdb pairs
            # them under the "map" hint); the index counts every child,
            # as libstdc++'s own printers do.
            i = 0
            for node in self._nodes():
                value = _node_value(node.cast(self.nodetype.pointer()).dereference())
                if self.is_map:
                    yield f"[{i}]", value["first"]
                    yield f"[{i + 1}]", value["second"]
                    i += 2
                else:
                    yield f"[{i}]", value
                    i += 1

        def display_hint(self):
            return "map" if self.is_map else "array"

    class _HashtablePrinter:
        """std::unordered_map/multimap/set/multiset."""

        def __init__(self, val, kind, is_map):
            self.val = val
            self.kind = kind
            self.is_map = is_map
            self.table = val["_M_h"]
            self.nodetype = _find_type(self.table.type, "__node_type").strip_typedefs()

        def to_string(self):
            count = int(self.table["_M_element_count"])
            return f"std::{self.kind} with {count} element{'s' if count != 1 else ''}"

        def children(self):
            node = self.table["_M_before_begin"]["_M_nxt"]
            i = 0
            while int(node) != 0:
                value = _node_value(node.cast(self.nodetype.pointer()).dereference())
                if self.is_map:
                    yield f"[{i}]", value["first"]
                    yield f"[{i + 1}]", value["second"]
                    i += 2
                else:
                    yield f"[{i}]", value
                    i += 1
                node = node["_M_nxt"]

        def display_hint(self):
            return "map" if self.is_map else "array"

    class _UniquePtrPrinter:
        def __init__(self, val):
            self.val = val
            impl = val["_M_t"]
            # gcc >= 9: unique_ptr::_M_t is __uniq_ptr_data/__uniq_ptr_impl
            # wrapping the tuple<pointer, deleter>; older: the tuple itself.
            if gdb.types.has_field(impl.type, "_M_t"):
                impl = impl["_M_t"]
            self.pointer = None
            try:
                tuple_impl = impl.type.fields()[0].type  # _Tuple_impl<0, ...>
                head_base = tuple_impl.fields()[1].type  # _Head_base<0, pointer>
                head = head_base.fields()[0]
                if head.name == "_M_head_impl":
                    self.pointer = impl.cast(head_base)["_M_head_impl"]
                elif head.is_base_class:
                    self.pointer = impl.cast(head.type)
            except Exception:
                self.pointer = None

        def to_string(self):
            elt = self.val.type.template_argument(0)
            return f"std::unique_ptr<{elt}>"

        def children(self):
            if self.pointer is not None:
                yield "get()", self.pointer

    class _SharedPtrPrinter:
        def __init__(self, val, kind):
            self.val = val
            self.kind = kind

        def to_string(self):
            elt = self.val.type.template_argument(0)
            pi = self.val["_M_refcount"]["_M_pi"]
            if int(pi) == 0:
                return f"std::{self.kind}<{elt}> (empty)"
            use = int(pi["_M_use_count"])
            weak = int(pi["_M_weak_count"]) - (1 if use > 0 else 0)
            return f"std::{self.kind}<{elt}> (use count {use}, weak count {weak})"

        def children(self):
            yield "get()", self.val["_M_ptr"]

    class _OptionalPrinter:
        def __init__(self, val):
            self.val = val
            payload = val["_M_payload"]
            if gdb.types.has_field(val.type, "_M_engaged"):  # gcc 7
                self.engaged = bool(val["_M_engaged"])
                self.value = payload
            else:
                self.engaged = bool(payload["_M_engaged"])
                storage = payload["_M_payload"]
                self.value = storage["_M_value"]

        def to_string(self):
            return (
                "std::optional"
                if self.engaged
                else "std::optional [no contained value]"
            )

        def children(self):
            if self.engaged:
                yield "[contained value]", self.value

    def _bundled_lookup(val):
        """tdb's own printer for a libstdc++ value, or None."""
        typ = gdb.types.get_basic_type(val.type)
        tag = typ.tag
        if not tag:
            return None
        name = strip_std_template(tag)
        if name is None:
            return None
        try:
            if name == "basic_string":
                return _StringPrinter(val)
            if name == "vector":
                return _VectorPrinter(val)
            if name == "deque":
                return _DequePrinter(val)
            if name == "list":
                return _ListPrinter(val)
            if name in ("map", "multimap"):
                return _RbTreePrinter(val, name, True)
            if name in ("set", "multiset"):
                return _RbTreePrinter(val, name, False)
            if name in ("unordered_map", "unordered_multimap"):
                return _HashtablePrinter(val, name, True)
            if name in ("unordered_set", "unordered_multiset"):
                return _HashtablePrinter(val, name, False)
            if name == "unique_ptr":
                return _UniquePtrPrinter(val)
            if name in ("shared_ptr", "weak_ptr"):
                return _SharedPtrPrinter(val, name)
            if name == "optional":
                return _OptionalPrinter(val)
        except Exception:
            # A layout this fallback does not understand: leave the
            # value to gdb's raw struct display rather than erroring.
            return None
        return None

    LIBSTDCXX_PRINTER_NAME = "libstdc++-v6"

    class _ShieldedPrinter:
        """A libstdc++ printer object as gdb's DAP layer should see it.

        Forwards to_string/children/display_hint (hasattr-driven, like
        gdb's own lookups) but is not a gdb.ValuePrinter, so the DAP
        layer never calls the inner printer's num_children()/child()
        and counts the children it is given instead."""

        __slots__ = ("_inner",)

        def __init__(self, inner) -> None:
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _ShieldedLookup:
        """Wraps a registered libstdc++ printer (the callable in an
        objfile's pretty_printers list): same name/enabled/subprinters
        for `info pretty-printer` and friends, shielded results."""

        def __init__(self, inner) -> None:
            self._inner = inner
            self.name = (
                getattr(inner, "name", LIBSTDCXX_PRINTER_NAME) + " (tdb-shielded)"
            )
            self.enabled = getattr(inner, "enabled", True)
            self.subprinters = getattr(inner, "subprinters", None)

        def __call__(self, val):
            printer = self._inner(val)
            return _ShieldedPrinter(printer) if printer is not None else None

    def _is_libstdcxx_lookup(printer) -> bool:
        return getattr(printer, "name", None) == LIBSTDCXX_PRINTER_NAME

    def _guard_objfile_printers(objfile, mode: str) -> None:
        """Shield (or, in bundled mode, drop) the `libstdc++-v6` printers
        gdb auto-loaded onto a libstdc++ objfile. Auto-load runs before
        the new_objfile event reaches Python, so they are already there."""
        printers = objfile.pretty_printers
        for i, printer in enumerate(list(printers)):
            if not _is_libstdcxx_lookup(printer):
                continue
            if mode.lower() == "bundled":
                printers.remove(printer)
            else:
                printers[i] = _ShieldedLookup(printer)

    class TdbStlPrinter:
        """The one global printer tdb appends. Resolves its delegate on
        the first `std::` value it sees, when the libstdc++ objfile is
        loaded and its location known."""

        name = PRINTER_NAME
        enabled = True
        subprinters = None

        def __init__(self, mode: str) -> None:
            self.mode = mode
            self.delegate = None
            self.source = None  # human-readable, for tdb-stl-printers
            self.searched: list[str] = []

        def __call__(self, val):
            try:
                tag = gdb.types.get_basic_type(val.type).tag
            except Exception:
                return None
            if not tag or not tag.startswith("std::"):
                return None
            if self.delegate is None:
                self._resolve()
            printer = self.delegate(val)
            if printer is not None and self.delegate is not _bundled_lookup:
                printer = _ShieldedPrinter(printer)
            return printer

        def _resolve(self) -> None:
            if self.mode.lower() != "bundled":
                try:
                    data_directory = gdb.parameter("data-directory")
                except Exception:
                    data_directory = None
                objfiles = []
                try:
                    objfiles = [o.filename for o in gdb.objfiles() if o.filename]
                except Exception:
                    pass
                self.searched = candidate_dirs(self.mode, objfiles, data_directory)
                found = find_libstdcxx_dir(self.searched)
                if found is not None:
                    try:
                        self.delegate = _load_libstdcxx(found)
                        self.source = f"libstdc++'s own printers from {found}"
                        return
                    except Exception as e:  # pragma: no cover - needs a broken install
                        self.searched.append(f"({found}: import failed: {e})")
            self.delegate = _bundled_lookup
            self.source = "tdb's bundled fallback printers"

    def _load_libstdcxx(pythondir: str):
        """Import gcc's printers from `pythondir` and return their lookup
        callable without registering it: the delegate is called from
        tdb's slot in gdb's printer list, so insertion order (and
        mutating that list mid-lookup) never comes into it."""
        if pythondir not in sys.path:
            sys.path.insert(0, pythondir)
        from libstdcxx.v6 import printers  # type: ignore[import-not-found]

        if getattr(printers, "libstdcxx_printer", None) is None:
            printers.build_libstdcxx_dictionary()
        return printers.libstdcxx_printer

    class _StatusCommand(gdb.Command):
        """Report which libstdc++ pretty-printers tdb's helper uses."""

        def __init__(self, printer: TdbStlPrinter | None) -> None:
            super().__init__(STATUS_COMMAND, gdb.COMMAND_STATUS)
            self.printer = printer

        def invoke(self, arg, from_tty):
            if self.printer is None:
                gdb.write(f"tdb STL printers: off (${ENV_VAR}=off)\n")
            elif self.printer.delegate is None:
                gdb.write(
                    "tdb STL printers: not resolved yet (resolves on the first "
                    "std:: value gdb's own printers do not handle)\n"
                )
            else:
                gdb.write(f"tdb STL printers: {self.printer.source}\n")
                if self.printer.searched:
                    gdb.write("searched: " + ", ".join(self.printer.searched) + "\n")
            for objfile in gdb.objfiles():
                names = [getattr(p, "name", "?") for p in objfile.pretty_printers]
                if (
                    names
                    and objfile.filename
                    and is_libstdcxx_objfile(objfile.filename)
                ):
                    gdb.write(
                        f"note: {objfile.filename} has printers {names}; "
                        "they take precedence\n"
                    )

    def _install() -> None:
        mode = os.environ.get(ENV_VAR, "") or "auto"
        if mode.strip().lower() == "off":
            _StatusCommand(None)
            return
        printer = TdbStlPrinter(mode)
        gdb.pretty_printers.append(printer)
        _StatusCommand(printer)

        def _on_new_objfile(event) -> None:
            objfile = event.new_objfile
            if objfile.filename and is_libstdcxx_objfile(objfile.filename):
                try:
                    _guard_objfile_printers(objfile, mode)
                except Exception:
                    pass  # never let a printer tweak break the session

        gdb.events.new_objfile.connect(_on_new_objfile)

    _install()
