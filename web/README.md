# Sagüin-viewer

An operator's window onto a running [Sagüin](https://github.com/ifnesi/saguin)
broker: a topic
tree, the messages on any topic, a publish form, a replay control, the
sessions connected to it, the retained messages and dead letters it holds,
and a dashboard of the numbers the broker keeps about itself.

```sh
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.txt
../.venv/bin/python app.py saguin-viewer.yaml
```

**The RFC numbers below are saguin's**, and its specification is not in this
repository: [RFC 0002][r2] is the configuration and the broker's own rules,
[RFC 0003][r3] the delivery semantics, [RFC 0005][r5] the operations listener and
the metric catalogue this page draws. They are cited by number and section
throughout.

[r2]: https://github.com/ifnesi/saguin/blob/main/docs/rfcs/0002-channels-and-configuration.md
[r3]: https://github.com/ifnesi/saguin/blob/main/docs/rfcs/0003-delivery-semantics.md
[r5]: https://github.com/ifnesi/saguin/blob/main/docs/rfcs/0005-operations.md

One virtual environment at the top of the repository, which is also what
`make viewer` runs the suite with; `requirements.txt` beside this file is the
viewer's own.

Or as a container, against a broker of your own. Every setting that names a
broker is `${VAR:-default}`, so override as much or as little of the connection
as you need - `dashboard:` is the exception, being a mapping rather than a
string, and a container wanting different dashboards mounts a file over
`/viewer/saguin-viewer.yaml`:

```sh
docker build -f web/Dockerfile -t saguin-viewer .   # from the repo root
docker run --rm -p 8080:8080 \
  -e SAGUIN_VIEWER_LISTEN=0.0.0.0:8080 \
  -e SAGUIN_MQTT_HOST=broker.example.com \
  -e SAGUIN_OPS_URL=http://broker.example.com:9090 \
  -e SAGUIN_OPS_USER=operator -e SAGUIN_OPS_PASSWORD="$OPS_PASSWORD" \
  saguin-viewer
```

`0.0.0.0` in a container and nowhere else: the viewer defaults to loopback,
which is right on your own machine and wrong inside a container, where
loopback is the container and the published port would reach nothing.

**A demo stack can run this same image**, pointed at its own broker - the
viewer is a client of any Sagüin, and a stack that ships one is a deployment
of it rather than the home of it.

Then open <http://127.0.0.1:8080>. With no configuration file at all it looks
for a broker on loopback with no credentials, which is what Sagüin's own
operations listener allows there.

**The page can be served on a Unix socket instead of a port**, for a reverse
proxy on the same machine or wherever who may open it is a question of file
permissions rather than of network. `listen` has the shape a Sagüin listener
has - a `tcp` block with an address, or a `unix` block with a path and the
socket's permission bits - but one block or the other, never both at once,
because a second door is a second thing to secure. The shipped file carries
the `unix` block commented out; swap the comments and the viewer serves on
the socket:

```yaml
listen:
  # tcp:
  #   address: "${SAGUIN_VIEWER_LISTEN:-127.0.0.1:8080}"
  unix:
    path: "${SAGUIN_VIEWER_SOCKET:-/run/saguin-viewer.sock}"
    mode: "${SAGUIN_VIEWER_SOCKET_MODE:-0660}"
```

```sh
.venv/bin/python app.py saguin-viewer.yaml
curl --unix-socket /run/saguin-viewer.sock http://viewer/
```

A stale socket file left by a viewer that died is replaced at the next start;
a `listen` with both blocks filled stops the viewer and names them both, and
one with neither is loopback on port 8080.

**The TCP door answers to an address and refuses a name**, with a `403` saying
so. A browser sends the name it was loaded by and cannot be made to send an
address instead, so a request arriving as `viewer.example.com` is a page on
another site reaching this viewer through your browser - which is what a
domain that re-resolves to `127.0.0.1` buys an attacker against a viewer on
loopback, where none of the cross-site rules apply any more. It would be
publishing records, hanging clients up and requeueing dead letters with your
credentials and your browser. A request labelled `Origin:` somewhere other
than this viewer is refused the same way. There is nothing to configure:
being reached by a name means a proxy, and a proxy goes in front of the
socket door, where who may open the page is a file permission.

So there are two arrangements, and both are ordinary. **A proxy beside the
viewer** - on the same box, a sidecar, however it is put together -
terminating TLS for a real name and reaching the socket: that is a viewer
living on its own box in a deployment. **Or the address in the browser's
bar**, which is what `docker run -p 8080:8080` opened at
<http://localhost:8080> already is. The one arrangement with no answer is a
viewer on its own box, no proxy, opened by hostname - give it the proxy, or
reach it by its address.

**None of this is about the broker.** Which Sagüin this viewer reads - over
TLS or not, on this machine or across a network, through something or
directly - is the `broker` block below. The check is on requests arriving at
this page, never on what the page goes and asks for.

## What it is

**Two doors, and neither has to be the same credential.** The MQTT listener
carries the records; the operations listener carries the numbers and the
routes that answer what numbers cannot. Both are ordinary Sagüin interfaces -
this is a client, and Sagüin needs no change to serve it.

**There is no login page.** An operator configures this the way a service is
configured and types nothing. A page taking two sets of broker credentials
would put those credentials in a browser, and everything that followed from
that - cookies holding passwords, sessions, TLS before the Secure flag means
anything, one full replay per visitor - was cost with no matching benefit.

**It is a personal instrument, not a deployment.** Run it, look, close it.
Nothing stops you leaving it on an ops box and it will work; there is no
machinery here for several people using it at once.

## Configuring it

Every value in `saguin-viewer.yaml` is an environment variable with its
default beside it, the shape docker compose uses:

```yaml
password: "${SAGUIN_OPS_PASSWORD:-}"     # the variable, or empty
password: "${SAGUIN_OPS_PASSWORD}"       # the variable, or refuse to start
```

