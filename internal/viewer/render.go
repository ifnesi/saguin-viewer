package viewer

import (
	"fmt"
	"io"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"

	"golang.org/x/term"
)

// FallbackWidth is the width used when there is no terminal to ask - a pipe, a
// cron job, a file. Eighty, because that is what a terminal was before anybody
// could ask.
const FallbackWidth = 80

// Width is how wide to draw, from the terminal or the fallback.
func Width() int {
	w, _, err := term.GetSize(int(os.Stdout.Fd()))
	if err != nil || w <= 0 {
		return FallbackWidth
	}
	return w
}

// Screen is one drawing of one reading.
type Screen struct {
	Dashboard *Dashboard
	Scrape    *Scrape
	// Where is the socket or address this was read from. RFC 0005 notes that no
	// metric carries a hostname, so this is the only thing here that says which
	// broker - and it is what the reader typed.
	Where string
	Now   time.Time
	// Interval is what the refresh settled on, Written is what the file asked
	// for, and BrokerInterval is what the broker says it recomputes at. All
	// three are on the screen when they differ, because a reader who wrote 10s
	// and is silently given a minute draws every conclusion about a spike at the
	// wrong scale.
	Interval       time.Duration
	Written        time.Duration
	BrokerInterval time.Duration
	// Absent is what the shipped default asked for and this broker does not
	// serve, printed rather than dropped.
	Absent []string
	Once   bool
	Width  int
}

// Render draws the whole screen.
func (s Screen) Render(w io.Writer) error {
	width := s.width()
	var b strings.Builder
	s.header(&b)
	for i := range s.Dashboard.Groups {
		g := &s.Dashboard.Groups[i]
		b.WriteString("\n")
		b.WriteString(g.Title)
		b.WriteString("\n")
		switch {
		case len(g.Drawn()) == 0:
			// **Not silently gone.** A heading with a sentence under it saying
			// which metrics this broker has none of is the whole point of
			// reporting rather than dropping. Wrapped like everything else: the
			// first version of this line printed straight and ran to a hundred
			// and twenty-eight characters on a broker with no bridge.
			writeList(&b, "nothing on this broker:", g.absent, "  ", "    ", width)
		case g.Layout == LayoutRows:
			s.table(&b, g)
		default:
			s.row(&b, g, width)
		}
	}
	if len(s.Absent) > 0 {
		b.WriteString("\n")
		writeList(&b, "not served by this broker:", s.Absent, "", "  ", width)
	}
	_, err := io.WriteString(w, b.String())
	return err
}

// header names the broker and how old the reading is.
func (s Screen) header(b *strings.Builder) {
	version := s.Scrape.Label("saguin_build_info", "version")
	id := s.Scrape.Label("saguin_build_info", "broker_id")
	switch {
	case version == "" && id == "":
		// A broker that served a catalogue without this is a broker that changed
		// its mind about the one metric RFC 0005 says its version is stated in,
		// so say that rather than printing "saguin" with two gaps.
		fmt.Fprintf(b, "saguin (no saguin_build_info in this scrape)  %s\n", s.Where)
	default:
		fmt.Fprintf(b, "saguin %s  broker_id %s  %s\n", blankAs(version, "?"),
			blankAs(id, "?"), s.Where)
	}

	// **The age of the reading, not the age of the redraw**, which is why it is
	// measured from the scrape.
	age := s.Now.Sub(s.Scrape.At)
	if age < 0 {
		age = 0
	}
	parts := []string{"read " + short(age) + " ago"}
	if !s.Once {
		redraw := "redrawing every " + short(s.Interval)
		if s.Interval != s.Written {
			// The clamp, said out loud: RFC 0005 refuses to raise a configured
			// interval quietly, and neither does this.
			redraw += fmt.Sprintf(" (%s in the dashboard, raised to the broker's)",
				short(s.Written))
		}
		parts = append(parts, redraw)
	}
	// **Always the broker's own number.** There is nothing to qualify it with: a
	// broker that serves none is refused before a screen is drawn, rather than
	// having a minute assumed on its behalf.
	parts = append(parts, "broker recomputes every "+short(s.BrokerInterval))
	// Wrapped like a row of numbers is, because the clamp note makes this line
	// long enough to run off an eighty-column terminal.
	wrap(b, parts, ", ", "", "  ", s.width())
}

