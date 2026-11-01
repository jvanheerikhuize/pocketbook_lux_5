# pbtk — PocketBook toolkit
#
#   make test     host test suite (builds the demo app against the InkView stub)
#   make device   build the demo app for the reader (needs the SDK)
#   make install  install the `pbtk` command (uv tool, editable)

.PHONY: test device install clean

test:
	$(MAKE) -s -C templates/app host
	cd host && PYTHONPATH=. python3 -m unittest discover -s tests -t tests

device:
	$(MAKE) -C templates/app pocketbook

install:
	uv tool install --force --editable ./host

clean:
	$(MAKE) -C templates/app clean
