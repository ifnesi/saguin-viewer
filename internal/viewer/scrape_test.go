package viewer

import (
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// door is a stand-in operations listener. It records what it was asked for and
// what credential arrived, so a case can prove the request it claims to have
// made rather than only that a body came back.
type door struct {
	metrics  string
	config   string
	requires string // "user:password", or empty for a door with no credential
	scopes   string // a path prefix this credential reaches; empty means all

	asked atomic.Int64
	last  atomic.Value // the last path
	auth  atomic.Value // the last Authorization header
}

func (d *door) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		d.asked.Add(1)
		d.last.Store(r.URL.RequestURI())
		d.auth.Store(r.Header.Get("Authorization"))
		if d.requires != "" {
			user, pass, ok := r.BasicAuth()
			if !ok || user+":"+pass != d.requires {
				w.Header().Set("WWW-Authenticate", `Basic realm="saguin"`)
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
			if d.scopes != "" && !strings.HasPrefix(r.URL.Path, d.scopes) {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
		}
		switch {
		case r.URL.Path == "/metrics":
			w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
			fmt.Fprint(w, d.metrics)
		case r.URL.Path == "/v1/operations/config":
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, d.config)
		default:
			http.NotFound(w, r)
		}
	})
}

const twoMetrics = `# HELP saguin_connections Clients connected now.
# TYPE saguin_connections gauge
saguin_connections 41
# HELP saguin_build_info Always 1.
# TYPE saguin_build_info gauge
saguin_build_info{version="0.1.0",broker_id="edge-07"} 1
`

// TestASocketReachesAScrapeWithNoCredential. RFC 0005: a door naming no
// password file anywhere has no credential at all, and the socket's permissions
// are the gate. This is the default door, so it is the one that has to work with
// nothing configured.
func TestASocketReachesAScrapeWithNoCredential(t *testing.T) {
	// **Not t.TempDir().** A Unix socket path may be 107 characters and the
	// kernel refuses a longer one with "invalid argument", which says nothing
	// about the length; a temp directory named after this test is long enough to
	// hit it on some machines. saguin's own configuration check reports that
	// limit by name, and this is the same limit.
	dir, err := os.MkdirTemp("", "sv")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	path := filepath.Join(dir, "ops.sock")

	d := &door{metrics: twoMetrics, config: `{"operations":{"min_scrape_interval":"90s"}}`}
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatalf("listening on %s: %v", path, err)
	}
	srv := &http.Server{Handler: d.handler()}
	go srv.Serve(ln)
	t.Cleanup(func() { srv.Close() })

	src := Unix(path, 5*time.Second)
	if src.Where() != path {
		t.Errorf("Where() is %q, want the socket path %q", src.Where(), path)
	}
	at := time.Unix(1700000000, 0)
	s, err := src.Scrape(at)
	if err != nil {
		t.Fatalf("scraping the socket: %v", err)
	}
	if v, ok := s.One("saguin_connections"); !ok || v != 41 {
		t.Errorf("saguin_connections = %v %v, want 41", v, ok)
	}
	if !s.At.Equal(at) {
		t.Errorf("the scrape is stamped %v, want %v", s.At, at)
	}
	if got := d.last.Load(); got != "/metrics" {
		t.Errorf("the door was asked for %v, want /metrics", got)
	}
	// The instrument proving it did the work: nothing was sent, because nothing
	// was configured.
	if got := d.auth.Load(); got != "" {
		t.Errorf("a credential was sent to a door that asks for none: %q", got)
	}
	if n := d.asked.Load(); n != 1 {
		t.Errorf("the door was asked %d times, want 1", n)
	}
}

// TestATCPDoorWithACredentialReachesAScrape, which is the arrangement RFC 0005
// requires on any address that is not loopback: the broker refuses to start on
// a routable address with no password file governing it.
func TestATCPDoorWithACredentialReachesAScrape(t *testing.T) {
	d := &door{metrics: twoMetrics, requires: "prometheus:s3cret",
		config: `{"operations":{"min_scrape_interval":"60s"}}`}
	srv := httptest.NewServer(d.handler())
	t.Cleanup(srv.Close)
	address := strings.TrimPrefix(srv.URL, "http://")

	src := TCP(address, "prometheus", "s3cret", 5*time.Second)
	if src.Where() != address {
		t.Errorf("Where() is %q, want %q", src.Where(), address)
	}
	s, err := src.Scrape(time.Now())
	if err != nil {
		t.Fatalf("scraping %s: %v", address, err)
	}
	if v, ok := s.One("saguin_connections"); !ok || v != 41 {
		t.Errorf("saguin_connections = %v %v, want 41", v, ok)
	}
	if got, _ := d.auth.Load().(string); !strings.HasPrefix(got, "Basic ") {
		t.Errorf("the door saw %q, want a Basic credential - without this the "+
			"case passes against a door that asked for nothing", got)
	}
}

