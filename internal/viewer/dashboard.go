package viewer

import (
	"embed"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

//go:embed default.yaml
var shipped embed.FS

// Layouts are the two shapes a group can take, and there are two because a
// number is either one value or one per entity.
const (
	// LayoutColumns is a row of labelled numbers: the broker-wide gauges and
	// counters, which have one value each.
	LayoutColumns = "columns"
	// LayoutRows is a table, one row per value of the group's `by` label and one
	// column per metric.
	LayoutRows = "rows"
)

// A Column is one number on the screen: either a metric read straight from the
// scrape, or a value worked out from several.
//
//	metrics:
//	  - saguin_channel_records
//	  - name: lag
//	    value: saguin_channel_next_offset - saguin_channel_consumer_position_min
//
// **A group with computed columns is a widget somebody defined**, which is why
// there is no separate widget kind: the shape comes from the layout and the
// content from this list. A group worth reusing is reused with a YAML anchor,
// which needs nothing from this program.
type Column struct {
	// Metric is set for a bare name, and Expr for a computed column.
	Metric string
	Name   string
	Expr   *Expr
	// Format is `number`, `bytes` or `duration`, and only a computed column takes
	// one: a metric's own name already says which it is.
	Format string
}

// Heading is what the column is called on the screen.
func (c Column) Heading() string {
	if c.Name != "" {
		return c.Name
	}
	return c.Metric
}

// Reads is every metric this column needs.
func (c Column) Reads() []string {
	if c.Expr != nil {
		return c.Expr.Metrics()
	}
	return []string{c.Metric}
}

// Computed says whether this column is worked out rather than read.
func (c Column) Computed() bool { return c.Expr != nil }

// UnmarshalYAML accepts either shape.
//
// **The unknown-key refusal is done by hand here.** `KnownFields` is a setting on
// the decoder and does not reach a type that unmarshals itself, so a `valeu:`
// would be silently ignored by exactly the entry that most needs to be refused -
// a column whose value nobody wrote draws nothing and says nothing.
func (c *Column) UnmarshalYAML(node *yaml.Node) error {
	if node.Kind == yaml.ScalarNode {
		var name string
		if err := node.Decode(&name); err != nil {
			return err
		}
		if name == "" {
			return fmt.Errorf("an empty metric name")
		}
		c.Metric = name
		return nil
	}
	if node.Kind != yaml.MappingNode {
		return fmt.Errorf("a column is either a metric name or a `name` and a " +
			"`value`, and this is neither")
	}
	allowed := map[string]*string{}
	var name, value, format string
	allowed["name"], allowed["value"], allowed["format"] = &name, &value, &format
	for i := 0; i+1 < len(node.Content); i += 2 {
		key := node.Content[i].Value
		into, ok := allowed[key]
		if !ok {
			return fmt.Errorf("`%s` is not a key a column takes - a computed "+
				"column is `name`, `value` and optionally `format`", key)
		}
		if err := node.Content[i+1].Decode(into); err != nil {
			return fmt.Errorf("`%s`: %w", key, err)
		}
	}
	if name == "" {
		return fmt.Errorf("a computed column with no `name`, so its column would " +
			"have no heading")
	}
	if value == "" {
		return fmt.Errorf("column %q has no `value`, which is the expression it "+
			"draws", name)
	}
	switch format {
	case "", "number", "bytes", "duration":
	default:
		return fmt.Errorf("column %q: format %q is not one this draws - number, "+
			"bytes or duration", name, format)
	}
	expr, err := ParseExpr(value)
	if err != nil {
		return fmt.Errorf("column %q: %w", name, err)
	}
	c.Name, c.Expr, c.Format = name, expr, format
	return nil
}

// Group is one block of the screen.
type Group struct {
	Title   string   `yaml:"title"`
	Layout  string   `yaml:"layout"`
	By      string   `yaml:"by"`
	Columns []Column `yaml:"metrics"`

	// present and absent are what the last Validate made of Columns against a
	// real scrape. **Columns itself is never narrowed**, so a metric this broker
	// has none of today is asked for again on the next screen: a queue channel's
	// first record and a bridge's first connection both arrive while the program
	// is running, and a list narrowed once would say they were absent for the
	// life of the process.
	present []Column
	absent  []string
}

// Drawn is the columns of this group the broker can answer, which is what the
// screen draws. Empty until Validate has run.
func (g *Group) Drawn() []Column { return g.present }

// Absent is the metrics of this group the broker does not serve, which the screen
// says rather than leaving a group looking complete.
func (g *Group) Absent() []string { return g.absent }

// Dashboard is a whole screen: how often to redraw it and what goes on it.
type Dashboard struct {
	Refresh string  `yaml:"refresh"`
	Groups  []Group `yaml:"groups"`

	refresh time.Duration
	// strict is true for a file somebody named and false for the shipped
	// default. **The difference is who chose the metric.** An operator who wrote
	// `saguin_queue_depth` on a dashboard asked for that number and is owed a
	// refusal when the broker does not serve it; the default asks for queues,
	// bridges and refusals on every broker, most of which have no queue and no
	// bridge, so refusing there would make the tool unusable out of the box on
	// the ordinary deployment.
	strict bool
	source string
}

// Default is the dashboard used when no file is named.
func Default() (*Dashboard, error) {
	body, err := shipped.ReadFile("default.yaml")
	if err != nil {
		return nil, err
	}
	d, err := parseDashboard(body, "the shipped default")
	if err != nil {
		// A broken default is a broken build, not an operator's mistake.
		return nil, fmt.Errorf("the dashboard built into this binary does not "+
			"load, which is a defect in it rather than in your configuration: %w", err)
	}
	d.strict = false
	return d, nil
}

// Load reads a dashboard file.
func Load(path string) (*Dashboard, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	d, err := parseDashboard(body, path)
	if err != nil {
		return nil, err
	}
	d.strict = true
	return d, nil
}

func parseDashboard(body []byte, source string) (*Dashboard, error) {
	d := &Dashboard{source: source}
	dec := yaml.NewDecoder(strings.NewReader(string(body)))
	// **An unknown key is refused rather than ignored**, so a `metric:` written
	// where `metrics:` was meant is caught rather than producing a group with
	// nothing on it.
	dec.KnownFields(true)
	if err := dec.Decode(d); err != nil {
		return nil, fmt.Errorf("%s: %w", source, err)
	}
	if d.Refresh == "" {
		return nil, fmt.Errorf("%s: no `refresh`, which is how often the screen "+
			"is redrawn - write one, such as `refresh: 60s`", source)
	}
	r, err := time.ParseDuration(d.Refresh)
	if err != nil {
		return nil, fmt.Errorf("%s: refresh %q is not a duration, such as 60s or "+
			"2m", source, d.Refresh)
	}
	if r <= 0 {
		return nil, fmt.Errorf("%s: refresh %q is not a length of time", source,
			d.Refresh)
	}
	d.refresh = r
	if len(d.Groups) == 0 {
		return nil, fmt.Errorf("%s: no `groups`, so the screen would be empty",
			source)
	}
	for i := range d.Groups {
		g := &d.Groups[i]
		where := fmt.Sprintf("%s: group %d", source, i+1)
		if g.Title != "" {
			where = fmt.Sprintf("%s: group %q", source, g.Title)
		}
		if g.Title == "" {
			return nil, fmt.Errorf("%s: no `title`, and a block of numbers with "+
				"no heading is a block nobody can name on the telephone", where)
		}
		switch g.Layout {
		case LayoutColumns:
			// **`by` is what makes a group a table, so it cannot be on this
			// one.** A row of labelled numbers has no rows to divide.
			if g.By != "" {
				return nil, fmt.Errorf("%s: `by: %s` with `layout: columns`. `by` "+
					"puts one row per label value, which is `layout: rows`; a "+
					"columns group is one number each", where, g.By)
			}
		case LayoutRows:
			if g.By == "" {
				return nil, fmt.Errorf("%s: `layout: rows` with no `by`, so there "+
					"is nothing to put one row per - name the label that divides "+
					"them, such as `by: channel`", where)
			}
		case "":
			return nil, fmt.Errorf("%s: no `layout` - %s or %s", where,
				LayoutColumns, LayoutRows)
		default:
			return nil, fmt.Errorf("%s: layout %q is not one this draws - %s is a "+
				"row of labelled numbers, %s is a table", where, g.Layout,
				LayoutColumns, LayoutRows)
		}
		if len(g.Columns) == 0 {
			return nil, fmt.Errorf("%s: no `metrics`, so it would draw a heading "+
				"and nothing under it", where)
		}
		seen := map[string]bool{}
		for _, c := range g.Columns {
			if seen[c.Heading()] {
				return nil, fmt.Errorf("%s: %s twice, which would draw the same "+
					"number in two places", where, c.Heading())
			}
			seen[c.Heading()] = true
			// A computed column named after a metric would be two different
			// numbers answering to one name in the same file.
			if c.Computed() && strings.HasPrefix(c.Name, "saguin_") {
				return nil, fmt.Errorf("%s: computed column %q is named like a "+
					"metric. Give it a name of its own, so a reader can tell what "+
					"this broker publishes from what this file works out", where,
					c.Name)
			}
		}
	}
	return d, nil
}

// Interval is how often to redraw, clamped up to what the broker says it
// recomputes at and never below it.
//
// **Up, never down.** RFC 0005: a scrape arriving sooner than
// min_scrape_interval is answered from the previous computation, so a shorter
// refresh redraws the same numbers while the age on the screen resets - which
// reads as a broker whose numbers have stopped moving. The screen says which
// interval it settled on for the same reason.
func (d *Dashboard) Interval(brokerFloor time.Duration) time.Duration {
	if d.refresh < brokerFloor {
		return brokerFloor
	}
	return d.refresh
}

// Written is the refresh as the file wrote it, which the screen says when it was
// clamped.
func (d *Dashboard) Written() time.Duration { return d.refresh }

// Source names where this dashboard came from, for an error message.
func (d *Dashboard) Source() string { return d.source }

// Validate holds the dashboard against a real scrape, and is the second half of
// "refused by name at startup": a name is a metric of the broker in front of
// this program rather than of the catalogue in general.
//
// It returns the metrics the broker does not serve, which is never non-empty for
// a file somebody wrote - that is an error instead.
func (d *Dashboard) Validate(s *Scrape) ([]string, error) {
	var missing []string
	for i := range d.Groups {
		g := &d.Groups[i]
		g.absent, g.present = nil, nil
		for _, c := range g.Columns {
			// **Every metric a column reads is checked, expression or not**, so a
			// typo inside a `value` is refused exactly as a bare name is. A
			// computed column drops out when any one of its operands is missing:
			// the cell would be unknown on every row, which is a column of
			// dashes claiming to be a reading.
			var lost []string
			for _, name := range c.Reads() {
				m, ok := s.Family(name)
				if !ok {
					if d.strict {
						return nil, fmt.Errorf("%s: group %q %s and this broker "+
							"does not serve it. Either it is not a metric saguin "+
							"publishes, or nothing on this broker has one yet - a "+
							"queue metric needs a queue channel, a bridge metric a "+
							"bridge", d.source, g.Title, c.blames(name))
					}
					lost = append(lost, name)
					continue
				}
				if g.By != "" && !hasLabel(m, g.By) {
					if d.strict {
						return nil, fmt.Errorf("%s: group %q is `by: %s` and %s "+
							"carries no such label. It carries %s", d.source,
							g.Title, g.By, name, listLabels(m))
					}
					lost = append(lost, name)
					continue
				}
			}
			if len(lost) > 0 {
				// **Named once, however many columns wanted it.** In the shipped
				// dashboard `behind` and `data lost` both read
				// saguin_channel_consumer_position_min, so a broker nothing has
				// consumed from listed it twice - once per column that lost it.
				g.absent = addOnce(g.absent, lost)
				continue
			}
			g.present = append(g.present, c)
		}
		// And once across the screen, however many groups wanted it: the foot line
		// is a list of what this broker does not serve, not a tally of
		// disappointed columns.
		missing = addOnce(missing, g.absent)
	}
	return missing, nil
}

// blames says which part of a column a missing metric belongs to, because "names
// saguin_x" is not enough once a name can be inside an expression.
func (c Column) blames(name string) string {
	if c.Computed() {
		return fmt.Sprintf("computes %q from %s", c.Name, name)
	}
	return "names " + name
}

// addOnce appends the names that are not already there, keeping the order they
// arrived in.
func addOnce(into []string, names []string) []string {
	for _, name := range names {
		seen := false
		for _, have := range into {
			if have == name {
				seen = true
				break
			}
		}
		if !seen {
			into = append(into, name)
		}
	}
	return into
}

// hasLabel says whether every sample of a family carries the label. **Every
// rather than any**: a table with one row per label value would put a sample
// that has no such label in a row named for nothing.
func hasLabel(m *Metric, label string) bool {
	if len(m.Samples) == 0 {
		return false
	}
	for _, s := range m.Samples {
		if _, ok := s.Labels[label]; !ok {
			return false
		}
	}
	return true
}

func listLabels(m *Metric) string {
	seen := map[string]bool{}
	for _, s := range m.Samples {
		for k := range s.Labels {
			seen[k] = true
		}
	}
	if len(seen) == 0 {
		return "no labels at all"
	}
	names := make([]string, 0, len(seen))
	for k := range seen {
		names = append(names, k)
	}
	sort.Strings(names)
	return strings.Join(names, ", ")
}
