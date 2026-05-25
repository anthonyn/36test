#!/usr/bin/env python3
"""
zephyr_stack_analyze.py — Static worst-case stack depth analyzer for Zephyr ELFs
==================================================================================
Supports: RISC-V (rv32/rv64), ARM Cortex-M, ARM64, x86, x86-64
Requires: Python 3.8+, Zephyr SDK in PATH or /opt/zephyr-sdk

HOW IT WORKS
  1. Reads per-function stack frame sizes from compiler-generated *.su files
     (if --build-dir is given) or by parsing function prologues in the
     disassembly (fallback — less accurate on ARM).
  2. Builds a call graph from direct branch/call instructions in the
     disassembly.  Indirect calls (function pointers) are flagged but not
     followed (they can't be resolved statically).
  3. Discovers Zephyr threads from ELF symbols:
       • K_THREAD_DEFINE  → _k_thread_stack_<name> symbols
       • main / idle      → z_main_stack / z_idle_stack symbols
     Threads created with k_thread_create() are not auto-detected (use
     --thread to add them manually).
  4. For each thread, DFS-walks the call graph from the entry function to
     find the deepest frame-sum path, then adds a configurable ISR overhead
     (because on many Zephyr targets — especially RISC-V M-mode — timer
     interrupts run on the current thread's stack, not a separate ISR stack).

USAGE
  # Auto-detect everything:
  python zephyr_stack_analyze.py path/to/zephyr.elf

  # With .su files for accurate ARM frames:
  python zephyr_stack_analyze.py zephyr.elf --build-dir hello_hifive/build

  # Add threads that were created with k_thread_create:
  python zephyr_stack_analyze.py zephyr.elf \\
      --thread thread_0  thread_0_fn  1024 \\
      --thread mock_irq  mock_irq_fn  2048

  # Full output — call chains, frame tables:
  python zephyr_stack_analyze.py zephyr.elf --show-chains --build-dir build/

  # ARM target, no colour (e.g. piped into less):
  python zephyr_stack_analyze.py zephyr.elf --no-color --toolchain arm-zephyr-eabi
"""

import argparse
import json
import os
import re
import struct
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Architecture detection
# ─────────────────────────────────────────────────────────────────────────────

# ELF e_machine value → (arch_tag, [toolchain_prefix_candidates])
_EMACHINE_MAP = {
    0x28: ("arm",    ["arm-zephyr-eabi",     "arm-none-eabi"]),
    0xB7: ("arm64",  ["aarch64-zephyr-elf"]),
    0xF3: ("riscv",  ["riscv32-zephyr-elf",  "riscv64-zephyr-elf"]),
    0x03: ("x86",    ["i686-zephyr-elf"]),
    0x3E: ("x86_64", ["x86_64-zephyr-elf"]),
}


def detect_arch(elf_path):
    """Return (arch_tag, [toolchain_prefixes]) from the ELF header."""
    with open(elf_path, "rb") as f:
        hdr = f.read(20)
    if hdr[:4] != b"\x7fELF":
        raise ValueError(f"Not an ELF file: {elf_path}")
    e_data  = hdr[5]   # 1=LE 2=BE
    e_class = hdr[4]   # 1=32-bit 2=64-bit
    fmt     = "<" if e_data == 1 else ">"
    e_mach  = struct.unpack_from(fmt + "H", hdr, 18)[0]
    arch, prefixes = _EMACHINE_MAP.get(e_mach, ("unknown", []))
    # Distinguish 32-bit vs 64-bit RISC-V
    if arch == "riscv":
        prefixes = ["riscv32-zephyr-elf"] if e_class == 1 else ["riscv64-zephyr-elf"]
    return arch, prefixes


# ─────────────────────────────────────────────────────────────────────────────
# Toolchain binary discovery
# ─────────────────────────────────────────────────────────────────────────────

_SDK_ROOTS = [
    Path("/opt/zephyr-sdk"),
    Path.home() / "zephyr-sdk",
    Path.home() / ".local" / "zephyr-sdk",
]