// TestARejectedCredentialSaysToTryAnother and TestACredentialOutOfScopeSaysToWiden
// are separate because RFC 0005 makes them different answers on purpose: a 401
// sends an operator to check a password, a 403 says the password is right and
// does not reach this route. A message that conflates them sends them to the
// wrong place.
func TestARejectedCredentialSaysToTryAnother(t *testing.T) {
	d := &door{metrics: twoMetrics, requires: "prometheus:s3cret"}
	srv := httptest.NewServer(d.handler())
	t.Cleanup(srv.Close)
	_, err := TCP(strings.TrimPrefix(srv.URL, "http://"), "prometheus", "wrong",
		5*time.Second).Scrape(time.Now())
	if err == nil {
		t.Fatal("a wrong password was accepted")
	}
	for _, want := range []string{"401", "--user", "SAGUIN_OPS_PASSWORD"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error does not mention %q: %v", want, err)
		}
	}
}

func TestACredentialOutOfScopeSaysToWiden(t *testing.T) {
	// A user scoped to /metrics, exactly as RFC 0005's own password file writes
	// one: `prometheus:$7$…:/metrics`.
	d := &door{metrics: twoMetrics, requires: "prometheus:s3cret", scopes: "/metrics",
		config: `{"operations":{"min_scrape_interval":"60s"}}`}
	srv := httptest.NewServer(d.handler())
	t.Cleanup(srv.Close)
	src := TCP(strings.TrimPrefix(srv.URL, "http://"), "prometheus", "s3cret",
		5*time.Second)
	if _, err := src.Scrape(time.Now()); err != nil {
		t.Fatalf("/metrics is in scope and was refused: %v", err)
	}
	_, err := src.MinScrapeInterval()
	if err == nil {
		t.Fatal("a route outside the credential's scope answered")
	}
	for _, want := range []string{"403", "saguin --passwd scope", "prometheus"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error does not mention %q: %v", want, err)
		}
	}
}

// TestTheIntervalIsAskedForBySection, because RFC 0005 warns that the whole
// route hands over the resolved configuration in full - every channel's name,
// filter, type and provider - and this wants one duration.
func TestTheIntervalIsAskedForBySection(t *testing.T) {
	d := &door{config: `{"operations":{"min_scrape_interval":"90s"}}`}
	srv := httptest.NewServer(d.handler())
	t.Cleanup(srv.Close)
	src := TCP(strings.TrimPrefix(srv.URL, "http://"), "", "", 5*time.Second)
	got, err := src.MinScrapeInterval()
	if err != nil {
		t.Fatal(err)
	}
	if want := 90 * time.Second; got != want {
		t.Errorf("interval is %v, want %v", got, want)
	}
	if asked, _ := d.last.Load().(string); asked != "/v1/operations/config?section=operations" {
		t.Errorf("asked for %q, want the operations section alone", asked)
	}
}

// TestAnAbsentIntervalIsRefused.
//
// **This used to assume RFC 0005's floor, and that was the wrong shape.** While
// saguin's resolved configuration omitted a min_scrape_interval left at its
// default, this program carried the RFC's minute as a fallback - which saguin,
// fixing it, called "a second place for the number to be wrong". It was: a screen
// clamped against a minute this program assumed, on a broker recomputing at
// something else, is confidently out of step and says nothing. So the broker's
// answer is the only answer, and its absence is refused by name.
func TestAnAbsentIntervalIsRefused(t *testing.T) {
	d := &door{config: `{"operations":{"listen":{"tcp":{"address":"127.0.0.1:9090"}}}}`}
	srv := httptest.NewServer(d.handler())
	t.Cleanup(srv.Close)
	_, err := TCP(strings.TrimPrefix(srv.URL, "http://"), "", "",
		5*time.Second).MinScrapeInterval()
	if err == nil {
		t.Fatal("an operations section with no min_scrape_interval was accepted")
	}
	for _, want := range []string{"serves no min_scrape_interval",
		"every default filled in", "build it again"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not say %q: %v", want, err)
		}
	}
}

// TestAnIntervalThatIsNotADurationIsRefused rather than quietly becoming the
// floor: a value the broker served and this could not read is a disagreement
// about the schema, and hiding it behind a default is how one survives.
func TestAnIntervalThatIsNotADurationIsRefused(t *testing.T) {
	d := &door{config: `{"operations":{"min_scrape_interval":"a minute"}}`}
	srv := httptest.NewServer(d.handler())
	t.Cleanup(srv.Close)
	_, err := TCP(strings.TrimPrefix(srv.URL, "http://"), "", "",
		5*time.Second).MinScrapeInterval()
	if err == nil {
		t.Fatal(`"a minute" was accepted as a duration`)
	}
	if !strings.Contains(err.Error(), `"a minute"`) {
		t.Errorf("the error does not quote the value: %v", err)
	}
}

// TestADoorThatIsNotThere names what was dialled, because the first thing an
// operator mistypes is the path or the port.
func TestADoorThatIsNotThere(t *testing.T) {
	_, err := Unix("/nonexistent/ops.sock", time.Second).Scrape(time.Now())
	if err == nil {
		t.Fatal("a socket that does not exist answered")
	}
	if !strings.Contains(err.Error(), "/nonexistent/ops.sock") {
		t.Errorf("the error does not name the socket: %v", err)
	}
}