So the file runs as it stands, and a password reaches the viewer without being
written down. **An unset variable with no fallback stops the viewer and names
it** rather than becoming an empty password that fails at the broker and sends
somebody to look at the broker. A key the file does not know is refused, and so
is a value of the wrong shape - `true` where a number belongs.

The file is the only place settings come from. A variable *beside* it would be
a second way to set one thing and a precedence table nobody needs.

## What each credential reaches

| You have | You get |
|---|---|
| MQTT only | a plain topic tree, messages, publish. No channel names, types or badges |
| operations, narrowed to `/metrics` | the whole working half: the channel list, the badges, the filters to subscribe to, and the dashboard |
| operations, reaching `/v1/operations` | the above plus who is behind, who lost records, what a queue holds, who may connect, what a client may do, and the resolved configuration |

**Nothing refuses to start because one of them is missing.** A route this
credential cannot reach degrades that panel and says so - Sagüin answers a good
credential that does not reach a route with `403` precisely so a reader does not
retry as though the password were wrong.

### An `acl_file` has to grant the page's reply prefix

**`viewer-reply/+`, read.** Every request this page makes that expects an answer -
a point read of a `latest` channel, a channel seek, the sessions verbs - is an
MQTT 5 request naming `viewer-reply/<id>` as its Response Topic. The broker
answers there. So an `acl_file` that grants this client the data filters and not
that prefix produces the worst shape there is: **every request is accepted and
every answer is lost**, with nothing failing anywhere.

```yaml
roles:
  viewer:
    # A topic no channel claims, so `topic:` rather than `channel:` - it is a
    # broadcast topic, and the answer is published on it.
    - topic: viewer-reply/+
      allow: [read]
```

It is not silent: when the broker answers `0x87` for the reply filter, the
viewer prints the refusal and what it costs, the header carries a **replies
refused** pill for as long as it holds, and `/api/state` carries
`granted: false` for the filter.

A broker with no `acl_file` grants everything, so none of this arises there.

Retention badges and the broker's `min_scrape_interval` need
`/v1/operations/config`; without it the interval falls back to the documented
default of a minute and the page says which it is using.

## The schema registry, which is a channel and an agreement

**Sagüin has a schema registry and it needs nothing from the broker.** That
is RFC 0003's point, and this viewer is a reader of it: a `latest` channel
is already a key-value store with delete, so a registry is a channel and a
convention.

```yaml
channels:
  schemas:
    type: latest
    filter: schemas/#
```

- **Register** by publishing the schema text to a topic in that channel.
- **Retire** by publishing a zero-length payload, which is how this channel
  type deletes.
- **Produce** with a Content Type saying the serialization format and a
  User Property named `schema` carrying the schema's *topic*.
- **Consume** by reading that property and point-reading the topic it names.

**Any message carrying a `schema` property gets a control that reads it.**
The viewer point-reads that topic over `$saguin/kv/get` - the one verb on a
`latest` channel that is not already ordinary MQTT - and shows what is
registered there, beside the payload it describes.

**It carries no schemas and knows no deployment.** That is what makes it
work against any broker following the convention rather than against one:
the pointer is read from the message and the schema from the broker. A page
that matched a `msg_type` property against classes compiled into its own
image would work for one stack and no other.

**The pointer is a whole topic rather than a bare id**, and answers are
cached by topic - which is what the convention says to cache. A bare id has
two holes a topic closes: two publishers in different domains choosing the
same name, and nothing in the message saying which prefix to look under.
There is a *read again* control for when a schema is republished at the same
topic.

**Absence has one meaning.** A topic never registered and one whose schema
was retired are the same answer, because a zero-length payload is already
how that channel type deletes. A read whose key names no `latest` channel is
refused on the `PUBACK` rather than answered, so the page says that is what
silence means rather than reporting a timeout.

**A deserialized payload is drawn as its own card, and says what read it.** The
bytes stay above it: they are the record, and the card is an interpretation
of them made with a schema fetched from the broker at the moment of asking.
It carries a *schema registry* mark and names both the message type and the
topic the schema came from, so nobody has to wonder where a field list
appeared from.

**Deserializing is a control in the same row as the other payload
controls** - *deserialize with the registry* - remembered like the rest.
Turned off, messages show their bytes and the schema is still one click
away; turned on, anything whose headers name a schema is read through it.

**Where a publisher names a schema and no Content Type**, the schema itself
says which reader it needs - a `.proto` can only be read by protobuf, and an
Avro schema is JSON. That is not a guess about the payload; it is the schema
settling the question the missing header would have answered, and the page
says when it did so. A declared Content Type always wins: a publisher saying
what it sent is better evidence than anything inferred about it. (The
bento-connectors generator is one that names a schema and no Content Type.)

**Avro as well as protobuf**, through the same path - `fastavro` reads it,
and `requirements.txt` installs both deserializers.

**The `schema` property is the one thing it cannot do without.** That is
what says *where* the schema is, and nothing here guesses it from a topic or
a message type - so a payload carrying no pointer is shown as bytes and left
alone. A JSON payload needs no schema and never did.

**Compiled once per schema, and the cache is keyed by the schema's text.**
Compiling costs about 5ms and a cache hit about 1 microsecond - some six
thousand times cheaper - but the digest is what makes the cache *safe*: a
schema republished at the same topic recompiles, and one that has not
changed does not. A cache keyed on the topic alone would answer with the old
shape for ever, which is the failure a registry exists to prevent.

**Each schema gets a descriptor pool of its own**, and that is not
tidiness. protobuf keeps one global pool keyed by fully-qualified name, so a
republished message fails to load beside the first with `duplicate symbol
'iot.WaterMeasurement'`. A registry exists so schemas can change; a
deserializer that died the first time one did would defeat the thing it
reads.

**A declared format beats a lucky parse.** A protobuf payload whose bytes
happen to be printable reads as UTF-8 without error; shown as text, a
record its own headers describe as protobuf would render as gibberish and
never be offered to the schema that describes it. What the publisher said
it sent wins.