// wrap writes cells joined by sep, breaking to a new line rather than running
// past width. first is the indent of the first line and rest of the others.
//
// **A cell is never split**, so a line with one very long cell on it is longer
// than the width - a number cut in half is worse than a line that wraps.
func wrap(b *strings.Builder, cells []string, sep, first, rest string, width int) {
	if len(cells) == 0 {
		return
	}
	line, indent := first+cells[0], first
	for _, cell := range cells[1:] {
		if len(line)+len(sep)+len(cell) > width {
			b.WriteString(strings.TrimRight(line, " ") + strings.TrimSpace(sep) + "\n")
			line, indent = rest+cell, rest
			continue
		}
		line += sep + cell
	}
	_ = indent
	b.WriteString(line)
	b.WriteString("\n")
}

// writeList writes a sentence and a comma-separated list of names after it,
// wrapped to the width.
//
// **Its own helper rather than a call to wrap, for the width where the sentence
// and the first name cannot share a line.** wrap never splits a cell, so putting
// the two in one cell overran a forty-column screen, and putting the sentence in
// a cell of its own put a comma after it. Here the sentence starts the first line
// and drops to a line of its own only when it has to.
func writeList(b *strings.Builder, prefix string, names []string, first, rest string,
	width int) {
	if len(names) == 0 {
		return
	}
	line := first + prefix
	if len(line)+1+len(names[0]) > width {
		b.WriteString(line + "\n")
		line = rest
	}
	for i, name := range names {
		tail := name
		if i < len(names)-1 {
			tail += ","
		}
		sep := " "
		if line == rest || line == first+prefix {
			// Straight after the sentence, or at the start of a continuation.
			sep = " "
		}
		if line != rest && len(line)+len(sep)+len(tail) > width {
			b.WriteString(line + "\n")
			line = rest + tail
			continue
		}
		if line == rest {
			line += tail
			continue
		}
		line += sep + tail
	}
	b.WriteString(line + "\n")
}

func (s Screen) width() int {
	if s.Width <= 0 {
		return FallbackWidth
	}
	return s.Width
}

// row draws a columns group: a line of labelled numbers, wrapped to the width.
func (s Screen) row(b *strings.Builder, g *Group, width int) {
	drawn := g.Drawn()
	labels := headings(drawn)
	cells := make([]string, 0, len(drawn))
	for i, c := range drawn {
		// The family's single value, which is what a broker-wide gauge or counter
		// has. A computed column reads several of them the same way.
		resolve := func(name string) (float64, bool) { return s.Scrape.One(name) }
		cells = append(cells, labels[i]+" "+cell(c, resolve))
	}
	wrap(b, cells, "    ", "  ", "  ", width)
}

