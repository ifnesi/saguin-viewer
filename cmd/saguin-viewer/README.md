# The terminal viewer

A saguin broker's numbers on a box with no browser and no Prometheus, read over
SSH. One static Go binary, two dependencies, no agent to install on the broker:
it reads the operations listener [RFC 0005][rfc] already serves.

The two are `gopkg.in/yaml.v3` and `golang.org/x/term`, both already saguin's at
saguin's versions. Three modules end up linked in, the third being `x/sys`, which
`x/term` brings; [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) carries all
three because that is what travels in the binary.

The other viewer in this repository is [the browser one](../../web/). Neither
needs the other.

[rfc]: https://github.com/ifnesi/saguin/blob/main/docs/rfcs/0005-operations.md

## Building and running

From the repository root, which is where the module is:

    go build -o bin/saguin-viewer ./cmd/saguin-viewer
    bin/saguin-viewer --once

or `make build`.

`--licenses` prints [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md), which is
embedded in the binary: this is a tool that gets copied onto a box with no clone
of the repository, so the attribution has to travel inside it.

With nothing else said it reads `/run/saguin/operations.sock` and draws the
dashboard built into the binary, so there is nothing to configure to see
something:

    saguin 0.1.0  broker_id edge-07  /run/saguin/operations.sock
    read 12s ago, redrawing every 1m, broker recomputes every 1m

    Broker
      uptime_seconds 1d 2h    connections 41    sessions_offline 6
      subscriptions 118    retained_messages 4

    Channels
      channel      records      bytes  next_offset  consumers   behind  data lost
      events     1,204,882  412.1 MiB    1,204,883        312  304,885        yes
      telemetry     88,301   31.4 MiB       88,302         17      302          -
      jobs               -    1.1 MiB            -          -        -          -

`--once` prints one screen and exits, for a shell script or a `watch`. Without
it the screen is cleared and redrawn on the interval until Ctrl-C.

## Which door, and what it asks for

Every flag, and there are eight:

| | |
|---|---|
| `--socket PATH` | the operations listener's Unix socket. The default, `/run/saguin/operations.sock`. |
| `--address HOST:PORT` | a TCP listener instead, for a box where no socket is configured or a broker on another machine. Naming both doors is refused. |
| `--user NAME` | an operator in the broker's `broker.operations.password_file`. |
| `--password-file PATH` | that operator's password. |
| `--dashboard PATH` | a dashboard file; the one built into the binary is used without it. |
| `--once` | print one screen and exit. |
| `--timeout DURATION` | how long to wait for the broker to answer, `10s` by default. It bounds each request rather than the whole run, so a broker that has gone away is one refusal rather than a hang. |
| `--licenses` | print the notices embedded in the binary. |

**A door naming no `password_file` needs no credential**, which is the usual
arrangement for a socket: RFC 0005 leaves the socket's own file permissions to
decide who may speak to it. Where the broker does name one, **the credential
applies to the socket too** - it is a second gate rather than an alternative to
the first - so `--user` goes with `--socket` as readily as with `--address`.

**There is no `--password` flag.** An argument is in `/proc/<pid>/cmdline` and in
everybody's `ps`, so a password there is readable by every process on the box.
The password comes from `SAGUIN_OPS_PASSWORD`, from `--password-file`, or from a
prompt at the terminal.

A 401 means the broker wants a credential it did not get. A 403 means the one it
got is correct and does not reach that route, and is widened with `saguin
--passwd scope` on the broker.

## How often it redraws

`refresh` in the dashboard file, **clamped up to what the broker says it
recomputes at and never below it**. RFC 0005 gives the reason: a scrape arriving
sooner than `min_scrape_interval` is answered from the previous computation, so a
shorter refresh redraws the same numbers while the age on the screen resets -
which reads as a broker whose numbers have stopped moving. The clamp is never
silent; the screen says the interval it settled on and the one the file asked
for.

**The age on the screen is the age of the reading**, not of the redraw. A screen
drawn from a scrape that failed says how old the numbers are and that the last
scrape failed, rather than going blank or showing a stale number as a new one.

The broker's own interval has a floor of one minute, which is also its default -
so a refresh under a minute is always raised. **The broker's answer is the only
answer**: a broker that serves no `min_scrape_interval` is refused rather than
having a minute assumed on its behalf, because two places for one number is one
of them being quietly wrong.

## Writing a dashboard

`--dashboard edge.yaml`. A `refresh` and a list of `groups`; each group has a
`title`, a `layout`, a list of `metrics` and - on one of the two layouts - a `by`.
Every key is checked at startup:

```yaml
refresh: 60s

groups:
  - title: Clients
    layout: columns          # a row of labelled numbers
    metrics: [saguin_connections, saguin_sessions_offline, saguin_subscriptions]

  - title: Channels
    layout: rows             # a table
    by: channel              # one row per value of this label
    metrics: [saguin_channel_records, saguin_channel_bytes]
```

