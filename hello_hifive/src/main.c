/*
 * Hello World — 3 semaphore-driven threads + mock-interrupt thread
 * SiFive HiFive1 Rev B / qemu_riscv32
 * SPDX-License-Identifier: Apache-2.0
 *
 * Each application thread blocks on its own semaphore (K_FOREVER).
 * A fourth "mock IRQ" thread fires each semaphore at a random interval
 * between 1 s and 5 s, simulating hardware interrupts posting work.
 *
 * Debug outputs
 * ─────────────
 * Serial (UART)  : printk messages with uptime timestamps
 * SEGGER RTT     : same printk stream via RTT channel 0 (J-Link RTT Viewer)
 * SEGGER SystemView : full thread-switch + semaphore timeline (SystemView app)
 *                    Connect: Target → J-Link, Device = FE310, Interface = JTAG
 *
 * NOTE: SEGGER_SYSVIEW_PrintfHost() uses ~400 B of stack internally.
 * On 16 KB SRAM we cannot afford user-event marks on top of printk.
 * Thread switches and semaphore ops are recorded automatically by the
 * Zephyr tracing hooks — no manual marks are needed.
 */

#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include <zephyr/fatal.h>      /* K_ERR_* + k_fatal_halt() */

/* ── fatal error handler ───────────────────────────────────────────────────
 * Overrides the __weak default.  Prints thread name, stack bounds, saved SP,
 * and (when CONFIG_INIT_STACKS=y) the high-water mark before halting.
 * Runs with IRQs locked; keep it short.  printk→UART is safe.            */
static const char *fatal_reason_str(unsigned int r)
{
	switch (r) {
	case K_ERR_CPU_EXCEPTION:   return "CPU exception";
	case K_ERR_SPURIOUS_IRQ:    return "spurious IRQ";
	case K_ERR_STACK_CHK_FAIL:  return "stack sentinel fail";
	case K_ERR_KERNEL_OOPS:     return "kernel oops";
	case K_ERR_KERNEL_PANIC:    return "kernel panic";
	default:                    return "unknown";
	}
}

void k_sys_fatal_error_handler(unsigned int reason, const z_arch_esf_t *esf)
{
	ARG_UNUSED(esf);

	struct k_thread *t = k_current_get();

	printk("\n");
	printk("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n");
	printk("!! FATAL: %s (code %u)\n", fatal_reason_str(reason), reason);

	if (t != NULL) {
		uint32_t base  = (uint32_t)t->stack_info.start;
		uint32_t size  = (uint32_t)t->stack_info.size;
		uint32_t top   = base + size;
		uint32_t sp    = (uint32_t)t->callee_saved.sp;

		printk("!! thread : %s\n", t->name);
		printk("!! stack  : 0x%08x – 0x%08x  (%u B)\n", base, top, size);
		printk("!! saved SP: 0x%08x\n", sp);

		if (sp >= base && sp < top) {
			uint32_t used = top - sp;
			printk("!! used   : %u B / %u B  (%u%%)\n",
			       used, size, used * 100u / size);
		} else {
			printk("!! *** SP outside stack — overflow confirmed ***\n");
		}

#ifdef CONFIG_INIT_STACKS
		size_t unused = 0;
		if (k_thread_stack_space_get(t, &unused) == 0) {
			printk("!! HWM    : %u B used of %u B\n",
			       size - (uint32_t)unused, size);
		}
#endif
	}

	printk("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n\n");
	k_fatal_halt(reason);
}

/* ── stack / priority ──────────────────────────────────────────────────── */
/* App threads: k_sem_take + printk(4 args).  1 KB has ~300 B headroom.    */
/* mock_irq:  k_sleep + sys_rand32_get + printk(4 args) + k_sem_give.      */
/*            2 KB has ample headroom even under -Og.                       */
#define APP_STACK_SIZE 1024
#define IRQ_STACK_SIZE 2048

#define APP_PRIO 5 /* preemptive; all three run at equal priority */
#define IRQ_PRIO 3 /* higher than app threads so give() is prompt */

/* ── per-thread config ─────────────────────────────────────────────────── */
struct thread_cfg {
  const char *name;
};

static const struct thread_cfg cfgs[3] = {
    {"thread_0"},
    {"thread_1"},
    {"thread_2"},
};

/* ── semaphores — one per application thread ───────────────────────────── */
K_SEM_DEFINE(sem0, 0, 1);
K_SEM_DEFINE(sem1, 0, 1);
K_SEM_DEFINE(sem2, 0, 1);

static struct k_sem *const sems[3] = {&sem0, &sem1, &sem2};

/* ── stacks ────────────────────────────────────────────────────────────── */
K_THREAD_STACK_ARRAY_DEFINE(app_stacks, 3, APP_STACK_SIZE);
K_THREAD_STACK_DEFINE(irq_stack, IRQ_STACK_SIZE);

static struct k_thread app_threads[3];
static struct k_thread irq_thread;