**Showing a schema needs nothing installed**; reading a payload through
one needs its deserializer. `requirements.txt` installs both -
`grpcio-tools` for protobuf and `fastavro` for avro - and the container
image installs that file, so both deserialize everywhere this runs. Where
one is absent anyway, the viewer names the missing package and shows the
bytes rather than failing. Carrying them is why the image is Debian slim
rather than Alpine: grpcio-tools publishes no musl wheels, so on Alpine it
compiles protoc for ten minutes. On Debian slim both arrive as wheels -
the build takes about ten seconds, and the two deserializers are the
difference between a 95MB image and a 268MB one.

## The shape of it

The **Broker** tab holds two things: the channel tree, and an **Operations**
group of the views that are about the broker rather than about a topic - who
is behind, who lost records, what may connect, the resolved configuration,
the retained messages, the dead letters, the sessions.

**That group folds, and starts folded.** There are eleven of them and they sat
above the tree, so on a short window the topics - the thing the tab is mostly
for - began below the fold. Folded, the header carries a count and the view
you are looking at stays visible, because a fold that hides the row saying
where you are reads as nothing being selected. The choice is remembered.

**The resolved configuration is shown as YAML**, though the operations route
answers JSON. An operator reading it has a YAML file open beside them, and a
document in braces and quotes asks them to translate every line before they
can compare it with what they wrote. The writer is held to a real parser in
the suite: what it prints, read back by PyYAML, must be the document it was
given - so a quoting rule that is wrong for one value is a failing test rather
than a line somebody misreads.

**Two of the rows carry a count**: the dead letters and the retained
messages, the two whose number is a thing an operator watches rather than
reads once. It is what that panel would list - what this viewer is holding,
which is what you can open - and past ninety-nine it reads `+99`, because
beyond that what you take from the badge is "a lot" and a four-digit number
pushes the label out of the row. A nought is shown rather than hidden: an
absent badge would say the page does not know, and it does.

## Finding things, and reading numbers out of them

**The tree filters.** A tree of three hundred devices is unusable without
it. Typing narrows to matching topics and opens every level on the way to a
match, and says how many of how many matched. It is remembered between visits
like every other selection - with a *clear* button beside it that appears only
when there is something to clear, which is what keeps a restored filter from
reading as a broker that has lost its topics. The line under the box saying
"20 of 340 topics match" is doing the same work.

**Everything, as it arrives.** One list of every message reaching the
viewer, newest first, whatever topic it is on - the thing a tree cannot show,
which is what is happening *now*. It holds up to a chosen number of arrivals,
pauses, and filters on any part of a message rather than only the topic.

**Search the payloads.** The box beside the payload controls matches anywhere
in a message: the text as it arrived, the hex of a binary payload, the content
type, a user property, and the deserialized record - that last one only
while *deserialize with the registry* is on, because matching text that is
nowhere on the screen would hide messages for a reason the page does not
show. The search runs over every arrival the page holds rather than only
the cards on screen, and it says how many of how many matched.

**Copy a message as JSON**, from the raw card or the deserialized one.
Either way the copy carries the whole record - topic, channel, timestamp to the
millisecond, QoS, offset and every user property - because a payload pasted on
its own is one nobody can place. The deserialized copy also names the
serialization format and whether the publisher declared it or the schema
settled it.

**"Where does a topic land?"** answers the question `saguin --route` answers
at a shell, for somebody with a credential and no shell. A channel claims
whatever its filter matches and filters overlap deliberately, so a name says
nothing about where a record goes. It names the channel, its type and the
filter that claimed it - and where more than one filter matches, it lists
them all and says which one holds it, because an overlap is otherwise
invisible. A topic no channel claims is reported as broadcast, which is also
what a mistyped topic looks like.

**Values over time.** Pick a numeric field out of a topic's payloads and
watch it. JSON needs nothing; a protobuf or avro payload is read through the
schema its own headers name, so this works against any deployment following
the registry convention rather than against the one it was written for. The
same *deserialize with the registry* switch governs it as governs the message
cards - two controls for one decision is a page that disagrees with itself.

A boolean is left out of the field list on purpose: charting a flag as 0 and
1 draws a line between two states that were never on a scale. A protobuf
`int64` arrives as a string, because JSON cannot hold one exactly, and is
read back as a number - otherwise every timestamp and counter in a protobuf
payload would be unplottable.

**A payload that is only a number is a series too.** A sensor publishing
`28.89` on its own topic is the commonest thing on any broker, and it charts
with no interpretation at all. It has to be the whole payload: a number inside
a sentence is a number that happened to be in a sentence, and picking one out
of `pressure 28.89 psi` is how a chart ends up plotting a device id.

**How many messages a topic keeps is a control, not a fixed cap.** The page
holds the last `messages_per_topic` arrivals per topic - the configured value
is the default - and the *keep* selector above the messages offers the same
100/500/1000/2000 the live feed does, because two views answering "how much do
you hold" with different vocabularies is one page disagreeing with itself.
Choosing a larger one **resizes the ring on the server**: a control that only
sliced what was already held would report success and show nothing more.
Beyond two hundred rows the list grows with a *show older* button rather than
page numbers - what sits on page two of a topic that is still receiving
changes every second - and when the ring is full a chip says so, because a
message dropped *here* must not read as one the broker never sent. A `latest`
channel keeps one value and the selector leaves it alone: that is what the
channel holds, not a preference.

**The chart is drawn from what this page is holding**, bounded by that same
ring, and says so - including how many of those arrivals actually carried
the field, when that is fewer than all of them. Sagüin keeps no history;
this is the ring.

## Dashboards you write, in a file

The dashboard above is the built-in one. You can add your own: YAML files that
say which cards to draw, where, and at what size - the same idea as a Grafana
dashboard, kept as a file rather than clicked together in a browser. Each one
becomes a **tab**, to the right of Topics, so several can be open at once.

```yaml
# saguin-viewer.yaml - a mapping of tab name to file, in the order they appear
dashboard:
  Traffic: "dashboards/traffic.yaml"
  Clients: "dashboards/clients.yaml"
  Queues:  "dashboards/queues.yaml"
  Storage: "dashboards/storage.yaml"
  Bridges: "dashboards/bridge.yaml"
  QoS 2:   "dashboards/qos2.yaml"
  Runtime: "dashboards/runtime.yaml"
```

