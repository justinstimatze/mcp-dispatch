package relay

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
)

// Task is one claimable work item, as written by the MCP server's task tool.
// Read-only here: claiming is an O_EXCL race that must have exactly one
// implementation, and this is not it.
type Task struct {
	ID        string `json:"id"`
	Title     string `json:"title"`
	Detail    string `json:"detail"`
	CreatedBy string `json:"created_by"`
	CreatedAt string `json:"created_at"`
	State     string `json:"state"`
	ClaimedBy string `json:"claimed_by"`
	ClaimedAt string `json:"claimed_at"`
	DoneAt    string `json:"done_at"`
	Target    string `json:"target"`
}

// LoadTasks reads the task board, oldest first. A missing store is not an
// error — it just means nobody has created a task yet.
func LoadTasks(relayDir string) []Task {
	var out []Task
	files, err := filepath.Glob(filepath.Join(relayDir, ".tasks", "task-*.json"))
	if err != nil {
		return out
	}
	for _, f := range files {
		data, err := os.ReadFile(f) //nolint:gosec // enumerated relay file
		if err != nil {
			continue
		}
		var t Task
		if json.Unmarshal(data, &t) != nil || t.ID == "" {
			continue
		}
		out = append(out, t)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].CreatedAt < out[j].CreatedAt })
	return out
}
