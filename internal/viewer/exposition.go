// Package viewer reads a saguin broker's operations listener and draws one
// screen of it. RFC 0005 is what it is written against: the listener's paths,
// the metrics catalogue, and the rule that the broker rather than the reader
// decides how often the numbers are recomputed.
package viewer

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// Sample is one line of the exposition: a value and the labels that pick it
// out of its family. Labels is nil where the line carried none.
type Sample struct {
	Labels map[string]string
	Value  float64
}

// Metric is one family: the name, what the broker says it means, and every
// sample of it in one scrape.
type Metric struct {
	Name    string
	Help    string
	Kind    string
	Samples []Sample
}

// Scrape is one reading of /metrics, with the moment it was taken.
//
// **At is when this program read the body, which is not when the broker
// computed it.** A scrape arriving inside min_scrape_interval is answered from
// the previous computation (RFC 0005, "The observer does not set the cost"), so
// a sample can be up to one interval older than this. The screen says both
// numbers for that reason: how long ago it was read, and how often the broker
// recomputes.
type Scrape struct {
	At      time.Time
	Metrics map[string]*Metric
	// Order is the order the families arrived in, so anything walking every
	// family reports in the broker's own order rather than a map's.
	Order []string
}

// Family returns the family of that name, and whether the scrape carried one.
func (s *Scrape) Family(name string) (*Metric, bool) {
	m, ok := s.Metrics[name]
	return m, ok
}

// One returns the value of a family with a single unlabelled sample, which is
// what most of the broker-wide gauges and counters are.
func (s *Scrape) One(name string) (float64, bool) {
	m, ok := s.Metrics[name]
	if !ok || len(m.Samples) == 0 {
		return 0, false
	}
	return m.Samples[0].Value, true
}

// Label returns one label of the first sample of a family, which is how the
// header reads saguin_build_info.
func (s *Scrape) Label(name, label string) string {
	m, ok := s.Metrics[name]
	if !ok || len(m.Samples) == 0 {
		return ""
	}
	return m.Samples[0].Labels[label]
}

// ParseScrape reads a /metrics body.
//
// **Three line shapes and nothing else**, because saguin emits three: a HELP
// line, a TYPE line, and a sample. A fourth shape is an error naming the line
// rather than a line quietly dropped - the Python viewer dropped every sample
// that carried no labels and showed a page with channels and no connection
// count, which is the failure a tolerant parser produces: a screen that looks
// complete.
func ParseScrape(body string, at time.Time) (*Scrape, error) {
	s := &Scrape{At: at, Metrics: map[string]*Metric{}}
	for n, line := range strings.Split(body, "\n") {
		where := n + 1
		// A trailing newline makes a last empty field, and the broker separates
		// nothing with blank lines, so an empty line carries no information
		// either way.
		if strings.TrimSpace(line) == "" {
			continue
		}
		if strings.HasPrefix(line, "#") {
			if err := s.comment(line, where); err != nil {
				return nil, err
			}
			continue
		}
		if err := s.sample(line, where); err != nil {
			return nil, err
		}
	}
	return s, nil
}

// family returns the named family, creating it in arrival order if this is the
// first line to mention it. HELP, TYPE and the samples themselves all reach a
// family this way, so whichever comes first creates it.
func (s *Scrape) family(name string) *Metric {
	if m, ok := s.Metrics[name]; ok {
		return m
	}
	m := &Metric{Name: name}
	s.Metrics[name] = m
	s.Order = append(s.Order, name)
	return m
}

func (s *Scrape) comment(line string, where int) error {
	rest, kind := "", ""
	switch {
	case strings.HasPrefix(line, "# HELP "):
		kind, rest = "HELP", line[len("# HELP "):]
	case strings.HasPrefix(line, "# TYPE "):
		kind, rest = "TYPE", line[len("# TYPE "):]
	default:
		return fmt.Errorf("line %d: %q is a comment that is neither HELP nor "+
			"TYPE, and saguin writes no others", where, line)
	}
	name, tail, found := strings.Cut(rest, " ")
	// **A HELP with no sentence is not a metric with an empty one.** An empty
	// description would print a name with a blank beside it where the broker's
	// own words belong.
	if !found || strings.TrimSpace(tail) == "" || name == "" {
		return fmt.Errorf("line %d: %q is a %s line with no %s after the name",
			where, line, kind, map[string]string{"HELP": "description", "TYPE": "type"}[kind])
	}
	m := s.family(name)
	if kind == "HELP" {
		// **Taken whole, to the end of the line.** These sentences carry
		// backticks, braces, quotes and dashes - RFC 0005 writes them that way -
		// and a parser that split on any of them would show half a sentence.
		m.Help = tail
		return nil
	}
	m.Kind = strings.TrimSpace(tail)
	return nil
}

