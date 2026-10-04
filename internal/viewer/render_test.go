package viewer

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

const screenDashboard = `
refresh: 30s
groups:
  - title: Clients
    layout: columns
    metrics: [saguin_uptime_seconds, saguin_connections, saguin_sessions_offline, saguin_subscriptions]
  - title: Channels
    layout: rows
    by: channel
    metrics:
      - saguin_channel_records
      - saguin_channel_bytes
      - saguin_channel_next_offset
      - name: behind
        value: saguin_channel_next_offset - saguin_channel_consumer_position_min
      - name: data lost
        value: saguin_channel_floor_offset > saguin_channel_consumer_position_min
      - name: per record
        value: saguin_channel_bytes / saguin_channel_records
        format: bytes
  - title: Queues
    layout: rows
    by: channel
    metrics: [saguin_queue_depth]
`

// screen builds the drawing the fixture below is of: a scrape stamped at a known
// moment, read twelve seconds before the redraw, with a dashboard asking for a
// refresh the broker's interval raises.
func screen(t *testing.T, width int) (Screen, *Dashboard) {
	t.Helper()
	body, err := os.ReadFile(filepath.Join("testdata", "screen-metrics.txt"))
	if err != nil {
		t.Fatal(err)
	}
	at := time.Unix(1700000000, 0)
	s, err := ParseScrape(string(body), at)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "dashboard.yaml")
	if err := os.WriteFile(path, []byte(screenDashboard), 0o600); err != nil {
		t.Fatal(err)
	}
	d, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := d.Validate(s); err != nil {
		t.Fatal(err)
	}
	return Screen{
		Dashboard: d, Scrape: s,
		Where:          "/run/saguin/operations.sock",
		Now:            at.Add(12 * time.Second),
		Interval:       d.Interval(time.Minute),
		Written:        d.Written(),
		BrokerInterval: time.Minute,
		Width:          width,
	}, d
}

func draw(t *testing.T, s Screen) string {
	t.Helper()
	var b strings.Builder
	if err := s.Render(&b); err != nil {
		t.Fatal(err)
	}
	return b.String()
}

// TestTheScreenMatchesItsFixture is the whole drawing against a file, so that a
// change to any part of the layout shows up in a diff rather than in somebody's
// terminal. Run with -update to rewrite the fixture after an intended change,
// and read the diff before you do.
var update = os.Getenv("UPDATE_FIXTURES") == "1"

