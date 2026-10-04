package viewer

import (
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fixture is RFC 0005's own example scrape plus the label values the format
// allows and that example does not show.
//
// **The file carries no comment of its own**, and the first draft of it did:
// the parser refused the header explaining where the lines came from, because a
// comment that is neither HELP nor TYPE is a shape saguin does not write. That
// is the check working, so the provenance is here instead.
//
// The ordinary lines are transcribed from RFC 0005's "What a scrape looks
// like" - an append, a latest and a queue channel, one inbound bridge, a
// durable consumer behind the head, a worker holding three jobs, one broadcast
// publish and one refused retain. The rest are what that example does not show
// and the format allows on a label value: each of the three escapes, a
// backslash before something else, an empty value, a negative number, and a
// filter written with braces - which RFC 0005 says saguin_channel_info carries
// "as written, braces and all".
func fixture(t *testing.T) *Scrape {
	t.Helper()
	body, err := os.ReadFile(filepath.Join("testdata", "metrics.txt"))
	if err != nil {
		t.Fatalf("reading the fixture: %v", err)
	}
	s, err := ParseScrape(string(body), time.Unix(1700000000, 0))
	if err != nil {
		t.Fatalf("parsing the fixture: %v", err)
	}
	return s
}

// TestTheFixtureIsRead counts what the parse examined. Every case below asks
// about one family, so a parse that returned an empty scrape would pass all of
// them by finding nothing to disagree with.
func TestTheFixtureIsRead(t *testing.T) {
	s := fixture(t)
	if got, want := len(s.Metrics), 9; got != want {
		t.Fatalf("the fixture holds %d families, parsed %d: %v", want, got, s.Order)
	}
	samples := 0
	for _, m := range s.Metrics {
		samples += len(m.Samples)
		if m.Help == "" {
			t.Errorf("%s: no HELP", m.Name)
		}
		if m.Kind == "" {
			t.Errorf("%s: no TYPE", m.Name)
		}
	}
	if got, want := samples, 15; got != want {
		t.Errorf("parsed %d samples, the fixture has %d", got, want)
	}
}

// TestTheFamiliesKeepTheBrokersOrder is why Order exists: anything walking
// every family reports in the order the broker wrote them rather than in a
// Go map's, which changes between runs.
func TestTheFamiliesKeepTheBrokersOrder(t *testing.T) {
	s := fixture(t)
	want := []string{
		"saguin_build_info", "saguin_uptime_seconds", "saguin_connections",
		"saguin_connections_by_protocol", "saguin_channel_info",
		"saguin_channel_bytes", "saguin_channel_records",
		"saguin_publish_refused_total", "saguin_escapes_probe",
	}
	got := s.Order
	if len(got) < len(want) {
		t.Fatalf("only %d families in order: %v", len(got), got)
	}
	for i, name := range want {
		if got[i] != name {
			t.Errorf("family %d is %q, the body wrote %q", i, got[i], name)
		}
	}
}

// TestASampleWithNoLabelsIsKept is the defect the Python viewer shipped: it
// dropped every sample that carried no labels, so its page showed channels and
// no connection count. An integer and a float, because both shapes are in the
// body.
func TestASampleWithNoLabelsIsKept(t *testing.T) {
	s := fixture(t)
	for _, c := range []struct {
		name string
		want float64
	}{
		{"saguin_connections", 1},
		{"saguin_uptime_seconds", 63.6040226},
	} {
		v, ok := s.One(c.name)
		if !ok {
			t.Fatalf("%s: no sample", c.name)
		}
		if v != c.want {
			t.Errorf("%s = %v, want %v", c.name, v, c.want)
		}
		m, _ := s.Family(c.name)
		if m.Samples[0].Labels != nil {
			t.Errorf("%s: labels are %v, want none at all", c.name, m.Samples[0].Labels)
		}
	}
}

// TestANegativeValueSurvives because a value is a float and the sign is part
// of it: a parser reading the number with the name's own character rule would
// stop at the minus.
func TestANegativeValueSurvives(t *testing.T) {
	s := fixture(t)
	m, ok := s.Family("saguin_escapes_probe")
	if !ok {
		t.Fatal("no saguin_escapes_probe")
	}
	var found bool
	for _, sample := range m.Samples {
		if sample.Labels["unlabelled_neighbour"] == "yes" {
			found = true
			if sample.Value != -1.5 {
				t.Errorf("value is %v, want -1.5", sample.Value)
			}
		}
	}
	if !found {
		t.Error("the negative sample was not parsed")
	}
}

// TestAFilterKeepsItsBraces is the other defect the Python viewer shipped:
// the label parser split on a comma and cut `iot/+/{status,location}/+` in
// half. RFC 0005 says this metric carries the filter as written, braces and
// all, precisely because a brace stands for several plain filters and one
// series cannot hold both.
func TestAFilterKeepsItsBraces(t *testing.T) {
	s := fixture(t)
	m, _ := s.Family("saguin_channel_info")
	got := map[string]string{}
	for _, sample := range m.Samples {
		got[sample.Labels["channel"]] = sample.Labels["filter"]
	}
	if want := "iot/+/{status,location}/+"; got["state"] != want {
		t.Errorf("state's filter is %q, want %q", got["state"], want)
	}
	if want := "iot/+/events/+"; got["events"] != want {
		t.Errorf("events' filter is %q, want %q", got["events"], want)
	}
	if len(m.Samples) != 3 {
		t.Errorf("%d channels, want 3 - a label parser that ended a sample at "+
			"the first brace would find a different number", len(m.Samples))
	}
}

// TestTheThreeEscapesOnALabelValue. The exposition format defines three and no
// more, so a backslash before anything else stands for itself - which is what
// a Windows path or a regular expression would be made of.
func TestTheThreeEscapesOnALabelValue(t *testing.T) {
	s := fixture(t)
	m, _ := s.Family("saguin_escapes_probe")
	var labels map[string]string
	for _, sample := range m.Samples {
		if _, ok := sample.Labels["quote"]; ok {
			labels = sample.Labels
		}
	}
	if labels == nil {
		t.Fatal("the escapes sample was not parsed")
	}
	for _, c := range []struct{ label, want string }{
		{"quote", `a"b`},
		{"backslash", `a\b`},
		{"newline", "a\nb"},
		{"empty", ""},
		{"other", `a\tb`},
	} {
		if got := labels[c.label]; got != c.want {
			t.Errorf("%s = %q, want %q", c.label, got, c.want)
		}
	}
	// An empty value is a value, not an absent label: a reader asking whether
	// the label is there has to get yes.
	if _, ok := labels["empty"]; !ok {
		t.Error(`empty="" was dropped rather than kept as an empty value`)
	}
	if len(labels) != 5 {
		t.Errorf("%d labels, want 5: %v", len(labels), labels)
	}
}

// TestHelpIsTheRestOfTheLine. These sentences carry backticks, braces, quotes
// and dashes, and a parser that split on any of them would show half of one.
func TestHelpIsTheRestOfTheLine(t *testing.T) {
	s := fixture(t)
	m, _ := s.Family("saguin_channel_info")
	if !strings.Contains(m.Help, "{status,location}") {
		t.Errorf("the braces are gone from the description: %q", m.Help)
	}
	if !strings.HasSuffix(m.Help, `and "type".`) {
		t.Errorf("the description does not end where the line does: %q", m.Help)
	}
	if m.Kind != "gauge" {
		t.Errorf("TYPE is %q, want gauge", m.Kind)
	}
}

// TestALineShapeSaguinDoesNotWriteIsAnError. The item this tool was built for
// says it parses the three shapes saguin emits "and nothing else", and the
// reason to refuse rather than skip is the Python viewer's own history: a
// tolerant parser produced a screen that looked complete.
func TestALineShapeSaguinDoesNotWriteIsAnError(t *testing.T) {
	for _, c := range []struct{ name, body, says string }{
		{"a comment that is neither HELP nor TYPE",
			"# EOF\n", "neither HELP nor TYPE"},
		{"a HELP with no description",
			"# HELP saguin_odd\n", "no description"},
		{"a TYPE with no type",
			"# TYPE saguin_odd\n", "no type"},
		{"a sample with no value",
			"saguin_odd\n", "no value"},
		{"a value that is not a number",
			"saguin_odd fifteen\n", "is not a number"},
		{"a timestamp after the value",
			"saguin_odd 1 1700000000000\n", "after the value"},
		{"a line that starts with neither a name nor a hash",
			"1saguin_odd 1\n", "does not start with a metric name"},
		{"a label value that is not quoted",
			"saguin_odd{channel=events} 1\n", "is not quoted"},
		{"a label value that is never closed",
			"saguin_odd{channel=\"events} 1\n", "is not closed"},
		{"a label with no value",
			"saguin_odd{channel} 1\n", "has no value"},
	} {
		t.Run(c.name, func(t *testing.T) {
			_, err := ParseScrape(c.body, time.Now())
			if err == nil {
				t.Fatalf("%q was accepted", c.body)
			}
			if !strings.Contains(err.Error(), c.says) {
				t.Errorf("the error does not say %q: %v", c.says, err)
			}
			if !strings.Contains(err.Error(), "line 1") {
				t.Errorf("the error does not name the line: %v", err)
			}
		})
	}
}

// TestBlankLinesAreNotAnError, because a body ends with a newline and the
// split that reads it makes a last empty field.
func TestBlankLinesAreNotAnError(t *testing.T) {
	s, err := ParseScrape("saguin_connections 1\n\n", time.Now())
	if err != nil {
		t.Fatalf("a trailing newline was refused: %v", err)
	}
	if v, ok := s.One("saguin_connections"); !ok || v != 1 {
		t.Errorf("saguin_connections = %v %v", v, ok)
	}
}

// TestNaNAndInfinity are legal exposition values, so reading them must not be
// the error that a word is.
func TestNaNAndInfinity(t *testing.T) {
	s, err := ParseScrape("saguin_a NaN\nsaguin_b +Inf\n", time.Now())
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
	if v, _ := s.One("saguin_a"); !math.IsNaN(v) {
		t.Errorf("saguin_a = %v, want NaN", v)
	}
	if v, _ := s.One("saguin_b"); !math.IsInf(v, 1) {
		t.Errorf("saguin_b = %v, want +Inf", v)
	}
}

// TestADigitInAMetricName, which saguin_qos2_held needs. The Python viewer's
// pattern refused one until that metric arrived, because every name until then
// happened to be letters.
func TestADigitInAMetricName(t *testing.T) {
	s, err := ParseScrape("saguin_qos2_held 4\n", time.Now())
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
	if v, ok := s.One("saguin_qos2_held"); !ok || v != 4 {
		t.Errorf("saguin_qos2_held = %v %v", v, ok)
	}
}