func (s *Scrape) sample(line string, where int) error {
	name, rest, err := metricName(line, where)
	if err != nil {
		return err
	}
	labels, rest, err := parseLabels(rest, where, line)
	if err != nil {
		return err
	}
	value := strings.TrimSpace(rest)
	if value == "" {
		return fmt.Errorf("line %d: %q names %s and gives it no value",
			where, line, name)
	}
	// **One field, so a timestamp is an error rather than a value read wrong.**
	// The exposition format allows a millisecond timestamp after the value and
	// saguin writes none; a parser taking the first field of two would read
	// such a line as a sample it is not.
	if i := strings.IndexAny(value, " \t"); i >= 0 {
		return fmt.Errorf("line %d: %q has %q after the value, and saguin "+
			"writes a value and nothing else", where, line, strings.TrimSpace(value[i:]))
	}
	v, err := strconv.ParseFloat(value, 64)
	if err != nil {
		return fmt.Errorf("line %d: %q: %q is not a number", where, line, value)
	}
	m := s.family(name)
	m.Samples = append(m.Samples, Sample{Labels: labels, Value: v})
	return nil
}

// metricName takes the family name off the front of a sample line. The
// exposition format allows a digit anywhere but the first character, which
// saguin_qos2_held needs.
func metricName(line string, where int) (string, string, error) {
	i := 0
	for i < len(line) {
		c := line[i]
		letter := c == '_' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
		if letter || (i > 0 && c >= '0' && c <= '9') {
			i++
			continue
		}
		break
	}
	if i == 0 {
		return "", "", fmt.Errorf("line %d: %q does not start with a metric name",
			where, line)
	}
	return line[:i], line[i:], nil
}

// parseLabels reads an optional `{a="1",b="2"}` off the front of what follows a
// metric name, and returns what is left.
//
// **Scanned rather than split on a comma or a closing brace**, because a label
// value is arbitrary text: saguin_channel_info carries the filter as written,
// braces and all, so `filter="iot/+/{status,location}/+"` is a real line. The
// Python viewer split on those characters and shipped a parser that cut such a
// filter in half.
func parseLabels(rest string, where int, line string) (map[string]string, string, error) {
	if !strings.HasPrefix(rest, "{") {
		// No labels at all, which most of the broker-wide numbers have. This is
		// the case the Python parser dropped.
		return nil, rest, nil
	}
	labels := map[string]string{}
	i := 1
	for {
		for i < len(rest) && (rest[i] == ' ' || rest[i] == ',') {
			i++
		}
		if i < len(rest) && rest[i] == '}' {
			return labels, rest[i+1:], nil
		}
		start := i
		for i < len(rest) && rest[i] != '=' && rest[i] != '}' {
			i++
		}
		if i >= len(rest) || rest[i] != '=' {
			return nil, "", fmt.Errorf("line %d: %q: label %q has no value",
				where, line, rest[start:i])
		}
		name := rest[start:i]
		i++ // past '='
		if i >= len(rest) || rest[i] != '"' {
			return nil, "", fmt.Errorf("line %d: %q: the value of %q is not quoted",
				where, line, name)
		}
		i++ // past the opening quote
		var value strings.Builder
		for {
			if i >= len(rest) {
				return nil, "", fmt.Errorf("line %d: %q: the value of %q is not "+
					"closed", where, line, name)
			}
			c := rest[i]
			if c == '"' {
				i++
				break
			}
			// **The three escapes the exposition format defines on a label
			// value, and only those three.** A backslash before anything else is
			// not an escape in that format, so it stands for itself - which is
			// what a Windows path or a regular expression in a filter would be
			// made of.
			if c == '\\' && i+1 < len(rest) {
				switch rest[i+1] {
				case '\\':
					value.WriteByte('\\')
					i += 2
					continue
				case '"':
					value.WriteByte('"')
					i += 2
					continue
				case 'n':
					value.WriteByte('\n')
					i += 2
					continue
				}
			}
			value.WriteByte(c)
			i++
		}
		if name == "" {
			return nil, "", fmt.Errorf("line %d: %q: a label with no name",
				where, line)
		}
		labels[name] = value.String()
	}
}