func TestTheScreenMatchesItsFixture(t *testing.T) {
	s, _ := screen(t, 80)
	got := draw(t, s)
	path := filepath.Join("testdata", "screen.txt")
	if update {
		if err := os.WriteFile(path, []byte(got), 0o600); err != nil {
			t.Fatal(err)
		}
		t.Fatal("fixture rewritten; run again without UPDATE_FIXTURES=1")
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if got != string(want) {
		t.Errorf("the screen has changed.\n--- want ---\n%s\n--- got ---\n%s",
			want, got)
	}
	// **Nothing on the screen is wider than the terminal**, except where one
	// cell is: the wrap never splits a cell, and a table's width comes from the
	// scrape.
	for _, line := range strings.Split(strings.TrimRight(got, "\n"), "\n") {
		if len(line) > 80 {
			t.Errorf("a line is %d characters wide against a width of 80: %q",
				len(line), line)
		}
	}
}

// TestTheAgeIsOfTheScrapeRatherThanOfTheRedraw. One reading, drawn twice at
// different moments: the age moves with the clock and the reading does not, so a
// screen redrawn from a scrape that failed to refresh says how stale it is
// instead of resetting to nothing.
func TestTheAgeIsOfTheScrapeRatherThanOfTheRedraw(t *testing.T) {
	s, _ := screen(t, 120)
	for _, c := range []struct {
		after time.Duration
		says  string
	}{
		{0, "read 0s ago"},
		{12 * time.Second, "read 12s ago"},
		{95 * time.Second, "read 1m 35s ago"},
	} {
		s.Now = s.Scrape.At.Add(c.after)
		got := draw(t, s)
		if !strings.Contains(got, c.says) {
			t.Errorf("drawn %v after the scrape, the screen does not say %q:\n%s",
				c.after, c.says, firstLines(got, 2))
		}
	}
	// A clock that went backwards between the scrape and the redraw is 0s rather
	// than a negative age.
	s.Now = s.Scrape.At.Add(-time.Hour)
	if got := draw(t, s); !strings.Contains(got, "read 0s ago") {
		t.Errorf("a redraw before the scrape says: %s", firstLines(got, 2))
	}
}

// TestTheScreenSaysTheIntervalItSettledOn. RFC 0005 refuses to raise a
// configured interval quietly, on the grounds that an operator who writes 10s
// and is silently given a minute reads their graphs at the wrong scale. The same
// argument applies to a screen.
func TestTheScreenSaysTheIntervalItSettledOn(t *testing.T) {
	s, d := screen(t, 200)
	if want := time.Minute; s.Interval != want {
		t.Fatalf("the fixture's 30s settled on %v, want %v", s.Interval, want)
	}
	got := draw(t, s)
	for _, want := range []string{"redrawing every 1m", "30s in the dashboard",
		"broker recomputes every 1m"} {
		if !strings.Contains(got, want) {
			t.Errorf("the screen does not say %q:\n%s", want, firstLines(got, 3))
		}
	}
	// And where nothing was clamped, it does not explain a clamp that did not
	// happen.
	s.Interval, s.Written = d.Interval(10*time.Second), d.Written()
	got = draw(t, s)
	if strings.Contains(got, "in the dashboard") {
		t.Errorf("an unclamped refresh is explained as clamped:\n%s",
			firstLines(got, 3))
	}
	if !strings.Contains(got, "redrawing every 30s") {
		t.Errorf("the settled interval is not on the screen:\n%s", firstLines(got, 3))
	}
}

// **There was a case here about an assumed interval, and it is gone with the
// behaviour.** This program carried RFC 0005's floor as a fallback while the
// broker's resolved configuration omitted a min_scrape_interval left at its
// default, and the screen said "assumed" so that a number from this program's own
// default could not be mistaken for the broker's. saguin fills the default in
// now, so there is one number, it comes from the broker, and a broker that serves
// none is refused before a screen is drawn - which TestAnAbsentIntervalIsRefused
// covers.

// TestNoneOfItIsSilent: a group the broker has no metrics for keeps its heading
// and says which metrics those were, and the names are repeated once at the foot.
func TestNoneOfItIsSilent(t *testing.T) {
	body, err := os.ReadFile(filepath.Join("testdata", "screen-metrics.txt"))
	if err != nil {
		t.Fatal(err)
	}
	sc, err := ParseScrape(string(body), time.Unix(1700000000, 0))
	if err != nil {
		t.Fatal(err)
	}
	d, err := Default()
	if err != nil {
		t.Fatal(err)
	}
	absent, err := d.Validate(sc)
	if err != nil {
		t.Fatal(err)
	}
	if len(absent) == 0 {
		t.Fatal("this fixture carries no bridge or refusal families, so something " +
			"should be absent - an empty list means the case is comparing nothing")
	}
	got := draw(t, Screen{Dashboard: d, Scrape: sc, Where: "/run/x.sock",
		Now: sc.At, Interval: time.Minute, Written: time.Minute,
		BrokerInterval: time.Minute, Absent: absent, Width: 100})
	if !strings.Contains(got, "Bridges") {
		t.Error("a group with nothing on this broker lost its heading")
	}
	if !strings.Contains(got, "nothing on this broker: saguin_bridge_connected") {
		t.Errorf("the Bridges group does not say what it has none of:\n%s", got)
	}
	if !strings.Contains(got, "not served by this broker:") {
		t.Errorf("the foot of the screen does not list what was absent:\n%s", got)
	}
}

// TestTheHeaderNamesTheBrokerAndTheDoor - and no hostname, because RFC 0005
// publishes none and one invented here would be this machine's rather than the
// broker's.
func TestTheHeaderNamesTheBrokerAndTheDoor(t *testing.T) {
	s, _ := screen(t, 120)
	first := strings.SplitN(draw(t, s), "\n", 2)[0]
	for _, want := range []string{"saguin 0.1.0", "broker_id edge-07",
		"/run/saguin/operations.sock"} {
		if !strings.Contains(first, want) {
			t.Errorf("the header does not carry %q: %q", want, first)
		}
	}
	host, _ := os.Hostname()
	if host != "" && strings.Contains(first, host) {
		t.Errorf("the header carries this machine's hostname, which is not the "+
			"broker's: %q", first)
	}
}

// TestAScrapeWithNoBuildInfoSaysSo rather than printing "saguin" with two gaps
// where the version and the id belong.
func TestAScrapeWithNoBuildInfoSaysSo(t *testing.T) {
	sc, err := ParseScrape("# HELP saguin_connections n\n# TYPE saguin_connections gauge\nsaguin_connections 1\n",
		time.Unix(1700000000, 0))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "d.yaml")
	os.WriteFile(path, []byte("refresh: 60s\ngroups:\n  - title: A\n    layout: columns\n    metrics: [saguin_connections]\n"), 0o600)
	d, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := d.Validate(sc); err != nil {
		t.Fatal(err)
	}
	got := draw(t, Screen{Dashboard: d, Scrape: sc, Where: "/run/x.sock",
		Now: sc.At, Interval: time.Minute, Written: time.Minute,
		BrokerInterval: time.Minute, Width: 80})
	if !strings.Contains(got, "no saguin_build_info in this scrape") {
		t.Errorf("a scrape with no build info draws a header that claims one:\n%s",
			firstLines(got, 1))
	}
}

