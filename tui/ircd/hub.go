// hub.go — the relay side of the gateway: one poller, many IRC clients.
//
// The gateway is an observer and a sender, exactly like dispatch-tui: it reads
// every inbox on the relay (and the git lanes when configured) and it writes
// messages the way a session would. It deliberately does NOT claim presence, so
// agents never see a human's IRC client as a live session — the relay's
// presence semantics stay owned by the MCP server alone.
//
// One poller feeds every connected client, so N clients cost one relay scan per
// interval rather than N.
package main

import (
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/justinstimatze/mcp-dispatch/tui/relay"
)

const maxTranscript = 2000

type hub struct {
	relayDir string
	repoDir  string
	readGit  bool
	interval time.Duration
	history  int

	mu         sync.RWMutex
	snap       relay.Snapshot
	transcript []relay.Message
	seen       map[string]bool
	clients    map[*session]struct{}
	nicks      map[string]bool
}

// claimNick reserves a nick for one client. Two clients answering to the same
// nick would each see half the DMs addressed to it, so the second is refused
// (ERR_NICKNAMEINUSE) exactly as a real server would.
func (h *hub) claimNick(n string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.nicks[n] {
		return false
	}
	h.nicks[n] = true
	return true
}

func (h *hub) releaseNick(n string) {
	h.mu.Lock()
	delete(h.nicks, n)
	h.mu.Unlock()
}

func newHub(relayDir, repoDir string, readGit bool, interval time.Duration, history int) *hub {
	return &hub{
		relayDir: relayDir, repoDir: repoDir, readGit: readGit,
		interval: interval, history: history,
		seen:    map[string]bool{},
		clients: map[*session]struct{}{},
		nicks:   map[string]bool{},
	}
}

func (h *hub) add(s *session) {
	h.mu.Lock()
	h.clients[s] = struct{}{}
	h.mu.Unlock()
}

func (h *hub) remove(s *session) {
	h.mu.Lock()
	delete(h.clients, s)
	h.mu.Unlock()
}

// closeAll says goodbye to every connected client, then closes their
// connections. Shutdown used to drop the listeners and let the process exit,
// which killed live connections with the process: no IRC `ERROR` line and, on
// TLS, no close_notify — so a strict client reports a truncation error rather
// than a disconnect. rustls says "peer closed connection without sending TLS
// close_notify". Under systemd the gateway is restarted for every upgrade, so
// that turned routine restarts into something that reads like a fault.
func (h *hub) closeAll(reason string) {
	h.mu.RLock()
	live := make([]*session, 0, len(h.clients))
	for s := range h.clients {
		live = append(live, s)
	}
	h.mu.RUnlock()

	for _, s := range live {
		s.sendRaw("ERROR :" + reason)
	}
	for _, s := range live {
		s.drain() // let the ERROR reach the wire before the socket goes
		s.close() // *tls.Conn.Close() emits close_notify; a raw fd exit does not
	}
}

func (h *hub) clientCount() int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.clients)
}

func (h *hub) snapshot() relay.Snapshot {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.snap
}

// recent returns the last n transcript entries, oldest first — replayed to a
// client on JOIN so a channel isn't empty until the next message arrives.
func (h *hub) recent(n int) []relay.Message {
	h.mu.RLock()
	defer h.mu.RUnlock()
	if n > len(h.transcript) {
		n = len(h.transcript)
	}
	out := make([]relay.Message, n)
	copy(out, h.transcript[len(h.transcript)-n:])
	return out
}

// run polls the relay until ctx is done. The first pass is absorbed silently:
// everything already on disk becomes history rather than a burst of "new"
// traffic replayed into whoever happens to be connected at startup.
func (h *hub) run(done <-chan struct{}) {
	h.poll(true)
	t := time.NewTicker(h.interval)
	defer t.Stop()
	for {
		select {
		case <-done:
			return
		case <-t.C:
			h.poll(false)
		}
	}
}