def find_tool(prefixes, tool_name):
    """Return the path to <prefix>-<tool_name>, searching PATH then SDK roots."""
    for prefix in prefixes:
        binary = f"{prefix}-{tool_name}"
        r = subprocess.run(["which", binary], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
        for sdk in _SDK_ROOTS:
            if not sdk.is_dir():
                continue
            hits = [h for h in sdk.rglob(binary)
                    if h.is_file() and os.access(str(h), os.X_OK)]
            if hits:
                return str(hits[0])
    return None


# ─────────────────────────────────────────────────────────────────────────────
# .su file parsing — compiler-accurate frame sizes
# ─────────────────────────────────────────────────────────────────────────────
# GCC -fstack-usage output format:
#   /path/to/file.c:42:5:function_name   128   static

_SU_LINE = re.compile(r"^.+:\d+:\d+:(\S+)\s+(\d+)\s+(static|dynamic|bounded)\s*$")


def load_su_files(build_dir):
    """
    Recursively scan build_dir for *.su files.
    Returns {func_name: (frame_bytes, qualifier)}.
    When the same name appears multiple times, the largest frame wins.
    """
    frames = {}
    n_files = 0
    for su_path in Path(build_dir).rglob("*.su"):
        n_files += 1
        try:
            text = su_path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _SU_LINE.match(line.strip())
            if not m:
                continue
            func, size, qual = m.group(1), int(m.group(2)), m.group(3)
            if func not in frames or size > frames[func][0]:
                frames[func] = (size, qual)
    return frames, n_files


# ─────────────────────────────────────────────────────────────────────────────
# Disassembly parsing — call graph + fallback frame sizes
# ─────────────────────────────────────────────────────────────────────────────

_FUNC_LABEL = re.compile(r"^[0-9a-f]+\s+<([^>]+)>:\s*$")

# First SP-decrement in each function's prologue → local frame size
_FRAME_RE = {
    "arm":    re.compile(r"sub\s+sp,\s*(?:sp,\s*)?#(\d+)"),
    "arm64":  re.compile(r"sub\s+sp,\s*sp,\s*#(\d+)"),
    "riscv":  re.compile(r"addi\s+sp,sp,-(\d+)"),
    "x86":    re.compile(r"sub\s+\$0x([0-9a-f]+),%esp"),
    "x86_64": re.compile(r"sub\s+\$0x([0-9a-f]+),%rsp"),
}

# ARM Cortex-M: push {r4,r5,lr} also consumes stack (4 B per register)
_ARM_PUSH = re.compile(r"(?:push|stmdb\s+sp!),\s*\{([^}]+)\}")

# Call-instruction patterns per arch.
# Two classes of call:
#   • Normal call  — saves a return address (jal ra / bl / call)
#   • Tail call    — compiler replaces call+ret with a plain jump when the
#                    callee's return goes straight back to the caller's caller.
#                    (j / b / jmp).  The callee's frame replaces ours, so the
#                    stack depth still increases by the callee's frame size.
# We rely on _CALL_TARGET to filter: only lines where objdump annotates a
# <symbol_name> are treated as cross-function calls.  Local branches to
# un-labelled code (loop back-edges, epilogue jumps) have no annotation.
_CALL_RE = {
    "arm":    re.compile(r"\bbl[x]?\b|\bb\b"),
    "arm64":  re.compile(r"\bbl\b|\bb\b"),
    # riscv: jal can appear as:
    #   "jal ra, <sym>"  (GNU binutils two-operand form)
    #   "jal <addr> <sym>"  (Zephyr SDK one-operand form, ra implicit)
    #   "j <sym>"        (jal x0 pseudoinstruction — tail call)
    "riscv":  re.compile(r"\bjal\b|\bj\b"),
    "x86":    re.compile(r"\bcall|\bjmp"),
    "x86_64": re.compile(r"\bcall|\bjmp"),
}

# objdump annotates branch targets: "... jal ra,0x1234 <my_function>"
_CALL_TARGET = re.compile(r"<([^>+]+?)(?:\+0x[0-9a-f]+)?>$")


def _run_objdump(elf_path, objdump):
    r = subprocess.run(
        [objdump, "-d", "--no-show-raw-insn", "--wide", elf_path],
        capture_output=True, text=True, errors="replace",
    )
    if r.returncode != 0:
        sys.exit(f"objdump failed:\n{r.stderr[:400]}")
    return r.stdout.splitlines()


def parse_disassembly(lines, arch):
    """
    Parse objdump -d output.

    Returns:
        frame_sizes : {func_name: int}         first SP adjustment per function
        call_graph  : {func_name: set(str)}    direct-call edges only
        indirect    : {func_name: int}         count of indirect calls per function
    """
    frame_re = _FRAME_RE.get(arch)
    call_re  = _CALL_RE.get(arch)
    is_arm   = arch == "arm"

    frame_sizes = {}
    call_graph  = defaultdict(set)
    indirect    = defaultdict(int)
    cur = None
    # Track whether we already have a frame size for cur
    # (to avoid double-counting push + sub on ARM)
    cur_has_frame = False

    for line in lines:
        # New function label
        m = _FUNC_LABEL.match(line)
        if m:
            raw = m.group(1).split("@")[0]   # strip @plt etc.
            cur = raw
            cur_has_frame = False
            continue

        if cur is None:
            continue

        # Frame size from SP decrement
        if frame_re and not cur_has_frame:
            fm = frame_re.search(line)
            if fm:
                base = 16 if arch in ("x86", "x86_64") else 10
                existing = frame_sizes.get(cur, 0)
                frame_sizes[cur] = existing + int(fm.group(1), base)
                cur_has_frame = True

        # ARM: also count registers saved by push instruction
        if is_arm:
            pm = _ARM_PUSH.search(line)
            if pm:
                regs = [r.strip() for r in pm.group(1).split(",") if r.strip()]
                frame_sizes[cur] = frame_sizes.get(cur, 0) + len(regs) * 4

        # Call instruction?
        if call_re and call_re.search(line):
            tm = _CALL_TARGET.search(line)
            if tm:
                callee = tm.group(1).split("@")[0]
                if callee and callee != cur:
                    call_graph[cur].add(callee)
            else:
                # Indirect call — can't resolve target
                indirect[cur] += 1

    return frame_sizes, dict(call_graph), dict(indirect)


# ─────────────────────────────────────────────────────────────────────────────
# Thread discovery from ELF symbols
# ─────────────────────────────────────────────────────────────────────────────

def read_symbols(elf_path, nm_bin):
    """Return {sym_name: size_bytes} from nm output."""
    r = subprocess.run(
        [nm_bin, "--print-size", "-S", elf_path],
        capture_output=True, text=True, errors="replace",
    )
    syms = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            try:
                syms[parts[3]] = int(parts[1], 16)
            except ValueError:
                pass
    return syms


# Stack symbol prefix → (human thread name, likely entry function)
_KNOWN_STACKS = {
    "z_main_stack":        ("main",     "main"),
    "z_idle_stack":        ("idle",     "idle"),
    "_idle_stack":         ("idle",     "idle"),
    "z_sys_work_q_stack":  ("sysworkq", "z_work_q_main"),
}

# Prefixes created by K_THREAD_DEFINE
_KTHREAD_STACK_PREFIX = "_k_thread_stack_"

# Rough guard/alignment overhead Zephyr adds around thread stacks per arch.
# Subtract this from the ELF symbol size to get the usable stack bytes.
_STACK_GUARD = {"arm": 32, "arm64": 32, "riscv": 0, "x86": 0, "x86_64": 0}

# Broad heuristic: any ELF symbol whose name ends with _stack or _stacks
# (and is large enough to be a thread stack) is a candidate.
_STACK_SYM_RE = re.compile(r"(?:^|_)stacks?$", re.IGNORECASE)

# Suffixes tried in order when guessing an entry function from a stack-symbol
# base name (e.g. "irq_stack" → base "irq" → try "irq_fn", "irq_entry", …)
_ENTRY_SUFFIXES = ["_fn", "_entry", "_task", "_thread", "_body",
                   "_main", "_handler", "_routine", "_func", ""]


def guess_entry_fn(stack_sym, known_fns):
    """
    Guess the likely entry-function name from a stack symbol name.

    Strategy:
      1. Strip known Zephyr prefixes (_k_thread_stack_, z_, _).
      2. Strip _stack / _stacks suffix.
      3. Try appending each suffix in _ENTRY_SUFFIXES, return on first match
         against known_fns (the set of functions found in the disassembly).
      4. If nothing matches, return the bare base name as a best-effort hint
         so the user has something to work with.

    Examples:
      "irq_stack"            → "irq_fn"   (if irq_fn is in the ELF)
      "_k_thread_stack_led"  → "led_fn"
      "z_main_stack"         → "main"
    """
    base = stack_sym
    for prefix in ("_k_thread_stack_", "z_", "_"):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    for suffix in ("_stacks", "_stack"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if not base:
        return None
    for sfx in _ENTRY_SUFFIXES:
        candidate = base + sfx
        if candidate in known_fns:
            return candidate
    # Nothing verified — return the bare base as a starting hint
    return base


def discover_threads(syms, arch, known_fns=None):
    """
    Return list of thread dicts for stacks found in the ELF symbol table.
    Each dict: {name, entry_fn, stack_size, note}

    known_fns — set of function names from the disassembly, used by
    guess_entry_fn() to verify guessed entry names against real code.
    """
    fns     = known_fns or set()
    guard   = _STACK_GUARD.get(arch, 0)
    threads = []
    seen    = set()

    for sym, total in syms.items():
        usable = max(0, total - guard)

        if sym in _KNOWN_STACKS:
            tname, entry = _KNOWN_STACKS[sym]
            threads.append(dict(name=tname, entry_fn=entry,
                                stack_size=usable, note=f"[{sym}]"))
            seen.add(tname)

        elif sym.startswith(_KTHREAD_STACK_PREFIX):
            tname = sym[len(_KTHREAD_STACK_PREFIX):]
            entry = guess_entry_fn(sym, fns)
            threads.append(dict(name=tname, entry_fn=entry,
                                stack_size=usable, note=f"[{sym}]"))
            seen.add(tname)

    return threads


def discover_stacks_broad(syms, arch, known_fns=None):
    """
    Broader stack-symbol discovery than discover_threads().

    Finds everything discover_threads() finds PLUS any ELF symbol whose name
    ends with _stack or _stacks and whose size is plausibly a thread stack
    (≥ 64 B).  The extra symbols are flagged heuristic=True so callers can
    warn the user to verify them.

    Returns a sorted list of dicts:
      sym         — raw ELF symbol name
      name        — suggested human thread name
      entry_guess — guessed entry function (verified against known_fns if possible)
      stack_size  — usable bytes (after arch guard/alignment deduction)
      heuristic   — True when found only by the broad _STACK_SYM_RE pattern
    """
    fns    = known_fns or set()
    guard  = _STACK_GUARD.get(arch, 0)
    result = []
    seen   = set()

    # Pass 1 — well-known Zephyr stacks (z_main_stack, z_idle_stack, …)
    for sym, total in syms.items():
        if sym not in _KNOWN_STACKS:
            continue
        tname, entry = _KNOWN_STACKS[sym]
        usable  = max(0, total - guard)
        guessed = entry if entry != "?" else guess_entry_fn(sym, fns)
        result.append(dict(sym=sym, name=tname, entry_guess=guessed,
                           stack_size=usable, heuristic=False))
        seen.add(sym)

    # Pass 2 — K_THREAD_DEFINE stacks (_k_thread_stack_<name>)
    for sym, total in syms.items():
        if sym in seen or not sym.startswith(_KTHREAD_STACK_PREFIX):
            continue
        tname  = sym[len(_KTHREAD_STACK_PREFIX):]
        usable = max(0, total - guard)
        result.append(dict(sym=sym, name=tname,
                           entry_guess=guess_entry_fn(sym, fns),
                           stack_size=usable, heuristic=False))
        seen.add(sym)

    # Pass 3 — any other *_stack / *_stacks symbol ≥ 64 B (heuristic)
    for sym, total in syms.items():
        if sym in seen or total < 64:
            continue
        if _STACK_SYM_RE.search(sym):
            usable = max(0, total - guard)
            result.append(dict(sym=sym, name=sym,
                               entry_guess=guess_entry_fn(sym, fns),
                               stack_size=usable, heuristic=True))
            seen.add(sym)

    return sorted(result, key=lambda d: d["name"])


def load_config(path):
    """
    Load a JSON project config file and return its contents as a dict.

    Supported top-level keys (all optional):
      isr_overhead  — int: bytes reserved for timer-IRQ overhead per thread
      build_dir     — str: path to the CMake build directory (for .su files)
      threads       — list of thread descriptors; each entry may have:
          name         (required) — human-readable thread name
          entry        — entry function name (omit to have it guessed)
          stack_bytes  — explicit stack size in bytes
          stack_symbol — ELF symbol name whose size to look up at runtime

    Every thread entry must have either stack_bytes or stack_symbol.

    Example zephyr_stack.json
    ─────────────────────────
    {
      "isr_overhead": 256,
      "build_dir":    "hello_hifive/build",
      "threads": [
        { "name": "thread_0", "entry": "thread_0_fn", "stack_bytes": 1024 },
        { "name": "thread_1", "entry": "thread_1_fn", "stack_bytes": 1024 },
        { "name": "thread_2", "entry": "thread_2_fn", "stack_bytes": 1024 },
        { "name": "mock_irq", "entry": "mock_irq_fn", "stack_symbol": "irq_stack" }
      ]
    }
    """
    with open(path) as f:
        cfg = json.load(f)
    for t in cfg.get("threads", []):
        if "name" not in t:
            raise ValueError(f"Thread config entry missing 'name': {t}")
        if "stack_bytes" not in t and "stack_symbol" not in t:
            raise ValueError(
                f"Thread '{t['name']}' must have 'stack_bytes' or 'stack_symbol'"
            )
    return cfg


def print_stack_discovery(stacks, isr_overhead, elf_path):
    """
    Print the --list-stacks discovery table, suggested CLI flags, and a
    starter zephyr_stack.json that the user can commit alongside their code.
    """
    print(f"\n  {'═'*60}")
    print(f"  STACK DISCOVERY  ({len(stacks)} stack symbol(s) found in ELF)")
    print(f"  {'═'*60}")
    print(f"\n  {'Symbol':<36}  {'Bytes':>6}  Guessed entry function")
    print(f"  {'─'*36}  {'─'*6}  {'─'*28}")
    for s in stacks:
        flag = "  ← heuristic" if s.get("heuristic") else ""
        eg   = s["entry_guess"] or "?"
        print(f"  {s['sym']:<36}  {s['stack_size']:>5}  {eg}{flag}")

    print(f"\n  ── Suggested --thread flags ─────────────────────────────────")
    for s in stacks:
        eg = s["entry_guess"] or "?"
        print(f"    --thread {s['name']:<18} {eg:<26} {s['stack_size']}")

    cfg = {
        "isr_overhead": isr_overhead,
        "build_dir":    "build",
        "threads": [
            {
                "name":        s["name"],
                "entry":       s["entry_guess"] or "?",
                "stack_bytes": s["stack_size"],
            }
            for s in stacks
        ],
    }
    print(f"\n  ── Starter zephyr_stack.json ────────────────────────────────")
    print(json.dumps(cfg, indent=2))
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Worst-case stack depth  (DFS with cycle detection)
# ─────────────────────────────────────────────────────────────────────────────

def _dfs(fn, frame_sizes, call_graph, limit, visited):
    """
    Returns (total_bytes, [fn, callee, callee_callee, ...]).
    Stops at already-visited nodes (handles mutual recursion) and at depth limit.
    """
    if fn in visited or limit == 0:
        return 0, []
    visited = visited | {fn}
    own     = frame_sizes.get(fn, 0)
    best_b, best_c = 0, []
    for callee in call_graph.get(fn, ()):
        b, c = _dfs(callee, frame_sizes, call_graph, limit - 1, visited)
        if b > best_b:
            best_b, best_c = b, c
    return own + best_b, [fn] + best_c


def worst_case_depth(entry_fn, frame_sizes, call_graph, limit=50):
    """Return (worst_bytes, call_chain_list)."""
    if entry_fn not in call_graph and entry_fn not in frame_sizes:
        return None, []
    return _dfs(entry_fn, frame_sizes, call_graph, limit, set())


# ─────────────────────────────────────────────────────────────────────────────
# Terminal output helpers
# ─────────────────────────────────────────────────────────────────────────────

_COLOR = True


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _bar(used, total, width=30):
    if not total:
        return "[" + "?" * width + "]  ???%"
    pct    = min(100, used * 100 // total)
    filled = min(width, int(width * used / total))
    code   = "92" if pct < 60 else "93" if pct < 80 else "91"
    bar    = _c(code, "█" * filled) + "░" * (width - filled)
    return f"[{bar}] {pct:3d}%"


def _status(pct):
    if pct < 60:
        return _c("92", "✓  comfortable")
    if pct < 80:
        return _c("93", "⚠  getting tight")
    if pct < 100:
        return _c("91", "✗  danger — overflow likely")
    return _c("91;1", "✗✗ OVERFLOW")


# ─────────────────────────────────────────────────────────────────────────────
# Per-thread report
# ─────────────────────────────────────────────────────────────────────────────

def print_thread_report(t, frame_sizes, call_graph, indirect,
                        isr_overhead, show_chains):
    name     = t["name"]
    entry    = t["entry_fn"]
    stksz    = t["stack_size"]
    note     = t.get("note", "")
    manual   = t.get("manual", False)

    src = "(manual)" if manual else note

    print()
    print(_c("1", f"  ┌─ {name}") + (f"  {src}" if src else ""))
    print(f"  │  entry fn   : {entry}")
    print(f"  │  stack size : {stksz} B")

    if not entry or entry == "?":
        print(f"  │  " + _c("93", "entry function unknown — use --thread N ENTRY SIZE"))
        print(f"  └{'─'*54}")
        return

    depth, chain = worst_case_depth(entry, frame_sizes, call_graph)

    if depth is None:
        print(f"  │  " + _c("93",
              f"'{entry}' not found in disassembly (check spelling / LTO)"))
        print(f"  └{'─'*54}")
        return

    # Check for indirect calls on the worst path
    indir_fns = [fn for fn in chain if indirect.get(fn, 0) > 0]

    total = depth + isr_overhead
    pct   = min(100, total * 100 // stksz) if stksz else 0

    print(f"  │  call chain : {depth} B  (static worst-case)")
    print(f"  │  ISR pad    : {isr_overhead} B")
    print(f"  │  {'─'*44}")
    print(f"  │  total est. : ~{total} B  /  {stksz} B  →  "
          f"{stksz - total} B free   {_status(pct)}")
    print(f"  │  {_bar(total, stksz)}")

    if indir_fns:
        print(f"  │")
        print(f"  │  " + _c("93", "⚠  indirect calls detected on worst path:"))
        for fn in indir_fns:
            print(f"  │     {fn}  ({indirect[fn]} indirect call(s)) "
                  "← cannot follow function pointers")
        print(f"  │     actual depth may be higher than shown")

    if show_chains and chain:
        print(f"  │")
        print(f"  │  Worst-case call chain  ({len(chain)} frames):")
        running = 0
        for fn in chain:
            f = frame_sizes.get(fn, 0)
            running += f
            tail = "  ← deepest" if fn == chain[-1] else ""
            print(f"  │    {running:6d} B   {fn}  (+{f} B){tail}")
        if isr_overhead:
            running += isr_overhead
            print(f"  │    {running:6d} B   [timer ISR on thread stack]  (+{isr_overhead} B)")

    print(f"  └{'─'*54}")


# ─────────────────────────────────────────────────────────────────────────────
# Largest-frame summary table
# ─────────────────────────────────────────────────────────────────────────────

def print_frame_table(frame_sizes, su_frames, top_n=20):
    """Print the top N functions by stack frame size."""
    print(f"\n  Top {top_n} functions by stack frame size:")
    print(f"  {'─'*54}")
    print(f"  {'Frame':>8}   {'Source':<10}  Function")
    print(f"  {'─'*8}   {'─'*10}  {'─'*30}")

    # Mark which came from .su files
    rows = []
    for fn, sz in frame_sizes.items():
        src = "compiler" if fn in su_frames else "disasm"
        qual = su_frames[fn][1] if fn in su_frames else ""
        rows.append((sz, fn, src, qual))
    rows.sort(reverse=True)

    for sz, fn, src, qual in rows[:top_n]:
        dyn = "  ⚠ dynamic" if qual == "dynamic" else ""
        print(f"  {sz:>8} B   {src:<10}  {fn}{dyn}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global _COLOR

    ap = argparse.ArgumentParser(
        description="Zephyr static worst-case stack analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("elf", help="Path to zephyr.elf")

    # ── Generic / project config ─────────────────────────────────────────
    ap.add_argument("--config", metavar="FILE",
                    help="JSON project config file (isr_overhead, build_dir, threads).\n"
                         "CLI flags take precedence over config values.\n"
                         "See load_config() docstring for the file format.")
    ap.add_argument("--list-stacks", action="store_true",
                    help="Discovery mode: print all stack symbols found in the ELF,\n"
                         "guess their entry functions, and emit a starter JSON config.\n"
                         "Useful when starting analysis on an unfamiliar project.")

    # ── Analysis options ─────────────────────────────────────────────────
    ap.add_argument("--build-dir", metavar="DIR",
                    help="Build directory scanned for *.su files (recommended).\n"
                         "Can also be set via 'build_dir' in --config.")
    ap.add_argument("--thread", nargs=3, action="append", default=[],
                    metavar=("NAME", "ENTRY_FN", "STACK_BYTES"),
                    help="Add/override a thread (repeatable).\n"
                         "Takes precedence over --config thread entries.")
    ap.add_argument("--isr-overhead", type=int, default=None, metavar="N",
                    help="Bytes reserved for timer-IRQ overhead (default 256).\n"
                         "RISC-V M-mode / Cortex-M without ISR stack: use 256+.\n"
                         "Cortex-M with MSP/PSP split: use 0.\n"
                         "Can also be set via 'isr_overhead' in --config.")
    ap.add_argument("--show-chains", action="store_true",
                    help="Print the full worst-case call chain per thread")
    ap.add_argument("--frame-table", action="store_true",
                    help="Print top-20 functions by frame size")
    ap.add_argument("--no-color", action="store_true",
                    help="Disable ANSI colour output")
    ap.add_argument("--toolchain", metavar="PREFIX",
                    help="Toolchain prefix (e.g. arm-zephyr-eabi)")
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty():
        _COLOR = False

    if not Path(args.elf).is_file():
        sys.exit(f"ERROR: ELF not found: {args.elf}")

    # ── Load project config (if given) ───────────────────────────────────
    cfg = {}
    if args.config:
        try:
            cfg = load_config(args.config)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            sys.exit(f"ERROR loading config '{args.config}': {e}")

    # Resolve settings: explicit CLI flags override config; config overrides defaults
    build_dir    = args.build_dir or cfg.get("build_dir")
    isr_overhead = (args.isr_overhead if args.isr_overhead is not None
                    else cfg.get("isr_overhead", 256))

    # ── Detect architecture ──────────────────────────────────────────────
    try:
        arch, prefixes = detect_arch(args.elf)
    except (ValueError, OSError) as e:
        sys.exit(f"ERROR: {e}")

    if args.toolchain:
        prefixes = [args.toolchain]

    print(f"\n  {_c('1', 'Zephyr stack analyzer')}"
          f"  —  {Path(args.elf).name}  [{arch}]")
    print(f"  {'─'*60}")

    # ── Locate toolchain binaries ────────────────────────────────────────
    objdump = find_tool(prefixes, "objdump")
    nm_bin  = find_tool(prefixes, "nm")
    if not objdump:
        sys.exit(f"ERROR: {prefixes[0]}-objdump not found.\n"
                 "Install the Zephyr SDK or pass --toolchain PREFIX.")
    if not nm_bin:
        sys.exit(f"ERROR: {prefixes[0]}-nm not found.")

    print(f"  arch      : {arch}")
    print(f"  objdump   : {objdump}")
    if args.config:
        print(f"  config    : {args.config}")

    # ── Load .su files (compiler-accurate frame sizes) ───────────────────
    su_frames = {}
    if build_dir:
        su_frames, n_su = load_su_files(build_dir)
        print(f"  .su files : {len(su_frames)} functions in {n_su} files "
              f"from {build_dir}")
    else:
        print(f"  .su files : not loaded  "
              f"(pass --build-dir or set build_dir in config)")

    # ── Disassemble ──────────────────────────────────────────────────────
    print(f"  Disassembling...", end="", flush=True)
    asm_lines = _run_objdump(args.elf, objdump)
    asm_frames, call_graph, indirect = parse_disassembly(asm_lines, arch)
    n_edges = sum(len(v) for v in call_graph.values())
    print(f" {len(asm_frames)} frame sizes,  {n_edges} call edges,  "
          f"{sum(indirect.values())} indirect calls")

    # Merge: .su files take priority (accurate), disassembly fills the gaps
    frame_sizes = dict(asm_frames)
    for fn, (sz, _) in su_frames.items():
        frame_sizes[fn] = sz

    # Set of all known function names (for entry-fn guessing)
    known_fns = set(frame_sizes.keys()) | set(call_graph.keys())

    # ── Read ELF symbols ─────────────────────────────────────────────────
    syms = read_symbols(args.elf, nm_bin)

    # ── Discovery mode (--list-stacks) ───────────────────────────────────
    if args.list_stacks:
        stacks = discover_stacks_broad(syms, arch, known_fns)
        print_stack_discovery(stacks, isr_overhead, args.elf)
        sys.exit(0)

    # ── Assemble thread list ─────────────────────────────────────────────
    # Priority (highest → lowest):
    #   1. --thread CLI flags
    #   2. "threads" list in --config file
    #   3. Auto-detected from ELF symbols

    cli_names = {t[0] for t in args.thread}
    cfg_threads = cfg.get("threads", [])
    cfg_names   = {t["name"] for t in cfg_threads}

    # Auto-detected threads not overridden by CLI or config
    auto_thr = discover_threads(syms, arch, known_fns)
    threads  = [t for t in auto_thr if t["name"] not in (cli_names | cfg_names)]

    # Config-file threads (not overridden by CLI flags)
    for ct in cfg_threads:
        if ct["name"] in cli_names:
            continue  # CLI always wins

        # Resolve stack size — explicit bytes or look up a symbol
        if "stack_bytes" in ct:
            stack_sz = int(ct["stack_bytes"])
        else:  # "stack_symbol" (validated by load_config)
            sym_name = ct["stack_symbol"]
            raw = syms.get(sym_name)
            if raw is None:
                print(_c("93", f"  WARNING: stack_symbol '{sym_name}' not found "
                               f"in ELF — skipping thread '{ct['name']}'"))
                continue
            stack_sz = max(0, raw - _STACK_GUARD.get(arch, 0))

        # Resolve entry function — explicit or guessed from the stack symbol
        entry = (ct.get("entry")
                 or guess_entry_fn(ct.get("stack_symbol", ct["name"]), known_fns))

        threads.append(dict(name=ct["name"], entry_fn=entry,
                            stack_size=stack_sz, note="(config)", manual=False))

    # CLI-specified threads (always added; they already won't appear above)
    for name, entry, size_str in args.thread:
        threads.append(dict(name=name, entry_fn=entry,
                            stack_size=int(size_str),
                            note="", manual=True))

    if not threads:
        print()
        print("  No threads found.")
        print("  Options:")
        print("    --list-stacks          discover stack symbols in this ELF")
        print("    --config FILE          load thread list from a JSON config")
        print("    --thread N ENTRY BYTES specify a thread explicitly (repeatable)")
        print()
        if args.frame_table:
            print_frame_table(frame_sizes, su_frames)
        sys.exit(0)

    # ── Print per-thread report ──────────────────────────────────────────
    # z_thread_entry is the real call-chain root for every Zephyr thread.
    # Its frame is on the stack before your entry_fn starts, so the chain
    # totals below slightly underestimate the true worst case.
    z_entry_sz = frame_sizes.get("z_thread_entry", 0)

    print()
    print(f"  ISR overhead   : {isr_overhead} B per timer interrupt")
    if z_entry_sz:
        print(f"  z_thread_entry : {z_entry_sz} B  "
              f"← always present below your entry_fn (add to chain totals for "
              f"true worst case)")
    if not su_frames and arch == "arm":
        print(f"  " + _c("93",
              "Tip: ARM push{{regs}} adds stack too — add -fstack-usage to "
              "CMakeLists.txt\n"
              "  and pass --build-dir for accurate results."))

    print(f"\n  {'═'*60}")
    print(f"  THREAD ANALYSIS  ({len(threads)} threads found)")
    print(f"  {'═'*60}")

    for t in sorted(threads, key=lambda x: x["name"]):
        print_thread_report(t, frame_sizes, call_graph, indirect,
                            isr_overhead, args.show_chains)

    # ── Optional frame-size table ────────────────────────────────────────
    if args.frame_table:
        print_frame_table(frame_sizes, su_frames)

    print()


if __name__ == "__main__":
    main()