// TestARowKeepsEveryCellWhenItWraps. The first version of the wrap dropped the
// last cell whenever it landed on a new line, so `subscriptions 118` was on no
// screen - which is a number an operator would look for and not find, with
// nothing saying it was missing.
func TestARowKeepsEveryCellWhenItWraps(t *testing.T) {
	for _, width := range []int{40, 60, 80, 120, 200} {
		s, _ := screen(t, width)
		got := draw(t, s)
		for _, want := range []string{"uptime_seconds", "connections 41",
			"sessions_offline 6", "subscriptions 118"} {
			if !strings.Contains(got, want) {
				t.Errorf("at width %d the screen does not carry %q:\n%s", width,
					want, got)
			}
		}
	}
}

// TestNarrowerIsMoreLinesRatherThanLongerOnes.
func TestNarrowerIsMoreLinesRatherThanLongerOnes(t *testing.T) {
	wide, _ := screen(t, 200)
	narrow, _ := screen(t, 40)
	if a, b := len(strings.Split(draw(t, wide), "\n")), len(strings.Split(draw(t, narrow), "\n")); b <= a {
		t.Errorf("a 40-column screen is %d lines and a 200-column one is %d; "+
			"narrower should wrap to more", b, a)
	}
}

// TestWidthFallsBackToEightyWithNoTerminal, which is what a pipe, a cron job or
// a redirect to a file is.
func TestWidthFallsBackToEightyWithNoTerminal(t *testing.T) {
	if got := Width(); got <= 0 {
		t.Errorf("Width() is %d", got)
	}
	// Under `go test` stdout is not a terminal, so this is the fallback path.
	if got := Width(); got != FallbackWidth {
		t.Logf("stdout is a terminal of %d columns here, so the fallback was not "+
			"the path taken", got)
	}
	s, _ := screen(t, 0)
	if got := draw(t, s); !strings.Contains(got, "subscriptions 118") {
		t.Errorf("a screen with no width given lost a cell:\n%s", got)
	}
}

// TestAnEntityMissingOneMetricGetsADashRatherThanAZero. RFC 0005:
// saguin_channel_bytes does not measure a `latest` channel, so such a channel
// has no sample of it - and a zero would say the channel holds nothing.
func TestAnEntityMissingOneMetricGetsADashRatherThanAZero(t *testing.T) {
	sc, err := ParseScrape(`# HELP saguin_channel_records n
# TYPE saguin_channel_records gauge
saguin_channel_records{channel="events"} 6
saguin_channel_records{channel="state"} 1
# HELP saguin_channel_bytes n
# TYPE saguin_channel_bytes gauge
saguin_channel_bytes{channel="events"} 348
`, time.Unix(1700000000, 0))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "d.yaml")
	os.WriteFile(path, []byte("refresh: 60s\ngroups:\n  - title: Channels\n    layout: rows\n    by: channel\n    metrics: [saguin_channel_records, saguin_channel_bytes]\n"), 0o600)
	d, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := d.Validate(sc); err != nil {
		t.Fatal(err)
	}
	got := draw(t, Screen{Dashboard: d, Scrape: sc, Where: "/run/x.sock",
		Now: sc.At, Interval: time.Minute, Written: time.Minute,
		BrokerInterval: time.Minute, Width: 80})
	var stateRow string
	for _, line := range strings.Split(got, "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), "state") {
			stateRow = line
		}
	}
	if stateRow == "" {
		t.Fatalf("no row for the latest channel:\n%s", got)
	}
	if !strings.HasSuffix(strings.TrimSpace(stateRow), "-") {
		t.Errorf("a channel with no bytes sample draws %q; it should be a dash, "+
			"because a zero says the channel holds nothing", strings.TrimSpace(stateRow))
	}
}

