// The terminal viewer, for a headless box with no browser and no Prometheus.
// `web/` is the other viewer and shares nothing with this one: it is Python, it
// has its own dependencies, and neither module builds or tests the other.
//
// **Two dependencies, and no others** - `gopkg.in/yaml.v3` for the dashboard
// file and `golang.org/x/term` for the terminal's width. Both are already
// saguin's, at the versions saguin holds them at, so an operator building this
// on the box that runs the broker downloads nothing new. No Prometheus client
// library: this reads the three line shapes saguin emits and would gain a
// parser for a format it never sees. No TUI library: the screen is
// text/tabwriter and one call for the width. Each arrives in the commit that
// first needs it, so the require block below grows rather than being declared
// ahead of the code.
module github.com/ifnesi/saguin-viewer

go 1.25.0

require (
	golang.org/x/term v0.45.0
	gopkg.in/yaml.v3 v3.0.1
)

require golang.org/x/sys v0.47.0 // indirect
