# dispatch-tui — the git tag is the single source of truth for the version.
# `git describe` yields the mcp-dispatch repo tag (the TUI ships with the repo),
# baked into the binary via -ldflags; a plain `go build` falls back to the vcs
# revision through buildVersion(). See main.go.
VERSION := $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LDFLAGS := -ldflags "-X main.version=$(VERSION)"

.PHONY: build install test vet fmt lint tidy run dump

build:          ## build ./dispatch-tui with the version baked in
	go build $(LDFLAGS) -o dispatch-tui .

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

run: build      ## build and run against the resolved relay
	./dispatch-tui

dump: build     ## render one frame to stdout (no TTY)
	./dispatch-tui --dump