The **key is the tab's name**, the **value is the file**; the tabs appear in
the order written here, left to right. A relative path is resolved beside the
program, so `dashboards/x.yaml` works wherever the viewer is started. Empty or
absent is no extra tabs and the built-in dashboard. The files are read once,
at startup - **a change to a file, or to which files, needs a restart**, which
is how every other setting here works and is right for a personal instrument
you run, look at, and close.

**The seven shipped in `dashboards/` are a partition of what the broker
exports**: every metric Sagüin publishes a number for appears on exactly one
of them, and none appears on two. So a number has one place to be looked
for, and a metric added to the broker is visibly missing rather than quietly
absent from all seven. A test holds them to that.

| tab | what it owns |
|---|---|
| `traffic.yaml` | the message path - what arrived, what was stored, what went out, and every way a message did not reach somebody |
| `clients.yaml` | connections, subscriptions, protocols, and what the broker keeps for a session that has gone |
| `queues.yaml` | leases: depth, in flight, and the four ways work went round again or did not come back |
| `storage.yaml` | what is held and where - records and bytes per channel, and each store's fill, commits and errors |
| `bridge.yaml` | the links this broker dials: which are up, which gave up, and how much each has taken |
| `qos2.yaml` | exactly-once publishing: what is held between a receipt and a release, and what was taken in and never finished. Every broker offers QoS 2 and carries these series, `broker.qos2` block or none |
| `runtime.yaml` | the Go runtime under the broker: collections a second first, then what collecting costs in CPU, heap live against its goal, stacks, allocation, and GOGC and GOMEMLIMIT |

**A card with nothing to draw says so rather than disappearing.** Several
families carry no series until something happens - the broker publishes none
for `saguin_storage_errors_total` until a provider has actually failed, which
is deliberate - and a card that vanished would leave a reader unable to tell
"nothing has failed" from "I have mistyped the metric". So it stays, with a
dash and a line saying the broker publishes no series for it yet.

**No shipped card names a provider, a channel, a queue or a bridge**, so every
one of them runs against any broker. Where Sagüin labels a metric per store,
the card is a split rather than a value: a split is over every one of them and
names none, while a meter comparing one store's fill with its own bound has to
say which store - so that form takes a `{provider="..."}` selector and a
breakdown refuses one. A test holds every shipped dashboard to naming nothing.

Copying one is the way to start; between them they use every card type. The
four `*_info` families are on none of them, and cannot be: each is the
constant 1 with its facts in labels - a build's version, a channel's filter, a
provider's kind, a bridge's upstream - so a chart of one is a flat line at
one. What they carry is on the page already, in the header and in the
`channels`, `storage` and `bridges` widgets, which read the same scrape.

### The file

```yaml
title: "Fleet"            # the dashboard's name, shown as its header
grid:
  columns: 12             # the horizontal grid a card's width spans (default 12)
  gap: 12                 # px between cards (default 12)
cards:
  - { type: stat, title: "Connections", metric: saguin_connections, width: 3, height: small }
  - { type: timeseries, title: "Publishes", metric: saguin_publishes_received_total, width: 9, height: medium }
```

**Where a card goes.** The layout is Bootstrap's, not coordinates. A card has a
`width` - a span of the grid's columns, `1` to `12` - and cards flow to fill
each row and wrap onto the next, following the window rather than sitting at a
fixed spot; a narrow window stacks them one per row. `height` is `small`,
`medium` or `large` - a named box the card scrolls inside - and every card type
has a sensible default, so a card can give neither and still look right. A
`break` card starts a fresh row, which is how a short tile is kept from sitting
beside a tall chart.

### The cards

Two kinds. **Metric cards** put one number, or one family of numbers, on the
grid - you choose which. **Widgets** are the composite panels the built-in
dashboard already draws (the channel table, the storage meters, the bridge
table); you place and size them, but they draw themselves.

| `type` | draws | key fields |
|---|---|---|
| `stat` | a single number, optionally a sparkline | `metric`; `minus: [<metric>, …]`, `sparkline`, `alarm: "> 0"`, `format: duration`, `note`, `unit` |
| `timeseries` | a line over the window | `metric`, `mode: level\|rate`; `color`, `format`, `note` |
| `breakdown` | a labelled family as bars or a stacked bar | `metric`, `by: <label>`; `style: bars\|stacked`, `severity: warning\|critical` |
| `meter` | a value against a ceiling | `value`, `max` (two metrics); `unit: bytes`, `label` |
| `text` | your own words | `markdown` (see below) |
| `heading` | a section label | `text` |
| `channels` | the per-channel table | - |
| `storage` | per-provider fill meters and commit notes | - |
| `bridges` | the upstream-link table | - |
| `queues` | one card per queue | - |
| `alerts` | whatever alert is firing, or nothing | - |

**A card names a metric, and the name is checked before the viewer starts.**
A card may name any metric Sagüin publishes a number for - all seventy-eight of
them - and a card is refused, with a **startup error naming the card and the
reason**, when it asks for something the renderer cannot honour: a metric
outside the set, a `rate` or sparkline of a metric that keeps no history, a
`by:` on a metric the page has no split for, a single value on a
breakdown-only metric, or a `{label="…"}` selector on a metric that does not
take one. A dashboard that loads is a dashboard that draws; there is no card
that renders as a silent blank.

**`minus:` draws a stat's metric less others**, each a single value, for the
figure no one metric carries: the shipped traffic dashboard's "Holding now" is
`saguin_shares_held_total` minus drained and dropped, which RFC 0005 says is
exactly what shared groups hold. One unknown operand makes the card a dash
rather than a wrong number, and a `minus` card takes no sparkline, whose
history would be the first metric's alone.

