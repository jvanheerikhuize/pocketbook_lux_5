# pbtk-demo: the starter app. Copy this folder, rename APP, keep the include.
#
#   make host        build/<app>-host  (runs on the PC against the InkView stub)
#   make pocketbook  dist/applications/<app>/ + <app>.app launcher
#   pbtk deploy      copies dist/applications/ to the reader (see pbtk.toml)

APP := pbtk-demo
PBTK ?= $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../..)
include $(PBTK)/sdk/pocketbook.mk

SRC := main.c
BUNDLE := dist/applications/$(APP)

.PHONY: all host pocketbook clean
all: host

host: build/$(APP)-host
build/$(APP)-host: $(SRC) $(PBAGENT_SRC) $(HOST_STUB_SRC)
	@mkdir -p build
	$(HOST_CC) $(HOST_CFLAGS) $(PBAGENT_CFLAGS) -o $@ $(SRC) $(PBAGENT_SRC) $(HOST_STUB_SRC) $(HOST_LDLIBS)

pocketbook: $(BUNDLE)/$(APP)
$(BUNDLE)/$(APP): $(SRC) $(PBAGENT_SRC) launcher.app
ifeq ($(PB_HAVE_SDK),1)
	@mkdir -p $(BUNDLE)
	$(PB_CC) $(PB_CFLAGS) $(PBAGENT_CFLAGS) -o $@ $(SRC) $(PBAGENT_SRC) $(PB_LDLIBS)
	$(PB_STRIP) $@
	sed 's/@APP@/$(APP)/g' launcher.app > dist/applications/$(APP).app
else
	@echo "pocketbook: SDK not found at $(PB_SDK); device build skipped"
endif

clean:
	rm -rf build dist
