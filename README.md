# saguin-viewer

Two viewers for a [saguin](https://github.com/ifnesi/saguin) broker. Both are
clients of any broker rather than part of one: they read the operations
listener RFC 0005 specifies, and nothing here runs inside saguin.

A look before you install: every message as it arrives, decoded through the
broker's schema registry, and the broker's own numbers on a dashboard.

![Sagüin Viewer's live feed: protobuf readings arriving on iot/water and iot/weather topics, each deserialized to JSON beside its raw bytes, channel and offset](docs/img/viewer-live-topics.png)

![Sagüin Viewer's Dashboard tab: uptime, publishes received, deliveries sent and live rate charts for a broker under steady load](docs/img/viewer-dashboard.png)

| | |
|---|---|
| [`web/`](web/) | A browser dashboard - Flask serving static React. Channels, topics, payloads through their registered schemas, queues and dead letters. |
| [`cmd/saguin-viewer/`](cmd/saguin-viewer/) | A terminal screen, for a headless box with no browser and no Prometheus. A single static Go binary. |

They share no code and no dependencies, and each carries its own README. The Go
module is rooted here, so the terminal viewer builds from the repository root;
`web/` is a Python program with its own `requirements.txt` and its own suite.
Neither's contributor needs the other's toolchain.

    go build -o bin/saguin-viewer ./cmd/saguin-viewer
    bin/saguin-viewer --once

## Running the suites

    make test      # both
    make go        # the terminal viewer
    make viewer    # the browser viewer

The web viewer's suite needs a virtualenv:

    python3 -m venv .venv
    .venv/bin/pip install -r web/requirements.txt

**Some cases need saguin's own checkout**, because both viewers are clients of a
broker and nothing here can stand in for one: the drift check between saguin's
metric catalogue and what the viewers draw, the parser against a real scrape,
both credential arrangements on the operations listener, and `saguin --route`.
They look for a sibling `../saguin` with `bin/saguin` built; `SAGUIN_REPO` names
it elsewhere. Absent, they skip and say what to build and set.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md). The short version: RFC 0005 is the
specification for everything here, a metric neither viewer may draw until that
document names it, and the drift test is what keeps the two in step.

## Development approach

**saguin-viewer is written by Claude.**

## Licence

[Apache-2.0](LICENSE), the same as the broker's.

Attribution for code this project does not own is in
[THIRD-PARTY-NOTICES.md](cmd/saguin-viewer/THIRD-PARTY-NOTICES.md), which
`saguin-viewer --licenses` prints: the three Go modules linked into the terminal
binary, and the three libraries in `web/static/lib/` that this repository serves
to a browser. It sits beside the binary's own source rather than here because
`go:embed` cannot reach above its package, and a notice that stayed behind in git
is not one that travelled with the binary it describes.

The web viewer's Python packages are not in it, and that file says why: pip
fetches them with their own licences and nothing here redistributes them.