**The only metrics that take a selector are the provider ones**, and whether
they need one depends on what is being asked. A `stat` or a `meter` is one
number and has to say which store -
`saguin_provider_bytes{provider="durable"}` - so a bare name there is refused.
A `breakdown` of the same family is over every store and must *not* name one,
so a selector there is refused instead: that is what lets the shipped
dashboards draw per-store metrics without naming a store that exists on one
broker and not another. Everything else is a bare family name.

**A widget with nothing to say draws nothing; a metric card says so.** The
difference is what the reader can conclude. A widget with an empty dataset -
no queues, no bridges - and the `alerts` card when nothing is firing hide
themselves, so a dashboard written for a busy broker is not a wall of empty
panels on a quiet one. A `stat` or a `breakdown` whose family the broker is
not publishing keeps its card and says the broker publishes no series for it
yet, because those two are the ones a reader goes looking for by name: a card
that vanished left them unable to tell "nothing has failed" from "I have
mistyped the metric".

**`text` and `heading` carry your own words**, which is what turns a set of
numbers into a dashboard for *your* estate - what a topic prefix means, which
device is where, what a reader should look at first. The markdown is
deliberately small: headings, **bold**, _italic_, `code`, links and line
breaks. It is a caption, not a document.

## Alarms

**Nothing here is a built-in rule, and there is no alarm you did not write.**
An alarm is one line on one stat card in one of *your* dashboard files - an
`alarm:` threshold beside the metric - and it exists only because you put it
there. The viewer ships no default alarms and has no opinion about what a
worrying number is:

```yaml
- { type: stat, title: "Publishes refused", metric: saguin_publish_refused_total,
    alarm: "> 0" }
```

The rule is the whole of the expression: a comparison - `>`, `<`, `>=`, `<=`,
`==`, `!=` - against one number. Every scrape, the viewer reads that metric and
evaluates it. **It starts firing on the first scrape where the comparison is
true, and it clears on the first scrape where it is false** - nothing else opens
or closes one, and there is nothing to acknowledge. So an alarm's resolution is
the scrape interval: the broker recomputes its catalogue at most once a minute,
so an alarm cannot notice anything faster than that, and something that goes
wrong and comes right between two scrapes is never seen at all. On a counter -
anything ending `_total`, which only ever goes up - `> 0` therefore fires the
first time it moves and then stays firing for the life of the broker, which is
usually what you want from "has this ever happened", and never what you want
from "is this happening now".

A metric the broker is not exporting reads as absent rather than as zero, so
`< n` does not fire on a provider that is not running. Where the reference names
one of several - `saguin_provider_bytes{provider="durable"}` - the selector is
honoured, and the value compared is that one's, not the total.

To change what fires, edit the dashboard file and restart the viewer; the cards
are validated at startup, so a threshold the page cannot evaluate stops it there
with the card named. To stop an alarm firing, delete its `alarm:` line - its
open episode is closed the next time the viewer starts and then ages out of the
window like any other.

A stat card's `alarm:` threshold turns the card red the moment it fires. The
**Alarms tab**, right of Dashboard, makes that durable: the viewer's
own background loop evaluates every dashboard alarm on each scrape - on the
server, not in your browser - and records each *episode*, when a threshold
started firing and when it stopped. So an alarm that fired and cleared while no
page was open is still there when you next look, which is what makes the viewer
worth leaving running as the monitor it is shaped like.

The tab lists what is firing now on top, then the episodes that fired and
cleared within the window - each with its metric, its dashboard, when it
started, when it cleared, and for how long. The record shares the metrics
ring's storage and its five-day bound: set `history_file` and the episodes
survive a restart - a still-firing one stays a single episode across it - and
are trimmed to five days; leave it empty and the tab still shows what is firing
now and whatever accrued this run, but nothing outlives the process. There is
no acknowledgement to manage: an alarm is firing or it is not, and the record
says which, for how long, and when.

Only the dashboards you write define alarms the tab records; the built-in
dashboard draws its own in the page. The threshold is the card's own and the
recorder is the only thing that evaluates it, so the tab, its badge and the red
card never disagree about what is firing. **The Dashboard tab carries no alarm
strip and no firing count** - a card that is firing is already red where it is
drawn, and alarms are the Alarms tab's business.

**Notify me**, at the right of the Alarms tab head, asks the browser to pop a
desktop notification when an alarm *starts* firing. It is per browser, it
remembers itself between visits, and it needs a page left open - this is a page
watching a broker, not a paging system. Arming it while something is firing
notifies about that too, so the click shows what it did. It says why when it
cannot: a browser that is blocking notifications for the page, a prompt that
was dismissed, or an origin that offers no notifications at all - a page served
over plain HTTP to anything but localhost, which is most LAN deployments, and
where putting the viewer behind TLS is the answer.

## Retained messages, which are not a channel's values

**Retained messages and `latest` channels are two different stores**, and
the page keeps them apart. A retained message is the value the broker keeps
for a **broadcast** topic - one that no channel's filter claims - and it lives
in the `broker.retained` block. A `latest` channel holds the current value of
the topics its own filter claims. Both arrive with the retain flag set,
because that is how MQTT says "this was stored before you subscribed", so
collecting by the flag put every channel value in a panel about broadcast.
The test is the channel: only a topic no channel claims can be retained.

The panel lists them, filters by topic, and clears one - which publishes an
empty retained message, the way MQTT deletes a value.

**Clearing one deletes no message.** A broadcast message is delivered to
whoever is subscribed and stored nowhere; the retained value is a separate
slot for the topic. So the topic's list is unchanged by a clear, and the
empty publish that did it is not shown there either: among a thousand
arrivals it invites the reader to work out which one it removed, and the
answer is none of them. Whether a topic holds a retained value *now* is said
beside the topic's heading, read from the store - the only place that can
answer it, since a retained publish reaching a live subscriber carries the
flag clear and is indistinguishable from any other message.

**A viewer that has just started sees only what it is served.** A retained
value published by somebody else appears after this viewer reconnects and is
handed the store; one published from this page's own form appears at once,
because the page knows what it asked for.

