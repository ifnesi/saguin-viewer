package viewer

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"
)

// DefaultSocket is the operations socket RFC 0005 and RFC 0002 write in every
// example. It is where this looks when nothing names a door, because a local
// reader on the broker's own box is what this tool is for.
const DefaultSocket = "/run/saguin/operations.sock"

// Source is one door onto a broker's operations listener: a Unix socket, or a
// TCP address with whatever credential that door asks for.
//
// **The socket is the default and it usually needs no credential.** RFC 0005:
// a door naming no password file anywhere has none, and the socket's file
// permissions are what decide who may speak to it. A TCP listener on a routable
// address always has one, because the broker refuses to start otherwise.
type Source struct {
	// Socket is a filesystem path; Address is host:port. Exactly one is set.
	Socket  string
	Address string

	User     string
	Password string

	client *http.Client
}

// Unix dials the operations socket.
func Unix(path string, timeout time.Duration) *Source {
	s := &Source{Socket: path}
	s.client = &http.Client{
		Timeout: timeout,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "unix", path)
			},
		},
	}
	return s
}

// TCP dials an address, with a credential where the door asks for one.
func TCP(address, user, password string, timeout time.Duration) *Source {
	return &Source{
		Address:  address,
		User:     user,
		Password: password,
		client:   &http.Client{Timeout: timeout},
	}
}

// Where names the door, for the header. RFC 0005 notes that no metric carries a
// hostname, so this is the only thing on the screen that says which broker was
// read - and it is what the reader typed rather than anything the broker said.
func (s *Source) Where() string {
	if s.Socket != "" {
		return s.Socket
	}
	return s.Address
}

func (s *Source) url(path string) string {
	if s.Socket != "" {
		// The host is unused - the transport dials the socket whatever it says -
		// but net/http requires one, and "saguin" is what appears in an error.
		return "http://saguin" + path
	}
	return "http://" + s.Address + path
}

// get reads one path off the listener.
func (s *Source) get(path string) ([]byte, error) {
	req, err := http.NewRequest(http.MethodGet, s.url(path), nil)
	if err != nil {
		return nil, err
	}
	// **Basic, in the request header**, which RFC 0005 chose because it is what
	// every scraper already sends. Set only when a user was named: a door with
	// no password file has no credential at all, and sending an empty one there
	// is a credential that was offered and failed.
	if s.User != "" {
		req.SetBasicAuth(s.User, s.Password)
	}
	// Prometheus asks for gzip and saguin offers it. This does not: one screen a
	// minute over a loopback socket is not a link where a fifteenth of the bytes
	// is worth a decompressor, and Go would add one for free only in the sense
	// that it hides which body was read.
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", s.Where(), err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("%s, reading the answer to %s: %w", s.Where(), path, err)
	}
	switch resp.StatusCode {
	case http.StatusOK:
		return body, nil
	case http.StatusUnauthorized:
		// RFC 0005 draws the line between these two, so the message does too: a
		// 401 says try again with a credential, a 403 says this one is correct
		// and does not reach here. An operator told the wrong one checks the
		// wrong thing.
		// **The door and the path, told apart.** These were concatenated, so a
		// socket read as one long filesystem path - `/run/saguin/ops.sock/metrics`
		// is not a thing that exists, and the reader's first thought was that the
		// tool had built a bad path rather than that the broker had refused them.
		return nil, fmt.Errorf("%s answered 401 for %s, so it wants a credential "+
			"and did not get one it accepts. Name a user with --user; the password "+
			"comes from SAGUIN_OPS_PASSWORD, --password-file, or a prompt",
			s.Where(), path)
	case http.StatusForbidden:
		return nil, fmt.Errorf("%s answered 403 for %s, so the credential is good "+
			"and does not reach that route. Widen it with `saguin --passwd scope "+
			"<file> %s <routes>` on the broker", s.Where(), path, s.User)
	default:
		return nil, fmt.Errorf("%s answered %s for %s: %s", s.Where(), resp.Status,
			path, strings.TrimSpace(string(body)))
	}
}

// Scrape reads /metrics and parses it.
//
// **at is passed in rather than taken here**, so that the age the screen prints
// is the age of this reading and a test can hold a reading at a known moment.
func (s *Source) Scrape(at time.Time) (*Scrape, error) {
	body, err := s.get("/metrics")
	if err != nil {
		return nil, err
	}
	return ParseScrape(string(body), at)
}

// MinScrapeInterval asks the broker how often it recomputes.
//
// **`?section=operations` rather than the whole document**, which RFC 0005
// warns is otherwise handed over in full: at ten thousand channels that route
// answers with every channel's name, filter, type and provider, to read one
// duration.
//
// **The broker's answer is the only answer, and an absent one is an error.**
// This carried RFC 0005's floor as a fallback while the resolved configuration
// omitted the key that had been left at its default - which saguin now fills in,
// having reasoned that the fallback was "a second place for the number to be
// wrong". It was: a screen clamped against a minute this program assumed, on a
// broker recomputing at something else, would be confidently out of step and say
// nothing. So there is one number now and it comes from the broker.
func (s *Source) MinScrapeInterval() (time.Duration, error) {
	route := "/v1/operations/config?section=operations"
	body, err := s.get(route)
	if err != nil {
		return 0, err
	}
	var doc struct {
		Operations struct {
			MinScrapeInterval string `json:"min_scrape_interval"`
		} `json:"operations"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return 0, fmt.Errorf("%s, answering %s: the answer is not the JSON this "+
			"route promises: %w", s.Where(), route, err)
	}
	written := doc.Operations.MinScrapeInterval
	if written == "" {
		return 0, fmt.Errorf("%s, answering %s: the broker serves no "+
			"min_scrape_interval. "+
			"RFC 0005 says this route answers with every default filled in, so a "+
			"broker that omits it is one from before saguin did that - build it "+
			"again, or write the key in its configuration", s.Where(), route)
	}
	d, err := time.ParseDuration(written)
	if err != nil {
		return 0, fmt.Errorf("the broker says min_scrape_interval is %q, "+
			"which is not a duration this reads", written)
	}
	return d, nil
}
