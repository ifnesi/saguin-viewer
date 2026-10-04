package viewer

import (
	"context"
	"errors"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// clock is a hand-wound clock: Run's sleeps advance it rather than waiting, so
// the loop can be driven through a dozen minute-long intervals in no time.
type clock struct {
	now    time.Time
	slept  []time.Duration
	ticks  atomic.Int64
	cancel context.CancelFunc
	// stopAfter cancels the context once this many sleeps have happened, which is
	// how a case says "let it draw three screens and then Ctrl-C it".
	stopAfter int
}

func (c *clock) Now() time.Time { return c.now }

func (c *clock) Sleep(ctx context.Context, d time.Duration) error {
	c.slept = append(c.slept, d)
	c.now = c.now.Add(d)
	if n := int(c.ticks.Add(1)); c.stopAfter > 0 && n >= c.stopAfter {
		c.cancel()
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
		return nil
	}
}

// runAgainst wires Run to a stand-in listener and a dashboard, with the clock
// under the test's control.
func runAgainst(t *testing.T, d *door, dashboard string, once bool,
	stopAfter int) (string, *clock, *door, error) {
	t.Helper()
	srv := httptest.NewServer(d.handler())
	t.Cleanup(srv.Close)

	var dash *Dashboard
	var err error
	if dashboard == "" {
		dash, err = Default()
	} else {
		path := filepath.Join(t.TempDir(), "dashboard.yaml")
		if err := os.WriteFile(path, []byte(dashboard), 0o600); err != nil {
			t.Fatal(err)
		}
		dash, err = Load(path)
	}
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	c := &clock{now: time.Unix(1700000000, 0), cancel: cancel, stopAfter: stopAfter}

	var out strings.Builder
	err = Run(ctx, Options{
		Source:    TCP(strings.TrimPrefix(srv.URL, "http://"), "", "", 5*time.Second),
		Dashboard: dash, Once: once, Out: &out,
		Now: c.Now, Sleep: c.Sleep, Width: func() int { return 100 },
	})
	return out.String(), c, d, err
}

const runMetrics = `# HELP saguin_build_info Always 1.
# TYPE saguin_build_info gauge
saguin_build_info{version="0.1.0",broker_id="edge-07"} 1
# HELP saguin_connections Clients connected now.
# TYPE saguin_connections gauge
saguin_connections 41
`

const oneGroup = `
refresh: 30s
groups:
  - title: Clients
    layout: columns
    metrics: [saguin_connections]
`

// TestOnceDrawsOneScreenAndReturns, and takes exactly two requests to do it: the
// operations section, then the scrape.
func TestOnceDrawsOneScreenAndReturns(t *testing.T) {
	d := &door{metrics: runMetrics,
		config: `{"operations":{"min_scrape_interval":"60s"}}`}
	// **stopAfter is a safety net rather than part of the case.** --once must
	// return without sleeping at all; giving the clock a stop means that a
	// version which fell into the loop instead fails here on the counts below,
	// rather than hanging until the test binary's own timeout - a defect a test
	// only catches by timing out is one nobody sees for ten minutes.
	out, c, d, err := runAgainst(t, d, oneGroup, true, 2)
	if err != nil {
		t.Fatalf("--once returned an error: %v", err)
	}
	if n := strings.Count(out, "broker_id edge-07"); n != 1 {
		t.Errorf("%d screens drawn, want 1:\n%s", n, out)
	}
	if len(c.slept) != 0 {
		t.Errorf("--once slept %v; it should return instead", c.slept)
	}
	if n := d.asked.Load(); n != 2 {
		t.Errorf("the broker was asked %d times, want 2 - the operations section "+
			"and one scrape", n)
	}
	// With nothing to redraw, the screen does not promise a redraw.
	if strings.Contains(out, "redrawing every") {
		t.Errorf("--once says it will redraw:\n%s", out)
	}
}

// TestTheLoopRedrawsOnTheSettledInterval: three screens, three scrapes after the
// first, and every sleep the interval the clamp settled on rather than the 30s
// the dashboard asked for.
func TestTheLoopRedrawsOnTheSettledInterval(t *testing.T) {
	d := &door{metrics: runMetrics,
		config: `{"operations":{"min_scrape_interval":"60s"}}`}
	out, c, d, err := runAgainst(t, d, oneGroup, false, 3)
	if err != nil {
		t.Fatalf("the loop returned an error: %v", err)
	}
	if n := strings.Count(out, "broker_id edge-07"); n != 3 {
		t.Errorf("%d screens drawn, want 3:\n%s", n, out)
	}
	if len(c.slept) != 3 {
		t.Fatalf("slept %d times, want 3: %v", len(c.slept), c.slept)
	}
	for i, d := range c.slept {
		if d != time.Minute {
			t.Errorf("sleep %d was %v, want the settled 1m rather than the "+
				"dashboard's 30s", i+1, d)
		}
	}
	// One request for the interval, then one scrape per screen. The third
	// screen's sleep cancels the context, so the fourth scrape never happens.
	if n := d.asked.Load(); n != 4 {
		t.Errorf("the broker was asked %d times, want 4: the operations section "+
			"and one scrape per screen", n)
	}
	// And the age is of each reading rather than climbing across the run: every
	// screen was drawn straight after its own scrape.
	if n := strings.Count(out, "read 0s ago"); n != 3 {
		t.Errorf("%d screens say the reading is fresh, want 3:\n%s", n, out)
	}
}

// TestTheClearSequenceIsOnlyForATerminal, because a file or a pipe should
// accumulate screens rather than collect escape sequences.
func TestTheClearSequenceIsOnlyForATerminal(t *testing.T) {
	for _, clear := range []bool{true, false} {
		srv := httptest.NewServer((&door{metrics: runMetrics,
			config: `{"operations":{"min_scrape_interval":"60s"}}`}).handler())
		path := filepath.Join(t.TempDir(), "d.yaml")
		if err := os.WriteFile(path, []byte(oneGroup), 0o600); err != nil {
			t.Fatal(err)
		}
		dash, err := Load(path)
		if err != nil {
			t.Fatal(err)
		}
		var out strings.Builder
		err = Run(context.Background(), Options{
			Source:    TCP(strings.TrimPrefix(srv.URL, "http://"), "", "", 5*time.Second),
			Dashboard: dash, Once: true, Out: &out, Clear: clear,
			Now: time.Now, Width: func() int { return 100 },
		})
		srv.Close()
		if err != nil {
			t.Fatal(err)
		}
		if got := strings.Contains(out.String(), "\033[H\033[J"); got != clear {
			t.Errorf("Clear=%v produced an escape sequence: %v", clear, got)
		}
	}
}

// TestADashboardNamingAMetricTheBrokerLacksDrawsNothingAtAll. "Refused by name
// at startup" means before a screen, not on the second one.
func TestADashboardNamingAMetricTheBrokerLacksDrawsNothingAtAll(t *testing.T) {
	d := &door{metrics: runMetrics,
		config: `{"operations":{"min_scrape_interval":"60s"}}`}
	out, _, _, err := runAgainst(t, d, `
refresh: 60s
groups:
  - title: Queues
    layout: columns
    metrics: [saguin_connections, saguin_queue_depth]
`, true, 0)
	if err == nil {
		t.Fatal("a dashboard naming a metric the broker does not serve ran")
	}
	if !strings.Contains(err.Error(), "saguin_queue_depth") {
		t.Errorf("the refusal does not name the metric: %v", err)
	}
	if out != "" {
		t.Errorf("a refused dashboard drew something first:\n%s", out)
	}
}

// TestADefaultWithNothingToSayIsAnErrorRatherThanAScreenOfHeadings.
func TestADefaultWithNothingToSayIsAnErrorRatherThanAScreenOfHeadings(t *testing.T) {
	d := &door{metrics: "# HELP saguin_odd n\n# TYPE saguin_odd gauge\nsaguin_odd 1\n",
		config: `{"operations":{"min_scrape_interval":"60s"}}`}
	out, _, _, err := runAgainst(t, d, "", true, 0)
	if err == nil {
		t.Fatalf("the default drew a screen against a broker serving none of "+
			"it:\n%s", out)
	}
	if !strings.Contains(err.Error(), "serves none of the metrics") {
		t.Errorf("the error does not say why: %v", err)
	}
}

// TestAFirstScrapeThatFailsIsAnErrorRatherThanAnEmptyScreen.
func TestAFirstScrapeThatFailsIsAnErrorRatherThanAnEmptyScreen(t *testing.T) {
	// A door that answers the config route and 404s /metrics, which is what a
	// reader pointed at something that is not saguin would meet.
	d := &door{metrics: "", config: `{"operations":{"min_scrape_interval":"60s"}}`}
	srv := httptest.NewServer(d.handler())
	t.Cleanup(srv.Close)
	path := filepath.Join(t.TempDir(), "d.yaml")
	os.WriteFile(path, []byte(oneGroup), 0o600)
	dash, _ := Load(path)
	var out strings.Builder
	err := Run(context.Background(), Options{
		Source:    TCP(strings.TrimPrefix(srv.URL, "http://"), "", "", 5*time.Second),
		Dashboard: dash, Once: true, Out: &out, Now: time.Now,
		Width: func() int { return 100 },
	})
	if err == nil {
		t.Fatalf("an empty /metrics drew a screen:\n%s", out.String())
	}
	if out.Len() != 0 {
		t.Errorf("something was drawn before the failure:\n%s", out.String())
	}
}

// TestAScrapeThatFailsMidLoopKeepsTheOldReadingAndSaysSo. An operator watching a
// broker through a link that just dropped wants the last numbers and the word
// that they are the last - not a blank screen, and not a stale number shown as
// though it were new.
func TestAScrapeThatFailsMidLoopKeepsTheOldReadingAndSaysSo(t *testing.T) {
	d := &door{metrics: runMetrics,
		config: `{"operations":{"min_scrape_interval":"60s"}}`}
	srv := httptest.NewServer(d.handler())
	t.Cleanup(srv.Close)
	path := filepath.Join(t.TempDir(), "d.yaml")
	os.WriteFile(path, []byte(oneGroup), 0o600)
	dash, _ := Load(path)

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	c := &clock{now: time.Unix(1700000000, 0), cancel: cancel, stopAfter: 3}
	// The door stops serving after the first scrape, so the second and third
	// screens are drawn from the first reading.
	closed := false
	var out strings.Builder
	err := Run(ctx, Options{
		Source:    TCP(strings.TrimPrefix(srv.URL, "http://"), "", "", 5*time.Second),
		Dashboard: dash, Out: &out, Now: c.Now,
		Sleep: func(ctx context.Context, dur time.Duration) error {
			if !closed {
				srv.Close()
				closed = true
			}
			return c.Sleep(ctx, dur)
		},
		Width: func() int { return 100 },
	})
	if err != nil {
		t.Fatalf("a dropped link ended the program: %v", err)
	}
	screens := strings.Count(out.String(), "broker_id edge-07")
	if screens < 2 {
		t.Fatalf("%d screens drawn; the loop should keep drawing the last "+
			"reading:\n%s", screens, out.String())
	}
	if !strings.Contains(out.String(), "the last scrape failed") {
		t.Errorf("a stale reading is drawn without saying so:\n%s", out.String())
	}
	// And the age climbs, because the reading did not change.
	if !strings.Contains(out.String(), "read 1m ago") {
		t.Errorf("the age of a kept reading does not climb:\n%s", out.String())
	}
	// The connection count is still there: the numbers are the old ones rather
	// than blanks.
	if n := strings.Count(out.String(), "connections 41"); n != screens {
		t.Errorf("%d screens carry the number and %d were drawn", n, screens)
	}
}

// TestTheContextEndingIsNotAnError, because Ctrl-C is how this program is meant
// to be stopped.
func TestTheContextEndingIsNotAnError(t *testing.T) {
	d := &door{metrics: runMetrics,
		config: `{"operations":{"min_scrape_interval":"60s"}}`}
	_, _, _, err := runAgainst(t, d, oneGroup, false, 1)
	if err != nil && !errors.Is(err, context.Canceled) {
		t.Errorf("stopping the loop returned %v", err)
	}
	if err != nil {
		t.Errorf("a cancelled context is reported as an error: %v", err)
	}
}