**Each row says how many messages arrived on that topic**, beside the one
value being kept. A retained value is one slot per topic: publish five times
and four were delivered and are gone, and the fifth is what a new subscriber
is handed. One row where somebody has just published five times reads as a
panel that lost four - and the tree beside it, which lists all five arrivals,
makes that look confirmed. Two different facts, and the column says which is
which without making anybody open both.

## Sessions, and hanging one up

Who is connected, who is holding a session with nothing behind it, and the
one verb an operator takes.

**Two numbers could not answer this.** `saguin_connections` beside
`saguin_sessions_offline` says three hundred devices with two hundred
connected, which is either a rota or a hundred that have stopped calling -
the counts read the same for both. `/v1/operations/sessions` names them: each
client, whether a connection is behind it, which listener it came in by, its
keepalive, how long its session has left, and how far behind it is, that last
joined from `/v1/operations/consumers` rather than asked for twice. Held
sessions sort first, because the list is capped and what should fall off the
end is the fleet behaving.

**Hanging one up ends the connection and leaves the session alone.** The
device reconnects and resumes at its stored position, so the whole cost is
one reconnection - which is what makes it safe to put behind a button.

**It withdraws nothing by itself**, and the button does not pretend
otherwise. The device comes back with whatever the broker's password file
and `acl_file` say at that moment. Taking a device's access away is: edit
those two files, signal the broker to re-read them, then hang the client up
so it connects again and is refused (RFC 0002, *Withdrawing a device's
access*). What the button is for on its own is a client that is stuck - and
on a queue that is not cosmetic, because the jobs it was holding go back for
another worker when its connection ends.

**It is an MQTT publish, not a route on the operations listener.** Whether
somebody may hang up a client is an authorization question, and the broker
answers those in exactly one place - a verb on the read-only operations
listener would need a second answer to it, kept in step with the ACL for
ever. So the page publishes to `$saguin/sessions/disconnect`, and the broker
answers on the reply topic (RFC 0002, *Hanging up a client*).

**It needs a grant the viewer will not have by default.** `disconnect` is a
`broker: sessions` rule - a rule kind of its own, which no channel or topic
grant confers however wide. Without it the broker answers `0x87` and the page
says which rule to write. The button is on the same MQTT credential as
everything else the page publishes; there is no second user.

**The page will not hang itself up.** The broker refuses that too, but its
refusal would arrive on the connection it is about, so the page settles it
first and keeps the answer.

## Who lost records

Retention moves a channel's floor - its oldest readable offset - forward. When
the floor passes a reader's stored position the records between are gone, and
that reader's claim on them goes with them. **Who lost records** in the sidebar
names the readers it happened to and how much each lost, worst first by
records lost, from `/v1/operations/position-lost`.

**The counter cannot answer it.** `saguin_channel_position_lost_total` is one
number per channel and it counts occurrences rather than readers - the floor
overtakes the same lagging reader thousands of times in a busy run - so a rate
that will not come down says how much and never which one. A series keyed by
client id is not available to it either, for the reason the consumers route
exists: a client chooses its own id.

**A reader is named with its scheme, and the page shows it whole.** A
position is stored under `mqtt:<client id>` for an MQTT session and
`bridge:<rule name>` for an outbound bridge rule - the prefix is there
because a client may legally call itself `bridge:head-office`, and without it
a device and a bridge link would be one row. `/v1/operations/consumers` keys
its rows by the same string, so the name in this table is the one to paste
into *Who is behind* to find the same reader; the export writes it whole for
the same reason. The `kind` under the name is a different question - a
`session` and a `consumer` share the `mqtt:` scheme.

**The rows that were never told are drawn as the loss they are**, in the
colour *Who is behind* deliberately does not use for a straggler still
catching up. A durable session whose stored position had already been passed
when it reconnected is told - `Session Present = 0` - and starts again knowing
it lost its place. A consumer overtaken while it was connected and reading
cannot be told at all: MQTT has no way to say it, so the device believes it is
up to date, cannot ask for what it missed, and nobody but whoever opens this
page ever finds out. As one column among eight that row would be the easiest
thing on the page to miss, and it is the only one nothing else in the system
will report.

**Three numbers, and the page says all three.** `returned` is what this body
carried, capped at a hundred; `tracked` is what the broker is holding, up to a
thousand; `beyond` is rows that never fit in the record at all and were never
kept. A page showing a hundred rows and mentioning neither of the others has
an operator believe they have seen the whole fleet.

**It is held in memory, and a restart empties it.** An empty page is a broker
that has lost nothing since it started, which is not the same as one that
never has. Nothing here is history.

## Dead letters, and putting one back

Work a queue gave up on lands in its dead-letter channel - an ordinary `append`
channel the broker derives, browsable in the tree like any other. **Dead
letters, and putting one back** in the sidebar answers what the tree does not:
which jobs failed, why, and a way to put one back once the bug is fixed.

Each record shows the `saguin-dlq-*` metadata the broker stamped on it - the
reason, the attempts spent, the queue it came from, when it failed - and the
topic it would go back to.

**Requeue is an ordinary publish, and needs nothing from the broker.** A dead
letter carries the job's own topic with one `__dlq` level inserted, so putting
it back is that level removed and the payload republished. Where the level sits
depends on the queue's filter, and the viewer does not re-derive that rule: the
dead-letter channel's own filter carries it at exactly the position to remove
(`jobs/__dlq/#`, `iot/+/work/+/__dlq`), and that comes from the broker's
catalogue.

What it costs, said on the page rather than discovered: the job goes back as a
**new record** - a new offset, and an attempt count starting at 1. What survives
is its identity, because `saguin-id` is carried back, so a consumer can tell
this is the same work returning rather than new work that looks like it. The
publisher's own properties go back with it; the broker's `saguin-dlq-*` ones do
not, because the broker refuses them from any client.

**One record per click, and there is no drain-all.** Redriving into a queue
whose bug is not fixed dead-letters the job again, and doing it in a loop is a
way to fill a disk - so an operator putting fifty back does it fifty times, on
purpose.

