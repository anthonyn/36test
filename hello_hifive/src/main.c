/*
 * Hello World with 4 configurable threads
 * SiFive HiFive1 Rev B / qemu_riscv32
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>

/* Stack size for each thread — 2 KB keeps headroom with debug builds */
#define STACK_SIZE  2048
#define THREAD_PRIO 5

/* Per-thread configuration: name + sleep period from Kconfig */
struct thread_cfg {
	const char  *name;
	k_timeout_t  period;
};

static const struct thread_cfg cfgs[4] = {
	{ "thread_0", K_MSEC(CONFIG_THREAD0_PERIOD_MS) },
	{ "thread_1", K_MSEC(CONFIG_THREAD1_PERIOD_MS) },
	{ "thread_2", K_MSEC(CONFIG_THREAD2_PERIOD_MS) },
	{ "thread_3", K_MSEC(CONFIG_THREAD3_PERIOD_MS) },
};

/* One stack block per thread */
K_THREAD_STACK_ARRAY_DEFINE(stacks, 4, STACK_SIZE);
static struct k_thread threads[4];

/*
 * Each thread has its own named entry function so you can set a plain
 * unconditional breakpoint on any one of them:
 *   (gdb) break thread_0_fn
 *
 * They all call the shared thread_body() for the actual work.
 */
static void thread_body(const struct thread_cfg *cfg)
{
	uint32_t tick = 0;

	while (1) {
		printk("[%s] tick %u  (period %u ms)\n",
		       cfg->name, tick++,
		       (uint32_t)k_ticks_to_ms_floor64(cfg->period.ticks));
		k_sleep(cfg->period);
	}
}

static void thread_0_fn(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p2); ARG_UNUSED(p3);
	thread_body((const struct thread_cfg *)p1);
}

static void thread_1_fn(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p2); ARG_UNUSED(p3);
	thread_body((const struct thread_cfg *)p1);
}

static void thread_2_fn(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p2); ARG_UNUSED(p3);
	thread_body((const struct thread_cfg *)p1);
}

static void thread_3_fn(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p2); ARG_UNUSED(p3);
	thread_body((const struct thread_cfg *)p1);
}

/* Map index → entry function */
static k_thread_entry_t entry_fns[4] = {
	thread_0_fn, thread_1_fn, thread_2_fn, thread_3_fn,
};

int main(void)
{
	printk("Hello World! Running on %s\n", CONFIG_BOARD);
	printk("Spawning 4 threads...\n\n");

	for (int i = 0; i < 4; i++) {
		k_thread_create(&threads[i],
				stacks[i], STACK_SIZE,
				entry_fns[i],
				(void *)&cfgs[i], NULL, NULL,
				THREAD_PRIO, 0, K_NO_WAIT);
		k_thread_name_set(&threads[i], cfgs[i].name);
	}

	return 0;
}
