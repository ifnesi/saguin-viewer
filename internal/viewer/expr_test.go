package viewer

import (
	"strings"
	"testing"
)

// values is a stand-in scrape: a resolver over a plain map, so a case can say
// what the broker serves and what it does not.
func values(m map[string]float64) func(string) (float64, bool) {
	return func(name string) (float64, bool) {
		v, ok := m[name]
		return v, ok
	}
}

// TestTheTwoReadingsRFC0005AsksFor. A subtraction and a comparison, which are
// what the catalogue exists for and what no metric can carry: the lag would need
// a threshold nobody can choose for an operator, or a series per consumer.
func TestTheTwoReadingsRFC0005AsksFor(t *testing.T) {
	scrape := values(map[string]float64{
		"saguin_channel_next_offset":           1204883,
		"saguin_channel_floor_offset":          900000,
		"saguin_channel_consumer_position_min": 899_998,
	})

	lag, err := ParseExpr("saguin_channel_next_offset - saguin_channel_consumer_position_min")
	if err != nil {
		t.Fatal(err)
	}
	got := lag.Eval(scrape)
	if !got.Known || got.IsBool || got.Num != 304885 {
		t.Errorf("lag is %+v, want 304885", got)
	}

	lost, err := ParseExpr("saguin_channel_floor_offset > saguin_channel_consumer_position_min")
	if err != nil {
		t.Fatal(err)
	}
	got = lost.Eval(scrape)
	if !got.Known || !got.IsBool || got.Num == 0 {
		t.Errorf("the alert is %+v, want a true boolean - the floor is above the "+
			"lowest stored position, so retention has passed a consumer", got)
	}
	// And the other way, where nothing has been lost.
	got = lost.Eval(values(map[string]float64{
		"saguin_channel_floor_offset": 1, "saguin_channel_consumer_position_min": 5}))
	if !got.Known || !got.IsBool || got.Num != 0 {
		t.Errorf("the alert is %+v on a channel where nothing was lost", got)
	}
}

// TestUnknownSpreads is the rule that keeps a computed column honest: one
// operand the broker has no sample for makes the answer unknown rather than a
// number worked out from a zero that was never measured. A `latest` channel has
// no bytes and no consumer position.
func TestUnknownSpreads(t *testing.T) {
	e, err := ParseExpr("saguin_channel_next_offset - saguin_channel_consumer_position_min")
	if err != nil {
		t.Fatal(err)
	}
	got := e.Eval(values(map[string]float64{"saguin_channel_next_offset": 7}))
	if got.Known {
		t.Errorf("a missing operand gave %v; the answer is not known", got.Num)
	}
	// The comparison too, and in both positions.
	c, _ := ParseExpr("saguin_a > saguin_b")
	if v := c.Eval(values(map[string]float64{"saguin_a": 1})); v.Known {
		t.Errorf("a comparison with a missing right-hand side answered %v", v)
	}
	if v := c.Eval(values(map[string]float64{"saguin_b": 1})); v.Known {
		t.Errorf("a comparison with a missing left-hand side answered %v", v)
	}
}

// TestDivisionByNothingIsUnknownRatherThanInfinity. A ratio whose denominator is
// zero is a question the scrape cannot answer - no deliveries yet, no records yet
// - and a cell reading +Inf says the opposite of "nothing has happened".
func TestDivisionByNothingIsUnknownRatherThanInfinity(t *testing.T) {
	e, err := ParseExpr("saguin_queue_acknowledged_total / saguin_queue_delivered_total")
	if err != nil {
		t.Fatal(err)
	}
	if v := e.Eval(values(map[string]float64{
		"saguin_queue_acknowledged_total": 3, "saguin_queue_delivered_total": 0})); v.Known {
		t.Errorf("3/0 answered %v", v.Num)
	}
	if v := e.Eval(values(map[string]float64{
		"saguin_queue_acknowledged_total": 3, "saguin_queue_delivered_total": 4})); !v.Known ||
		v.Num != 0.75 {
		t.Errorf("3/4 = %+v, want 0.75", v)
	}
}

// TestPrecedenceAndParentheses, because an operator who writes one and gets the
// other reads a wrong number with nothing saying so.
func TestPrecedenceAndParentheses(t *testing.T) {
	scrape := values(map[string]float64{"saguin_a": 10, "saguin_b": 4, "saguin_c": 2})
	for _, c := range []struct {
		src  string
		want float64
	}{
		{"saguin_a - saguin_b * saguin_c", 2},
		{"(saguin_a - saguin_b) * saguin_c", 12},
		{"saguin_a / saguin_c - saguin_b", 1},
		{"saguin_a - saguin_b - saguin_c", 4},
		{"saguin_a + 100", 110},
		{"-saguin_b + saguin_a", 6},
		{"saguin_a * 100 / saguin_c", 500},
		{"2 * (saguin_a - (saguin_b - saguin_c))", 16},
	} {
		e, err := ParseExpr(c.src)
		if err != nil {
			t.Fatalf("%s: %v", c.src, err)
		}
		if got := e.Eval(scrape); !got.Known || got.Num != c.want {
			t.Errorf("%s = %+v, want %v", c.src, got, c.want)
		}
	}
	// A comparison is outermost, so arithmetic on either side of it is worked out
	// first rather than the comparison binding tighter than the minus.
	e, _ := ParseExpr("saguin_a - saguin_b > saguin_c")
	if got := e.Eval(scrape); !got.IsBool || got.Num == 0 {
		t.Errorf("10 - 4 > 2 came out %+v", got)
	}
}