**The head stays put, and pages from where you are reading.** Both this panel
and Retained messages pin their title, count, filter and pager to the top of
the pane, and the table pins its column names directly under them - fifty rows
in, a reader would otherwise be looking at eight untitled columns with the
controls somewhere above. The column row is flush against the strip, and that
is exact rather than tidy: any distance between where it rests and where it
pins is distance it visibly travels first, so it would creep upward as you
scrolled and stop, and a gap left open is a gap the list scrolls through in
plain sight.

The pager is in that strip *and* under the table: paging only from the bottom
means scrolling to the end to move and back to the top to read, and a reader
who has just typed in the filter is at the top, which is exactly where the
control was not. **The pinned copy says nothing when everything fits**, because
the panel's own heading is two lines above it and already carries the count -
"35 dead letters" under a title reading "Dead letters 35" is one number
printed twice and a reader checking whether they agree. The copy under the
table still shows it, where there is no heading in sight.

**Two grants, on two channels.** Reading a dead-letter channel needs `read` on
it; putting a job back needs `write` on the *queue*. An ACL that gives the
viewer the first and not the second is the ordinary case, and the requeue is
then refused by the broker with `Not authorized` - shown on the page, with what
to check, and nothing is requeued. Check a credential's grants with
`saguin --acl <config> <user>`.

The page can only put back what it is holding: it lists the last
`messages_per_topic` records per topic, from what has arrived since it
connected, and says so. The list filters by topic, pages fifty at a time -
the filter applied before the page, because filtering a page is a search
whose answer depends on where the reader was standing - and hides what has
already been put back, on a toggle.

**It remembers what it put back, and that mark can only live here.** The dead
letter itself cannot carry it: a dead-letter channel is an `append` channel
and a record written once never changes. Nor can the job going back -
properties there show up only if it fails *again*, and a passive site holding
a copy of the dead-letter channel would then have the original while the
active one had the marked retry, which is two sites disagreeing about one
record. So the viewer keeps it, beside the metrics history in the same SQLite
file when `history_file` names one, and a restart does not turn a handled
row back into an unhandled one.

It is keyed by `saguin-id`, because that is the work's identity across the
whole round trip: a job put back, failed again and dead-lettered a second
time carries the same id, so the new record says *already put back once*
rather than looking untouched. It expires with the record it is about - the
dead-letter channel's own `dlq_retention_period`, read from the broker, per
channel, because two queues need not agree. A mark outliving its record is a
row nobody can see; one expiring first is a row that silently forgets.

**What it buys is that the second click is a different decision from the
first.** The queue cannot refuse a duplicate - a requeue is an ordinary
publish - so knowing it has been done is the only thing between an operator
and the same work twice. The confirmation says how many times and by which
credential.

**A scoped credential needs `subscribe` narrowed to match, and this is the trap
the feature sets for itself.** The viewer subscribes to `#` by default, which a
broker with no `acl_file` grants - but a credential scoped to the dead-letter
channels, which is exactly what this feature invites you to give it, is answered
`Not authorized` for `#`. That refusal arrives in the SUBACK, an ordinary
acknowledgement, so the viewer stays connected and simply receives nothing: an
empty Dead letters list, and no reason for it. Narrow the subscription to what
the credential may read:

```yaml
broker:
  mqtt:
    subscribe: "jobs/__dlq/#"      # or a {a,b} list of the dead-letter filters
```

The page says so rather than leaving it to be worked out - a refused filter is
named in the header from every tab, and the Dead letters list says which
subscription was refused and what to narrow it to.

## TLS, and mutual TLS

Both doors take the same block, so an operator learns one shape:

```yaml
broker:
  operations:
    url: "https://broker.example.com:9443"
    tls:
      enabled: true
      ca_file: /etc/saguin/tls/ca.pem      # a private authority
      cert_file: /etc/saguin/tls/viewer.pem   # given together with the key
      key_file: /etc/saguin/tls/viewer-key.pem
      insecure_skip_verify: false
```

**A client certificate on the operations listener names the caller**, and a
certificate Sagüin verified but has no password-file entry for reaches
`/metrics` and nothing else - so the dashboard works and the operations
panels are refused, each saying which and why. That is Sagüin's rule rather
than a limitation here: widen it with `saguin --passwd add` and a scope, or
configure a password as well.

`insecure_skip_verify` is honoured and said out loud in the log, because a
tool that silently stopped checking certificates is worse than one that
never offered to.

## What it deliberately will not do

**It never consumes a queue.** A queue delivers each job to one consumer, so a
viewer subscribing would take work from the worker meant to have it. Queues
appear as channels it does not read, and what one holds is read from
`/v1/operations/queues/<channel>`, which leases nothing and returns no payloads.

You cannot break this by widening the filter: the viewer subscribes to `#` and
the *broker* leaves a queue's records out of any filter that merely crosses one.

**It is not Prometheus.** Sagüin keeps no history - every scrape is a snapshot -
so any line here begins when the viewer began. Current values are right on the
first scrape because the counters are cumulative; a rate needs a second.

**A minute is the floor and there is nothing faster to have.** Sagüin
recomputes its catalogue at most once per `min_scrape_interval`, and answers a
scrape arriving sooner with the previous one - so a faster reading is the same
reading, and the failure looks exactly like success. The viewer scrapes on the
broker's interval, the page asks again just after the next sample is due, and
the "taken Ns ago" counter ticks locally without asking anything.
`scrape_interval` in the configuration can make it *longer* for a busy broker;
asking for shorter is clamped, with a line on the page saying so.

