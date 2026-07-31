// relay_alias.go — the TUI's local names for the shared relay package.
//
// relay.go used to live in this package. It moved to relay/ so dispatch-ircd
// could import the same on-disk contract instead of reimplementing it; these
// aliases keep the UI code (model.go, view.go) reading the way it always has.
// Aliases, not wrappers — Message here *is* relay.Message, so the two binaries
// can never drift on the message shape.
package main

import "github.com/justinstimatze/mcp-dispatch/tui/relay"

type (
	Message  = relay.Message
	Agent    = relay.Agent
	Snapshot = relay.Snapshot
)

var (
	Load       = relay.Load
	Send       = relay.Send
	AckInbox   = relay.AckInbox
	RelayDir   = relay.RelayDir
	GitRepoDir = relay.GitRepoDir
	loadConfig = relay.LoadConfig
	expandUser = relay.ExpandUser
	validID    = relay.ValidID
	project    = relay.Project
)