// TestTheMetricsAnExpressionReadsAreListedInOrder, because the loader holds each
// of them to the scrape exactly as it holds a bare column - which is how a typo
// inside a `value` is refused at startup rather than drawing a dash for ever.
func TestTheMetricsAnExpressionReadsAreListedInOrder(t *testing.T) {
	e, err := ParseExpr("saguin_channel_next_offset - saguin_channel_consumer_position_min + saguin_channel_next_offset / 2")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"saguin_channel_next_offset", "saguin_channel_consumer_position_min"}
	got := e.Metrics()
	if len(got) != len(want) {
		t.Fatalf("reads %v, want %v - a name written twice is one name", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("read %d is %q, want %q", i, got[i], want[i])
		}
	}
	if e.Source() == "" {
		t.Error("the expression does not keep what was written, so a message cannot quote it")
	}
}

// TestEveryWayAnExpressionIsRefused. Nothing is silently skipped, and each
// refusal quotes what was written - an expression is the one part of the file
// somebody gets wrong by a character.
func TestEveryWayAnExpressionIsRefused(t *testing.T) {
	for _, c := range []struct{ name, src, says string }{
		{"nothing at all", "", "empty"},
		{"only spaces", "   ", "empty"},
		{"a number and no metric", "1 + 2",
			"reads no metric, so it is the same number on every screen"},
		{"a dangling operator", "saguin_a -", "ends where a metric or a number should be"},
		{"two operators", "saguin_a - - - ", "ends where"},
		{"an unclosed bracket", "(saguin_a - saguin_b", "never closed"},
		{"a bracket that was never opened", "saguin_a)", "left over"},
		{"a stray closing bracket", ")saguin_a", "a `)` with no `(`"},
		{"a single equals", "saguin_a = saguin_b", "a single `=` compares nothing"},
		{"a bare bang", "saguin_a ! saguin_b", "a bare `!` is not an operator"},
		{"two comparisons", "saguin_a < saguin_b < saguin_c", "two comparisons in one"},
		{"a character this does not read", "saguin_a & saguin_b", `"&" is not something this reads`},
		{"a percent", "saguin_a % saguin_b", `"%" is not something this reads`},
		{"a function call", "min(saguin_a, saguin_b)", "is not something this reads"},
		{"a quoted string", `saguin_a == "x"`, "is not something this reads"},
	} {
		t.Run(c.name, func(t *testing.T) {
			_, err := ParseExpr(c.src)
			if err == nil {
				t.Fatalf("%q was accepted", c.src)
			}
			if !strings.Contains(err.Error(), c.says) {
				t.Errorf("the refusal does not say %q: %v", c.says, err)
			}
		})
	}
}

// TestWhatTheLanguageDeliberatelyDoesNotHave. These are refused on purpose
// rather than by oversight: the two readings RFC 0005 asks for are a subtraction
// and a comparison, and everything past that is a language rather than a column.
// If one of these is ever wanted it arrives with the failure that wanted it, and
// this case is the record that its absence was a decision.
func TestWhatTheLanguageDeliberatelyDoesNotHave(t *testing.T) {
	for _, src := range []string{
		"saguin_a and saguin_b",
		"saguin_a > 1 and saguin_b > 1",
		"sum(saguin_a)",
		"max(saguin_a, saguin_b)",
		"saguin_a if saguin_b else 0",
		"saguin_a ** 2",
	} {
		if _, err := ParseExpr(src); err == nil {
			t.Errorf("%q was accepted; this language has no such thing, and one "+
				"that quietly appeared is one nothing specifies", src)
		}
	}
}

// TestAMetricNameWithADigit, which saguin_qos2_held needs - the same omission
// that made the browser viewer's own pattern refuse it.
func TestAMetricNameWithADigit(t *testing.T) {
	e, err := ParseExpr("saguin_qos2_held - saguin_qos2_abandoned_total")
	if err != nil {
		t.Fatal(err)
	}
	if got := e.Metrics(); len(got) != 2 || got[0] != "saguin_qos2_held" {
		t.Errorf("reads %v", got)
	}
}
