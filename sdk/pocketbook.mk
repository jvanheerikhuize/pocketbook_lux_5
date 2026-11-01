# pbtk shared build settings for PocketBook (PB628, firmware 6.x, SDK-B288).
#
# Include from any app's Makefile:
#     PBTK ?= $(HOME)/Repos/pocketbook_lux_5
#     include $(PBTK)/sdk/pocketbook.mk
#
# Then use:
#     $(PB_CC) $(PB_CFLAGS) $(PBAGENT_CFLAGS) -o app app.c $(PBAGENT_SRC) $(PB_LDLIBS)
#     $(HOST_CC) $(HOST_CFLAGS) -o app-host app.c $(PBAGENT_SRC) $(HOST_STUB_SRC) $(HOST_LDLIBS)
#
# Facts behind these values (from inkquest, confirmed on the device):
#   - the firmware has only the soft-float loader and glibc 2.23, so use the
#     SDK-B288 `arm-obreey-linux-gnueabi` toolchain, never a gnueabihf one;
#   - that compiler needs the SDK's own host libraries (libmpfr.so.4) on
#     LD_LIBRARY_PATH for the compiler process only;
#   - vendor flags: -fsigned-char -march=armv7-a -mfpu=neon -mfloat-abi=softfp;
#   - GCC 6 warns on a local named `main` (-Wmain).

PBTK_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)

PB_SDK ?= $(HOME)/opt/pocketbook-sdk/SDK_6.3.0-6.5/SDK-B288
PB_CROSS ?= $(PB_SDK)/usr/bin/arm-obreey-linux-gnueabi-
PB_ENV := env LD_LIBRARY_PATH=$(PB_SDK)/usr/lib
PB_CC := $(PB_ENV) $(PB_CROSS)gcc
PB_CXX := $(PB_ENV) $(PB_CROSS)g++
PB_AR := $(PB_ENV) $(PB_CROSS)ar
PB_STRIP := $(PB_ENV) $(PB_CROSS)strip
PB_ARCH := -fsigned-char -march=armv7-a -mfpu=neon -mfloat-abi=softfp
PB_CFLAGS ?= -O2 -std=gnu11 -Wall -Wextra -Wno-main $(PB_ARCH) -DPOCKETBOOK
PB_CXXFLAGS ?= -O2 -std=gnu++14 -Wall -Wno-main $(PB_ARCH) -DPOCKETBOOK
# InkView and glibc stay dynamic (they come from the firmware); link C++
# runtimes statically with -static-libstdc++ -static-libgcc.
PB_LDLIBS ?= -linkview -ldl -lpthread -lm

# True when the toolchain is installed; lets Makefiles skip device builds cleanly.
PB_HAVE_SDK := $(shell test -x $(PB_CROSS)gcc && echo 1)

PBAGENT_DIR := $(PBTK_ROOT)/device/libpbagent
PBAGENT_SRC := $(PBAGENT_DIR)/pbagent.c
PBAGENT_CFLAGS := -I$(PBAGENT_DIR)

HOST_STUB_DIR := $(PBTK_ROOT)/device/host-stub
HOST_STUB_SRC := $(HOST_STUB_DIR)/inkview_stub.c
HOST_CC ?= cc
HOST_CFLAGS ?= -O1 -g -std=gnu11 -Wall -Wextra -I$(HOST_STUB_DIR) -DPBTK_HOST
HOST_LDLIBS ?= -lpthread -lm
