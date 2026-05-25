# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(hello_hifive)

target_sources(app PRIVATE src/main.c)

# Generate per-function stack frame reports alongside object files.
# After building, inspect:
#   cat hello_hifive/build/hello_hifive/src/CMakeFiles/app.dir/main.c.su
target_compile_options(app PRIVATE -fstack-usage)
