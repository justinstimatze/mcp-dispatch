# dispatch-tui — the git tag is the single source of truth for the version.
# `git describe` yields the mcp-dispatch repo tag (the TUI ships with the repo),
# baked into the binary via -ldflags; a plain `go build` falls back to the vcs
# revision through buildVersion(). See main.go.
VERSION := $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LDFLAGS := -ldflags "-X main.version=$(VERSION)"

.PHONY: build build-tui build-ircd install test vet fmt lint tidy run dump

build: build-tui build-ircd  ## build both binaries with the version baked in

build-tui:      ## build ./dispatch-tui
	go build $(LDFLAGS) -o dispatch-tui .

build-ircd:     ## build ./dispatch-ircd (the IRC gateway)
	go build $(LDFLAGS) -o dispatch-ircd ./ircd

install:        ## go install with the version baked in
	go install $(LDFLAGS) .

test:           ## run the test suite
	go test ./...

vet:            ## go vet
	go vet ./...

fmt:            ## format all sources
	gofmt -w .

lint: fmt vet   ## format + vet

tidy:           ## tidy the module
	go mod tidy

run: build-tui  ## build and run the TUI against the resolved relay
	./dispatch-tui

dump: build-tui ## render one frame to stdout (no TTY)
	./dispatch-tui --dump

ircd: build-ircd ## build and run the IRC gateway
	./dispatch-ircd