// TestFormatPicksItsRuleFromTheName: the item's two rules - counts get thousands
// separators and a `*_bytes` metric gets human units - plus durations for
// `*_seconds`, which is the same kind of rule and is what stops uptime reading
// as 93,784.
func TestFormatPicksItsRuleFromTheName(t *testing.T) {
	for _, c := range []struct {
		name string
		v    float64
		want string
	}{
		{"saguin_channel_records", 1204882, "1,204,882"},
		{"saguin_channel_records", 412, "412"},
		{"saguin_channel_records", 0, "0"},
		{"saguin_connections", 1000, "1,000"},
		{"saguin_channel_bytes", 0, "0 B"},
		{"saguin_channel_bytes", 999, "999 B"},
		{"saguin_channel_bytes", 1024, "1 KiB"},
		{"saguin_channel_bytes", 432164864, "412.1 MiB"},
		{"saguin_provider_max_bytes", 17179869184, "16 GiB"},
		{"saguin_uptime_seconds", 63.6, "1m 3s"},
		{"saguin_uptime_seconds", 93784.5, "1d 2h"},
		{"saguin_uptime_seconds", 0.5, "0s"},
		{"saguin_max_session_expiry_seconds", 2592000, "30d"},
		// Not a count of anything whole, so it keeps its places and loses the
		// trailing zeroes.
		{"saguin_odd", 1.5, "1.5"},
		{"saguin_odd", -1.5, "-1.5"},
		{"saguin_odd", -2500, "-2,500"},
	} {
		if got := format(c.name, c.v); got != c.want {
			t.Errorf("format(%s, %v) = %q, want %q", c.name, c.v, got, c.want)
		}
	}
}