// table draws a rows group: one row per value of the group's `by` label, one
// column per metric, with the widths coming from the scrape rather than from
// anything configured.
func (s Screen) table(b *strings.Builder, g *Group) {
	drawn := g.Drawn()
	// The rows, and every metric any column reads, keyed by the label value that
	// names the row.
	var order []string
	seen := map[string]bool{}
	values := map[string]map[string]float64{}
	for _, c := range drawn {
		for _, name := range c.Reads() {
			m, ok := s.Scrape.Family(name)
			if !ok {
				continue
			}
			for _, sample := range m.Samples {
				key := sample.Labels[g.By]
				if !seen[key] {
					seen[key] = true
					order = append(order, key)
				}
				if values[key] == nil {
					values[key] = map[string]float64{}
				}
				values[key][name] = sample.Value
			}
		}
	}
	sort.Strings(order)

	// **The label column reads left and the numbers read right**, which is how
	// a table of names against quantities is read. tabwriter aligns a whole
	// writer one way, so the first column is padded to its own width here and
	// the right-alignment then has nothing left to do to it.
	label := len(g.By)
	for _, key := range order {
		if len(key) > label {
			label = len(key)
		}
	}
	pad := func(s string) string { return s + strings.Repeat(" ", label-len(s)) }

	tw := tabwriter.NewWriter(b, 0, 0, 2, ' ', tabwriter.AlignRight)
	// No indent written here: tabwriter right-aligns every column, so its own
	// two spaces of padding land in front of the first cell and are the indent.
	fmt.Fprintf(tw, "%s\t%s\t\n", pad(g.By),
		strings.Join(headings(drawn), "\t"))
	for _, key := range order {
		row := key
		cells := make([]string, 0, len(drawn))
		for _, c := range drawn {
			resolve := func(name string) (float64, bool) {
				v, ok := values[row][name]
				return v, ok
			}
			cells = append(cells, cell(c, resolve))
		}
		fmt.Fprintf(tw, "%s\t%s\t\n", pad(key), strings.Join(cells, "\t"))
	}
	tw.Flush()
}

// cell is what one column draws for one row, or for the whole screen in a
// columns group.
//
// **An unknown value is a dash and never a zero**, whether it is a metric the
// scrape has no sample of - RFC 0005 does not measure a `latest` channel's bytes,
// so such a channel has none - or a computed column one of whose operands is
// missing. A zero would say the channel holds nothing.
func cell(c Column, resolve func(string) (float64, bool)) string {
	if !c.Computed() {
		v, ok := resolve(c.Metric)
		if !ok {
			return "-"
		}
		return format(c.Metric, v)
	}
	v := c.Expr.Eval(resolve)
	if !v.Known {
		return "-"
	}
	if v.IsBool {
		// **A condition reads as a word, and only when it is true.** The reason
		// to compute `floor > position_min` at all is that it is the one alert
		// RFC 0005 says the catalogue exists for, and a column of `no` with one
		// `yes` in it hides the `yes`.
		if v.Num != 0 {
			return "yes"
		}
		return "-"
	}
	switch c.Format {
	case "bytes":
		return bytesOf(v.Num)
	case "duration":
		return short(time.Duration(v.Num * float64(time.Second)))
	default:
		return count(v.Num)
	}
}

// headings is what each column is called on the screen: a computed column's own
// name, and for a metric the name with the prefix its neighbours share dropped.
func headings(cols []Column) []string {
	var metrics []string
	for _, c := range cols {
		if !c.Computed() {
			metrics = append(metrics, c.Metric)
		}
	}
	short := shortNames(metrics)
	out := make([]string, len(cols))
	at := 0
	for i, c := range cols {
		if c.Computed() {
			out[i] = c.Name
			continue
		}
		out[i] = short[at]
		at++
	}
	return out
}

// shortNames turns the metric names of one group into column headings, by
// dropping the prefix they share. Computed columns are not among them: each has
// a name its author chose, and including one here would let it shorten the
// others' headings.
//
// **Mechanical rather than configured.** The dashboard file's keys are the four
// the item specifies and an unknown one is refused, so a per-metric label would
// be a fifth. Dropping the shared prefix is what makes a channel table read
// `records bytes next_offset` instead of three names beginning
// `saguin_channel_`, and it needs nothing written down.
func shortNames(names []string) []string {
	if len(names) == 0 {
		return nil
	}
	prefix := "saguin_"
	if len(names) > 1 {
		prefix = commonPrefix(names)
		// Back up to the last underscore, so a shared prefix never cuts a word
		// in half: `saguin_queue_depth` beside `saguin_queue_delivered_total`
		// share `saguin_queue_de`, and `pth` is not a column heading.
		if i := strings.LastIndex(prefix, "_"); i >= 0 {
			prefix = prefix[:i+1]
		}
	}
	out := make([]string, len(names))
	for i, name := range names {
		short := strings.TrimPrefix(name, prefix)
		if short == "" {
			// One name that is the whole prefix. Keep it whole rather than print
			// an empty heading.
			short = strings.TrimPrefix(name, "saguin_")
		}
		out[i] = short
	}
	return out
}