/* ── stack high-water mark helper ─────────────────────────────────────── */
static void log_stack_hwm(const char *name)
{
#ifdef CONFIG_INIT_STACKS
	size_t unused = 0;
	struct k_thread *t = k_current_get();

	if (k_thread_stack_space_get(t, &unused) == 0) {
		uint32_t size = (uint32_t)t->stack_info.size;
		uint32_t used = size - (uint32_t)unused;

		printk("[%6u ms] [%s] stack: %u / %u B used (%u%% full)\n",
		       k_uptime_get_32(), name, used, size,
		       used * 100u / size);
	}
#else
	ARG_UNUSED(name);
#endif
}

/* ── shared application thread body ───────────────────────────────────── */
static void thread_body(const struct thread_cfg *cfg, struct k_sem *sem) {
  uint32_t wake_count = 0;

  while (1) {
    /* ── WAIT ─────────────────────────────────────────────── */
    printk("[%6u ms] [%s] waiting on semaphore (count=%u)\n", k_uptime_get_32(),
           cfg->name, k_sem_count_get(sem));

    k_sem_take(sem, K_FOREVER);

    /* ── AWAKE ────────────────────────────────────────────── */
    wake_count++;
    printk("[%6u ms] [%s] WAKE #%u - semaphore taken (count now=%u)\n",
           k_uptime_get_32(), cfg->name, wake_count, k_sem_count_get(sem));

    log_stack_hwm(cfg->name);

    /* Do a small amount of "work" so SystemView shows the thread
     * actually running before it blocks again.                  */
    k_busy_wait(500); /* 500 us of work */
  }
}

/* Individual entry points — unconditional GDB breakpoint targets:
 *   (gdb) break thread_0_fn                                               */
static void thread_0_fn(void *p1, void *p2, void *p3) {
  ARG_UNUSED(p2);
  ARG_UNUSED(p3);
  thread_body((const struct thread_cfg *)p1, sems[0]);
}
static void thread_1_fn(void *p1, void *p2, void *p3) {
  ARG_UNUSED(p2);
  ARG_UNUSED(p3);
  thread_body((const struct thread_cfg *)p1, sems[1]);
}
static void thread_2_fn(void *p1, void *p2, void *p3) {
  ARG_UNUSED(p2);
  ARG_UNUSED(p3);
  thread_body((const struct thread_cfg *)p1, sems[2]);
}

static k_thread_entry_t const entry_fns[3] = {
    thread_0_fn,
    thread_1_fn,
    thread_2_fn,
};

/* ── mock-interrupt thread ─────────────────────────────────────────────── */
static void mock_irq_fn(void *p1, void *p2, void *p3) {
  ARG_UNUSED(p1);
  ARG_UNUSED(p2);
  ARG_UNUSED(p3);

  uint32_t fire_count = 0;

  while (1) {
    for (int i = 0; i < 3; i++) {
      uint32_t delay_ms = (sys_rand32_get() % 1000U);

      printk("[%6u ms] [mock_irq] next: %s in %u ms\n", k_uptime_get_32(),
             cfgs[i].name, delay_ms);

      k_sleep(K_MSEC(delay_ms));

      fire_count++;
      printk("[%6u ms] [mock_irq] --> k_sem_give(%s)  "
             "fire #%u  sem_count_before=%u\n",
             k_uptime_get_32(), cfgs[i].name, fire_count,
             k_sem_count_get(sems[i]));

      k_sem_give(sems[i]);
      log_stack_hwm("mock_irq");
    }
  }
}

/* ── main ──────────────────────────────────────────────────────────────── */
int main(void) {
  printk("\n");
  printk("========================================\n");
  printk(" Hello World - %s\n", CONFIG_BOARD);
  printk(" Uptime clock: %u Hz\n", CONFIG_SYS_CLOCK_TICKS_PER_SEC);
  printk(" App thread stack : %d B\n", APP_STACK_SIZE);
  printk(" IRQ thread stack : %d B\n", IRQ_STACK_SIZE);
#ifdef CONFIG_SEGGER_SYSTEMVIEW
  printk(" SystemView       : ENABLED (RTT channel 1)\n");
#else
  printk(" SystemView       : disabled\n");
#endif
  printk("========================================\n\n");

  /* Application threads */
  for (int i = 0; i < 3; i++) {
    k_thread_create(&app_threads[i], app_stacks[i], APP_STACK_SIZE,
                    entry_fns[i], (void *)&cfgs[i], NULL, NULL, APP_PRIO, 0,
                    K_NO_WAIT);
    k_thread_name_set(&app_threads[i], cfgs[i].name);
  }

  /* Mock-IRQ thread */
  k_thread_create(&irq_thread, irq_stack, IRQ_STACK_SIZE, mock_irq_fn, NULL,
                  NULL, NULL, IRQ_PRIO, 0, K_NO_WAIT);
  k_thread_name_set(&irq_thread, "mock_irq");

  printk("[%6u ms] main: all threads spawned\n\n", k_uptime_get_32());
  return 0;
}
