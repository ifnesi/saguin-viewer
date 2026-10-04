package viewer

import (
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// A real saguin, because a parser written against a document and never pointed
// at the broker is a parser that agrees with the document.
//
// **It needs saguin's own checkout, which is not this repository.** SAGUIN_REPO
// names it; a sibling ../saguin is what a contributor usually has. Absent, these
// skip and say what to build - the same arrangement the web viewer's suite uses
// for `saguin --route`.
func saguinBinary(t *testing.T) string {
	t.Helper()
	root := os.Getenv("SAGUIN_REPO")
	if root == "" {
		root = filepath.Join("..", "..", "..", "saguin")
	}
	bin, err := filepath.Abs(filepath.Join(root, "bin", "saguin"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(bin); err != nil {
		t.Skipf("no %s - this needs saguin's own checkout: build it there with "+
			"`go build -o bin/saguin ./cmd/saguin`, and set SAGUIN_REPO if it is "+
			"not beside this one", bin)
	}
	return bin
}

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

// brokerConfig is the smallest broker that answers on both doors, with the
// credential line left as a placeholder.
//
// **Whether there is a password file changes what the socket asks for**, which
// is the rule RFC 0005 warns is worth knowing before one is configured: the
// credential "applies to every transport the listener answers on, including the
// Unix socket", and the socket's permissions are a second gate rather than an
// alternative to the first. So both arrangements are built below, and the first
// draft of this file got it wrong in the direction an operator would - it
// expected the socket to be exempt.
const brokerConfig = `
broker:
  id: %[1]s
  log_level: error
  mqtt:
    listen:
      tcp:
        address: 127.0.0.1:%[2]d
  operations:
    listen:
      tcp:
        address: 127.0.0.1:%[3]d
      unix:
        path: %[4]s
        mode: "0600"
%[5]s    min_scrape_interval: 90s
  storage:
    default: mem
    default_retention_period: none
    default_retention_bytes: none
    providers:
      - mem:
          type: memory
          snapshot_dir: none
channels:
  - events:
      type: append
      filter: iot/+/events/+
    state:
      type: latest
      filter: iot/+/state/+
    jobs:
      type: queue
      filter: iot/+/work/+
bridges:
  head-office:
    # Nothing listens there. A bridge that cannot connect still publishes every
    # bridge series, which is the point: the shipped default draws them, and a
    # link that is down is what an operator most wants a number for.
    peer: tcp://127.0.0.1:%[6]d
    client_id: drift-bridge
    topics:
      - filter: upstream/#
        topic: iot/depot/events/$#
        direction: in
`

type broker struct {
	socket  string
	address string
	log     string
	cmd     *exec.Cmd
}

// startBroker brings up a broker on both doors. guarded says whether an
// operators password file governs them; where one does, every door asks for the
// credential, the socket included.
func startBroker(t *testing.T, guarded bool) *broker {
	t.Helper()
	return startBrokerWith(t, guarded, nil)
}

// startBrokerWith is the same, with an edit applied to the configuration first -
// which is how the case about an unwritten default removes the line that writes
// it.
func startBrokerWith(t *testing.T, guarded bool, edit func(string) string) *broker {
	t.Helper()
	bin := saguinBinary(t)

	// **os.MkdirTemp rather than t.TempDir.** A Unix socket path may be 107
	// characters; t.TempDir names the directory after the test, which is long
	// enough to be refused with "invalid argument".
	dir, err := os.MkdirTemp("", "sv")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })

	passwd := filepath.Join(dir, "operations.passwd")
	out, err := exec.Command(bin, "--passwd", "add", passwd, "operator", "s3cret").
		CombinedOutput()
	if err != nil {
		t.Fatalf("writing the operators file: %v\n%s", err, out)
	}

	socket := filepath.Join(dir, "ops.sock")
	opsPort := freePort(t)
	cfg := filepath.Join(dir, "saguin.yaml")
	credential := ""
	if guarded {
		credential = "    password_file: " + passwd + "\n"
	}
	body := fmt.Sprintf(brokerConfig, "edge-07", freePort(t), opsPort, socket,
		credential, freePort(t))
	if edit != nil {
		body = edit(body)
	}
	if err := os.WriteFile(cfg, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	log := filepath.Join(dir, "broker.log")
	f, err := os.Create(log)
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(bin, "--config", cfg)
	cmd.Stdout, cmd.Stderr = f, f
	if err := cmd.Start(); err != nil {
		t.Fatalf("starting the broker: %v", err)
	}
	b := &broker{socket: socket, address: fmt.Sprintf("127.0.0.1:%d", opsPort),
		log: log, cmd: cmd}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		f.Close()
	})

	// **The socket appearing is the readiness signal**, because it is bound with
	// the listeners at startup and snapshots are loaded before any of them binds
	// (RFC 0005, "There is no /ready").
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := Unix(socket, 5*time.Second).get("/health"); err == nil {
			return b
		}
		if cmd.ProcessState != nil {
			t.Fatalf("the broker exited before it served /health. It said:\n%s",
				b.said())
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("the broker never served /health. It said:\n%s", b.said())
	return nil
}

func (b *broker) said() string {
	out, err := os.ReadFile(b.log)
	if err != nil {
		return "(no log)"
	}
	return string(out)
}

// TestARealScrapeParsesWhole is the case the fixture cannot be: every line of a
// body saguin actually wrote, refused on the first shape this does not know.
func TestARealScrapeParsesWhole(t *testing.T) {
	b := startBroker(t, false)
	s, err := Unix(b.socket, 10*time.Second).Scrape(time.Now())
	if err != nil {
		t.Fatalf("parsing a real scrape: %v\nThe broker said:\n%s", err, b.said())
	}
	// Count what was read. A parser that returned an empty scrape with no error
	// would satisfy every assertion below about what it found.
	if len(s.Order) < 40 {
		t.Fatalf("only %d families came off a real broker: %v", len(s.Order), s.Order)
	}
	samples := 0
	for _, m := range s.Metrics {
		samples += len(m.Samples)
		if m.Help == "" {
			t.Errorf("%s arrived with no HELP, so a screen has nothing to say "+
				"about it", m.Name)
		}
		if m.Kind != "gauge" && m.Kind != "counter" {
			t.Errorf("%s is a %q, and RFC 0005's catalogue has only gauges and "+
				"counters", m.Name, m.Kind)
		}
	}
	if samples < len(s.Order) {
		t.Errorf("%d samples across %d families, so some family arrived with a "+
			"HELP and no sample", samples, len(s.Order))
	}
	// The three channels the configuration declares, plus the queue's derived
	// dead-letter channel, each with the filter as written.
	info, ok := s.Family("saguin_channel_info")
	if !ok {
		t.Fatal("no saguin_channel_info in a real scrape")
	}
	filters := map[string]string{}
	for _, sample := range info.Samples {
		filters[sample.Labels["channel"]] = sample.Labels["filter"]
	}
	for channel, filter := range map[string]string{
		"events": "iot/+/events/+", "state": "iot/+/state/+",
		"jobs": "iot/+/work/+", "jobs__dlq": "iot/+/work/+/__dlq",
	} {
		if filters[channel] != filter {
			t.Errorf("channel %q has filter %q, the configuration wrote %q",
				channel, filters[channel], filter)
		}
	}
	if got := s.Label("saguin_build_info", "broker_id"); got != "edge-07" {
		t.Errorf("broker_id is %q, the configuration wrote edge-07", got)
	}
	if got := s.Label("saguin_build_info", "version"); got == "" {
		t.Error("saguin_build_info carries no version, and the header prints one")
	}
}

// TestADoorNamingNoPasswordFileHasNoCredential. RFC 0005: "A door naming no
// file anywhere has no credential at all", and the socket's file permissions are
// what decide who may speak to it. This is the arrangement the tool defaults to,
// so it is the one that has to work with nothing configured.
func TestADoorNamingNoPasswordFileHasNoCredential(t *testing.T) {
	b := startBroker(t, false)
	for _, c := range []struct {
		door string
		src  *Source
	}{
		{"the socket", Unix(b.socket, 10*time.Second)},
		{"loopback", TCP(b.address, "", "", 10*time.Second)},
	} {
		if _, err := c.src.Scrape(time.Now()); err != nil {
			t.Errorf("%s refused a reader with no credential: %v\nThe broker "+
				"said:\n%s", c.door, err, b.said())
		}
	}
}

// TestACredentialAppliesToTheSocketToo, which is the rule RFC 0005 says is
// "worth knowing before configuring one": the credential "applies to every
// transport the listener answers on, including the Unix socket. The socket's
// permissions are a second gate rather than an alternative to the first."
//
// **The first draft of this file assumed the opposite** - that a socket was
// exempt - and the broker answered 401, correctly. That is the mistake an
// operator makes with this setting, so it is the one worth a case.
func TestACredentialAppliesToTheSocketToo(t *testing.T) {
	b := startBroker(t, true)
	for _, c := range []struct {
		name     string
		src      *Source
		wantFail bool
	}{
		{"the socket with nothing offered", Unix(b.socket, 10*time.Second), true},
		{"the socket with the credential",
			socketAs(b.socket, "operator", "s3cret"), false},
		{"the address with the credential",
			TCP(b.address, "operator", "s3cret", 10*time.Second), false},
		{"the address with the wrong password",
			TCP(b.address, "operator", "wrong", 10*time.Second), true},
	} {
		t.Run(c.name, func(t *testing.T) {
			_, err := c.src.Scrape(time.Now())
			if c.wantFail {
				if err == nil {
					t.Fatal("reached /metrics without a credential the broker accepts")
				}
				if !strings.Contains(err.Error(), "401") {
					t.Errorf("want a 401, got: %v", err)
				}
				return
			}
			if err != nil {
				t.Errorf("refused: %v\nThe broker said:\n%s", err, b.said())
			}
		})
	}
}

// TestTheBrokerIsAskedHowOftenItRecomputes, against a broker that wrote the
// value, so the reading is the broker's rather than this program's assumption.
func TestTheBrokerIsAskedHowOftenItRecomputes(t *testing.T) {
	b := startBroker(t, false)
	got, err := Unix(b.socket, 10*time.Second).MinScrapeInterval()
	if err != nil {
		t.Fatalf("asking for the operations section: %v\nThe broker said:\n%s",
			err, b.said())
	}
	if want := 90 * time.Second; got != want {
		t.Errorf("the broker recomputes every %v; its configuration says %v",
			got, want)
	}
}

// TestTheBrokerFillsTheDefaultIn, which it did not when this program was
// written.
//
// **This case was the opposite of itself yesterday.** RFC 0005 says
// /v1/operations/config answers with "every default filled in", and a broker that
// left min_scrape_interval at its default served an operations section without
// it - so this program carried the RFC's minute as a fallback and a case here
// recorded the gap, failing on the day it closed and saying to delete the
// fallback. It closed: saguin fills it in, reasoning that a fallback here was "a
// second place for the number to be wrong". The fallback is gone and this case
// now holds the broker to the promise from the other side, which is where it
// belonged all along.
func TestTheBrokerFillsTheDefaultIn(t *testing.T) {
	b := startBrokerWith(t, false, func(cfg string) string {
		out := strings.ReplaceAll(cfg, "    min_scrape_interval: 90s\n", "")
		if strings.Contains(out, "min_scrape_interval") {
			t.Fatal("the line was not removed, so this case is not testing what " +
				"it says it is")
		}
		return out
	})
	got, err := Unix(b.socket, 10*time.Second).MinScrapeInterval()
	if err != nil {
		t.Fatalf("the broker serves no min_scrape_interval when nothing wrote "+
			"one, although RFC 0005 says the resolved configuration has every "+
			"default filled in: %v\nThe broker said:\n%s", err, b.said())
	}
	// RFC 0005: "A minute is the floor and the default, and a shorter one is a
	// startup error."
	if want := time.Minute; got != want {
		t.Errorf("the default is %v; RFC 0005 says the floor and the default are "+
			"both %v", got, want)
	}
}

// socketAs dials a socket with a credential, which is what a socket behind an
// operations password file needs.
func socketAs(path, user, password string) *Source {
	s := Unix(path, 10*time.Second)
	s.User, s.Password = user, password
	return s
}

// TestTheShippedDefaultDrawsAgainstARealBroker. The unit case holds it to
// RFC 0005's catalogue, which is a document; this holds it to a broker that has
// one of each channel type and a bridge, which is what the default was written
// for.
//
// **Three families are legitimately absent, and each for what it is rather
// than for this broker.** The two refusal counters follow the rule RFC 0005
// states for them - "no series until a provider has failed ... a rate over a
// series that does not exist is nothing, and nothing has gone wrong" - and
// nothing was refused here. `saguin_channel_consumer_position_min` is one
// position per durable consumer and nobody has consumed, so there is no lowest
// position to take. Anything else absent is the default naming a metric this
// broker should have served.
//
// A group may therefore keep nothing - "Refusals, by reason" does, on a broker
// that has refused nothing - but only when every one of its metrics is on that
// list. A group emptied by anything else is the failure this case is for.
func TestTheShippedDefaultDrawsAgainstARealBroker(t *testing.T) {
	b := startBroker(t, false)
	s, err := Unix(b.socket, 10*time.Second).Scrape(time.Now())
	if err != nil {
		t.Fatalf("%v\nThe broker said:\n%s", err, b.said())
	}
	d, err := Default()
	if err != nil {
		t.Fatal(err)
	}
	absent, err := d.Validate(s)
	if err != nil {
		t.Fatalf("the shipped default refused a real broker: %v", err)
	}
	allowed := map[string]bool{
		"saguin_publish_refused_total":         true,
		"saguin_subscriptions_refused_total":   true,
		"saguin_connections_refused_total":     true,
		"saguin_channel_consumer_position_min": true,
		// RFC 0005: "no series until a provider has failed", so a healthy
		// broker serves none.
		"saguin_storage_errors_total": true,
	}
	for _, name := range absent {
		if !allowed[name] {
			t.Errorf("the shipped default names %s and this broker - which has an "+
				"append, a latest and a queue channel and a bridge - does not "+
				"serve it", name)
		}
	}
	// Count what was drawn, so a Validate that emptied every group would not
	// pass by having nothing left to complain about.
	drawn := 0
	for i := range d.Groups {
		g := &d.Groups[i]
		if len(g.Drawn()) == 0 {
			// Every metric of an emptied group has to be one of the three above,
			// which is what makes an empty group here a fact about the broker
			// rather than a defect in the default.
			for _, c := range g.Columns {
				for _, name := range c.Reads() {
					if allowed[name] {
						continue
					}
					t.Errorf("group %q kept nothing, and %s is not a metric that "+
						"is absent for what it is", g.Title, name)
				}
			}
			continue
		}
		drawn += len(g.Drawn())
	}
	if drawn < 20 {
		t.Errorf("only %d metrics survived validation against a real broker", drawn)
	}
}