func commonPrefix(names []string) string {
	prefix := names[0]
	for _, name := range names[1:] {
		for !strings.HasPrefix(name, prefix) {
			prefix = prefix[:len(prefix)-1]
			if prefix == "" {
				return ""
			}
		}
	}
	return prefix
}

// format turns a value into what goes on the screen, deciding from the metric's
// own name.
//
// **Three rules, and the name is what picks one**, so nothing has to be
// configured per metric: bytes get binary units, seconds get a duration, and
// everything else is a count with thousands separators.
func format(name string, v float64) string {
	switch {
	case math.IsNaN(v):
		return "NaN"
	case math.IsInf(v, 1):
		return "+Inf"
	case math.IsInf(v, -1):
		return "-Inf"
	}
	switch {
	case strings.HasSuffix(name, "_bytes"):
		return bytesOf(v)
	case strings.HasSuffix(name, "_seconds"):
		return short(time.Duration(v * float64(time.Second)))
	default:
		return count(v)
	}
}

// count writes a number with thousands separators.
func count(v float64) string {
	if v != math.Trunc(v) {
		// A gauge that is not whole - a fraction of a second somewhere, or a
		// ratio - kept to three places and no trailing zeroes.
		s := strconv.FormatFloat(v, 'f', 3, 64)
		s = strings.TrimRight(strings.TrimRight(s, "0"), ".")
		return s
	}
	digits := strconv.FormatFloat(math.Abs(v), 'f', 0, 64)
	var b strings.Builder
	if v < 0 {
		b.WriteByte('-')
	}
	for i, c := range digits {
		if i > 0 && (len(digits)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(c)
	}
	return b.String()
}

// bytesOf writes a byte count in the units saguin's own configuration is
// written in.
//
// **KiB rather than kB**, because `max_bytes: 16MiB` is what an operator wrote
// in the configuration file and a screen reporting 16.8 MB of it would be the
// same number in a vocabulary the broker does not use. RFC 0005's own cost
// tables are in KiB and MiB for the same reason.
func bytesOf(v float64) string {
	if v < 0 {
		return "-" + bytesOf(-v)
	}
	units := []string{"B", "KiB", "MiB", "GiB", "TiB", "PiB"}
	i := 0
	for v >= 1024 && i < len(units)-1 {
		v /= 1024
		i++
	}
	if i == 0 {
		return strconv.FormatFloat(v, 'f', 0, 64) + " B"
	}
	s := strconv.FormatFloat(v, 'f', 1, 64)
	return strings.TrimSuffix(s, ".0") + " " + units[i]
}

// short writes a duration the way an operator reads one: the two largest units
// that say anything, and no nanoseconds.
func short(d time.Duration) string {
	if d < 0 {
		return "-" + short(-d)
	}
	switch {
	case d < time.Second:
		return "0s"
	case d < time.Minute:
		return strconv.Itoa(int(d.Seconds())) + "s"
	case d < time.Hour:
		m := int(d.Minutes())
		if s := int(d.Seconds()) % 60; s != 0 {
			return fmt.Sprintf("%dm %ds", m, s)
		}
		return fmt.Sprintf("%dm", m)
	case d < 24*time.Hour:
		h := int(d.Hours())
		if m := int(d.Minutes()) % 60; m != 0 {
			return fmt.Sprintf("%dh %dm", h, m)
		}
		return fmt.Sprintf("%dh", h)
	default:
		days := int(d.Hours()) / 24
		if h := int(d.Hours()) % 24; h != 0 {
			return fmt.Sprintf("%dd %dh", days, h)
		}
		return fmt.Sprintf("%dd", days)
	}
}

func blankAs(s, or string) string {
	if s == "" {
		return or
	}
	return s
}
