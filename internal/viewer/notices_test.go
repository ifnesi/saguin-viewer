package viewer

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// The notices file is held to the module cache rather than trusted.
//
// **A licence text that was typed is a licence text that can be wrong**, and a
// wrong one is worse than none: it says the project asserts terms nobody granted.
// So this reads what `go list -deps` says is linked into the binary, reads each
// module's own LICENSE, NOTICE and PATENTS out of the cache, and requires the
// notices file to carry them verbatim.
//
// It is also what catches a new dependency. Adding one is a two-line change to
// go.mod and nothing else says its licence now has to travel.
func TestTheNoticesCarryEveryModuleLinkedIntoTheBinary(t *testing.T) {
	root := filepath.Join("..", "..")
	notices := filepath.Join(root, "cmd", "saguin-viewer", "THIRD-PARTY-NOTICES.md")
	body, err := os.ReadFile(notices)
	if err != nil {
		t.Fatal(err)
	}
	text := string(body)

	// The modules that reach the binary, with where the cache holds each. The
	// module's own path is left out: this file is about code the project does not
	// own.
	// Run from the module root: a test's working directory is its own package,
	// where ./cmd/saguin-viewer does not exist.
	list := exec.Command("go", "list", "-deps", "-f",
		"{{if .Module}}{{.Module.Path}}\t{{.Module.Version}}\t{{.Module.Dir}}{{end}}",
		"./cmd/saguin-viewer")
	list.Dir = root
	out, err := list.Output()
	if err != nil {
		var said string
		if e, ok := err.(*exec.ExitError); ok {
			said = strings.TrimSpace(string(e.Stderr))
		}
		t.Fatalf("asking go what is linked in: %v\n%s", err, said)
	}

	seen := map[string]bool{}
	checked := 0
	for _, line := range strings.Split(string(out), "\n") {
		f := strings.Split(strings.TrimSpace(line), "\t")
		if len(f) != 3 || f[0] == "" || f[0] == "github.com/ifnesi/saguin-viewer" {
			continue
		}
		path, version, dir := f[0], f[1], f[2]
		if seen[path] {
			continue
		}
		seen[path] = true
		checked++

		// The heading, with the version, so an upgrade that left the licence text
		// alone is still a change to this file.
		if want := "### " + path + " " + version; !strings.Contains(text, want) {
			t.Errorf("the notices do not carry %q - a module is linked into the "+
				"binary and its licence does not travel with it", want)
			continue
		}
		if dir == "" {
			t.Errorf("%s: go reports no directory, so its licence cannot be "+
				"compared", path)
			continue
		}
		found := 0
		for _, name := range []string{"LICENSE", "NOTICE", "PATENTS"} {
			licence, err := os.ReadFile(filepath.Join(dir, name))
			if err != nil {
				continue
			}
			found++
			if !strings.Contains(text, strings.TrimSpace(string(licence))) {
				t.Errorf("%s: the %s in the module cache is not in the notices "+
					"file verbatim. Regenerate it rather than editing the text: "+
					"a licence somebody retyped is one that can be wrong, and a "+
					"wrong one asserts terms nobody granted", path, name)
			}
		}
		if found == 0 {
			t.Errorf("%s has no LICENSE, NOTICE or PATENTS in the cache at %s, so "+
				"this case checked nothing about it", path, dir)
		}
	}
	// Count what was examined. `go list` answering nothing would leave every
	// assertion above unrun and this case green over a file that carries nothing.
	if checked < 3 {
		t.Fatalf("only %d modules were checked; the binary links at least three "+
			"that this project does not own", checked)
	}
}

// TestTheNoticesSayWhatIsDeliberatelyAbsent, because the Python packages'
// absence is a decision and an undocumented decision gets corrected by the next
// reader - who then has two lists to keep in step with requirements.txt.
func TestTheNoticesSayWhatIsDeliberatelyAbsent(t *testing.T) {
	body, err := os.ReadFile(filepath.Join("..", "..", "cmd", "saguin-viewer",
		"THIRD-PARTY-NOTICES.md"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"What is deliberately not here",
		"requirements.txt", "redistributes none"} {
		if !strings.Contains(string(body), want) {
			t.Errorf("the notices do not say %q", want)
		}
	}
}