// TestFormatOnValuesThatAreNotNumbers, which the exposition format allows.
func TestFormatOnValuesThatAreNotNumbers(t *testing.T) {
	sc, err := ParseScrape("saguin_a NaN\nsaguin_b +Inf\nsaguin_c -Inf\n", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{"saguin_a": "NaN", "saguin_b": "+Inf",
		"saguin_c": "-Inf"} {
		v, _ := sc.One(name)
		if got := format(name, v); got != want {
			t.Errorf("format(%s) = %q, want %q", name, got, want)
		}
	}
}

// TestShortNamesDropTheSharedPrefixAtAWordBoundary. The trap is a prefix that
// ends mid-word: `saguin_queue_depth` and `saguin_queue_delivered_total` share
// `saguin_queue_de`, and `pth` is not a column heading.
func TestShortNamesDropTheSharedPrefixAtAWordBoundary(t *testing.T) {
	for _, c := range []struct {
		in, want []string
	}{
		{[]string{"saguin_channel_records", "saguin_channel_bytes"},
			[]string{"records", "bytes"}},
		// The real pair from the shipped default, and the real trap: these two
		// share `saguin_queue_d`.
		{[]string{"saguin_queue_depth", "saguin_queue_inflight",
			"saguin_queue_dead_lettered_total"},
			[]string{"depth", "inflight", "dead_lettered_total"}},
		{[]string{"saguin_connections", "saguin_subscriptions"},
			[]string{"connections", "subscriptions"}},
		{[]string{"saguin_publish_refused_total", "saguin_connections_refused_total"},
			[]string{"publish_refused_total", "connections_refused_total"}},
		{[]string{"saguin_queue_depth"}, []string{"queue_depth"}},
		// One name that is a prefix of the other, which is where backing up to
		// the underscore takes the most off: the shared prefix is the whole
		// shorter name, so what is left is the word after the last underscore
		// in it. Both headings still differ, which is what matters.
		{[]string{"saguin_shares_held_total", "saguin_shares_held_total_extra"},
			[]string{"total", "total_extra"}},
	} {
		got := shortNames(c.in)
		if len(got) != len(c.want) {
			t.Fatalf("shortNames(%v) = %v", c.in, got)
		}
		for i := range got {
			if got[i] != c.want[i] {
				t.Errorf("shortNames(%v)[%d] = %q, want %q", c.in, i, got[i], c.want[i])
			}
		}
	}
}

func firstLines(s string, n int) string {
	lines := strings.Split(s, "\n")
	if len(lines) > n {
		lines = lines[:n]
	}
	return strings.Join(lines, "\n")
}

// TestAComputedColumnDrawsItsAnswerRatherThanItsOperands. The fixture above
// covers the whole screen; this says what each kind of cell is, so a change to
// one is a failure here rather than only a diff in the golden file.
func TestAComputedColumnDrawsItsAnswerRatherThanItsOperands(t *testing.T) {
	s, _ := screen(t, 100)
	got := draw(t, s)
	rows := map[string]string{}
	for _, line := range strings.Split(got, "\n") {
		f := strings.Fields(line)
		if len(f) > 3 {
			rows[f[0]] = line
		}
	}
	// events: next 1,204,883 against a lowest stored position of 899,998.
	if !strings.Contains(rows["events"], "304,885") {
		t.Errorf("the lag is not on the events row: %q", rows["events"])
	}
	// **The condition reads as a word and only when it is true.** events has a
	// floor of 900,000 above that position, so retention has passed a consumer -
	// the one alert RFC 0005 says the catalogue exists for. telemetry has not.
	if !strings.HasSuffix(strings.TrimSpace(rows["events"]), "359 B") ||
		!strings.Contains(rows["events"], "yes") {
		t.Errorf("events should say yes and end in a byte figure: %q", rows["events"])
	}
	if strings.Contains(rows["telemetry"], "yes") {
		t.Errorf("telemetry has lost nothing and says yes: %q", rows["telemetry"])
	}
	// jobs is a queue: no consumer position at all, so both computed columns are
	// unknown rather than nought. A zero would say nothing is behind.
	//
	// **Counted rather than indexed**, because a formatted cell is two fields -
	// `1.1 MiB` - so the first draft of this read the wrong column and reported a
	// row that was in fact correct.
	dashes := func(row string) int {
		n := 0
		for _, f := range strings.Fields(row) {
			if f == "-" {
				n++
			}
		}
		return n
	}
	if got := dashes(rows["jobs"]); got != 2 {
		t.Errorf("jobs has no consumer position, so both computed cells should be "+
			"a dash; found %d: %q", got, rows["jobs"])
	}
	if got := dashes(rows["events"]); got != 0 {
		t.Errorf("events can answer both computed columns and drew %d dashes: %q",
			got, rows["events"])
	}
	// telemetry knows its lag and has lost nothing, so exactly one dash - the
	// condition that is not true.
	if got := dashes(rows["telemetry"]); got != 1 {
		t.Errorf("telemetry drew %d dashes: %q", got, rows["telemetry"])
	}
	if strings.Contains(rows["jobs"], " 0 ") {
		t.Errorf("an unknown computed cell was drawn as a zero: %q", rows["jobs"])
	}
}

// TestAComputedColumnsFormatIsTheOneItWasGiven, since its name cannot say - the
// three name-based rules are about metrics.
func TestAComputedColumnsFormatIsTheOneItWasGiven(t *testing.T) {
	sc, err := ParseScrape("saguin_a 4096\nsaguin_b 2\n", time.Unix(1700000000, 0))
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct{ format, want string }{
		{"", "2,048"},
		{"number", "2,048"},
		{"bytes", "2 KiB"},
		{"duration", "34m 8s"},
	} {
		body := "refresh: 60s\ngroups:\n  - title: A\n    layout: columns\n    metrics:\n" +
			"      - name: worked out\n        value: saguin_a / saguin_b\n"
		if c.format != "" {
			body += "        format: " + c.format + "\n"
		}
		path := filepath.Join(t.TempDir(), "d.yaml")
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		d, err := Load(path)
		if err != nil {
			t.Fatalf("format %q: %v", c.format, err)
		}
		if _, err := d.Validate(sc); err != nil {
			t.Fatal(err)
		}
		got := draw(t, Screen{Dashboard: d, Scrape: sc, Where: "/run/x.sock",
			Now: sc.At, Interval: time.Minute, Written: time.Minute,
			BrokerInterval: time.Minute, Width: 80})
		if !strings.Contains(got, "worked out "+c.want) {
			t.Errorf("format %q drew %q, want %q", c.format,
				strings.TrimSpace(strings.Split(got, "\n")[4]), c.want)
		}
	}
}

// TestAComputedColumnDoesNotShortenItsNeighboursHeadings. The shared-prefix rule
// is about metric names; a column called `behind` has no prefix to share, and
// letting it into that calculation would leave the real headings full length.
func TestAComputedColumnDoesNotShortenItsNeighboursHeadings(t *testing.T) {
	expr, err := ParseExpr("saguin_channel_next_offset - saguin_channel_floor_offset")
	if err != nil {
		t.Fatal(err)
	}
	got := headings([]Column{
		{Metric: "saguin_channel_records"},
		{Name: "behind", Expr: expr},
		{Metric: "saguin_channel_bytes"},
	})
	want := []string{"records", "behind", "bytes"}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("heading %d is %q, want %q", i, got[i], want[i])
		}
	}
}

