package viewer

import (
	"fmt"
	"go/ast"
	// Aliased: this package has its own `parser` and `token`, in the expression
	// reader.
	goparser "go/parser"
	gotoken "go/token"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"
)

func writeDashboard(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "dashboard.yaml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

const goodDashboard = `
refresh: 60s
groups:
  - title: Clients
    layout: columns
    metrics: [saguin_connections, saguin_subscriptions]
  - title: Channels
    layout: rows
    by: channel
    metrics: [saguin_channel_records, saguin_channel_bytes]
`

// TestTheExampleInTheItemLoads, which is the file shape this was specified with:
// a refresh, a columns group of broker-wide numbers, and a rows group with one
// row per channel.
func TestTheExampleInTheItemLoads(t *testing.T) {
	d, err := Load(writeDashboard(t, goodDashboard))
	if err != nil {
		t.Fatalf("the specified shape was refused: %v", err)
	}
	if d.Written() != time.Minute {
		t.Errorf("refresh is %v, want 1m", d.Written())
	}
	if len(d.Groups) != 2 {
		t.Fatalf("%d groups, want 2", len(d.Groups))
	}
	if g := d.Groups[0]; g.Title != "Clients" || g.Layout != LayoutColumns || g.By != "" {
		t.Errorf("first group is %+v", g)
	}
	if g := d.Groups[1]; g.By != "channel" || g.Layout != LayoutRows {
		t.Errorf("second group is %+v", g)
	}
	if got := d.Groups[1].Columns; len(got) != 2 ||
		got[0].Metric != "saguin_channel_records" {
		t.Errorf("second group's metrics are %v", headings(got))
	}
}

// TestEveryRefusalNamesWhatIsWrong. The item's rule: "An unknown key, an unknown
// layout, a metric the scrape does not carry, or a `by:` label that metric does
// not have, is refused by name at startup. Nothing is silently skipped." These
// are the ones a file can be refused for before a scrape exists; the two that
// need one are below.
func TestEveryRefusalNamesWhatIsWrong(t *testing.T) {
	for _, c := range []struct {
		name, body string
		says       []string
	}{
		{"an unknown key",
			"refresh: 60s\ngroups:\n  - title: A\n    layout: columns\n    metric: saguin_connections\n",
			[]string{"metric"}},
		{"an unknown top-level key",
			"refresh: 60s\ncolumns: 12\ngroups:\n  - title: A\n    layout: columns\n    metrics: [saguin_connections]\n",
			[]string{"columns"}},
		{"an unknown layout",
			"refresh: 60s\ngroups:\n  - title: A\n    layout: grid\n    metrics: [saguin_connections]\n",
			[]string{`"grid"`, "A", "columns", "rows"}},
		{"no layout at all",
			"refresh: 60s\ngroups:\n  - title: A\n    metrics: [saguin_connections]\n",
			[]string{"no `layout`", "A"}},
		{"a by on a columns group",
			"refresh: 60s\ngroups:\n  - title: A\n    layout: columns\n    by: channel\n    metrics: [saguin_channel_records]\n",
			[]string{"by: channel", "layout: columns", "A"}},
		{"a rows group with no by",
			"refresh: 60s\ngroups:\n  - title: A\n    layout: rows\n    metrics: [saguin_channel_records]\n",
			[]string{"no `by`", "A"}},
		{"no metrics",
			"refresh: 60s\ngroups:\n  - title: A\n    layout: columns\n    metrics: []\n",
			[]string{"no `metrics`", "A"}},
		{"no title",
			"refresh: 60s\ngroups:\n  - layout: columns\n    metrics: [saguin_connections]\n",
			[]string{"no `title`", "group 1"}},
		{"the same metric twice",
			"refresh: 60s\ngroups:\n  - title: A\n    layout: columns\n    metrics: [saguin_connections, saguin_connections]\n",
			[]string{"saguin_connections twice", "A"}},
		{"no refresh",
			"groups:\n  - title: A\n    layout: columns\n    metrics: [saguin_connections]\n",
			[]string{"no `refresh`"}},
		{"a refresh that is not a duration",
			"refresh: often\ngroups:\n  - title: A\n    layout: columns\n    metrics: [saguin_connections]\n",
			[]string{`"often"`, "not a duration"}},
		{"a refresh of nothing",
			"refresh: 0s\ngroups:\n  - title: A\n    layout: columns\n    metrics: [saguin_connections]\n",
			[]string{"not a length of time"}},
		{"no groups",
			"refresh: 60s\n",
			[]string{"no `groups`"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			path := writeDashboard(t, c.body)
			_, err := Load(path)
			if err == nil {
				t.Fatalf("accepted:\n%s", c.body)
			}
			for _, want := range c.says {
				if !strings.Contains(err.Error(), want) {
					t.Errorf("the refusal does not name %q: %v", want, err)
				}
			}
			if !strings.Contains(err.Error(), path) {
				t.Errorf("the refusal does not name the file: %v", err)
			}
		})
	}
}

// TestAMetricTheBrokerDoesNotServeIsRefusedByName - the first of the two
// refusals that need a scrape to be judged against, because whether a metric
// exists is a question about the broker in front of the program rather than
// about the catalogue.
func TestAMetricTheBrokerDoesNotServeIsRefusedByName(t *testing.T) {
	s := fixture(t)
	d, err := Load(writeDashboard(t, `
refresh: 60s
groups:
  - title: Queues
    layout: columns
    metrics: [saguin_connections, saguin_queue_depth]
`))
	if err != nil {
		t.Fatal(err)
	}
	_, err = d.Validate(s)
	if err == nil {
		t.Fatal("a metric the scrape does not carry was accepted")
	}
	for _, want := range []string{"saguin_queue_depth", "Queues", "queue channel"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not mention %q: %v", want, err)
		}
	}
}

