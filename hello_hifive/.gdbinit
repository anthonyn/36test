# .gdbinit - Zephyr debug session for hello_hifive
#
# Build (always use --build-dir to keep ELF in the expected place):
#   cd ~/test/zephyr/36test
#   west build -p always -b hifive1_revb hello_hifive --build-dir hello_hifive/build
#   west flash --build-dir hello_hifive/build
#
# Terminal 1 - GDB server:
#   west debugserver --build-dir hello_hifive/build
#
# Terminal 2 - GDB client (run from hello_hifive/ so the ELF path is right):
#   cd ~/test/zephyr/36test/hello_hifive
#   gdb-multiarch -x .gdbinit build/zephyr/zephyr.elf

# Connection — QEMU uses :1234, J-Link GDB server uses :2331
# Swap the active line depending on your target:
#target remote :1234
target remote :2331

python

import gdb

# ---------------------------------------------------------------------------
# Thread-state bitmasks (include/zephyr/kernel_structs.h)
# ---------------------------------------------------------------------------
_THREAD_STATE = {
    0x01: "DUMMY",
    0x02: "PENDING",
    0x04: "PRESTART",
    0x08: "DEAD",
    0x10: "SUSPENDED",
    0x20: "ABORTING",
    0x40: "SUSPENDING",
    0x80: "QUEUED",
}

# User-option flags (include/zephyr/kernel.h)
_USER_OPTIONS = {
    0x01: "K_ESSENTIAL",   # system fatal if this thread dies
    0x02: "K_FP_REGS",     # uses FP/SIMD registers
    0x04: "K_USER",        # userspace thread
    0x08: "K_INHERIT_PERMS",
}

