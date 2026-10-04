// saguin-viewer reads a saguin broker's numbers on a box with no browser and no
// Prometheus, and prints one screen of them.
//
//	saguin-viewer                              # the socket, the shipped dashboard
//	saguin-viewer --once                       # one screen, then exit
//	saguin-viewer --dashboard edge.yaml
//	saguin-viewer --address broker:9090 --user operator
//
// The other viewer in this repository is the browser one, in web/. This is a
// client of the operations listener RFC 0005 specifies and nothing else.
package main

import (
	"context"
	"embed"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"golang.org/x/term"

	"github.com/ifnesi/saguin-viewer/internal/viewer"
)

// **The notices travel inside the binary.** This tool is built once and copied
// onto a box with no clone of this repository, so a notice left behind in git is
// not a notice that travelled with the thing it describes - which is also why the
// file sits beside this one rather than at the repository root, since `go:embed`
// cannot reach above its own package directory.
//
//go:embed THIRD-PARTY-NOTICES.md
var notices embed.FS

// PasswordEnv is where the password comes from when it is not in a file.
//
// **There is no --password flag, and that is deliberate.** An argument is in
// /proc/<pid>/cmdline and in everybody's `ps`, so a password there is readable by
// every process on the box - including the fleet's own, on the sort of machine
// this tool is for. The environment variable is the one the browser viewer's
// container already uses, so a deployment that sets one sets both.
const PasswordEnv = "SAGUIN_OPS_PASSWORD"

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "saguin-viewer: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	fs := flag.NewFlagSet("saguin-viewer", flag.ContinueOnError)
	var (
		socket    = fs.String("socket", viewer.DefaultSocket, "the operations listener's Unix socket")
		address   = fs.String("address", "", "host:port of the operations listener, instead of the socket")
		user      = fs.String("user", "", "the operator name in the broker's operations password file")
		passFile  = fs.String("password-file", "", "a file holding that operator's password, instead of "+PasswordEnv)
		dashboard = fs.String("dashboard", "", "a dashboard file; the shipped one is used when this is not given")
		once      = fs.Bool("once", false, "print one screen and exit")
		licenses  = fs.Bool("licenses", false, "print the licences of the code inside this binary, and exit")
		timeout   = fs.Duration("timeout", 10*time.Second, "how long to wait for the broker to answer")
	)
	fs.Usage = func() {
		fmt.Fprint(fs.Output(), `saguin-viewer reads a saguin broker's numbers and prints them.

  saguin-viewer                            the socket, the shipped dashboard
  saguin-viewer --once                     one screen, then exit
  saguin-viewer --dashboard edge.yaml
  saguin-viewer --address broker:9090 --user operator

`)
		fs.PrintDefaults()
		fmt.Fprintf(fs.Output(), `
The password for --user is read from %s, from --password-file, or asked for at
the terminal. There is no flag for it: an argument is readable by every process
on the machine.

A door with no password_file in the broker's configuration needs no credential,
which is the usual arrangement for the socket.
`, PasswordEnv)
	}
	if err := fs.Parse(os.Args[1:]); err != nil {
		// flag has already said what was wrong and printed the usage.
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if *licenses {
		body, err := notices.ReadFile("THIRD-PARTY-NOTICES.md")
		if err != nil {
			return err
		}
		_, err = os.Stdout.Write(body)
		return err
	}
	if fs.NArg() > 0 {
		return fmt.Errorf("%q is not an argument this takes - every setting is a "+
			"flag. --help lists them", fs.Arg(0))
	}

	// **One door, named once.** Both given is a reader who has not decided which
	// broker they mean, and guessing would read the wrong one silently.
	socketNamed := isSet(fs, "socket")
	if socketNamed && *address != "" {
		return errors.New("--socket and --address name two different doors; give one")
	}

	var source *viewer.Source
	if *address != "" {
		password, err := passwordFor(*user, *passFile)
		if err != nil {
			return err
		}
		source = viewer.TCP(*address, *user, password, *timeout)
	} else {
		password, err := passwordFor(*user, *passFile)
		if err != nil {
			return err
		}
		source = viewer.Unix(*socket, *timeout)
		// A socket behind broker.operations.password_file asks for the credential
		// too: RFC 0005 applies it to every transport the listener answers on.
		source.User, source.Password = *user, password
	}

	dash, err := loadDashboard(*dashboard)
	if err != nil {
		return err
	}

	// Ctrl-C and a systemd stop both end the loop rather than killing it
	// mid-screen.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	onATerminal := term.IsTerminal(int(os.Stdout.Fd()))
	return viewer.Run(ctx, viewer.Options{
		Source:    source,
		Dashboard: dash,
		Once:      *once,
		Out:       os.Stdout,
		// **Only a terminal is cleared.** Redirected to a file or piped into
		// anything, the escape sequences would be in the output and the screens
		// would overwrite nothing.
		Clear: onATerminal && !*once,
	})
}

func loadDashboard(path string) (*viewer.Dashboard, error) {
	if path == "" {
		return viewer.Default()
	}
	return viewer.Load(path)
}

// passwordFor finds the password for a named user: a file, the environment, or
// the terminal.
//
// **Nothing is asked for when no user was named**, because a door with no
// password file has no credential at all and prompting there would invent a
// requirement the broker does not have.
func passwordFor(user, file string) (string, error) {
	if file != "" {
		body, err := os.ReadFile(file)
		if err != nil {
			return "", fmt.Errorf("--password-file: %w", err)
		}
		// One line, and the newline an editor leaves is not part of the password.
		return strings.TrimRight(string(body), "\r\n"), nil
	}
	if user == "" {
		return "", nil
	}
	if password, ok := os.LookupEnv(PasswordEnv); ok {
		return password, nil
	}
	fd := int(os.Stdin.Fd())
	if !term.IsTerminal(fd) {
		return "", fmt.Errorf("--user %s was given and there is no password: set "+
			"%s, or name a --password-file. There is no flag for it, and there is "+
			"no terminal here to ask at", user, PasswordEnv)
	}
	fmt.Fprintf(os.Stderr, "password for %s: ", user)
	password, err := term.ReadPassword(fd)
	fmt.Fprintln(os.Stderr)
	if err != nil {
		return "", fmt.Errorf("reading the password: %w", err)
	}
	return string(password), nil
}

// isSet says whether a flag was given on the command line, as opposed to
// carrying its default. --socket has a default, so this is what tells "the
// default socket" apart from "this socket, which I typed".
func isSet(fs *flag.FlagSet, name string) bool {
	found := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == name {
			found = true
		}
	})
	return found
}
