#!/usr/bin/env python3
"""Generate tests/fixtures/vi/golden.json by running each case in real nvim.

Usage: python scripts/vi_golden.py [--only ID ...]

Each case in cases.json is run in a fresh ``nvim --headless -u NONE -i NONE -n``
on a temp file.  Keys are written in vim notation (``<Esc>``, ``<CR>`` ...).
With ``--only`` just those ids are regenerated and merged into golden.json.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "vi")
TIMEOUT = 10

LUA = r"""
vim.cmd('mapclear | mapclear!')
vim.cmd('set shortmess+=I cmdheight=10 nomore')
vim.cmd('set sw=4 sts=4 et ai nojoinspaces nrformats=bin,hex nostartofline')
vim.fn.cursor(tonumber(os.getenv('TDB_ROW')) + 1, tonumber(os.getenv('TDB_COL')) + 1)
local keys = vim.api.nvim_replace_termcodes(os.getenv('TDB_KEYS'), true, false, true)
pcall(vim.fn.feedkeys, keys, 'xt')
vim.cmd('stopinsert')
local lines = vim.fn.getline(1, '$')
local text = table.concat(lines, '\n')
if vim.fn.line2byte(1) == -1 then
  text = ''
elseif vim.bo.eol then
  text = text .. '\n'
end
local out = vim.fn.json_encode({text = text, cursor = {vim.fn.line('.') - 1, vim.fn.col('.') - 1}})
vim.fn.writefile({out}, os.getenv('TDB_OUT'))
vim.cmd('qa!')
"""


def run_case(nvim: str, case: dict, tmp: str) -> dict:
    src = os.path.join(tmp, "buf.txt")
    out = os.path.join(tmp, "out.json")
    lua = os.path.join(tmp, "run.lua")
    with open(src, "w", encoding="utf-8", newline="") as f:
        f.write(case["text"])
    with open(lua, "w", encoding="utf-8") as f:
        f.write(LUA)
    if os.path.exists(out):
        os.remove(out)
    env = dict(
        os.environ,
        TDB_ROW=str(case["cursor"][0]),
        TDB_COL=str(case["cursor"][1]),
        TDB_KEYS=case["keys"],
        TDB_OUT=out,
    )
    cmd = [
        nvim,
        "--headless",
        "-u",
        "NONE",
        "-i",
        "NONE",
        "-n",
        "-c",
        f"luafile {lua}",
        src,
    ]
    try:
        subprocess.run(
            cmd,
            env=env,
            timeout=TIMEOUT,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {TIMEOUT}s") from None
    if not os.path.exists(out):
        raise RuntimeError("nvim produced no output")
    with open(out, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", metavar="ID")
    args = ap.parse_args()
    nvim = shutil.which("nvim") or shutil.which("vim")
    if not nvim:
        print("error: nvim not found on PATH", file=sys.stderr)
        return 2
    with open(os.path.join(FIX, "cases.json"), encoding="utf-8") as f:
        all_cases = cases = json.load(f)
    gpath = os.path.join(FIX, "golden.json")
    golden: dict = {}
    if args.only and os.path.exists(gpath):
        with open(gpath, encoding="utf-8") as f:
            golden = json.load(f)
    if args.only:
        missing = set(args.only) - {c["id"] for c in cases}
        if missing:
            print(f"error: unknown case ids: {sorted(missing)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c["id"] in args.only]
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for case in cases:
            try:
                golden[case["id"]] = run_case(nvim, case, tmp)
            except (RuntimeError, OSError, ValueError) as e:  # report, keep going
                failed += 1
                print(f"FAIL {case['id']}: {e}", file=sys.stderr)
    ids = {c["id"] for c in all_cases}
    golden = {k: golden[k] for k in sorted(golden) if k in ids}
    with open(gpath, "w", encoding="utf-8") as f:
        json.dump(golden, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {len(golden)} golden entries ({failed} failed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