# RISC-V callee-saved register names in _callee_saved order
_RISCV_CS_REGS = ["sp", "s0", "s1", "s2", "s3", "s4",
                  "s5", "s6", "s7", "s8", "s9", "s10", "s11", "ra"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def decode_state(raw):
    if raw == 0:
        return "RUNNING"
    bits = [label for mask, label in _THREAD_STATE.items() if raw & mask]
    return "|".join(bits) if bits else "UNKNOWN(0x{:02x})".format(raw)

def decode_options(raw):
    bits = [label for mask, label in _USER_OPTIONS.items() if raw & mask]
    return "|".join(bits) if bits else "none"

def wait_reason(state_raw, pended_on):
    if int(pended_on) != 0:
        return "wait_q @ 0x{:08x}".format(int(pended_on))
    elif state_raw & 0x10:
        return "k_sleep / timeout queue"
    elif state_raw & 0x02:
        return "pending (timeout)"
    elif state_raw == 0x00:
        return "(running)"
    elif state_raw & 0x80:
        return "(ready to run)"
    return "-"

def find_thread(name_target):
    """Walk _kernel.threads list and return the k_thread for name_target, or None."""
    kernel    = gdb.parse_and_eval("_kernel")
    thread_t  = gdb.lookup_type("struct k_thread")
    ptr       = kernel["threads"]
    while int(ptr) != 0:
        t = ptr.cast(thread_t.pointer()).dereference()
        try:
            n = t["name"].string()
        except Exception:
            n = ""
        if n == name_target:
            return t, ptr
        ptr = t["next_thread"]
    return None, None

def sym_name(addr):
    """Try to resolve an address to a symbol name."""
    try:
        block = gdb.block_for_pc(int(addr))
        if block:
            return block.function.name if block.function else "0x{:08x}".format(int(addr))
    except Exception:
        pass
    return "0x{:08x}".format(int(addr))


# ---------------------------------------------------------------------------
# zephyr-threads  — summary table
# ---------------------------------------------------------------------------
class ZephyrThreads(gdb.Command):
    """List every Zephyr thread: name, state, priority, saved SP, and wait reason.
    Usage: zephyr-threads
           zephyr-threads verbose   (also dumps all callee-saved regs)
    """
    def __init__(self):
        super(ZephyrThreads, self).__init__("zephyr-threads", gdb.COMMAND_USER)

    def invoke(self, args, from_tty):
        verbose = "verbose" in args.lower()
        try:
            kernel = gdb.parse_and_eval("_kernel")
        except gdb.error as e:
            print("ERROR: {}".format(e))
            return

        thread_ptr = kernel["threads"]
        thread_t   = gdb.lookup_type("struct k_thread")

        print("")
        print("  {:<16} {:<22} {:>5}  {:<12}  {}".format(
            "NAME", "STATE", "PRIO", "SAVED SP", "WAITING ON"))
        print("  {:<16} {:<22} {:>5}  {:<12}  {}".format(
            "-"*16, "-"*22, "-"*5, "-"*12, "-"*28))

        count = 0
        while int(thread_ptr) != 0:
            t   = thread_ptr.cast(thread_t.pointer()).dereference()
            b   = t["base"]

            try:
                name = t["name"].string() or "<unnamed>"
            except Exception:
                name = "<no name>"

            state_raw = int(b["thread_state"])
            prio      = int(b["prio"])
            pended_on = b["pended_on"]

            try:
                saved_sp = "0x{:08x}".format(int(t["callee_saved"]["sp"]))
            except Exception:
                saved_sp = "n/a"

            print("  {:<16} {:<22} {:>5}  {:<12}  {}".format(
                name,
                decode_state(state_raw),
                prio,
                saved_sp,
                wait_reason(state_raw, pended_on)))

            if verbose:
                cs = t["callee_saved"]
                for reg in _RISCV_CS_REGS:
                    try:
                        print("      {:>4} = 0x{:08x}".format(reg, int(cs[reg])))
                    except Exception:
                        pass
                print("")

            thread_ptr = t["next_thread"]
            count += 1

        print("")
        print("  {} thread(s).  State legend:".format(count))
        for mask, label in _THREAD_STATE.items():
            print("    0x{:02x}  {}".format(mask, label))
        print("    0x00  RUNNING")
        print("")

ZephyrThreads()


# ---------------------------------------------------------------------------
# zephyr-thread <name>  — full detail for one thread
# ---------------------------------------------------------------------------
class ZephyrThread(gdb.Command):
    """Full detail for one named Zephyr thread.
    Usage: zephyr-thread <name>
    """
    def __init__(self):
        super(ZephyrThread, self).__init__("zephyr-thread", gdb.COMMAND_USER)

    def invoke(self, args, from_tty):
        target = args.strip().strip('"')
        if not target:
            print("Usage: zephyr-thread <thread_name>")
            return

        try:
            t, ptr = find_thread(target)
        except gdb.error as e:
            print("ERROR: {}".format(e))
            return

        if t is None:
            print("Thread '{}' not found.".format(target))
            return

        b         = t["base"]
        state_raw = int(b["thread_state"])
        pended_on = b["pended_on"]
        cs        = t["callee_saved"]
        si        = t["stack_info"]

        stack_start = int(si["start"])
        stack_size  = int(si["size"])
        stack_end   = stack_start + stack_size

        try:
            saved_sp   = int(cs["sp"])
            stack_used = stack_end - saved_sp
        except Exception:
            saved_sp = stack_used = 0

        # Entry function + args (available when CONFIG_THREAD_MONITOR=y)
        try:
            entry_fn  = sym_name(t["entry"]["pEntry"])
            arg1      = "0x{:08x}".format(int(t["entry"]["parameter1"]))
            arg2      = "0x{:08x}".format(int(t["entry"]["parameter2"]))
            arg3      = "0x{:08x}".format(int(t["entry"]["parameter3"]))
        except Exception:
            entry_fn = arg1 = arg2 = arg3 = "n/a"

        print("")
        print("  Thread     : {}".format(target))
        print("  Address    : 0x{:08x}".format(int(ptr)))
        print("  State      : {} (raw=0x{:02x})".format(decode_state(state_raw), state_raw))
        print("  Priority   : {}  ({})".format(
            int(b["prio"]),
            "cooperative" if int(b["prio"]) < 0 else "preemptive"))
        print("  Options    : {}".format(decode_options(int(b["user_options"]))))
        print("  Waiting    : {}".format(wait_reason(state_raw, pended_on)))
        print("")
        print("  Entry fn   : {}".format(entry_fn))
        print("  Arg 1 (p1) : {}".format(arg1))
        print("  Arg 2 (p2) : {}".format(arg2))
        print("  Arg 3 (p3) : {}".format(arg3))
        print("")
        print("  Stack start: 0x{:08x}".format(stack_start))
        print("  Stack end  : 0x{:08x}".format(stack_end))
        print("  Stack size : {} bytes".format(stack_size))
        print("  Saved SP   : 0x{:08x}".format(saved_sp))
        print("  Saved RA   : 0x{:08x}  ({})".format(
            int(cs["ra"]), sym_name(cs["ra"])))
        print("  Stack used : ~{} of {} bytes  ({:.0f}%)".format(
            stack_used, stack_size,
            100.0 * stack_used / stack_size if stack_size else 0))
        print("")
        print("  Callee-saved registers:")
        for reg in _RISCV_CS_REGS:
            try:
                val = int(cs[reg])
                sym = sym_name(val) if reg == "ra" else ""
                note = "  <- {}".format(sym) if sym and sym != "0x{:08x}".format(val) else ""
                print("    {:>4} = 0x{:08x}{}".format(reg, val, note))
            except Exception:
                pass
        print("")

ZephyrThread()


# ---------------------------------------------------------------------------
# zephyr-thread-bt <name>  — backtrace a sleeping thread
# ---------------------------------------------------------------------------
class ZephyrThreadBt(gdb.Command):
    """Backtrace a sleeping Zephyr thread by temporarily loading its register context.
    Works best with CONFIG_NO_OPTIMIZATIONS=y (frame pointers present).
    Usage: zephyr-thread-bt <name>
    """
    def __init__(self):
        super(ZephyrThreadBt, self).__init__("zephyr-thread-bt", gdb.COMMAND_USER)

    def invoke(self, args, from_tty):
        target = args.strip().strip('"')
        if not target:
            print("Usage: zephyr-thread-bt <thread_name>")
            return

        try:
            t, _ = find_thread(target)
        except gdb.error as e:
            print("ERROR: {}".format(e))
            return

        if t is None:
            print("Thread '{}' not found.".format(target))
            return

        cs = t["callee_saved"]

        # Save live register context so we can restore after bt
        save = {}
        for reg in _RISCV_CS_REGS + ["pc"]:
            try:
                save[reg] = int(gdb.parse_and_eval("${}".format(reg)))
            except Exception:
                save[reg] = 0

        # Load the sleeping thread's saved context
        try:
            gdb.execute("set $sp = 0x{:08x}".format(int(cs["sp"])), to_string=True)
            gdb.execute("set $pc = 0x{:08x}".format(int(cs["ra"])), to_string=True)
            gdb.execute("set $s0 = 0x{:08x}".format(int(cs["s0"])), to_string=True)
            gdb.execute("set $s1 = 0x{:08x}".format(int(cs["s1"])), to_string=True)
            gdb.execute("set $ra = 0x{:08x}".format(int(cs["ra"])), to_string=True)
            for reg in ["s2","s3","s4","s5","s6","s7","s8","s9","s10","s11"]:
                try:
                    gdb.execute("set ${} = 0x{:08x}".format(
                        reg, int(cs[reg])), to_string=True)
                except Exception:
                    pass

            print("")
            print("  Backtrace for '{}' (saved context):".format(target))
            gdb.execute("bt")
            print("")
        finally:
            # Always restore the live context
            for reg, val in save.items():
                try:
                    gdb.execute("set ${} = 0x{:08x}".format(reg, val), to_string=True)
                except Exception:
                    pass

ZephyrThreadBt()


# ---------------------------------------------------------------------------
# zephyr-thread-stack <name>  — hexdump the top of a thread's stack
# ---------------------------------------------------------------------------
class ZephyrThreadStack(gdb.Command):
    """Hexdump the top N bytes of a thread's stack (from saved SP upward).
    Usage: zephyr-thread-stack <name> [bytes]
    Default bytes = 64
    """
    def __init__(self):
        super(ZephyrThreadStack, self).__init__("zephyr-thread-stack", gdb.COMMAND_USER)

    def invoke(self, args, _):
        parts = args.strip().split()
        if not parts:
            print("Usage: zephyr-thread-stack <name> [bytes]")
            return
        target    = parts[0]
        dump_size = int(parts[1]) if len(parts) > 1 else 64

        try:
            t, _ = find_thread(target)
        except gdb.error as e:
            print("ERROR: {}".format(e))
            return

        if t is None:
            print("Thread '{}' not found.".format(target))
            return

        sp = int(t["callee_saved"]["sp"])

        print("")
        print("  Stack dump for '{}' (SP=0x{:08x}, showing {} bytes upward):".format(
            target, sp, dump_size))
        print("")

        inferior = gdb.selected_inferior()
        try:
            raw = inferior.read_memory(sp, dump_size)
        except Exception as e:
            print("  Cannot read memory: {}".format(e))
            return

        for i in range(0, len(raw), 16):
            chunk = raw[i:i+16]
            hex_part = " ".join("{:02x}".format(b if isinstance(b, int) else ord(b))
                                for b in chunk)
            asc_part = "".join(
                chr(b if isinstance(b, int) else ord(b))
                if 32 <= (b if isinstance(b, int) else ord(b)) < 127 else "."
                for b in chunk)
            print("  0x{:08x}  {:<48}  {}".format(sp + i, hex_part, asc_part))
        print("")

ZephyrThreadStack()


# ---------------------------------------------------------------------------
# zephyr-break <location> <thread_name>
#   Thread-specific breakpoint implemented via a Python stop() method.
#   GDB calls stop() in Python every time the underlying breakpoint fires —
#   much more reliable than a condition string, which GDB silently discards
#   on evaluation errors.
# ---------------------------------------------------------------------------
class _ZephyrThreadBP(gdb.Breakpoint):
    """Internal: a Breakpoint subclass that stops only for one thread."""

    def __init__(self, location, thread_addr, thread_name):
        super(_ZephyrThreadBP, self).__init__(location)
        self.thread_addr = thread_addr
        self.thread_name = thread_name

    def stop(self):
        try:
            current = int(gdb.parse_and_eval("_kernel.cpus[0].current"))
        except gdb.error as e:
            print("[zephyr-break] ERROR: cannot read _kernel.cpus[0].current: {}".format(e))
            return True

        # Always print so we can see if stop() is being called at all
        match = (current == self.thread_addr)
        print("[zephyr-break] hit — current=0x{:08x}  target=0x{:08x}  match={}".format(
            current, self.thread_addr, match))
        return match


class ZephyrBreak(gdb.Command):
    """Set a breakpoint that only triggers for one named Zephyr thread.
    Usage: zephyr-break <location> <thread_name>
    Examples:
      zephyr-break thread_fn thread_0
      zephyr-break src/main.c:35 thread_2
    """
    def __init__(self):
        super(ZephyrBreak, self).__init__("zephyr-break", gdb.COMMAND_BREAKPOINTS)

    def invoke(self, args, _):
        parts = args.strip().split()
        if len(parts) < 2:
            print("Usage: zephyr-break <location> <thread_name>")
            return

        location    = parts[0]
        thread_name = " ".join(parts[1:])

        try:
            t, ptr = find_thread(thread_name)
        except gdb.error as e:
            print("ERROR: {}".format(e))
            return

        if t is None:
            print("Thread '{}' not found — run 'zephyr-threads' to list names.".format(
                thread_name))
            return

        thread_addr = int(ptr)
        bp = _ZephyrThreadBP(location, thread_addr, thread_name)
        print("Breakpoint {} at '{}' — fires only for thread '{}' (@ 0x{:08x}).".format(
            bp.number, location, thread_name, thread_addr))

ZephyrBreak()

end

# Break at main, then let threads start
break main
continue