**`layout` is one of two, and there are two because a number is either one value
or one per entity.** `columns` is one number per metric, wrapped to the terminal.
`rows` is a table: one row per value of the `by` label, one column per metric,
with the widths coming from the scrape. `by` is what makes a group a table, so it
belongs to `rows` and is refused on `columns` - which is stricter than "optional"
and is why: a `columns` group has no rows to divide, and a `rows` group without
one names nothing to put a row per.

## Columns you work out yourself

An entry in `metrics:` can be a `name` and a `value` rather than a metric name,
which is how a group becomes a widget of your own rather than one of a fixed set.
`name` is the column's heading and `value` is the expression under it:

```yaml
  - title: Channels
    layout: rows
    by: channel
    metrics:
      - saguin_channel_records
      - name: behind
        value: saguin_channel_next_offset - saguin_channel_consumer_position_min
      - name: data lost
        value: saguin_channel_floor_offset > saguin_channel_consumer_position_min
      - name: per record
        value: saguin_channel_bytes / saguin_channel_records
        format: bytes
```

    channel      records      bytes  next_offset   behind  data lost  per record
    events     1,204,882  412.1 MiB    1,204,883  304,885        yes       359 B
    jobs             412    1.1 MiB          413        -          -     2.7 KiB
    telemetry     88,301   31.4 MiB       88,302      302          -       373 B

**This is the reason the feature exists.** The two readings RFC 0005 says the
whole catalogue is worth building for are a subtraction and a comparison, and it
is explicit that no metric can carry either: a lag would need a threshold nobody
can choose on an operator's behalf, or a series per consumer, which is the line
the catalogue is closed against. So the broker publishes the operands and a
dashboard does the arithmetic. `data lost` is a column rather than a subtraction
left to the reader because a condition shown as two adjacent numbers is one
nobody notices.

Metric names and numbers, with `+ - * /`, brackets, and at most one comparison -
`>` `<` `>=` `<=` `==` `!=`. **A comparison draws `yes` and nothing else**, so a
column of blanks with one `yes` in it puts the eye where the trouble is.

`format:` is `number` (the default), `bytes` or `duration`, and only a computed
column takes one - a metric's own name already says which rule it gets.

**A reusable group needs nothing from this program**: YAML anchors already do it.

```yaml
groups:
  - &channels
    title: Channels
    layout: rows
    by: channel
    metrics: [saguin_channel_records]
  - <<: *channels
    title: Channels again
```

### What it deliberately cannot do

No functions, no `and` or `or`, no strings, no chained comparisons. The two
readings above are what this is for, and everything past them is a language
rather than a column - the point at which a dashboard file stops being readable
by somebody who did not write it. If one of them is ever needed it arrives with
the failure that needed it.

Two rules make a computed column honest rather than convenient:

* **An unknown operand makes the whole cell unknown.** A `latest` channel has no
  bytes and no consumer position, and a queue has neither; reading either as
  zero would turn a lag nobody knows into a lag of nothing.
* **Division by nothing is unknown, not infinity.** A ratio whose denominator is
  zero is a question the scrape cannot answer - no deliveries yet, no records yet
  - and a cell reading `+Inf` says the opposite of "nothing has happened".

Every metric inside a `value` is checked against the broker at startup exactly as
a bare column name is, so a typo in an expression is refused by name rather than
drawing a dash for ever.

**Nothing is silently skipped.** An unknown key, an unknown layout, a group with
no title or no metrics, the same metric twice, a refresh that is not a duration,
a metric this broker does not serve, or a `by:` label that metric does not carry
is refused by name before a screen is drawn. A metric column's heading is its
name with the prefix its neighbours share dropped, which is why a channel table
reads `records bytes`; a computed column's heading is the `name` you gave it,
verbatim, and it is left out of that shared-prefix calculation so it cannot
lengthen the others.

**The shipped dashboard is the exception, and reports instead of refusing.** It
names queues, bridges and refusal counters, and most brokers have no queue and no
bridge - so it draws what this broker serves and says at the foot which of its
metrics it has none of. A file you wrote is refused, because you named the metric.

## Formatting

A metric's own name decides how its value is printed, so there is nothing to
configure per metric:

| | |
|---|---|
| `*_bytes` | binary units - `412.1 MiB`, the vocabulary `max_bytes: 16MiB` is written in |
| `*_seconds` | a duration - `1d 2h` rather than `93,784` |
| anything else | a count with thousands separators |

**A blank cell is a dash and never a zero.** RFC 0005 does not measure a `latest`
channel's bytes and a queue channel keeps no record count, so those cells have no
sample - and a zero there would say the channel holds nothing.

## Consumer lag

The shipped dashboard's Channels table carries both readings as computed columns
- `behind` and `data lost` above - because no metric is published for either:

    floor_offset > consumer_position_min      a consumer's data has been deleted
    next_offset − consumer_position_min       how far the furthest behind is

RFC 0005 calls the first the one alert the whole catalogue exists for. Retention
has passed a stored position, so when that consumer returns it is refused rather
than served the oldest surviving record - invariant 1 holding, and also somebody's
missing afternoon of telemetry.

Both are `-` until something has consumed durably, because
`saguin_channel_consumer_position_min` has no series until then. On such a broker
the two columns drop out and the metric is named at the foot of the screen.