// TestTheLinesThatReportAbsenceWrap.
//
// **The fixture case could not catch this**, because its dashboard names only
// metrics the fixture serves, so it has no absences and never draws the two lines
// that report them. They were printed straight rather than wrapped, and on a
// broker with no bridge one ran to 128 characters and the foot of the screen to
// 193 - the two lines most likely to be long, since both are lists of metric
// names.
//
// **Only these two lines are promised to fit**, and the promise is narrow on
// purpose: a table's columns come from the scrape and a cell is never split, so a
// wide table on a narrow terminal is longer than the terminal. At eighty columns
// and above - the fallback, and what a terminal was before anybody could ask -
// the shipped dashboard fits entirely, which the case below also holds.
func TestTheLinesThatReportAbsenceWrap(t *testing.T) {
	d, sc, absent := defaultAgainstAPartialScrape(t)
	for _, width := range []int{40, 60, 80, 100, 200} {
		got := draw(t, Screen{Dashboard: d, Scrape: sc, Where: "/run/saguin/operations.sock",
			Now: sc.At.Add(time.Second), Interval: time.Minute, Written: 30 * time.Second,
			BrokerInterval: time.Minute, Absent: absent, Width: width})
		reported := 0
		for _, line := range strings.Split(got, "\n") {
			// The continuation of a wrapped absence line starts with a metric
			// name, so it is checked too - that is the half that was over-long.
			trimmed := strings.TrimSpace(line)
			if !strings.HasPrefix(trimmed, "nothing on this broker:") &&
				!strings.HasPrefix(trimmed, "not served by this broker:") &&
				!strings.HasPrefix(trimmed, "saguin_") {
				continue
			}
			reported++
			// **A name is never split**, as a table's cell is not: a line
			// holding one name that is itself wider than the terminal is as
			// narrow as that name allows, and writeList puts it alone on its
			// line. Anything more on a line than one name must fit.
			if len(line) > width && len(strings.Fields(trimmed)) > 1 {
				t.Fatalf("at width %d an absence line of %d characters: %q",
					width, len(line), line)
			}
		}
		if reported < 2 {
			t.Errorf("at width %d only %d absence lines were found, so this case "+
				"is checking almost nothing", width, reported)
		}
	}
}

// TestTheShippedDashboardFitsEightyColumns, which is the fallback width and what
// a terminal is when there is none to ask - a pipe, a cron job, a redirect.
func TestTheShippedDashboardFitsEightyColumns(t *testing.T) {
	d, sc, absent := defaultAgainstAPartialScrape(t)
	got := draw(t, Screen{Dashboard: d, Scrape: sc, Where: "/run/saguin/operations.sock",
		Now: sc.At.Add(time.Second), Interval: time.Minute, Written: time.Minute,
		BrokerInterval: time.Minute, Absent: absent,
		Width: FallbackWidth})
	for _, line := range strings.Split(strings.TrimRight(got, "\n"), "\n") {
		if len(line) > FallbackWidth {
			t.Errorf("a line of %d characters at the fallback width: %q",
				len(line), line)
		}
	}
}