The dashboard keeps **up to five days** in memory and the header chooses how
much of it to draw - a window from `15m` up to `5d` (`15m`, `30m`, `1h`, `2h`,
`4h`, `8h`, `12h`, `1d`, `2d`, `3d`, `4d`, `5d`), defaulting to `2h`. The ring
is held to five days by the age of its samples, not a fixed count, so a slower
`scrape_interval` still keeps five days rather than five days' worth of a
faster cadence. By default that ring is memory only, so a chart begins when the
viewer began. Set `history_file` to a path and the ring is kept in a **SQLite
database** instead - committed at every scrape and reloaded at start - so a
restart resumes the charts rather than blanking them. The honest cost is drawn
rather than hidden: an hour the viewer was not running is a gap in the line,
not a straight line across it. The file is bounded to the same five days the
ring holds (so it does not grow without limit), created if it does not exist,
and a path that cannot be used - a missing directory, a file that is not a
database - stops the viewer naming the problem rather than losing the history
silently. The widest windows are drawn from a downsample of the line, so five
days stays a smooth chart; the numbers behind it are untouched. For history
beyond five days, point Prometheus at `/metrics`; that is what the endpoint is
for.

**It ships no schemas.** A payload is shown as JSON where it parses, as text
where it is text, and as hex where it is neither - and deserialized only
through a schema the message itself points at, fetched from the broker at
the moment of asking. A viewer with one deployment's protobuf compiled
into it would be a viewer for that deployment.

## Things worth knowing before they surprise you

**It connects clean, every time.** An MQTT 5 subscriber with no stored position
is served an append channel from its retention floor - the whole replay - so the
viewer is useful the moment it opens. A session kept across restarts would get
that history exactly once, ever.

**It is a durable consumer while it runs, and says so.** A seek needs a stored
position to move, so the session outlives the connection by
`session_expiry`. That means this viewer appears in
`/v1/operations/consumers` by name, and a viewer seeked back to an old offset
lowers `saguin_channel_consumer_position_min` - the number Sagüin's one standing
alert is built on. The client id is recognisable on purpose.

**Publishing uses a connection of its own.** Several MQTT refusals end the
connection rather than answering it, and a form that could drop the page's
subscription would be a form that wipes the window it sits in.

**A message's expiry is shown as the moment it runs out, not as the seconds
left.** On a channel that moment arrives ready made, as `saguin-expires`,
and the page reads it: it is there whether or not the deadline has passed,
which MQTT's own field cannot say, since the specification deletes an
expired message rather than delivering one and a channel still serves it. On
a broadcast topic, which carries none of Sagüin's own properties, the page
adds MQTT's remaining interval to the receipt time instead, and an expired
value there has been deleted rather than served, so the awkward case does
not arise. A message whose publisher set no expiry shows nothing at all,
because absent and zero are different statements and only one of them is a
deadline. The publish form sets one, so what the page shows can be driven
from the page itself.

**Broadcast topics are not stored anywhere**, so none replays when the viewer
connects - they appear as messages flow, and they are gone when it restarts.
The one exception is a retained value: the broker's retained store always
exists - `broker.retained` only changes which provider holds it and for how
long - and `0x9A` refuses a retained publish only from a client whose roles
deny `retained` ([RFC 0002][r2], *Retained messages on a broadcast topic*).

**MQTT 3.1.1 is selectable, and what changes is not what you might expect.**
Where a subscriber starts is the *channel's* business: a channel writing
`start: tail` serves a reader with no stored position only what arrives next,
and every other channel replays from its retention floor - the same for both
protocols, so that one application does not behave two ways depending on
which client library it linked. The tree marks a `tail` channel, because
that is what decides whether history appears.

What 3.1.1 does change is everything carried *beside* the payload. It has no
user properties, so the offset, the broker timestamp and the `schema` pointer
are all absent - which means **the schema registry is MQTT 5 only**, since
there is nothing on the message to name a schema with. It has no Response
Topic, so a point read is impossible and a seek can be sent but never
answered. And its `PUBACK` carries no reason code, so an acknowledgement says
only that one arrived. The viewer reports each of those in words rather than
showing a blank.

## The page

**A confirmation is drawn by the page, not by the browser.**
`window.confirm` cannot be styled, cannot lay out a paragraph about duplicate
work, and blocks the tab - so nothing can drive it, including a test. Escape
and a click on the backdrop both answer No, because the safe answer has to be
the one a hurried operator gets by dismissing the box.

React and htm are kept in `static/lib/`, so there is no build step, no
toolchain, and the page renders on a machine with no route to the internet -
which is most of the places a broker like this one runs. Editing
`static/app.js` and reloading is the whole development loop.

Kept in cookies: which tab you were on, where you were in the tree or which
panel you had open, whether the Operations group is folded, the sidebar
width, the seek settings, the theme, the tree filter, the message search, how
many messages a topic keeps, the dead-letter and retained filters and whether
put-back rows are hidden, the feed's filter and how many arrivals it keeps,
the payload controls (indented JSON, deserializing, the copy encoding), and the
dashboard's window and refresh interval. The only things written to disk are
the metrics ring and the alarm episodes, and only when `history_file` names a
SQLite database to keep them across restarts - off by default; nothing else is
stored. The one thing it always writes is a temporary directory while it
compiles a `.proto`, because protoc reads files rather than bytes, and that
is removed as soon as the descriptor is built.

## Tests

```sh
make viewer          # from the repository root
../.venv/bin/python -m unittest discover -s tests -t .   # or directly
```

`-t .` because the tests import `app` from the directory above them, and
`tests/__init__.py` exists because discovery refuses a start directory it
cannot import - without it the suite is silently not found, which looks
exactly like a suite that passed.

They run against no broker except where they say so, and two say so. Both need
saguin's own checkout - `SAGUIN_REPO`, or a sibling `../saguin` with `bin/saguin`
built - and both skip with a message naming what to build where there is none.
**A skip is a weaker run rather than a passing one**: these two are what hold this
viewer to a broker instead of to a document.

* One compares this viewer's topic-to-channel matching against `saguin --route`,
  which is what keeps the two from deciding differently where a record lands.
* The other starts a real broker, drives it until it publishes every metric it
  can, and fails if RFC 0005's catalogue, that broker's scrape, `ALLOWED_METRICS`
  and the shipped dashboards disagree in any direction. It also reads RFC 0005
  itself, so it needs the checkout for the document as well as the binary.

Three more skip without `node`, which drives `static/app.js` directly.
