package viewer

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"
)

// Options is everything Run needs, with the clock and the sleep passed in so
// that a test can drive the loop without waiting a minute per redraw.
type Options struct {
	Source    *Source
	Dashboard *Dashboard
	// Once prints one screen and returns.
	Once bool
	Out  io.Writer
	// Clear sends the terminal home and wipes it before each redraw. False where
	// the output is not a terminal, because a file or a pipe should accumulate
	// screens rather than collect escape sequences.
	Clear bool

	Now   func() time.Time
	Sleep func(context.Context, time.Duration) error
	Width func() int
}

func (o *Options) fill() {
	if o.Now == nil {
		o.Now = time.Now
	}
	if o.Width == nil {
		o.Width = Width
	}
	if o.Sleep == nil {
		o.Sleep = func(ctx context.Context, d time.Duration) error {
			t := time.NewTimer(d)
			defer t.Stop()
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-t.C:
				return nil
			}
		}
	}
}

// Run draws the screen once, or on an interval until the context ends.
func Run(ctx context.Context, o Options) error {
	o.fill()

	// **The broker's interval is read once, before anything is drawn.** It is a
	// configured value that the broker read at startup and never re-reads (RFC
	// 0005: the configuration file is read once at startup), so asking again
	// every minute would be a request per screen for a number that cannot have
	// changed.
	floor, err := o.Source.MinScrapeInterval()
	if err != nil {
		return err
	}
	interval := o.Dashboard.Interval(floor)

	// The first scrape, which is also when the dashboard is refused: whether a
	// metric exists is a question about this broker.
	scrape, err := o.Source.Scrape(o.Now())
	if err != nil {
		return err
	}
	absent, err := o.Dashboard.Validate(scrape)
	if err != nil {
		return err
	}

	// A dashboard somebody wrote whose every group came out empty would draw a
	// screen of headings. It cannot happen for a named file - that is refused
	// above - so this is the shipped default against a broker it can say nothing
	// about, which is worth an error rather than a blank screen.
	drawn := 0
	for _, g := range o.Dashboard.Groups {
		drawn += len(g.Drawn())
	}
	if drawn == 0 {
		return fmt.Errorf("%s: this broker serves none of the metrics on it: %s",
			o.Dashboard.Source(), strings.Join(absent, ", "))
	}

	// lastErr carries a scrape that failed, so the screen says the reading is
	// old and why, rather than showing a stale number as though it were new.
	var lastErr error
	for {
		if o.Clear {
			// Cursor home, then erase what is below it - so the screen is replaced
			// rather than scrolled, and a shorter screen does not leave the tail
			// of a longer one underneath.
			fmt.Fprint(o.Out, "\033[H\033[J")
		}
		screen := Screen{
			Dashboard: o.Dashboard, Scrape: scrape, Where: o.Source.Where(),
			Now: o.Now(), Interval: interval, Written: o.Dashboard.Written(),
			BrokerInterval: floor, Absent: absent,
			Once: o.Once, Width: o.Width(),
		}
		if err := screen.Render(o.Out); err != nil {
			return err
		}
		if lastErr != nil {
			fmt.Fprintf(o.Out, "\nthe last scrape failed, so these numbers are the "+
				"ones above's age old: %v\n", lastErr)
		}
		if o.Once {
			return nil
		}
		if err := o.Sleep(ctx, interval); err != nil {
			// The context ended: Ctrl-C, or a caller that has had enough. Not an
			// error of this program's.
			if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
				return nil
			}
			return err
		}
		next, err := o.Source.Scrape(o.Now())
		if err != nil {
			// **The old reading is kept rather than the screen going blank.** An
			// operator watching a broker through a link that just dropped wants
			// the last numbers and the word that they are the last.
			lastErr = err
			continue
		}
		scrape, lastErr = next, nil
		// Re-validated, because a broker can gain a metric between screens - a
		// queue channel's first record, a bridge's first connection - and the
		// shipped default should start drawing it rather than say it is absent
		// for the life of the process.
		if absent, err = o.Dashboard.Validate(scrape); err != nil {
			return err
		}
	}
}