// defaultAgainstAPartialScrape is the shipped dashboard against a broker missing
// whole groups' worth of metrics, which is the ordinary broker rather than an
// awkward one: most have no bridge and have refused nothing.
func defaultAgainstAPartialScrape(t *testing.T) (*Dashboard, *Scrape, []string) {
	t.Helper()
	body, err := os.ReadFile(filepath.Join("testdata", "screen-metrics.txt"))
	if err != nil {
		t.Fatal(err)
	}
	sc, err := ParseScrape(string(body), time.Unix(1700000000, 0))
	if err != nil {
		t.Fatal(err)
	}
	d, err := Default()
	if err != nil {
		t.Fatal(err)
	}
	absent, err := d.Validate(sc)
	if err != nil {
		t.Fatal(err)
	}
	if len(absent) < 4 {
		t.Fatalf("only %d metrics absent; these cases need a scrape missing enough "+
			"to make the reporting lines long: %v", len(absent), absent)
	}
	return d, sc, absent
}

// TestAMetricIsReportedAbsentOnceHoweverManyColumnsWantedIt.
//
// **Found driving the shipped dashboard against a broker nothing had
// consumed from.** `behind` and `data lost` both read
// saguin_channel_consumer_position_min, so the foot of the screen read "not served
// by this broker: saguin_channel_consumer_position_min,
// saguin_channel_consumer_position_min, ...". The list is what this broker does
// not serve, not a tally of disappointed columns.
func TestAMetricIsReportedAbsentOnceHoweverManyColumnsWantedIt(t *testing.T) {
	body, err := os.ReadFile(filepath.Join("testdata", "screen-metrics.txt"))
	if err != nil {
		t.Fatal(err)
	}
	// Take the consumer position out, which is the state of a broker nothing has
	// consumed from durably - and the state this was found in.
	trimmed := regexp.MustCompile(`(?m)^.*saguin_channel_consumer_position_min.*\n`).
		ReplaceAllString(string(body), "")
	if strings.Contains(trimmed, "saguin_channel_consumer_position_min") {
		t.Fatal("the metric is still in the scrape, so this case is not in the " +
			"state it says it is")
	}
	sc, err := ParseScrape(trimmed, time.Unix(1700000000, 0))
	if err != nil {
		t.Fatal(err)
	}
	d, err := Default()
	if err != nil {
		t.Fatal(err)
	}
	absent, err := d.Validate(sc)
	if err != nil {
		t.Fatal(err)
	}

	// Two columns of the shipped default read that metric, which is what makes
	// this case about the right thing - if one day only one does, it stops being
	// evidence and should be pointed at whatever the new pair is.
	readers := 0
	for _, g := range d.Groups {
		for _, c := range g.Columns {
			for _, name := range c.Reads() {
				if name == "saguin_channel_consumer_position_min" {
					readers++
				}
			}
		}
	}
	if readers < 2 {
		t.Fatalf("only %d columns of the shipped dashboard read that metric, so a "+
			"duplicate could not arise and this case proves nothing", readers)
	}

	seen := map[string]int{}
	for _, name := range absent {
		seen[name]++
		if seen[name] > 1 {
			t.Errorf("%s is reported absent %d times; the list is what this broker "+
				"does not serve, not a tally of the columns that wanted it",
				name, seen[name])
		}
	}
	// And on the screen itself, which is where a reader meets it.
	drawn := draw(t, Screen{Dashboard: d, Scrape: sc, Where: "/run/x.sock",
		Now: sc.At, Interval: time.Minute, Written: time.Minute,
		BrokerInterval: time.Minute, Absent: absent, Width: 200})
	for _, line := range strings.Split(drawn, "\n") {
		if !strings.Contains(line, "not served by this broker") &&
			!strings.Contains(line, "nothing on this broker") {
			continue
		}
		counted := map[string]int{}
		for _, f := range strings.Fields(strings.TrimRight(line, ",")) {
			f = strings.TrimSuffix(f, ",")
			if !strings.HasPrefix(f, "saguin_") {
				continue
			}
			counted[f]++
			if counted[f] > 1 {
				t.Errorf("the screen names %s twice on one line: %q", f, line)
			}
		}
	}
}