// TestAByLabelTheMetricDoesNotHaveIsRefusedAndTheRealOnesAreNamed, because the
// operator's next question is what the label is called.
func TestAByLabelTheMetricDoesNotHaveIsRefusedAndTheRealOnesAreNamed(t *testing.T) {
	s := fixture(t)
	d, err := Load(writeDashboard(t, `
refresh: 60s
groups:
  - title: Channels
    layout: rows
    by: queue
    metrics: [saguin_channel_records]
`))
	if err != nil {
		t.Fatal(err)
	}
	_, err = d.Validate(s)
	if err == nil {
		t.Fatal("`by: queue` on a metric labelled by channel was accepted")
	}
	for _, want := range []string{"by: queue", "saguin_channel_records", "channel"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not mention %q: %v", want, err)
		}
	}
}

// TestAByLabelIsRefusedWhenOnlySomeSamplesCarryIt. Every sample rather than any:
// a table with one row per label value would otherwise put the sample that has
// no such label into a row named for nothing.
func TestAByLabelIsRefusedWhenOnlySomeSamplesCarryIt(t *testing.T) {
	s, err := ParseScrape("saguin_odd{a=\"1\"} 1\nsaguin_odd{b=\"2\"} 2\n", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	d, err := Load(writeDashboard(t, `
refresh: 60s
groups:
  - title: Odd
    layout: rows
    by: a
    metrics: [saguin_odd]
`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := d.Validate(s); err == nil {
		t.Fatal("a label only some samples carry was accepted as a table's rows")
	}
}

// TestTheShippedDefaultReportsWhatIsAbsentRatherThanRefusing. The rule differs
// from a named file's and the reason is who chose the metric: the default asks
// for queues, bridges and refusals on every broker, and most brokers have no
// queue and no bridge. Refusing there would make the tool unusable out of the
// box on the ordinary deployment - but nothing is dropped quietly, so the names
// come back to be printed.
func TestTheShippedDefaultReportsWhatIsAbsentRatherThanRefusing(t *testing.T) {
	d, err := Default()
	if err != nil {
		t.Fatal(err)
	}
	// The fixture is a small scrape: it has the channel families and none of the
	// queue, bridge or session ones.
	absent, err := d.Validate(fixture(t))
	if err != nil {
		t.Fatalf("the default refused a broker rather than reporting: %v", err)
	}
	if len(absent) == 0 {
		t.Fatal("the fixture carries none of the queue or bridge families, so " +
			"something should have been reported absent - an empty list here " +
			"means this case is comparing nothing")
	}
	for _, want := range []string{"saguin_queue_depth", "saguin_bridge_connected"} {
		if !contains(absent, want) {
			t.Errorf("%s is not served by the fixture and was not reported: %v",
				want, absent)
		}
	}
	// And what was present is still drawn, rather than the group being lost with
	// the absent ones.
	var channels *Group
	for i := range d.Groups {
		if d.Groups[i].Title == "Channels" {
			channels = &d.Groups[i]
		}
	}
	if channels == nil {
		t.Fatal("no Channels group in the shipped default")
	}
	if !drawsMetric(channels, "saguin_channel_records") {
		t.Errorf("Channels kept %v, and the fixture serves saguin_channel_records",
			headings(channels.Drawn()))
	}
	// **Validating twice must give the same answer**, because the loop does: a
	// metric absent on one screen is asked for again on the next, so that a
	// queue channel's first record or a bridge's first connection starts being
	// drawn rather than being reported absent for the life of the process.
	again, err := d.Validate(fixture(t))
	if err != nil {
		t.Fatalf("a second Validate refused what the first accepted: %v", err)
	}
	if len(again) != len(absent) {
		t.Errorf("the first Validate reported %d absent and the second %d - the "+
			"configured list was narrowed rather than re-judged", len(absent),
			len(again))
	}
	if !drawsMetric(channels, "saguin_channel_records") {
		t.Errorf("after a second Validate, Channels kept %v",
			headings(channels.Drawn()))
	}
}

// drawsMetric says whether a group kept a column reading that metric.
func drawsMetric(g *Group, want string) bool {
	for _, c := range g.Drawn() {
		for _, name := range c.Reads() {
			if name == want {
				return true
			}
		}
	}
	return false
}

func contains(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

// TestTheRefreshIsClampedUpAndNeverDown. RFC 0005: a scrape arriving sooner than
// min_scrape_interval is answered from the previous computation, so a shorter
// refresh redraws the same numbers while the age on the screen resets - which
// reads as a broker whose numbers have stopped moving.
func TestTheRefreshIsClampedUpAndNeverDown(t *testing.T) {
	for _, c := range []struct {
		written, floor, want time.Duration
	}{
		{5 * time.Second, time.Minute, time.Minute},
		{time.Minute, time.Minute, time.Minute},
		{5 * time.Minute, time.Minute, 5 * time.Minute},
		// A broker configured slower than its floor still wins.
		{time.Minute, 90 * time.Second, 90 * time.Second},
		// And a broker somehow reporting less than a minute does not pull a
		// slower refresh down to it.
		{5 * time.Minute, time.Second, 5 * time.Minute},
	} {
		d, err := Load(writeDashboard(t, "refresh: "+c.written.String()+
			"\ngroups:\n  - title: A\n    layout: columns\n    metrics: [saguin_connections]\n"))
		if err != nil {
			t.Fatal(err)
		}
		if got := d.Interval(c.floor); got != c.want {
			t.Errorf("refresh %v against a floor of %v settled on %v, want %v",
				c.written, c.floor, got, c.want)
		}
	}
}

// TestTheShippedDefaultNamesOnlyMetricsRFC0005Promises. The catalogue is closed -
// "a name here is a promise; a name not here is not published" - so a default
// dashboard naming anything else is a panel that can never draw. The oracle is
// the RFC rather than the broker, so this fails on a name that was invented here
// even if some build happens to serve it.
func TestTheShippedDefaultNamesOnlyMetricsRFC0005Promises(t *testing.T) {
	catalogue := rfcCatalogue(t)
	d, err := Default()
	if err != nil {
		t.Fatal(err)
	}
	named := 0
	for _, g := range d.Groups {
		for _, c := range g.Columns {
			// Every metric the column reads, so an operand inside a computed
			// column's expression is held to the catalogue like a bare name.
			for _, name := range c.Reads() {
				named++
				if !catalogue[name] {
					t.Errorf("the shipped default names %s, which RFC 0005's "+
						"catalogue does not promise", name)
				}
			}
		}
	}
	if named < 20 {
		t.Errorf("only %d metrics were checked, and the default names more than "+
			"that - this case is reading a short list", named)
	}
	if len(d.Groups) != 8 {
		t.Errorf("%d groups in the shipped default, want 8", len(d.Groups))
	}
}

// rfcCatalogue reads the metric names RFC 0005 promises, between the catalogue's
// own two headings - so the example scrape above it and the alert below cannot
// contribute a name the catalogue does not.
func rfcCatalogue(t *testing.T) map[string]bool {
	t.Helper()
	root := os.Getenv("SAGUIN_REPO")
	if root == "" {
		root = filepath.Join("..", "..", "..", "saguin")
	}
	path := filepath.Join(root, "docs", "rfcs", "0005-operations.md")
	body, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("no %s - RFC 0005 is the oracle for this, and it is in saguin's "+
			"own checkout: set SAGUIN_REPO if it is not beside this one", path)
	}
	text := string(body)
	start := strings.Index(text, "\n### The catalogue\n")
	if start < 0 {
		t.Fatal("RFC 0005 has no `### The catalogue` heading, so this case cannot " +
			"read its oracle")
	}
	end := strings.Index(text[start+1:], "\n### ")
	if end < 0 {
		t.Fatal("the catalogue section does not end at another heading")
	}
	rows := regexp.MustCompile(`(?m)^\| `+"`"+`(saguin_[a-z0-9_]+)`).
		FindAllStringSubmatch(text[start:start+1+end], -1)
	if len(rows) < 60 {
		t.Fatalf("only %d metrics parsed out of RFC 0005's catalogue - the row "+
			"shape has moved, and an oracle this short agrees with anything",
			len(rows))
	}
	out := map[string]bool{}
	for _, m := range rows {
		out[m[1]] = true
	}
	return out
}

// TestAComputedColumnLoads - the shape a user defines a widget with. There is no
// separate widget kind: the layout gives the shape and this list gives the
// content.
func TestAComputedColumnLoads(t *testing.T) {
	d, err := Load(writeDashboard(t, `
refresh: 60s
groups:
  - title: Channels
    layout: rows
    by: channel
    metrics:
      - saguin_channel_records
      - name: behind
        value: saguin_channel_next_offset - saguin_channel_consumer_position_min
      - name: data lost
        value: saguin_channel_floor_offset > saguin_channel_consumer_position_min
      - name: average record
        value: saguin_channel_bytes / saguin_channel_records
        format: bytes
`))
	if err != nil {
		t.Fatalf("the specified shape was refused: %v", err)
	}
	cols := d.Groups[0].Columns
	if len(cols) != 4 {
		t.Fatalf("%d columns, want 4", len(cols))
	}
	if cols[0].Computed() || cols[0].Metric != "saguin_channel_records" {
		t.Errorf("the bare name came out as %+v", cols[0])
	}
	if !cols[1].Computed() || cols[1].Heading() != "behind" {
		t.Errorf("the computed column came out as %+v", cols[1])
	}
	if got := cols[1].Reads(); len(got) != 2 ||
		got[0] != "saguin_channel_next_offset" {
		t.Errorf("behind reads %v", got)
	}
	if cols[3].Format != "bytes" {
		t.Errorf("format is %q, want bytes", cols[3].Format)
	}
	// A bare name has no format of its own, because its name already says which
	// rule it takes.
	if cols[0].Format != "" {
		t.Errorf("a bare metric carries a format: %q", cols[0].Format)
	}
}

// TestEveryWayAColumnIsRefused. The unknown-key half is done by hand in
// UnmarshalYAML, because `KnownFields` is a decoder setting and does not reach a
// type that unmarshals itself - so a `valeu:` would be ignored by exactly the
// entry that most needs refusing.
func TestEveryWayAColumnIsRefused(t *testing.T) {
	head := "refresh: 60s\ngroups:\n  - title: A\n    layout: rows\n    by: channel\n    metrics:\n"
	for _, c := range []struct {
		name, body string
		says       []string
	}{
		{"a misspelt key",
			"      - name: lag\n        valeu: saguin_a - saguin_b\n",
			[]string{"valeu", "not a key a column takes"}},
		{"an extra key",
			"      - name: lag\n        value: saguin_a - saguin_b\n        colour: red\n",
			[]string{"colour", "not a key a column takes"}},
		{"no name",
			"      - value: saguin_a - saguin_b\n",
			[]string{"no `name`", "no heading"}},
		{"no value",
			"      - name: lag\n",
			[]string{`"lag"`, "no `value`"}},
		{"an unknown format",
			"      - name: lag\n        value: saguin_a - saguin_b\n        format: hex\n",
			[]string{`"hex"`, "number, bytes or duration"}},
		{"an expression that does not parse",
			"      - name: lag\n        value: saguin_a - \n",
			[]string{`"lag"`, "ends where a metric or a number should be"}},
		{"a computed column named like a metric",
			"      - name: saguin_lag\n        value: saguin_a - saguin_b\n",
			[]string{"saguin_lag", "named like a metric", "name of its own"}},
		{"a heading used twice",
			"      - name: lag\n        value: saguin_a - saguin_b\n      - name: lag\n        value: saguin_a + saguin_b\n",
			[]string{"lag twice"}},
		{"a bare metric and a computed column of the same name",
			"      - saguin_a\n      - name: saguin_a\n        value: saguin_a - saguin_b\n",
			[]string{"saguin_a"}},
		{"a column that is neither",
			"      - [saguin_a]\n",
			[]string{"either a metric name or a `name` and a `value`"}},
		{"an empty metric name",
			"      - \"\"\n",
			[]string{"empty metric name"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			path := writeDashboard(t, head+c.body)
			_, err := Load(path)
			if err == nil {
				t.Fatalf("accepted:\n%s", head+c.body)
			}
			for _, want := range c.says {
				if !strings.Contains(err.Error(), want) {
					t.Errorf("the refusal does not say %q: %v", want, err)
				}
			}
			if !strings.Contains(err.Error(), path) {
				t.Errorf("the refusal does not name the file: %v", err)
			}
		})
	}
}

// TestAnOperandTheBrokerDoesNotServeIsRefusedByName - a typo inside a `value` is
// held to the scrape exactly as a bare column name is, and the message says
// which column it came from, because "names saguin_x" is not enough once a name
// can be inside an expression.
func TestAnOperandTheBrokerDoesNotServeIsRefusedByName(t *testing.T) {
	d, err := Load(writeDashboard(t, `
refresh: 60s
groups:
  - title: Channels
    layout: rows
    by: channel
    metrics:
      - name: average record
        value: saguin_channel_bytes / saguin_channel_recrods
`))
	if err != nil {
		t.Fatal(err)
	}
	// The fixture serves saguin_channel_bytes, so the only name it cannot answer
	// is the misspelt one - which is what makes this case about the typo rather
	// than about whichever operand happened to be checked first.
	_, err = d.Validate(fixture(t))
	if err == nil {
		t.Fatal("a misspelt operand was accepted")
	}
	for _, want := range []string{"saguin_channel_recrods",
		`computes "average record" from`, "Channels"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not say %q: %v", want, err)
		}
	}
}

// TestAComputedColumnDropsWholeWhenAnOperandIsAbsent, in the shipped default
// where absence is reported rather than refused. A column whose cell would be
// unknown on every row is a column of dashes claiming to be a reading.
func TestAComputedColumnDropsWholeWhenAnOperandIsAbsent(t *testing.T) {
	d, err := Default()
	if err != nil {
		t.Fatal(err)
	}
	absent, err := d.Validate(fixture(t))
	if err != nil {
		t.Fatal(err)
	}
	// The fixture serves next_offset but no consumer position, so `behind` and
	// `data lost` both go, and the operand they are missing is reported.
	if !contains(absent, "saguin_channel_consumer_position_min") {
		t.Errorf("the missing operand was not reported: %v", absent)
	}
	var channels *Group
	for i := range d.Groups {
		if d.Groups[i].Title == "Channels" {
			channels = &d.Groups[i]
		}
	}
	for _, c := range channels.Drawn() {
		if c.Computed() {
			t.Errorf("column %q was kept although %v is not served", c.Name,
				c.Reads())
		}
	}
	// And the columns whose metrics are all there survive.
	if !drawsMetric(channels, "saguin_channel_records") {
		t.Errorf("Channels kept %v", headings(channels.Drawn()))
	}
}

// TestEveryDashboardExampleInTheReadmeLoads.
//
// **The README is the dashboard file's documentation, and nothing checked it.**
// An example that stops loading is worse than no example: somebody copies it,
// gets a refusal, and concludes the tool is broken rather than the prose. Three
// keys and one of two layouts is a small schema, which is exactly the kind that
// drifts from its documentation without anybody noticing.
//
// A block that is a whole file loads as one; a block that is a group or a list of
// groups is wrapped in the smallest file that can hold it, because the README
// writes those as fragments on purpose - a reader is being shown a group rather
// than a file.
func TestEveryDashboardExampleInTheReadmeLoads(t *testing.T) {
	body, err := os.ReadFile(filepath.Join("..", "..", "cmd", "saguin-viewer",
		"README.md"))
	if err != nil {
		t.Fatal(err)
	}
	blocks := regexp.MustCompile("(?sm)^```yaml\n(.*?)^```").
		FindAllStringSubmatch(string(body), -1)
	if len(blocks) < 3 {
		t.Fatalf("only %d yaml examples found in the README - the fence pattern "+
			"has moved and this case is reading almost nothing", len(blocks))
	}
	for i, b := range blocks {
		block := b[1]
		t.Run(fmt.Sprintf("example %d", i+1), func(t *testing.T) {
			file := block
			switch {
			case strings.HasPrefix(strings.TrimLeft(block, "\n"), "refresh:"),
				strings.HasPrefix(strings.TrimLeft(block, "\n"), "groups:"):
				if !strings.Contains(block, "refresh:") {
					file = "refresh: 60s\n" + block
				}
			default:
				// A group, or a list of them, shown without the file around it.
				file = "refresh: 60s\ngroups:\n" + indentBy(block, "  ")
			}
			path := writeDashboard(t, file)
			if _, err := Load(path); err != nil {
				t.Errorf("a README example does not load: %v\n--- as offered to the "+
					"loader ---\n%s", err, file)
			}
		})
	}
}

// indentBy shifts every non-empty line right, so a group written at the left
// margin can be dropped under `groups:`.
func indentBy(s, by string) string {
	var out []string
	for _, line := range strings.Split(s, "\n") {
		if strings.TrimSpace(line) == "" {
			out = append(out, line)
			continue
		}
		out = append(out, by+line)
	}
	return strings.Join(out, "\n")
}

// TestTheReadmeDescribesTheSchemaThatExists - the keys, the layouts and the
// formats it names are the ones the loader takes, and nothing it takes is
// undocumented.
func TestTheReadmeDescribesTheSchemaThatExists(t *testing.T) {
	body, err := os.ReadFile(filepath.Join("..", "..", "cmd", "saguin-viewer",
		"README.md"))
	if err != nil {
		t.Fatal(err)
	}
	text := string(body)
	// Named in prose, in backticks, as either `key` or `key:` - a reader
	// scanning for a key they saw in an example has to find it discussed.
	for _, key := range []string{"refresh", "layout", "by", "metrics", "name",
		"value", "format"} {
		if !strings.Contains(text, "`"+key+"`") && !strings.Contains(text, "`"+key+":") {
			t.Errorf("the dashboard file takes %q and the README's prose never "+
				"names it, so a reader who saw it in an example cannot look it up",
				key)
		}
	}
	for _, value := range []string{"columns", "rows", "number", "bytes", "duration"} {
		if !strings.Contains(text, "`"+value+"`") {
			t.Errorf("%q is a value the loader accepts and the README does not "+
				"name it", value)
		}
	}
	// And the other way: a layout the loader accepts and the README does not name
	// would be a feature nobody can find.
	for _, layout := range []string{LayoutColumns, LayoutRows} {
		if !strings.Contains(text, "`"+layout+"`") {
			t.Errorf("the loader accepts layout %q and the README does not name it",
				layout)
		}
	}
}

// TestEveryFlagTheToolHasIsInItsReadme.
//
// **`--timeout` was not, and that is the whole reason for this.** A flag the
// program accepts and the document never names is a setting nobody can find, and
// the one most likely to be missed is the one added last. The README's table
// claims to be every flag; this holds it to that.
//
// main.go is walked as a syntax tree rather than matched as text: a flag name only
// exists as the first argument of a `fs.String`/`fs.Bool`/`fs.Duration` call, and
// a regular expression over the source would miss one written across two lines.
func TestEveryFlagTheToolHasIsInItsReadme(t *testing.T) {
	dir := filepath.Join("..", "..", "cmd", "saguin-viewer")
	readme, err := os.ReadFile(filepath.Join(dir, "README.md"))
	if err != nil {
		t.Fatal(err)
	}
	file, err := goparser.ParseFile(gotoken.NewFileSet(), filepath.Join(dir, "main.go"),
		nil, 0)
	if err != nil {
		t.Fatal(err)
	}

	var flags []string
	ast.Inspect(file, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok || len(call.Args) == 0 {
			return true
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		switch sel.Sel.Name {
		case "String", "Bool", "Duration", "Int":
		default:
			return true
		}
		lit, ok := call.Args[0].(*ast.BasicLit)
		if !ok || lit.Kind != gotoken.STRING {
			return true
		}
		name, err := strconv.Unquote(lit.Value)
		if err != nil || name == "" {
			return true
		}
		flags = append(flags, name)
		return true
	})

	// Count what the walk found. A tree walk that matched nothing would leave
	// this case agreeing that every flag is documented.
	if len(flags) < 8 {
		t.Fatalf("only %d flags found in main.go: %v - the walk is reading less "+
			"than the command defines", len(flags), flags)
	}
	for _, name := range flags {
		if !strings.Contains(string(readme), "`--"+name) {
			t.Errorf("the tool takes --%s and its README never names it, so it is "+
				"a setting nobody can find", name)
		}
	}
	// And the table says how many there are, which is a number in prose and so
	// the one kind of claim that stops being true silently.
	if !strings.Contains(string(readme), fmt.Sprintf("there are %s:", spelled(len(flags)))) {
		t.Errorf("the README does not say there are %s flags (%d found)",
			spelled(len(flags)), len(flags))
	}
}

// spelled writes a small number as a word, which is how this project's prose
// writes them.
func spelled(n int) string {
	words := []string{"zero", "one", "two", "three", "four", "five", "six",
		"seven", "eight", "nine", "ten", "eleven", "twelve"}
	if n < len(words) {
		return words[n]
	}
	return strconv.Itoa(n)
}