func (h *hub) poll(initial bool) {
	snap := relay.Load(h.relayDir, h.repoDir, h.readGit)

	h.mu.Lock()
	h.snap = snap
	var fresh []relay.Message
	for _, m := range snap.Messages {
		if m.ID == "" || h.seen[m.ID] {
			continue
		}
		h.seen[m.ID] = true
		h.transcript = append(h.transcript, m)
		fresh = append(fresh, m)
	}
	sort.SliceStable(h.transcript, func(i, j int) bool {
		return h.transcript[i].SortMS < h.transcript[j].SortMS
	})
	if len(h.transcript) > maxTranscript {
		h.transcript = h.transcript[len(h.transcript)-maxTranscript:]
	}
	clients := make([]*session, 0, len(h.clients))
	for c := range h.clients {
		clients = append(clients, c)
	}
	h.mu.Unlock()

	if initial || len(fresh) == 0 {
		return
	}
	sort.SliceStable(fresh, func(i, j int) bool { return fresh[i].SortMS < fresh[j].SortMS })
	for _, c := range clients {
		for _, m := range fresh {
			c.onRelayMessage(m)
		}
	}
}

// channels lists every channel currently visible — from live subscriptions and
// from channel-addressed traffic in the transcript.
func (h *hub) channels() []string {
	h.mu.RLock()
	defer h.mu.RUnlock()
	set := map[string]bool{}
	for _, a := range h.snap.Agents {
		for _, c := range a.Channels {
			set[c] = true
		}
	}
	for _, m := range h.transcript {
		if strings.HasPrefix(m.To, "#") {
			set[strings.TrimPrefix(m.To, "#")] = true
		}
	}
	out := make([]string, 0, len(set))
	for c := range set {
		if relay.ValidID(c) {
			out = append(out, c)
		}
	}
	sort.Strings(out)
	return out
}

// membersOf returns the stable nicks (projects) subscribed to a channel.
func (h *hub) membersOf(channel string) []string {
	ch := strings.TrimPrefix(channel, "#")
	h.mu.RLock()
	defer h.mu.RUnlock()
	set := map[string]bool{}
	for _, a := range h.snap.Agents {
		for _, c := range a.Channels {
			if c == ch {
				set[relay.Project(a.ID)] = true
			}
		}
	}
	return sortedKeys(set)
}

// agents returns every nick the gateway knows about, live first.
func (h *hub) agents() []relay.Agent {
	h.mu.RLock()
	defer h.mu.RUnlock()
	out := make([]relay.Agent, len(h.snap.Agents))
	copy(out, h.snap.Agents)
	return out
}

// allNicks collapses the roster to stable project nicks.
func (h *hub) allNicks() []string {
	set := map[string]bool{}
	for _, a := range h.agents() {
		if p := relay.Project(a.ID); relay.ValidID(p) {
			set[p] = true
		}
	}
	return sortedKeys(set)
}

// send writes a message to the relay as `from`, using the current snapshot for
// channel fan-out. Returns the number of inboxes written.
func (h *hub) send(from, target, content, priority string) (int, error) {
	return relay.Send(h.relayDir, from, target, content, h.snapshot(), priority)
}

func (h *hub) ack(nick string) (int, error) { return relay.AckInbox(h.relayDir, nick) }

func (h *hub) inbox(nick string) ([]relay.Message, error) {
	return relay.LoadInbox(h.relayDir, nick)
}

func (h *hub) ackIDs(nick string, ids []string) (int, []string, error) {
	return relay.AckMessages(h.relayDir, nick, ids)
}

// tasks reads the task store. The gateway is read-only here on purpose:
// creating and claiming are the MCP server's job (claiming in particular is an
// O_EXCL race that must have exactly one implementation), so a human watching
// from an IRC client can see the board without becoming a second writer to it.
func (h *hub) tasks() []relay.Task { return relay.LoadTasks(h.relayDir) }

func sortedKeys(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
