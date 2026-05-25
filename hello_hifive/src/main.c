/*
 * Hello World — 4 semaphore-driven threads + mock-interrupt thread
 * SiFive HiFive1 Rev B / qemu_riscv32
 * SPDX-License-Identifier: Apache-2.0
 *
 * Each application thread blocks on its own semaphore (K_FOREVER).
 * A fifth "mock IRQ" thread fires each semaphore at a random interval
 * between 1 s and 5 s, simulating hardware interrupts posting work.
 */

#include <zephyr/kernel.h>
#include <zephyr/random/random.h>

/* ── stack / priority ──────────────────────────────────────────────── */
#define STACK_SIZE 2048
#define APP_PRIO 5 /* application threads */
#define IRQ_PRIO 3 /* mock-IRQ thread runs at higher priority  */
                   /* so it can preempt sleeping app threads   */

/* ── per-thread configuration ─────────────────────────────────────── */
struct thread_cfg {
  const char *name;
};

static const struct thread_cfg cfgs[4] = {
    {"thread_0"},
    {"thread_1"},
    {"thread_2"},
    {"thread_3"},
};

/* ── semaphores — one per application thread ───────────────────────── */
/*   K_SEM_DEFINE(name, initial_count, limit)                          */
K_SEM_DEFINE(sem0, 0, 1);
K_SEM_DEFINE(sem1, 0, 1);
K_SEM_DEFINE(sem2, 0, 1);
K_SEM_DEFINE(sem3, 0, 1);

static struct k_sem *const sems[4] = {&sem0, &sem1, &sem2, &sem3};

/* ── stacks ────────────────────────────────────────────────────────── */
K_THREAD_STACK_ARRAY_DEFINE(app_stacks, 4, STACK_SIZE);
K_THREAD_STACK_DEFINE(irq_stack, STACK_SIZE);

static struct k_thread app_threads[4];
static struct k_thread irq_thread;

/* ── shared application thread body ───────────────────────────────── */
static void thread_body(const struct thread_cfg *cfg, struct k_sem *sem) {
  uint32_t tick = 0;

  while (1) {
    /* Block until the mock-IRQ thread (or a real ISR) fires us */
    k_sem_take(sem, K_FOREVER);

    printk("[%s] wake #%u  (semaphore signalled)\n", cfg->name, tick++);
  }
}

/* Individual entry points — lets you set an unconditional GDB breakpoint
 * on any one thread:  (gdb) break thread_0_fn                         */
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
static void thread_3_fn(void *p1, void *p2, void *p3) {
  ARG_UNUSED(p2);
  ARG_UNUSED(p3);
  thread_body((const struct thread_cfg *)p1, sems[3]);
}

static k_thread_entry_t entry_fns[4] = {
    thread_0_fn,
    thread_1_fn,
    thread_2_fn,
    thread_3_fn,
};

/* ── mock-interrupt thread ─────────────────────────────────────────── */
/*
 * Cycles through all four semaphores in order, sleeping a random
 * duration between 1 000 ms and 5 000 ms before each k_sem_give().
 *
 * In a real application this role is played by an ISR calling
 * k_sem_give() from interrupt context.
 */
static void mock_irq_fn(void *p1, void *p2, void *p3) {
  ARG_UNUSED(p1);
  ARG_UNUSED(p2);
  ARG_UNUSED(p3);

  uint32_t fire_count = 0;

  while (1) {
    for (int i = 0; i < 4; i++) {
      /* Random delay 1 000–5 000 ms */
      uint32_t delay_ms = 1000U + (sys_rand32_get() % 1000U);

      printk("[mock_irq] sleeping %u ms before firing %s\n", delay_ms,
             cfgs[i].name);

      k_sleep(K_MSEC(delay_ms));

      printk("[mock_irq] --> k_sem_give → %s  (fire #%u)\n", cfgs[i].name,
             ++fire_count);

      k_sem_give(sems[i]);
    }
  }
}

/* ── main ──────────────────────────────────────────────────────────── */
int main(void) {
  printk("Hello World! Running on %s\n", CONFIG_BOARD);
  printk("Spawning 4 app threads + 1 mock-IRQ thread...\n\n");

  /* Application threads — block on their semaphore from the start */
  for (int i = 0; i < 4; i++) {
    k_thread_create(&app_threads[i], app_stacks[i], STACK_SIZE, entry_fns[i],
                    (void *)&cfgs[i], NULL, NULL, APP_PRIO, 0, K_NO_WAIT);
    k_thread_name_set(&app_threads[i], cfgs[i].name);
  }

  /* Mock-IRQ thread — higher priority so it runs promptly */
  k_thread_create(&irq_thread, irq_stack, STACK_SIZE, mock_irq_fn, NULL, NULL,
                  NULL, IRQ_PRIO, 0, K_NO_WAIT);
  k_thread_name_set(&irq_thread, "mock_irq");

  return 0;
}
