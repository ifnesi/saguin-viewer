#!/usr/bin/env python3
"""saguin-viewer - an operator's window onto a running saguin broker.

Two doors, and it needs neither of them to be the same credential. The MQTT
listener carries the records; the operations listener carries the numbers
and the three routes that answer what the numbers cannot. There is no login
page: an operator configures this the way a service is configured and types
nothing, because a page taking two sets of broker credentials would put
those credentials in a browser.

**It never consumes a queue.** A queue delivers each job to one consumer, so
a viewer subscribing would take work from the worker that is meant to have
it. Queues appear as channels it deliberately does not read, and what they
are holding is read from `/v1/operations/queues/<channel>`, which leases
nothing. Their dead-letter channels are ordinary `append` channels and are
read like any other.

**It asks the broker which channels exist**, from the operations listener's
own catalogue, rather than carrying a list. The catalogue is the right
source rather than the resolved configuration, and the difference is the
dead-letter channels: a queue derives one, nothing configured it, and the
configuration route leaves it out because a document naming it would not
load. The catalogue carries it, with the filter the broker derived.

**It asks the same catalogue where each channel is.** A channel carries a
topic filter rather than claiming the topics under its own name, so
`iot/water/location/wq-001` and `iot/water/measurement/wq-001` share three
levels and belong to two channels. The page subscribes to the filters it is
told and groups each topic under the channel the broker says holds it.

**A `latest` channel is shown as one value per topic, not as a history.** It
holds the current value of each topic and nothing else, so a scrolling log
of arrivals there is a picture of this viewer's own memory rather than of
the channel.

**A payload it cannot read is still shown.** Reporting only that something
could not be decoded describes the viewer rather than the record, and the
record is what somebody opened this page to see.

**Every count comes from the broker.** A page printing its own tally beside
a dashboard printing the broker's is how this viewer once showed 8 against
23,879 for one channel, with nothing to tell a reader that was not a fault.
"""

import binascii
import collections
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import sqlite3
import ssl
import sys
import shutil
import tempfile
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from base64 import b64encode

import yaml

try:
    from google.protobuf.json_format import MessageToDict
except ImportError:      # deserializing is optional; showing a schema is not
    MessageToDict = None

import paho.mqtt.client as mqtt
from paho.mqtt.client import topic_matches_sub
from flask import Flask, jsonify, request, send_from_directory
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

# **One file, and any value in it may name an environment variable.**
#
# A variable *beside* the file would be a second way to set one thing and a
# precedence table nobody needs - the reasoning that refuses
# SAGUIN_LOG_LEVEL in the broker. `${VAR}` inside it is not that: the file
# still says where every value comes from, and there is nothing to override
# it silently. What it buys is a password that is not written down, which is
# the whole reason to have it:
#
#     password: ${SAGUIN_OPS_PASSWORD}
#     password: ${SAGUIN_OPS_PASSWORD:-}     # and empty is fine
#
# **An unset variable with no fallback stops the viewer and names it**,
# rather than resolving to an empty string. An empty password is a
# credential that fails at the broker, and the operator would go looking at
# the broker for a variable they did not export.
DEFAULTS = {
    # The same shape as a saguin listener - a `tcp` block with an address,
    # or a `unix` block with a path and the socket's mode - but one door or
    # the other and never both: the page shows everything the two
    # credentials below can reach, and a second door is a second thing to
    # secure. An empty address or path is a block that is not there, and
    # neither block is loopback on port 8080 - applied in `bind_target`
    # rather than written here, because a default address here would be a
    # door the file could not close: commenting the `tcp` block out to
    # serve on a socket has to mean there is no TCP door.
    "listen": {
        "tcp": {"address": ""},
        "unix": {"path": "", "mode": "0660"},
    },
    "messages_per_topic": 25,
    # How many arrivals the live feed keeps, across every topic. Bounded for
    # the reason everything here is bounded: a viewer left open on a busy
    # broker would otherwise hold the fleet's traffic in memory until the
    # machine noticed.
    "feed_messages": 2000,
    # Seconds, or 0 to follow the broker. **It can only be longer.** Below
    # the broker's own min_scrape_interval a scrape is answered from the
    # previous catalogue, so a shorter setting here buys the same numbers
    # again and a rate that reads as a staircase - while looking exactly
    # like success. Lengthen it to scrape a busy broker less often.
    "scrape_interval": 0,
    # Where to keep the metrics ring across restarts, or "" to keep it only in
    # memory. The broker holds no history, so every chart begins when the viewer
    # begins - and blanks when it restarts. A path here names a SQLite database
    # the bounded ring (up to five days, by age) is committed to at every
    # scrape and reloaded from at start, so the dashboards resume rather than
    # starting over; the gap for any downtime is drawn as one.
    "history_file": "",
    # Dashboards to show as tabs, right of Topics, in the order written here:
    # a mapping of tab name to dashboard file. Empty is no extra tabs and the
    # built-in dashboard. An empty-dict default is a free-form mapping - the
    # keys are the operator's tab names, not a fixed schema - so `merge` does
    # not hold them to a known set the way it does everywhere else.
    "dashboard": {},
    "broker": {
        "mqtt": {
            "host": "127.0.0.1", "port": 1883,
            "username": "", "password": "",
            # "5" or "3.1.1". Where a subscriber starts is the channel's
            # business and the same for both; what 3.1.1 loses is everything
            # carried beside the payload - no user properties, so no offset,
            # broker timestamp or schema pointer, and no Response Topic, so
            # no point read. Connecting as 3.1.1 is how an operator sees
            # what an old device sees.
            "protocol": "5",
            "client_id": "saguin-viewer",
            # What the viewer watches. `#` is everything, and everything is
            # what an operator opening this wants: it is the only filter
            # that shows a topic no channel claims. Narrow it on a broker
            # holding more than one machine wants to replay.
            "subscribe": "#",
            # A seek needs a stored position to move, which needs a session
            # outliving the connection. It is deliberately short: a viewer
            # holding a position is a consumer like any other, and one left
            # parked behind the head drags
            # saguin_channel_consumer_position_min down - the input to the
            # one alert saguin's whole catalogue exists for.
            "session_expiry": 3600,
            "keepalive": 30,
            "tls": {"enabled": False, "ca_file": "", "cert_file": "",
                    "key_file": "", "insecure_skip_verify": False},
        },
        "operations": {
            "url": "http://127.0.0.1:9090",
            "username": "", "password": "",
            "tls": {"enabled": False, "ca_file": "", "cert_file": "",
                    "key_file": "", "insecure_skip_verify": False},
        },
    },
}


ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def expand_env(value, where):
    """Resolve ${VAR} and ${VAR:-fallback} inside one configuration value."""
    def one(m):
        name, fallback = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        if fallback is not None:
            return fallback
        raise SystemExit(
            f"saguin-viewer: {where} names ${{{name}}} and it is not set. "
            f"Export it, or write ${{{name}:-}} to accept an empty value.")
    return ENV_REF.sub(one, value)


def coerce(value, template, where):
    """A resolved value as the type its default is.

    **Everything arriving through an environment variable is a string**, and
    two of those strings are traps. `false` is a non-empty string and is
    therefore true, so a TLS block written `enabled: "${VIEWER_TLS:-false}"`
    would silently turn TLS on. And a port is a number that arrives as
    `"1883"`, which some libraries accept and others do not. So each value
    is read back into the type of the default beside it, and a value that
    cannot be is a startup error naming the key rather than a surprise later.
    """
    if isinstance(template, bool):
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off", ""):
            return False
        raise SystemExit(f"saguin-viewer: {where} is {value!r}; it is true or false")
    if isinstance(template, int):
        try:
            return int(str(value).strip())
        except ValueError:
            raise SystemExit(f"saguin-viewer: {where} is {value!r}; it is a number")
    return value


def merge(base, over, path=""):
    """Defaults under what the file says, a key at a time."""
    out = dict(base)
    for k, v in (over or {}).items():
        if k not in base:
            # Refused rather than ignored: a misspelled key that quietly
            # does nothing is a setting an operator believes they made.
            raise SystemExit(f"saguin-viewer: unknown configuration key "
                             f"{(path + '.' + k).lstrip('.')!r}")
        where = (path + "." + k).lstrip(".")
        if isinstance(base[k], dict):
            # A scalar where a block belongs - `listen: "127.0.0.1:8080"`,
            # the shape this key had before it took a saguin listener's -
            # is named here rather than crashing inside the recursion.
            if v is not None and not isinstance(v, dict):
                raise SystemExit(f"saguin-viewer: {where} is a block of keys, "
                                 f"not {v!r}")
            # An empty-dict default is a free-form mapping: operator-chosen
            # keys and string values, rather than a fixed schema to hold to.
            # `dashboard` is the one, its keys being tab names - so it is not
            # recursed and refused a key at a time like the blocks below it.
            out[k] = free_map(v, where) if base[k] == {} else merge(base[k], v, where)
        else:
            out[k] = coerce(expand_env(v, where) if isinstance(v, str) else v,
                            base[k], where)
    return out


def free_map(v, where):
    """A mapping of operator-chosen names to string values, with `${VAR}`
    resolved in each - the shape `dashboard` takes, where the keys are tab
    names a fixed schema cannot enumerate."""
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise SystemExit(f"saguin-viewer: {where} is a mapping of name to value")
    out = {}
    for name, val in v.items():
        if not isinstance(val, str):
            raise SystemExit(f"saguin-viewer: {where}.{name} is a string, "
                             f"not {type(val).__name__}")
        out[str(name)] = expand_env(val, f"{where}.{name}")
    return out


def load_config():
    """The file named on the command line, or the one shipped beside this.

    **Falling back to the bare defaults was a trap.** Environment variables
    are resolved inside the configuration file, which is what keeps one
    place saying where every value comes from - so with no file there was
    nothing to resolve, and `SAGUIN_MQTT_HOST` set by somebody running this
    in a container did precisely nothing while looking exactly like it
    worked. The file ships beside the program, so that is what is read when
    no path is given, and the variables work either way.
    """
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    if not path:
        beside = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "saguin-viewer.yaml")
        if not os.path.exists(beside):
            return DEFAULTS
        path = beside
    with open(path) as fh:
        return merge(DEFAULTS, yaml.safe_load(fh) or {})


def bind_target(cfg):
    """Where the page is served, read from `listen` and checked at startup:
    `("tcp", host, port, None)` or `("unix", path, 0, mode)`.

    One of `listen.tcp.address` and `listen.unix.path` is set, or neither
    is and the page is on loopback at 8080. Both stops the viewer here and
    names the keys, and so does an address that is not `host:port` or a
    mode that is not octal - rather than the value being handed to the
    server to fail with whatever it makes of it.
    """
    listen = cfg["listen"]
    address = str(listen["tcp"]["address"] or "")
    path, mode = str(listen["unix"]["path"] or ""), str(listen["unix"]["mode"])
    if address and path:
        raise SystemExit(f"saguin-viewer: listen.tcp.address is {address!r} and "
                         f"listen.unix.path is {path!r}; the page is served on "
                         f"one door or the other, so empty one of them")
    if path:
        try:
            bits = int(mode, 8)
        except ValueError:
            raise SystemExit(f"saguin-viewer: listen.unix.mode is {mode!r}; "
                             f"it is octal, such as 0660") from None
        return ("unix", path, 0, bits)
    host, _, port = (address or "127.0.0.1:8080").rpartition(":")
    if not port.isdigit():
        raise SystemExit(f"saguin-viewer: listen.tcp.address is {address!r}; "
                         f"it is host:port")
    return ("tcp", host or "127.0.0.1", int(port), None)


def http_server(cfg):
    """The page's server, bound where `bind_target` says and not yet serving
    - the same threaded werkzeug server `app.run` starts.

    Built with `make_server` rather than `app.run` so that there is a moment
    between the bind and the first accept to set the socket's mode. werkzeug
    unlinks a stale socket file and binds, and leaves the permissions to the
    umask; the umask is narrowed to owner-only around the bind so the socket
    is never briefly wider than the mode asked for, and chmod then sets
    exactly that mode.
    """
    from werkzeug.serving import make_server
    kind, host, port, bits = bind_target(cfg)
    if kind == "unix":
        was = os.umask(0o077)
        try:
            server = make_server("unix://" + host, 0, app, threaded=True)
        finally:
            os.umask(was)
        os.chmod(host, bits)
        print(f" * Serving on unix:{host} (mode {bits:04o})", file=sys.stderr)
    else:
        server = make_server(host, port, app, threaded=True)
        print(f" * Serving on http://{host}:{port}", file=sys.stderr)
    return server


# ---------------------------------------------------------------------------
# Dashboards: YAML files of cards the tabs draw. Validated here, at startup,
# for the same reason every other setting is - a card that silently drew
# nothing (a misspelled metric, a rate of a level) is a panel an operator
# believes they placed. A file that loads is a file that draws; a bad card
# stops the viewer and names itself. See the README's "Dashboards" section.

# The metrics a card may bind to, and what the page can *draw* with each - not
# only what the broker emits. A card the renderer cannot honour is refused here
# rather than drawn wrong or silently dropped, which is what keeps the feature's
# promise that a dashboard which loads is a dashboard which draws.
#
#   scalar    - the page can read a single value (a `stat`, or a `timeseries`
#               when `history` is also true).
#   history   - it keeps a series, so a level or a rate line, or a sparkline,
#               can be drawn.
#   breakdown - the one label the page has a renderer to split it by, else None.
#               A `by:` on anything else has no renderer and would draw nothing.
#   selector  - the label a `{label="v"}` selector is honoured on. Only the
#               provider metrics, and they *require* one to name which provider;
#               a selector anywhere else is read by nothing, so it is refused
#               rather than accepted and ignored.
#
# These describe what static/app.js implements in METRIC_MAP; the two are held
# in step by TestAllowListMatchesRenderer.
ALLOWED_METRICS = {
    "saguin_connections":                    {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_subscriptions":                  {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_uptime_seconds":                 {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_published_total":                {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_publishes_received_total":       {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_deliveries_sent_total":          {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_bytes_received_total":           {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_bytes_sent_total":               {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    # Labelled by channel in the broker (metrics.go), but the page sums it - as
    # the built-in card does, "across every queue" - so it is a scalar, not a
    # breakdown. A `by: channel` on it has no renderer and is refused.
    "saguin_queue_depth":                    {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_connections_total":              {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_broadcast_unmatched_total":      {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_deliveries_dropped_total":       {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_deliveries_refused_total":       {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_deliveries_expired_total":       {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_session_expiry_shortened_total": {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_sessions_offline":               {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    # What the last start did to a fleet's sessions: how many came back, and
    # how many were ended and why. Both move at the start and then stand
    # still, so neither carries history - a line of a number that changes
    # once is a flat line with a step nobody is watching for.
    "saguin_sessions_restored_total":        {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    # Wills: what the broker said on a client's behalf, how many deaths are
    # held and not yet announced, and how many a returning device cancelled.
    # The first splits by what made each Will due; the other two are one
    # number each.
    "saguin_wills_published_total":           {"scalar": True,  "history": False, "breakdown": "cause",    "selector": None},
    "saguin_wills_cancelled_total":           {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_wills_waiting":                   {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_sessions_dropped_total":         {"scalar": True,  "history": False, "breakdown": "cause",    "selector": None},
    # What sessions hold unacknowledged, summed by the broker over every one:
    # no label divides them, because the only one would be the client.
    "saguin_session_queue_messages":         {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_session_queue_bytes":            {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    # A total (a stat sums the causes) and a breakdown by cause both exist.
    "saguin_session_deliveries_dropped_total": {"scalar": True, "history": False, "breakdown": "cause",   "selector": None},
    # **Shared groups, which are the other half of that question.** Held is
    # what was put on a group's list, drained what was handed to a member,
    # dropped what was let go otherwise, so held minus drained minus dropped is
    # exactly what the groups hold (RFC 0005) - a stat with `minus:`. None
    # carries history: all three are counters that only climb, and the reading
    # RFC 0005 asks for - drained flat while held rises - is two numbers beside
    # each other rather than two lines.
    "saguin_shares_held_total":              {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_shares_drained_total":           {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_shares_dropped_total":           {"scalar": True,  "history": False, "breakdown": "cause",    "selector": None},
    "saguin_retained_messages":              {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    # A total (a stat sums the refusals) and a breakdown by reason both exist.
    "saguin_publish_refused_total":          {"scalar": True,  "history": False, "breakdown": "reason",   "selector": None},
    # Filters a SUBACK refused, by the code's name. Like the publish refusals
    # it has no series until the first refusal, and a total and a split both
    # exist.
    "saguin_subscriptions_refused_total":    {"scalar": True,  "history": False, "breakdown": "reason",   "selector": None},
    # **The Go runtime's own figures** (RFC 0005, "The Go runtime"). The four
    # counters keep a series because their rate is the reading: collections a
    # second first, since a small heap collecting dozens of times a second is
    # the regression this group exists to show. Stacks keep one as a level.
    # Heap live and goal are drawn as one meter and the two settings are
    # facts, so none of those carries history.
    "saguin_go_gc_cycles_total":             {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_go_gc_cpu_seconds_total":        {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_go_gc_assist_cpu_seconds_total": {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_go_heap_live_bytes":             {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_go_heap_goal_bytes":             {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_go_stack_bytes":                 {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_go_allocated_bytes_total":       {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_go_allocated_objects_total":     {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_go_gogc_percent":                {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    "saguin_go_memory_limit_bytes":          {"scalar": True,  "history": False, "breakdown": None,       "selector": None},
    # Breakdown only: the page has no single value for these, so a stat or a
    # line on one would draw nothing and is refused.
    "saguin_connections_by_protocol":        {"scalar": False, "history": False, "breakdown": "protocol", "selector": None},
    "saguin_connections_refused_total":      {"scalar": False, "history": False, "breakdown": "reason",   "selector": None},
    # Provider metrics: a scalar, but only once a `{provider="..."}` selector
    # names which one; the meter reads them that way.
    "saguin_provider_bytes":                 {"scalar": True,  "history": False, "breakdown": "provider",       "selector": "provider"},
    "saguin_provider_max_bytes":             {"scalar": True,  "history": False, "breakdown": "provider",       "selector": "provider"},
    "saguin_provider_publish_commit_max_records": {"scalar": True,  "history": False, "breakdown": "provider",       "selector": "provider"},

    # **The per-entity families, summed for a stat and split for a
    # breakdown.** The broker labels these by channel, queue, provider or
    # bridge, and both readings are worth having: the sum answers "how much
    # work is there", the split answers "which one". A dashboard asks for the
    # one it wants with `by:`, and asking for a split the page cannot draw is
    # refused rather than drawn empty.
    "saguin_max_session_expiry_seconds":     {"scalar": True,  "history": False, "breakdown": None,       "selector": None},

    # Exactly-once. **No breakdown on any of the three**, because the only
    # label that would divide them is the client, and the broker deliberately
    # publishes none: a per-client series is one whose label is a string the
    # client chose, which is a metric an anonymous client can grow.
    #
    # `held` and `abandoned` carry history because both are worth watching
    # over time - one should return to nothing between bursts, and the other
    # only ever climbs. The allowance does not: it is a configured value, and
    # a line of it is a flat line by construction.
    "saguin_qos2_held":                      {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_qos2_abandoned_total":           {"scalar": True,  "history": True,  "breakdown": None,       "selector": None},
    "saguin_qos2_max_inflight_per_client":   {"scalar": True,  "history": False, "breakdown": None,       "selector": None},

    "saguin_queue_inflight":                 {"scalar": True,  "history": True,  "breakdown": "queue",    "selector": None},
    "saguin_queue_delivered_total":          {"scalar": True,  "history": False, "breakdown": "queue",    "selector": None},
    "saguin_queue_acknowledged_total":       {"scalar": True,  "history": False, "breakdown": "queue",    "selector": None},
    "saguin_queue_redelivered_total":        {"scalar": True,  "history": False, "breakdown": "queue",    "selector": None},
    "saguin_queue_returned_total":           {"scalar": True,  "history": False, "breakdown": "queue",    "selector": None},
    "saguin_queue_expired_total":            {"scalar": True,  "history": False, "breakdown": "queue",    "selector": None},
    "saguin_queue_dead_lettered_total":      {"scalar": True,  "history": False, "breakdown": "queue",    "selector": None},
    # Not a fault: a producer set RETAIN on a topic a queue claims, and a queue
    # has no subscription a retained message could be delivered to. It is a
    # question about the operator's own filter, and this series is the only
    # place it is visible.
    "saguin_queue_retain_ignored_total":     {"scalar": True,  "history": False, "breakdown": "queue",    "selector": None},

    "saguin_channel_records":                {"scalar": True,  "history": False, "breakdown": "channel",  "selector": None},
    "saguin_channel_bytes":                  {"scalar": True,  "history": False, "breakdown": "channel",  "selector": None},
    "saguin_channel_consumers":              {"scalar": True,  "history": False, "breakdown": "channel",  "selector": None},
    "saguin_channel_partitioned_consumers":  {"scalar": True,  "history": False, "breakdown": "channel",  "selector": None},
    "saguin_channel_position_lost_total":    {"scalar": True,  "history": False, "breakdown": "channel",  "selector": None},
    "saguin_channel_retention_removed_total": {"scalar": True, "history": False, "breakdown": "channel",  "selector": None},
    # On a latest channel only, so a split names the latest channels and no
    # other: an append or queue channel carries no series rather than a zero.
    "saguin_latest_superseded_total":        {"scalar": True,  "history": False, "breakdown": "channel",  "selector": None},
    # **Offsets do not sum**, so these are a split and nothing else: adding one
    # channel's next offset to another's makes a number that is about no
    # channel at all, and a stat drawing it would be confidently wrong.
    "saguin_channel_next_offset":            {"scalar": False, "history": False, "breakdown": "channel",  "selector": None},
    "saguin_channel_floor_offset":           {"scalar": False, "history": False, "breakdown": "channel",  "selector": None},
    "saguin_channel_consumer_position_min":  {"scalar": False, "history": False, "breakdown": "channel",  "selector": None},

    "saguin_storage_commits_total":          {"scalar": True,  "history": False, "breakdown": "provider", "selector": None},
    "saguin_storage_committed_records_total": {"scalar": True, "history": False, "breakdown": "provider", "selector": None},
    "saguin_storage_errors_total":           {"scalar": True,  "history": False, "breakdown": "provider", "selector": None},

    "saguin_bridge_received_total":          {"scalar": True,  "history": False, "breakdown": "bridge",   "selector": None},
    "saguin_bridge_sent_total":              {"scalar": True,  "history": False, "breakdown": "bridge",   "selector": None},
    "saguin_bridge_loops_skipped_total":     {"scalar": True,  "history": False, "breakdown": "bridge",   "selector": None},
    "saguin_bridge_reconnects_total":        {"scalar": True,  "history": False, "breakdown": "bridge",   "selector": None},
    # Labelled by bridge and cause in the broker; the page adds the causes up
    # per bridge, as it does closed_by on saguin_storage_commits_total.
    "saguin_bridge_unsent_total":            {"scalar": True,  "history": False, "breakdown": "bridge",   "selector": None},
    "saguin_bridge_unstored_total":          {"scalar": True,  "history": False, "breakdown": "bridge",   "selector": None},
    "saguin_bridge_connected":               {"scalar": True,  "history": False, "breakdown": "bridge",   "selector": None},
    "saguin_bridge_stopped":                 {"scalar": True,  "history": False, "breakdown": "bridge",   "selector": None},
}

# **The four `*_info` families are not here and cannot be.** They carry no
# measurement: each is the constant 1 with the facts in its labels - a build's
# version, a channel's filter and type, a provider's kind, a bridge's
# upstream. A chart of them draws a flat line at one. What they carry is on
# this page already, in the header and in the channels, storage and bridges
# widgets, which read the same scrape.

# The widgets are the composite panels - self-contained, placed and sized but
# not otherwise configured. The metric cards take a metric and draw it.
WIDGET_TYPES = ("channels", "storage", "bridges", "queues", "alerts")
METRIC_TYPES = ("stat", "timeseries", "breakdown", "meter")
TEXT_TYPES = ("text", "heading")
# `break` forces the cards after it onto a new row, so a short card is not left
# beside a tall one. It draws nothing and takes a full row of its own.
LAYOUT_TYPES = ("break",)
CARD_TYPES = METRIC_TYPES + TEXT_TYPES + WIDGET_TYPES + LAYOUT_TYPES

# The fields each card type reads. An extra one is refused rather than
# ignored, so a `metrics:` written where `metric:` was meant is caught rather
# than doing nothing. `width`, `height` and `title` are allowed on every card.
CARD_FIELDS = {
    "stat":       {"metric", "minus", "sparkline", "alarm", "format", "note", "unit"},
    "timeseries": {"metric", "mode", "color", "format", "note"},
    "breakdown":  {"metric", "by", "style", "severity"},
    "meter":      {"value", "max", "unit", "label"},
    "text":       {"markdown"},
    "heading":    {"text"},
}
COMMON_FIELDS = {"type", "width", "height", "title"}
HEIGHTS = ("small", "medium", "large")
FORMATS = ("number", "duration", "bytes", "bytes_rate", "msgs_rate", "percent",
           "bytes_or_none")
COLORS = ("series-1", "series-2", "series-3", "series-4",
          "accent", "ok", "warn", "dim", "warning", "critical")
SEVERITIES = ("warning", "critical")

# Group 3 is the selector's *value*, which the page's parseRef has always
# captured and this did not - so the value half of a selector reference was
# read by nothing on this side, and an alarm on one compared against None.
# **A digit is part of a metric name**, which this pattern did not allow
# until `saguin_qos2_held` arrived: every name until then happened to be
# letters, so the omission read as deliberate and was not. A name the
# pattern cannot read is a card the viewer refuses at startup, which is at
# least loud - the page's own parseRef carries the same pattern and has to
# move with it, or the browser silently draws nothing where this accepted.
# Prometheus allows a digit anywhere but the first character.
METRIC_REF = re.compile(r'^([a-z_][a-z0-9_]*)(?:\{([a-z_][a-z0-9_]*)="([^"]*)"\})?$')
ALARM_RE = re.compile(r"^(>=|<=|>|<|==|!=)\s*-?\d+(\.\d+)?$")


def metric_family(ref, where, split=False):
    """The family a `name` or `name{label="value"}` reference names, checked
    against the allow-list. A selector is honoured only on a metric that
    declares one (the provider metrics), which then require it - anywhere else
    a selector is read by nothing, so it is refused rather than ignored.

    **Except on a breakdown, where the rule is the other way round.** A split
    is over every one of them, so naming one would ask for a chart of a single
    bar - and requiring a name there would make the shipped dashboards name a
    provider that exists on this broker and on no other. `split=True` is the
    breakdown card saying so.

    Returns (family, meta)."""
    m = METRIC_REF.match(str(ref).strip())
    if not m:
        raise SystemExit(f'saguin-viewer: {where}: {ref!r} is not a metric name '
                         f'or a name{{label="value"}} selector')
    fam, sel = m.group(1), m.group(2)
    meta = ALLOWED_METRICS.get(fam)
    if meta is None:
        raise SystemExit(f"saguin-viewer: {where}: {fam!r} is not a metric a card "
                         f"may draw. The dashboard draws: "
                         f"{', '.join(sorted(ALLOWED_METRICS))}")
    wanted = meta["selector"]
    if split:
        if sel:
            raise SystemExit(f"saguin-viewer: {where}: a breakdown of {fam!r} is over "
                             f"every one of them, so {{{sel}=\"...\"}} would ask for a "
                             f"chart of one bar")
        return fam, meta
    if sel and sel != wanted:
        if wanted:
            raise SystemExit(f"saguin-viewer: {where}: {fam!r} takes a "
                             f"{{{wanted}=\"...\"}} selector, not {sel!r}")
        raise SystemExit(f"saguin-viewer: {where}: {fam!r} takes no selector - "
                         f"{{{sel}=\"...\"}} would be read by nothing")
    if wanted and not sel:
        raise SystemExit(f"saguin-viewer: {where}: {fam!r} needs a "
                         f"{{{wanted}=\"...\"}} selector to name which one")
    return fam, meta


def require(card, field, where):
    if field not in card:
        raise SystemExit(f"saguin-viewer: {where}: needs '{field}'")
    return card[field]


def validate_card(card, i, columns, where0):
    where = f"{where0}: card {i}"
    if not isinstance(card, dict):
        raise SystemExit(f"saguin-viewer: {where}: a card is a mapping")
    t = card.get("type")
    if t not in CARD_TYPES:
        raise SystemExit(f"saguin-viewer: {where}: unknown type {t!r}. "
                         f"One of: {', '.join(CARD_TYPES)}")
    where = f"{where0}: card {i} ({t})"

    extra = set(card) - COMMON_FIELDS - CARD_FIELDS.get(t, set())
    if extra:
        raise SystemExit(f"saguin-viewer: {where}: unknown field(s) "
                         f"{', '.join(sorted(extra))}")

    # Bootstrap-style: a width in columns (1..grid columns) and a named height.
    # Cards flow and wrap to the window rather than being placed at coordinates.
    width = card.get("width")
    if width is not None and (not isinstance(width, int) or isinstance(width, bool)
                              or not 1 <= width <= columns):
        raise SystemExit(f"saguin-viewer: {where}: width is a whole number of "
                         f"columns from 1 to {columns}")
    height = card.get("height")
    if height is not None and height not in HEIGHTS:
        raise SystemExit(f"saguin-viewer: {where}: height is one of {', '.join(HEIGHTS)}")

    if t in ("stat", "timeseries"):
        fam, meta = metric_family(require(card, "metric", where), where)
        if not meta["scalar"]:
            raise SystemExit(f"saguin-viewer: {where}: {fam!r} has no single value "
                             f"to draw; it is only a breakdown by {meta['breakdown']!r}")
        needs_history = (card.get("sparkline") if t == "stat"
                         else True)  # a level or a rate line both need history
        if needs_history and not meta["history"]:
            what = "a sparkline" if t == "stat" else f"a {card.get('mode', 'level')} line"
            raise SystemExit(f"saguin-viewer: {where}: {what} needs a metric kept "
                             f"over time, and {fam!r} is not")
        # `minus:` draws the metric less others, each a single value: the one
        # figure no family carries, what shared groups are holding now, is
        # held minus drained minus dropped (RFC 0005). No sparkline, since the
        # history is the metric's alone.
        if "minus" in card:
            minus = card["minus"]
            if t != "stat" or not isinstance(minus, list) or not minus:
                raise SystemExit(f"saguin-viewer: {where}: minus is a list of metrics "
                                 f"to subtract, on a stat")
            if card.get("sparkline"):
                raise SystemExit(f"saguin-viewer: {where}: a sparkline draws the metric's "
                                 f"own history, which is not the difference minus draws")
            for ref in minus:
                mfam, mmeta = metric_family(ref, where)
                if not mmeta["scalar"]:
                    raise SystemExit(f"saguin-viewer: {where}: minus {mfam!r} has no single "
                                     f"value to subtract")
        if t == "timeseries" and card.get("mode", "level") not in ("level", "rate"):
            raise SystemExit(f"saguin-viewer: {where}: mode is level or rate")
    elif t == "breakdown":
        fam, meta = metric_family(require(card, "metric", where), where, split=True)
        by = require(card, "by", where)
        if meta["breakdown"] is None:
            raise SystemExit(f"saguin-viewer: {where}: {fam!r} cannot be broken "
                             f"down; the page has no split for it")
        if by != meta["breakdown"]:
            raise SystemExit(f"saguin-viewer: {where}: {fam!r} can be broken down "
                             f"by {meta['breakdown']!r}, not {by!r}")
        if card.get("style", "bars") not in ("bars", "stacked"):
            raise SystemExit(f"saguin-viewer: {where}: style is bars or stacked")
        if card.get("severity") is not None and card["severity"] not in SEVERITIES:
            raise SystemExit(f"saguin-viewer: {where}: severity is one of "
                             f"{', '.join(SEVERITIES)}")
    elif t == "meter":
        for field in ("value", "max"):
            fam, meta = metric_family(require(card, field, where), where + " " + field)
            if not meta["scalar"]:
                raise SystemExit(f"saguin-viewer: {where} {field}: {fam!r} has no "
                                 f"single value to meter")
    elif t == "text":
        require(card, "markdown", where)
    elif t == "heading":
        require(card, "text", where)

    fmt = card.get("format")
    if fmt is not None and fmt not in FORMATS:
        raise SystemExit(f"saguin-viewer: {where}: format {fmt!r} is not one of "
                         f"{', '.join(FORMATS)}")
    col = card.get("color")
    if col is not None and col not in COLORS:
        raise SystemExit(f"saguin-viewer: {where}: color {col!r} is not one of "
                         f"{', '.join(COLORS)}")
    alarm = card.get("alarm")
    if alarm is not None and not ALARM_RE.match(str(alarm)):
        raise SystemExit(f'saguin-viewer: {where}: alarm {alarm!r} is a comparison '
                         f'like "> 0" or ">= 5"')


def load_dashboard(name, path):
    """Read one dashboard file and validate every card, or stop the viewer
    naming the tab, the file and the card. Returns the normalised spec the
    page renders - grid and defaults filled, so the browser re-derives
    nothing."""
    where0 = f'dashboard "{name}" ({path})'
    if not os.path.exists(path):
        raise SystemExit(f"saguin-viewer: {where0}: no such file")
    with open(path) as fh:
        spec = yaml.safe_load(fh) or {}
    if not isinstance(spec, dict):
        raise SystemExit(f"saguin-viewer: {where0}: the file is a mapping with "
                         f"title, grid and cards")
    # Unknown keys are refused here as they are on a card, so a `titel:` typo
    # or a misspelled grid key is a startup error rather than a silent default.
    extra = set(spec) - {"title", "grid", "cards"}
    if extra:
        raise SystemExit(f"saguin-viewer: {where0}: unknown key(s) "
                         f"{', '.join(sorted(extra))}")
    if "title" in spec and not isinstance(spec["title"], str):
        raise SystemExit(f"saguin-viewer: {where0}: title is a string")
    grid = spec.get("grid") or {}
    if not isinstance(grid, dict):
        raise SystemExit(f"saguin-viewer: {where0}: grid is a mapping of columns and gap")
    gextra = set(grid) - {"columns", "gap"}
    if gextra:
        raise SystemExit(f"saguin-viewer: {where0}: unknown grid key(s) "
                         f"{', '.join(sorted(gextra))}")
    columns = grid.get("columns", 12)
    if not isinstance(columns, int) or isinstance(columns, bool) or columns < 1:
        raise SystemExit(f"saguin-viewer: {where0}: grid.columns is a positive integer")
    gap = grid.get("gap", 12)
    if not isinstance(gap, int) or isinstance(gap, bool) or gap < 0:
        raise SystemExit(f"saguin-viewer: {where0}: grid.gap is a non-negative integer")
    cards = spec.get("cards") or []
    if not isinstance(cards, list) or not cards:
        raise SystemExit(f"saguin-viewer: {where0}: cards is a non-empty list")
    for i, card in enumerate(cards):
        validate_card(card, i, columns, where0)
    return {
        "name": name,
        "title": spec.get("title", name),
        "grid": {"columns": columns, "gap": gap},
        "cards": cards,
    }


def load_dashboards(mapping):
    """Every configured dashboard, in the order the config wrote them - which
    is the order the tabs appear, left to right, right of Topics."""
    base = os.path.dirname(os.path.abspath(__file__))
    out = []
    for name, path in (mapping or {}).items():
        # **`builtin` is the viewer's own dashboard**, rendered by its own code
        # rather than reconstructed from a file - the one dashboard a file
        # cannot match, because its dense tiles, flowing rows and inline prose
        # are a hand-built layout rather than a grid of cards. A tab pointed
        # here shows it exactly as it is.
        if path == "builtin":
            out.append({"name": name, "builtin": True})
            continue
        # A relative path is resolved beside this program, so `dashboards/x`
        # works whatever directory the viewer is started from.
        full = path if os.path.isabs(path) else os.path.join(base, path)
        out.append(load_dashboard(name, full))
    return out


CONFIG = load_config()
MQTT_CFG = CONFIG["broker"]["mqtt"]
OPS_CFG = CONFIG["broker"]["operations"]

HOST = MQTT_CFG["host"]
PORT = int(MQTT_CFG["port"])
OPS_BASE = OPS_CFG["url"].rstrip("/")
OPS = OPS_BASE + "/metrics"
OPS_USER = OPS_CFG["username"]
OPS_PASSWORD = OPS_CFG["password"]
CLIENT_ID = MQTT_CFG["client_id"]

if MQTT_CFG["protocol"] not in ("5", "3.1.1"):
    raise SystemExit(f'saguin-viewer: broker.mqtt.protocol is '
                     f'{MQTT_CFG["protocol"]!r}; it is "5" or "3.1.1"')
PROTOCOL = mqtt.MQTTv5 if MQTT_CFG["protocol"] == "5" else mqtt.MQTTv311

# Checked now, so a `listen` of the wrong shape stops the viewer before it
# has connected to a broker rather than after. Which door it is is kept:
# `address_shaped` below applies to the TCP one and to nothing else.
DOOR = bind_target(CONFIG)[0]

# **How many arrivals are kept per topic, and it moves at runtime.** The
# configured value is the default; the topic view offers the same 100/500/
# 1000/2000 the live feed does, and a reader who asks for more has to get
# more from the ring rather than from a page that can only slice what is
# already there. Held in a list so the setter and every reader see one value
# without a global statement in each.
PER_TOPIC = CONFIG["messages_per_topic"]
HISTORY_FILE = CONFIG["history_file"]

# What the page may ask for, and the same list the live feed offers - one
# vocabulary for "how much do you keep", so the two views do not answer the
# same question with different numbers.
KEEP_CHOICES = (100, 500, 1000, 2000)

# The configured dashboards, validated now so a bad card stops the viewer here
# rather than drawing a blank later. Empty when none are configured, and the
# page shows its built-in dashboard then.
DASHBOARDS = load_dashboards(CONFIG["dashboard"])

# **The page's own reply address.** Every MQTT 5 request it makes names this as
# its Response Topic, so an acl_file that does not grant read on REPLY_PREFIX
# loses every answer while accepting every request - which is why the prefix is a
# constant here rather than a string in three places: the advice printed on a
# refusal, the README and this topic are one fact.
REPLY_PREFIX = "viewer-reply/+"
REPLY_TOPIC = f"viewer-reply/{os.urandom(4).hex()}"

lock = threading.Lock()
topics = {}          # topic -> {"count", "last", "channel", "kind", "messages"}
channels = {}        # name -> kind, from the broker's own catalogue
holds = {}           # name -> records the broker says it holds
seeks = {}           # correlation id -> reply, for seeks and point reads
pubacks = {}         # message id -> the reason code the broker answered with
# The retained value the broker keeps per broadcast topic, as this viewer has
# seen it: the retain flag is set on the messages delivered when it subscribes,
# so this is the retained set as of the latest connect. Cleared on reconnect and
# rebuilt from what the broker replays, so a value deleted while disconnected
# does not linger.
retained = {}        # topic -> {"topic", "at", "payload", "qos"}

# **A schema registry needs nothing from the broker**, which is the whole
# point of RFC 0003's convention: a `latest` channel is already a key-value
# store with delete, so a registry is a channel and an agreement. A producer
# publishes the schema text to a topic in it and names that *topic* in a
# `schema` user property; a consumer reads the property, point-reads the
# topic, and caches until the pointer changes.
#
# **Cached by topic, because that is what the convention says to cache.**
# The pointer is the whole topic rather than a bare id - which is what makes
# it work without an allocator, since two publishers in different domains
# cannot silently overwrite each other.
schema_cache = {}

# **Compiled once per schema, not once per message.** The text is cached
# above; this caches what it compiles to, keyed by the schema's topic and a
# digest of its text - so a schema republished at the same topic is
# recompiled and one that has not changed is not. Without the digest the
# cache would answer with the old shape for ever, which is the failure a
# registry exists to prevent.
schema_types = {}


# **A bounded ring, because the broker keeps no history at all.** Every
# scrape is a snapshot; a rate needs two of them, and a line needs many. So
# the viewer keeps its own, and the honest consequence is that a chart
# begins when the viewer began - which the page says rather than implying a
# month of history it does not have.
#
# Up to five days of it, and the page chooses a window out of it. The window
# is a cookie rather than server state: one ring serves whoever is looking,
# and a per-browser setting that resized a shared buffer would let one reader
# shorten another's history.
#
# The ring is bounded two ways, by age first. A scrape arrives at most once a
# minute (the broker's floor), so five days is at most 7200 samples - maxlen
# is that hard cap, which no faster cadence can exist to exceed. But an
# operator who sets a slower scrape_interval reaches 7200 samples only after
# far more than five days, so the age trim is what actually holds the window:
# after each scrape, samples older than the retention are dropped. The on-disk
# table follows for free, because save deletes below the ring's oldest (see
# save_history). Five days is fixed rather than a knob because it is also the
# widest window the picker offers - a retention an operator could set shorter
# than the picker would draw empty cards.
# **What this page has put back, so it can say a job has been redriven
# already.** The mark cannot live on the record: a dead-letter channel is an
# `append` channel and a record written once never changes. It cannot live on
# the job going back either - properties on the redriven job would show up
# only if it failed *again*, and a passive site holding a copy of the
# dead-letter channel would then have the original while the active one had
# the marked retry. Two sites disagreeing about a record is worse than not
# marking it.
#
# **Keyed by `saguin-id`**, which is the work's identity across the whole
# round trip (RFC 0003): a job put back, failed again and dead-lettered a
# second time carries the same id, so that second record says "already put
# back once" rather than looking untouched.
#
# **It expires with the record it is about**, which is the dead-letter
# channel's own `dlq_retention_period` - read from the broker's resolved
# configuration, per channel, because two queues on one broker do not have to
# agree. A mark outliving its record is a row nobody can see; a mark expiring
# first is a row that silently forgets. Neither is a window this page may pick
# for itself, which is why it is not a constant here.
#
# The size cap is a backstop rather than a policy: a channel whose retention
# is `none` keeps its dead letters for ever, and this page must not.
REDRIVE_MAX = 20000                                # ids, oldest dropped first
_REDRIVE_DDL = ("CREATE TABLE IF NOT EXISTS redrives ("
                "id TEXT PRIMARY KEY, at REAL NOT NULL, by TEXT NOT NULL, "
                "count INTEGER NOT NULL, channel TEXT NOT NULL)")
redriven = {}                                      # saguin-id -> {at, by, count, channel}

HISTORY_RETENTION = 5 * 24 * 60 * 60               # seconds; five days
metrics_history = collections.deque(maxlen=7200)   # five days at the one-a-minute floor
metrics_state = {"interval": 60, "taken": None, "error": None, "note": None}
latest_samples = []
metrics_help = {}    # metric name -> the catalogue's own one-line description


def trim_history_by_age():
    """Drop samples older than the retention window; the caller holds `lock`.
    Age, not count, is what bounds the ring when a slower scrape_interval keeps
    fewer than a ring's worth of samples across five days - maxlen is only the
    hard cap for the once-a-minute floor."""
    cutoff = time.time() - HISTORY_RETENTION
    while len(metrics_history) > 1 and metrics_history[0]["t"] < cutoff:
        metrics_history.popleft()


# The ring is persisted to SQLite rather than a JSON file, for consistency with
# the broker's own durability story: one small table, `history(t, row)`, each
# scrape a row whose metric values are a JSON blob - so a new metric needs no
# schema change. A short-lived connection per call, because the scrape runs once
# a minute so the cost is nothing, and the startup thread and the scrape thread
# never share a handle.
_HISTORY_DDL = "CREATE TABLE IF NOT EXISTS history (t REAL PRIMARY KEY, row TEXT NOT NULL)"
_ALARMS_DDL = ("CREATE TABLE IF NOT EXISTS alarms ("
               "id INTEGER PRIMARY KEY, dash TEXT NOT NULL, title TEXT NOT NULL, "
               "metric TEXT NOT NULL, started REAL NOT NULL, ended REAL, peak REAL)")


def save_history():
    """Commit the latest scrape to `history_file`, if one is configured, and
    trim to the ring's window - at every scrape rather than only at shutdown, so
    a restart does not blank every chart. A failure is a warning rather than a
    reason to stop scraping: the in-memory ring still serves this run."""
    if not HISTORY_FILE:
        return
    with lock:
        if not metrics_history:
            return
        newest = metrics_history[-1]
        oldest_t = metrics_history[0]["t"]
    try:
        conn = sqlite3.connect(HISTORY_FILE)
        try:
            with conn:
                conn.execute(_HISTORY_DDL)
                conn.execute("INSERT OR REPLACE INTO history (t, row) VALUES (?, ?)",
                             (newest["t"], json.dumps(newest)))
                conn.execute("DELETE FROM history WHERE t < ?", (oldest_t,))
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"viewer: could not write history to {HISTORY_FILE}: {e}", flush=True)


def load_history():
    """Fill the ring from `history_file` at startup, keeping at most a ring's
    worth and only well-formed rows, so a file that is not a database, or a row
    that is not JSON, degrades to less history rather than a crash. Charts resume
    where the last run left off; the gap for any downtime is drawn as one."""
    if not HISTORY_FILE or not os.path.exists(HISTORY_FILE):
        return
    rows = []
    try:
        conn = sqlite3.connect(HISTORY_FILE)
        try:
            conn.execute(_HISTORY_DDL)
            for (blob,) in conn.execute("SELECT row FROM history ORDER BY t"):
                try:
                    row = json.loads(blob)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict) and "t" in row:
                    rows.append(row)
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"viewer: could not read history from {HISTORY_FILE}: {e}", flush=True)
        return
    with lock:
        metrics_history.clear()
        for row in rows[-metrics_history.maxlen:]:
            metrics_history.append(row)


def open_history():
    """Make the configured history database usable before the viewer runs, or
    stop it loudly naming the problem - rather than starting with a history that
    silently goes nowhere. The file is created if it does not exist (its table
    too); a path whose directory is missing or unwritable, or a file that is not
    a SQLite database, is a configuration error refused here, the way a bad
    dashboard or a bad config value is. Empty history_file keeps the ring only
    in memory and needs no file."""
    if not HISTORY_FILE:
        return
    try:
        conn = sqlite3.connect(HISTORY_FILE)
        try:
            conn.execute(_HISTORY_DDL)                    # create the tables if missing
            conn.execute(_ALARMS_DDL)
            conn.execute("SELECT count(*) FROM history")  # prove they read back
            conn.execute("SELECT count(*) FROM alarms")
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        raise SystemExit(
            f"saguin-viewer: history_file {HISTORY_FILE!r} cannot be used: {e}. "
            f"It must be a writable path to a SQLite database - it is created if "
            f"missing, but its directory must exist and be writable, and an "
            f"existing file must be a SQLite database. Fix the path, or set "
            f'history_file to "" to keep the metrics history only in memory.')


# ===========================================================================
# **Alarms recorded by the backend, so one that fires with no browser open is
# still seen.** A dashboard stat card can carry an `alarm:` threshold; the page
# has always drawn it red live, but a viewer left running as a monitor needs
# the ones that fired while nobody was looking. The poll thread evaluates every
# alarm each scrape and records an *episode* - when it started firing and when
# it stopped - into the same SQLite database as the metrics ring, trimmed to
# the same five days. The Alarms tab reads them back. Off, like the ring, when
# history_file is empty: the tab still shows what is firing now and whatever
# accrued this run, but nothing survives a restart.
#
# The definitions come from the loaded dashboards, so the browser's live red
# card and this record share one source and one threshold. Built-in dashboards
# draw their own alarms in JS and are not recorded here; a file dashboard is.
def collect_alarms(dashboards):
    out = []
    for d in dashboards:
        if d.get("builtin"):
            continue
        for c in d.get("cards", []):
            if c.get("type") == "stat" and c.get("alarm"):
                out.append({"dash": d["name"], "metric": c["metric"],
                            "title": c.get("title") or c["metric"], "expr": c["alarm"]})
    return out


ALARM_SPECS = collect_alarms(DASHBOARDS)
ALARM_KEYS = {(s["dash"], s["title"]) for s in ALARM_SPECS}

# Episodes live in memory as the page reads them, mirrored to SQLite the way the
# ring is - the list is what /api/alarms serves, the database is what survives a
# restart. Both are held under `lock`, shared with the poll thread.
alarm_episodes = []            # {id, dash, title, metric, started, ended, peak}
alarm_firing = {}              # (dash, title) -> the open episode for it
_alarm_next_id = 1

_ALARM_RE = re.compile(r"^(>=|<=|>|<|==|!=)\s*(-?\d+(?:\.\d+)?)$")


def eval_alarm(expr, v):
    """Whether `expr` (e.g. `> 0`) holds for value `v`. The Python twin of the
    page's evalAlarm - same grammar, same answer - so the recorded episode and
    the live red card never disagree. An unparseable expression never fires;
    the card validator has already refused one at startup."""
    if v is None:
        return False
    m = _ALARM_RE.match(str(expr))
    if not m:
        return False
    n = float(m.group(2))
    op = m.group(1)
    return {">": v > n, "<": v < n, ">=": v >= n, "<=": v <= n,
            "==": v == n, "!=": v != n}[op]


def alarm_value(metric, samples):
    """The scalar an alarm compares, read the way the card reads it: the metric
    family summed over its labels, or - where the reference carries a
    `{label="value"}` selector - the one row that selector names. Absent (no
    sample) is None, not zero, so `< n` does not fire on a metric the broker has
    not exported yet - matching the page, which reads null for the same case.

    **The selector has to be honoured here or the record silently omits what
    the page shows in red.** This compared `n == metric` against the whole
    reference, selector text and all, so no sample name could ever equal
    `saguin_provider_bytes{provider="mem"}`: the value was always None and None
    never fires. The card turned red, the strip listed it and nothing was
    recorded - a record that disagrees with the page about what is firing,
    which is the one thing the Alarms tab exists to be trusted about.
    """
    m = METRIC_REF.match(str(metric).strip())
    if not m:
        return None
    family, label, want = m.group(1), m.group(2), m.group(3)
    vals = [v for n, lb, v in samples
            if n == family and (label is None or lb.get(label) == want)]
    return sum(vals) if vals else None


def evaluate_alarms(samples):
    """Open and close episodes from this scrape. Returns a snapshot of the
    episode list to persist when one opened, closed, or aged out - else None,
    so a steady scrape writes nothing. The caller persists outside the lock."""
    global _alarm_next_id
    now = time.time()
    cutoff = now - HISTORY_RETENTION
    changed = False
    with lock:
        for spec in ALARM_SPECS:
            key = (spec["dash"], spec["title"])
            v = alarm_value(spec["metric"], samples)
            ep = alarm_firing.get(key)
            if eval_alarm(spec["expr"], v):
                if ep is None:
                    ep = {"id": _alarm_next_id, "dash": spec["dash"],
                          "title": spec["title"], "metric": spec["metric"],
                          "started": now, "ended": None, "peak": v}
                    _alarm_next_id += 1
                    alarm_episodes.append(ep)
                    alarm_firing[key] = ep
                    changed = True
                elif v is not None and (ep["peak"] is None or v > ep["peak"]):
                    ep["peak"] = v          # in memory; persisted when it closes
            elif ep is not None:
                ep["ended"] = now
                del alarm_firing[key]
                changed = True
        # Closed episodes past the window fall away, like the ring's old samples;
        # an open one is kept however old, because it is still firing.
        keep = [e for e in alarm_episodes if e["ended"] is None or e["ended"] >= cutoff]
        if len(keep) != len(alarm_episodes):
            alarm_episodes[:] = keep
            changed = True
        rows = [dict(e) for e in alarm_episodes] if changed else None
    return rows


def save_alarms(rows):
    """Mirror the episode list to SQLite. A full rewrite of a small table, done
    only on an edge (open, close, age-out) rather than every scrape, so it costs
    nothing while alarms are steady. A no-op unless history_file is configured."""
    if not HISTORY_FILE or rows is None:
        return
    try:
        conn = sqlite3.connect(HISTORY_FILE)
        try:
            with conn:
                conn.execute(_ALARMS_DDL)
                conn.execute("DELETE FROM alarms")
                conn.executemany(
                    "INSERT INTO alarms (id, dash, title, metric, started, ended, peak) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(e["id"], e["dash"], e["title"], e["metric"],
                      e["started"], e["ended"], e["peak"]) for e in rows])
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"viewer: could not write alarms to {HISTORY_FILE}: {e}", flush=True)


def load_alarms():
    """Read the episodes back at startup: the recent history, and any that were
    firing when the viewer stopped so one still-firing alarm stays a single
    episode across the restart rather than splitting. An episode whose alarm the
    config no longer defines is closed here - its definition is gone, so it can
    only be history now - and that close is persisted, so its clear time is
    written once rather than re-stamped on every restart, and it then ages out
    of the five-day window like any other closed episode."""
    global _alarm_next_id
    if not HISTORY_FILE:
        return
    cutoff = time.time() - HISTORY_RETENTION
    try:
        conn = sqlite3.connect(HISTORY_FILE)
        try:
            conn.execute(_ALARMS_DDL)
            rows = conn.execute(
                "SELECT id, dash, title, metric, started, ended, peak FROM alarms "
                "WHERE ended IS NULL OR ended >= ? ORDER BY started", (cutoff,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"viewer: could not read alarms from {HISTORY_FILE}: {e}", flush=True)
        return
    closed_orphan = False
    with lock:
        alarm_episodes.clear()
        alarm_firing.clear()
        for (id_, dash, title, metric, started, ended, peak) in rows:
            if ended is None and (dash, title) not in ALARM_KEYS:
                ended = time.time()          # alarm no longer defined; close it
                closed_orphan = True
            ep = {"id": id_, "dash": dash, "title": title, "metric": metric,
                  "started": started, "ended": ended, "peak": peak}
            alarm_episodes.append(ep)
            if ended is None:
                alarm_firing[(dash, title)] = ep
            _alarm_next_id = max(_alarm_next_id, id_ + 1)
        rows_to_persist = [dict(e) for e in alarm_episodes] if closed_orphan else None
    # Write the orphan's close back, outside the lock like every other save. A
    # close left only in memory would keep the db row's `ended` NULL, which the
    # load query always matches - so it would never age out and its clear time
    # would be re-stamped to each restart. Persisting it once fixes both.
    save_alarms(rows_to_persist)


open_history()

def dlq_retention(dlq_channel):
    """How long the broker keeps records on this dead-letter channel, in
    seconds, or None for `none` - keep everything.

    **Read from the broker rather than assumed**, and per channel, because two
    queues on one broker do not have to agree. It is the queue's
    `dlq_retention_period`: a dead-letter channel is derived and has no block
    of its own, so that key on the queue is where its retention is written.

    A channel this cannot resolve returns None, which keeps the mark rather
    than dropping it - forgetting that a job was put back is the failure this
    exists to prevent, and holding one mark too long costs a row of text.
    """
    # A dead-letter channel's name is its queue's with the reserved level
    # appended, which is the broker's rule and the one place this page needs
    # to undo it. DLQ_LEVEL is defined further down and resolved when this
    # runs, so the two cannot be different strings.
    suffix = DLQ_LEVEL
    queue = dlq_channel[:-len(suffix)] if dlq_channel.endswith(suffix) else dlq_channel
    doc = ops_json("/v1/operations/config")
    if doc.get("error"):
        return None
    raw = ((doc.get("channels") or {}).get(queue) or {}).get("dlq_retention_period")
    if raw in (None, "none", ""):
        return None
    try:
        return int(float(str(raw)[:-1] if str(raw).endswith("s") else raw))
    except ValueError:
        return None


def note_redrive(saguin_id, by, dlq_channel):
    """Remember that this work has been put back, and by which credential.

    **Called only after the broker has acknowledged the publish.** Recording an
    intention rather than an outcome would mark a job the queue never received,
    and the row would then argue against the retry that was actually needed.

    The count is what an operator reads: a job on its third trip through the
    queue is one whose bug is not fixed, and that is a different decision from
    putting one back for the first time.
    """
    if not saguin_id:
        # A record with no identity cannot be followed across the round trip,
        # and inventing a key here would mark one arbitrary row.
        return
    now = time.time()
    with lock:
        prev = redriven.get(saguin_id)
        redriven[saguin_id] = {
            "at": now, "by": by or "", "channel": dlq_channel,
            "count": (prev["count"] + 1) if prev else 1,
        }
        prune_redriven(now)
        rows = [dict(v, id=k) for k, v in redriven.items()]
    save_redrives(rows)


def redrive_note(saguin_id):
    """What this page remembers about putting this work back, or None."""
    if not saguin_id:
        return None
    with lock:
        prune_redriven()
        note = redriven.get(saguin_id)
        if not note:
            return None
        return {"count": note["count"], "by": note["by"],
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(note["at"]))}


def prune_redriven(now=None):
    """Drop what the broker itself no longer holds, and cap what is left.

    **The window is the record's, not this page's.** A mark outliving its dead
    letter is a row nobody can see; a mark expiring first is a row that
    silently forgets it was put back. So each is measured against its own
    channel's retention.

    The cap is a backstop for the `none` case, where the broker keeps dead
    letters for ever and this page must not. Oldest first, because the recent
    marks are the ones somebody is about to look at.

    Called with `lock` held.
    """
    now = now or time.time()
    windows = {}
    for key, note in list(redriven.items()):
        chan = note.get("channel") or ""
        if chan not in windows:
            windows[chan] = dlq_retention(chan) if chan else None
        window = windows[chan]
        if window is not None and now - note["at"] > window:
            del redriven[key]
    if len(redriven) > REDRIVE_MAX:
        oldest = sorted(redriven.items(), key=lambda kv: kv[1]["at"])
        for key, _ in oldest[:len(redriven) - REDRIVE_MAX]:
            del redriven[key]


def save_redrives(rows):
    """Mirror the marks to SQLite, beside the metrics history and the alarms.

    **So that restarting the viewer does not forget what an operator did.** A
    mark held only in memory is one a restart turns back into an unmarked row,
    and the operator then puts the same work back twice - which is the whole
    thing this prevents. A full rewrite of a small table, on the edge that
    changes it. A no-op unless history_file is configured, and a failure is a
    warning: this run still has its own memory.
    """
    if not HISTORY_FILE or rows is None:
        return
    try:
        conn = sqlite3.connect(HISTORY_FILE)
        try:
            with conn:
                conn.execute(_REDRIVE_DDL)
                conn.execute("DELETE FROM redrives")
                conn.executemany(
                    "INSERT INTO redrives (id, at, by, count, channel) VALUES (?, ?, ?, ?, ?)",
                    [(r["id"], r["at"], r["by"], r["count"], r.get("channel") or "")
                     for r in rows])
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"viewer: could not write redrives to {HISTORY_FILE}: {e}", flush=True)


def load_redrives():
    """Read the marks back at startup, so a restart does not forget them.

    Pruned on the way in against each channel's own retention, so a viewer
    started after a long stop does not answer out of a window that has passed.
    """
    if not HISTORY_FILE:
        return
    try:
        conn = sqlite3.connect(HISTORY_FILE)
        try:
            conn.execute(_REDRIVE_DDL)
            rows = conn.execute(
                "SELECT id, at, by, count, channel FROM redrives").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"viewer: could not read redrives from {HISTORY_FILE}: {e}", flush=True)
        return
    with lock:
        for rid, at, by, count, channel in rows:
            redriven[rid] = {"at": at, "by": by, "count": count, "channel": channel}
        prune_redriven()

load_history()
load_alarms()
load_redrives()

# **Everything as it arrives, in arrival order, across every topic.** The
# tree answers "what is on this topic"; this answers "what is flowing", and
# they are different questions - a topic nobody thought to click is exactly
# the one worth seeing. It costs no extra subscription: the viewer already
# receives all of this, so the feed is a second reference to messages the
# per-topic ring is holding anyway.
#
# **Bounded to `feed_messages`, and it was not.** The ring was built twice -
# once from the configuration and again here with the default written in by
# hand - and this line, being the later one, won. So a correctly spelled,
# accepted, type-checked key did nothing for the whole life of the viewer,
# which is the case `merge` refuses a misspelled key to avoid: a setting that
# quietly does nothing is one an operator believes they made.
feed = collections.deque(maxlen=int(CONFIG["feed_messages"]))
feed_seq = [0]

# **Where a subscriber with no stored position starts, per channel.** This
# is the channel's business and not the client's: saguin once served MQTT 5
# from the floor and 3.1.1 from the tail, and that is exactly the rule it
# withdrew - one application behaved two ways depending on which client
# library it linked. `start: tail` is what a channel of commands writes so
# that a fleet restarted together does not act on yesterday's instructions
# a second time.
starts = {}
state = {"connected": False, "error": None, "catalogue": None, "received": 0,
         "connected_at": 0,
         # **What the broker said to each filter we asked for.** A SUBACK
         # carries a reason code per filter and a refused one is an ordinary
         # acknowledgement, so a client that ignores it is connected, silent
         # and empty - which reads as a broken viewer rather than as an ACL.
         # See on_subscribe.
         "subscriptions": []}
_sub_mids = {}       # mid -> the filter asked for, until its SUBACK arrives

app = Flask(__name__, static_folder="static", static_url_path="")
# **The page must not be cached, because the page changes.** Flask serves
# static files with a twelve-hour max-age by default, so `app.js` edited and
# the viewer restarted still gives the reader the old one - and the symptom is
# a feature that is plainly in the source and plainly not on the screen, which
# costs an hour before anybody suspects the browser. This is a personal
# instrument served from loopback: revalidating every load costs nothing worth
# measuring.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


def address_shaped(host):
    """True when a `Host` header names an address rather than a name.

    The port is stripped first, because `127.0.0.1:8080` and `[::1]:8080`
    are the forms a browser sends. `localhost` is a name and is accepted
    anyway: it is the one name nobody else's DNS can point somewhere else,
    and it is what an operator types.
    """
    host = host.strip().lower()
    if host.startswith("["):                     # [::1]:8080, and [::1]
        host = host.partition("]")[0][1:]
    else:
        host = host.partition(":")[0]
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@app.before_request
def refuse_a_request_that_did_not_ask_for_this_viewer():
    """The TCP door answers to an address and refuses a name.

    **A browser sends the name it was loaded by, and a page on another site
    cannot make it send an address instead.** That asymmetry is the whole
    defence, and it is the one a personal instrument on loopback needs. An
    attacker's domain that re-resolves to 127.0.0.1 becomes *same-origin*
    to this viewer - no preflight, none of the cross-site rules apply - and
    can then publish records, hang clients up, requeue dead letters, clear
    alarms and read everything both credentials reach, with the operator's
    own browser making every call and the operator's own credentials behind
    it. The single trace it leaves is the `Host` header carrying the
    attacker's domain, because a name is how the browser arrived.

    **A name is not wrong, it is a proxy** - and a proxy belongs in front of
    the Unix socket door, where who may open the page is a file permission
    rather than a question of which name resolved. So there is no list of
    accepted names to configure: the `listen` block already says which
    deployment this is, and a knob here would be a second answer to a
    question it has answered. A socket is exempt because no browser can
    reach one.

    The `Origin` header is the same test from the other side, and it is
    checked because today those requests are refused *by accident*: every
    verb reads its body as JSON, and cross-site JSON needs a preflight this
    viewer does not answer. An accident is not a defence - the first verb
    that takes a form-encoded body would lose it with nothing to say so.
    """
    if DOOR != "tcp":
        return None
    host = request.headers.get("Host", "")
    if host and not address_shaped(host):
        return jsonify({"error":
                        f"this viewer is served on an address and was asked for by "
                        f"the name {host!r}. A browser sends the name it was loaded "
                        f"by, so a name here is a page on another site reaching this "
                        f"viewer through your browser. Reaching it by name means a "
                        f"proxy, and a proxy goes in front of the socket door - the "
                        f"`listen.unix` block in saguin-viewer.yaml"}), 403
    origin = request.headers.get("Origin", "")
    if origin and urllib.parse.urlsplit(origin).netloc.lower() != host.lower():
        return jsonify({"error":
                        f"this request says it comes from {origin!r}, which is not "
                        f"this viewer. A page on another site does not drive an "
                        f"operator's tools through the operator's browser"}), 403
    return None


def parse_help(body):
    """What the catalogue says each metric means, by name.

    **The broker already writes this and nothing was reading it.** Every
    series in RFC 0005 carries a `# HELP` line saying what it counts and,
    more usefully, what it deliberately does not - a dashboard that repeats
    that in its own words is a second copy to keep in step, and the copy
    that goes stale is the one nobody scrapes. Read it off the wire instead,
    and a metric whose meaning is sharpened upstream sharpens here with it.

    The description is the rest of the line, whatever is in it: these
    sentences carry braces, quotes and dashes, and a parser that stopped
    at any of them would show half of one.
    """
    out = {}
    for line in body.splitlines():
        if not line.startswith("# HELP "):
            continue
        rest = line[len("# HELP "):].strip()
        name, _, text = rest.partition(" ")
        if name and text:
            out[name] = text.strip()
    return out


def parse_metrics(body):
    """The exposition format, parsed with the quoting respected.

    **The obvious parser is wrong here, and this page shipped it.** It found
    the label block with `line.index("}")` and split it on commas - and a
    channel's `filter` label carries the filter *as written*, braces and
    all, so `filter="iot/+/{status,location}/+"` ends the block at the wrong
    brace and splits the filter down the middle. The filters are the one
    thing this viewer cannot afford to misread: every topic-to-channel
    decision is made against them.

    A line this does not understand is skipped rather than failing the
    scrape. This reads a catalogue saguin promises never to break, so an
    unfamiliar line is far more likely to be a metric added later than a
    broker gone wrong, and refusing the whole scrape over one would turn a
    new metric into a blank page.
    """
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # **A sample with no labels has no brace**, and the value then
        # follows the name directly: `saguin_connections 1`. Reading from
        # the start of the line there takes the *name* as the value, fails
        # to parse it, and drops the sample - which is how every unlabelled
        # gauge in the catalogue went missing while every labelled one
        # arrived, so the page showed channels and no connection count.
        name, labels, i = line, {}, len(line)
        brace = line.find("{")
        if brace < 0:
            head = line.split(None, 1)
            name, i = head[0], len(head[0])
        elif brace >= 0:
            name, i = line[:brace], brace + 1
            while i < len(line):
                while i < len(line) and line[i] in " ,":
                    i += 1
                if i < len(line) and line[i] == "}":
                    i += 1
                    break
                eq = line.find("=", i)
                if eq < 0 or eq + 1 >= len(line) or line[eq + 1] != '"':
                    break
                key, i = line[i:eq].strip(), eq + 2
                val = []
                while i < len(line) and line[i] != '"':
                    if line[i] == "\\" and i + 1 < len(line):
                        val.append("\n" if line[i + 1] == "n" else line[i + 1])
                        i += 2
                        continue
                    val.append(line[i]); i += 1
                i += 1
                labels[key] = "".join(val)
        rest = line[i:].split()
        if not rest:
            continue
        try:
            out.append((name.strip(), labels, float(rest[0])))
        except ValueError:
            continue
    return out


def ops_get(path, timeout=10):
    """One GET against the operations listener, with the credential and TLS.

    **Both doors go through here, and that is the point.** The catalogue was
    fetched with the TLS context and the `/v1` routes were fetched without
    it, from a second copy of this code - so against a broker with a
    certificate from a private authority the dashboard worked and every
    operations panel failed to verify. Two places building one request is
    one of them going quietly wrong.
    """
    req = urllib.request.Request(OPS_BASE + path)
    if OPS_USER:
        token = b64encode(f"{OPS_USER}:{OPS_PASSWORD}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    ctx = ssl_context(OPS_CFG["tls"]) if OPS_CFG["tls"]["enabled"] else None
    return urllib.request.urlopen(req, timeout=timeout, context=ctx).read()


def metrics_samples(with_help=False):
    """One scrape, parsed. Raises so the caller can report why.

    The descriptions come off the same read rather than a second scrape:
    the broker recomputes its catalogue at most once a minute, so asking
    twice would be asking for the same answer and paying for it.
    """
    body = ops_get("/metrics").decode()
    samples = parse_metrics(body)
    return (samples, parse_help(body)) if with_help else samples


def one(samples, name, labels=None):
    """The first value of a name whose labels all match."""
    for n, lb, v in samples:
        if n == name and all(lb.get(k) == x for k, x in (labels or {}).items()):
            return v
    return None


def rows(samples, name):
    """Every sample of one name, as (labels, value)."""
    return [(lb, v) for n, lb, v in samples if n == name]


def catalogue(samples=None):
    """Which channels this broker has, of what type, and holding what - from
    the broker itself.

    `saguin_channel_info{channel,filter,type,provider}` is RFC 0005's own
    answer to "what channels are there", so the viewer needs no list that
    goes stale the moment somebody edits saguin.yaml. A dead-letter channel
    a queue derives shows up here too, which is the point: nothing
    configured it, so nothing could have listed it - and nothing else could
    have told you its topics either, since the broker derives those as well.

    **The `filter` label is why this still works.** A channel used to claim
    the topics under its own name, so a reader that knew the name knew where
    to subscribe. It now claims whatever its filter matches, and a name says
    nothing about that - so the broker publishes the filter here and the
    viewer subscribes to what it is told rather than to what it can guess.
    """
    if samples is None:
        samples = metrics_samples()
    found, held = {}, {}
    for lb, _ in rows(samples, "saguin_channel_info"):
        if lb.get("channel") and lb.get("type"):
            found[lb["channel"]] = (lb["type"], lb.get("filter", ""))
    # **What the channel holds, taken from the broker rather than counted
    # here.** The page used to print its own tally beside a channel -
    # arrivals since it connected - as a bare number, next to a Grafana
    # panel printing `saguin_channel_records` as a bare number. They answer
    # different questions and neither said which, so the two windows
    # disagreed by whatever the page had missed: 8 against 23,879 on a
    # dead-letter channel, and a reader comparing them has no way to tell
    # that is not a fault.
    for lb, v in rows(samples, "saguin_channel_records"):
        if lb.get("channel"):
            held[lb["channel"]] = int(v)
    # **A queue has no record count and that is deliberate.** RFC 0005:
    # resolution removes from the middle rather than the front, so
    # `next - floor` says nothing there and the store keeps the depth
    # itself. Unresolved work is the number that means something.
    for lb, v in rows(samples, "saguin_queue_depth"):
        if lb.get("channel"):
            held[lb["channel"]] = int(v)
    return found, held


# Formats whose payload is bytes, whatever those bytes happen to look
# like. A schema is what turns them into something a person reads.
BINARY_CONTENT_TYPES = ("application/x-protobuf", "application/protobuf",
                        "application/vnd.google.protobuf", "application/octet-stream",
                        "application/avro", "application/x-avro")


def render_payload(payload, content_type="", kind=""):
    """The payload as something a person can read, whatever it turns out to be.

    **The bytes are shown in every case.** Reporting only that something
    could not be decoded describes the viewer rather than the record, and
    the record is what somebody opened this page to see.

    It knows nothing about anybody's schema, deliberately. A viewer that
    deserialized one deployment's protobuf would be a viewer for that deployment;
    what saguin promises about a payload is that it is bytes the publisher
    chose, so what is shown is those bytes read as text where they are text,
    as JSON where they parse, and as hex where they are neither.
    """
    n = len(payload)
    # **A declared format beats a lucky parse.** A protobuf payload whose
    # bytes happen to be printable reads as UTF-8 without error and was
    # shown as text - so a record its own headers describe as protobuf was
    # rendered as gibberish and never offered to the schema that describes
    # it. What the publisher said it sent wins.
    binary_type = (content_type or "").lower().split(";")[0] in BINARY_CONTENT_TYPES
    if n and binary_type:
        return {"kind": "bytes", "bytes": n,
                "why": f"content type {content_type}",
                "hex": binascii.hexlify(payload).decode()}
    if n == 0:
        # **What zero length means depends on where it lands**, and the page
        # was saying "a delete on a latest channel" over every one of them -
        # including a broadcast topic, where it is the retained value being
        # cleared and there is no latest channel in sight. A note that names
        # the wrong mechanism is worse than none: the reader goes looking for
        # a channel that is not there.
        why = {
            "latest": "zero-length - on a `latest` channel this is a delete",
            "broadcast": "zero-length - published with RETAIN, this is how MQTT "
                         "clears a retained value",
        }.get(kind, "zero-length - an empty payload")
        return {"kind": "empty", "bytes": 0, "why": why}
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {"kind": "bytes", "bytes": n,
                "why": f"not valid UTF-8{f'; content type {content_type}' if content_type else ''}",
                "hex": binascii.hexlify(payload).decode()}
    if text.strip()[:1] in ("{", "["):
        try:
            return {"kind": "json", "bytes": n, "value": json.loads(text),
                    "text": text}
        except ValueError:
            # Shown as text rather than as a failure: it is a record that
            # arrived perfectly and merely is not JSON.
            pass
    return {"kind": "text", "bytes": n, "text": text,
            "why": f"content type {content_type}" if content_type else ""}


def user_properties(msg):
    pairs = getattr(getattr(msg, "properties", None), "UserProperty", None) or []
    return {k: v for k, v in pairs}


def message_expires_at(msg, at, props=None):
    """When the publisher's Message Expiry Interval runs out, or None.

    **`saguin-expires` first, because it is the only one that is always
    right.** A channel delivery carries it whenever the publisher set an
    expiry: an absolute moment in Unix milliseconds, there whether or not the
    deadline has passed. MQTT's own field cannot answer the passed case at
    all - the specification deletes an expired message rather than delivering
    one, so a countdown that reached zero is simply absent - and on a channel
    saguin still serves the record.

    **MQTT's field second**, for the paths that carry no saguin properties: a
    broadcast topic and the retained store, which deliberately stamp none of
    them. There the interval is the seconds remaining as of that delivery, so
    the moment is the receipt time plus it, and an expired value is deleted
    rather than served, so the ambiguous case does not arise.

    None where neither says anything, which is a publisher that set no
    expiry. That is not the same as zero, and rendering it as "expires now"
    would invent a deadline.
    """
    stamped = (props or {}).get("saguin-expires")
    if stamped:
        try:
            return int(stamped) / 1000
        except (TypeError, ValueError):
            # A value this page cannot read is not a deadline it may guess
            # at. The broker writes these, so this is a broker that has
            # changed shape rather than a client's mistake, and inventing a
            # moment would hide it.
            return None
    left = getattr(getattr(msg, "properties", None), "MessageExpiryInterval", None)
    if not left:
        return None
    return at + int(left)


def expand(filt):
    """The plain filters a written one stands for, resolving `{a,b}` levels.

    **The catalogue carries the filter as written, braces and all** - one
    series per channel, which is what makes `saguin_channel_info` a channel
    list. A brace is configuration syntax and not MQTT, so a filter carrying
    one cannot go on the wire: subscribing with `iot/+/{status,location}/+`
    is granted, treated as a literal level, and matches nothing for ever.
    That is the silent kind, so it is expanded here instead of sent.

    `saguin --route <config> <topic>` prints the expansions the broker made,
    and is the answer to settle an argument with; this mirrors the rule so a
    page can subscribe at all.
    """
    out = [""]
    for i, level in enumerate(filt.split("/")):
        alts = [level]
        if level.startswith("{") and level.endswith("}"):
            alts = [a for a in level[1:-1].split(",") if a]
        out = [a if i == 0 else p + "/" + a for p in out for a in alts]
    return out


def exactness(filt, i):
    """How exactly a filter spells out level `i`: the broker's own ordering.

    A spelled-out level beats `+`, `+` beats `#`, and a filter that has
    ended beats one still carrying `#` - because ending pins the topic's
    length. RFC 0002 "Which channel a topic belongs to" is where this is
    specified.
    """
    levels = filt.split("/")
    if i >= len(levels):
        return 4          # the filter ended here
    return {"#": 1, "+": 2}.get(levels[i], 3)


def channel_of(topic, chans=None):
    """The channel a topic belongs to, or None for broadcast.

    `chans` is a snapshot of the channel table to read; without one this takes
    `lock` and makes its own. **It iterated the live table, and the poll thread
    replaces that table wholesale - `clear()` then `update()` - under the same
    lock every scrape.** A replacement landing mid-iteration raises RuntimeError
    in CPython, which in `on_message` leaves paho's loop and costs a spurious
    reconnect, and on `/api/route` is a 500. The window is one dict-replace a
    minute against message arrival, so it is narrow rather than absent.

    Filters overlap on purpose, so more than one may match and the broker
    gives the topic to whichever spells it out most exactly. This mirrors
    that rule so the page can group a record under the channel that actually
    holds it.

    **It is a mirror and the broker is authoritative.** `saguin --route
    <config> <topic>` calls the code that decides, and is the answer to
    settle an argument with; this is a display grouping in a demo page. It
    is here rather than derived from the delivery because a channel's
    records are written by saguin itself and carry no Subscription
    Identifier to attribute them by (RFC 0001).
    """
    if chans is None:
        with lock:
            chans = dict(channels)
    best, best_name = None, None
    for name, (_kind, written) in chans.items():
        for filt in expand(written or ""):
            if not filt or not topic_matches_sub(filt, topic):
                continue
            score = [exactness(filt, i) for i in range(max(
                len(filt.split("/")), len(topic.split("/"))) + 1)]
            if best is None or score > best:
                best, best_name = score, name
    return best_name


def ssl_context(cfg):
    ctx = ssl.create_default_context(cafile=cfg["ca_file"] or None)
    if cfg["cert_file"]:
        ctx.load_cert_chain(cfg["cert_file"], cfg["key_file"] or None)
    if cfg["insecure_skip_verify"]:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def scrape_interval():
    """The broker's own recompute interval, and never faster.

    **`min_scrape_interval` is a floor the broker enforces by answering the
    previous catalogue**, so a viewer polling faster gets the same bytes
    back and draws a flat line that then jumps - a staircase that is the
    scraper's, not the broker's. Worse, it reads exactly like success. So
    the interval is read from the configuration the broker resolved rather
    than chosen here.

    A credential narrowed to /metrics cannot read that route. A minute is
    the documented floor and the documented default, so it is the fallback,
    and the page says which of the two it is using.
    """
    doc = ops_json("/v1/operations/config?section=operations")
    if doc.get("error"):
        return 60, ("this credential cannot read /v1/operations/config, so the "
                    "interval below is the documented default of a minute "
                    "rather than what this broker was configured with")
    raw = ((doc.get("operations") or {}).get("min_scrape_interval"))
    floor = 60
    try:
        if isinstance(raw, str) and raw.endswith("s"):
            floor = max(int(float(raw[:-1])), 60)
        elif isinstance(raw, (int, float)):
            floor = max(int(raw), 60)
    except ValueError:
        pass
    asked = int(CONFIG["scrape_interval"] or 0)
    if asked and asked < floor:
        return floor, (f"scrape_interval is set to {asked}s and this broker "
                       f"recomputes every {floor}s - a faster scrape is answered "
                       f"from the previous catalogue, so {floor}s is what is used")
    if asked:
        return asked, None
    if raw is None:
        return floor, None
    # The key is simply unset, which is the ordinary case: a minute is both
    # the floor and the default, so this is the broker's real interval and
    # not a guess. Saying "could not be read" here would send an operator to
    # check a credential that is working.
    return 60, None


def poll_metrics():
    """Scrape the catalogue on the broker's interval, and keep what changes.

    This also refreshes the channel list, which used to be read once per
    connection - so a channel added to a running broker, or a queue's
    holdings changing, only reached the page when the link dropped.
    """
    interval, note = 60, None
    last_interval_check = 0.0
    while True:
        if time.time() - last_interval_check > 300:
            interval, note = scrape_interval()
            doc = ops_json("/v1/operations/config")
            if not doc.get("error"):
                with lock:
                    starts.clear()
                    for name, c in (doc.get("channels") or {}).items():
                        if isinstance(c, dict) and c.get("start"):
                            starts[name] = c["start"]
            last_interval_check = time.time()
        try:
            samples, help_text = metrics_samples(with_help=True)
            found, held = catalogue(samples)
            with lock:
                channels.clear(); channels.update(found)
                holds.clear(); holds.update(held)
                state["catalogue"] = time.time()
                metrics_state.update(interval=interval, taken=time.time(),
                                     error=None, note=note)
                metrics_history.append(snapshot(samples))
                trim_history_by_age()   # hold the ring to five days by age, not just maxlen
                latest_samples[:] = samples
                metrics_help.clear()
                metrics_help.update(help_text)
            # Outside the lock: save_history takes it, and threading.Lock is
            # not reentrant. A no-op unless history_file is configured.
            save_history()
            # Alarms are recorded from the same scrape, so the record and the
            # page's live red card share one evaluation. evaluate_alarms takes
            # the lock itself and returns rows only on an edge; save is outside.
            save_alarms(evaluate_alarms(samples))
        except Exception as e:                 # noqa: BLE001 - reported, not hidden
            with lock:
                metrics_state["error"] = str(e)
        time.sleep(interval)


def snapshot(samples):
    """One sample, reduced to what a chart on this page reads.

    **Reduced rather than whole.** Keeping every series would put a fleet's
    worth of rows in memory a hundred and twenty times over, and invariant
    13's rule about bounded accumulation is not only the broker's problem.
    """
    tot = lambda name: sum(v for n, _lb, v in samples if n == name)
    return {
        "t": time.time(),
        "connections": one(samples, "saguin_connections"),
        "subscriptions": one(samples, "saguin_subscriptions"),
        "published": tot("saguin_published_total"),
        "refused": tot("saguin_publish_refused_total"),
        "queue_depth": tot("saguin_queue_depth"),
        "queue_inflight": tot("saguin_queue_inflight"),
        "provider_bytes": tot("saguin_provider_bytes"),
        "unmatched": one(samples, "saguin_broadcast_unmatched_total"),
        "dropped": one(samples, "saguin_deliveries_dropped_total"),
        # **Cumulative, and the page turns them into rates.** They are the
        # only numbers here a throughput can be drawn from: everything else
        # counts records the broker decided to keep. A counter is stored
        # rather than a rate because a rate needs two readings and this is
        # one - and because two readings taken a minute apart are what the
        # page has, whatever it asks for in between.
        "bytes_in": one(samples, "saguin_bytes_received_total"),
        "bytes_out": one(samples, "saguin_bytes_sent_total"),
        "publishes_in": one(samples, "saguin_publishes_received_total"),
        "deliveries_out": one(samples, "saguin_deliveries_sent_total"),
        "deliveries_refused": one(samples, "saguin_deliveries_refused_total"),
        "sessions_offline": one(samples, "saguin_sessions_offline"),
        "sessions_restored": one(samples, "saguin_sessions_restored_total"),
        "wills_waiting": one(samples, "saguin_wills_waiting"),
        "wills_cancelled": one(samples, "saguin_wills_cancelled_total"),
        "session_queue_messages": one(samples, "saguin_session_queue_messages"),
        "session_queue_bytes": one(samples, "saguin_session_queue_bytes"),
        "retained": one(samples, "saguin_retained_messages"),
        # The Go runtime: four cumulative counters the page turns into rates,
        # and the stacks as a level.
        "go_gc_cycles": one(samples, "saguin_go_gc_cycles_total"),
        "go_gc_cpu": one(samples, "saguin_go_gc_cpu_seconds_total"),
        "go_gc_assist_cpu": one(samples, "saguin_go_gc_assist_cpu_seconds_total"),
        "go_alloc_bytes": one(samples, "saguin_go_allocated_bytes_total"),
        "go_alloc_objects": one(samples, "saguin_go_allocated_objects_total"),
        "go_stack": one(samples, "saguin_go_stack_bytes"),
        # Exactly-once. **Absent rather than zero on a broker that does not
        # offer it**, which `one` already answers as None: saguin carries no
        # `saguin_qos2_*` series at all without a `broker.qos2` block, and a
        # zero here would draw a card saying the feature was on and idle.
        "qos2_held": one(samples, "saguin_qos2_held"),
        "qos2_abandoned": one(samples, "saguin_qos2_abandoned_total"),
        "qos2_max_inflight": one(samples, "saguin_qos2_max_inflight_per_client"),
    }


def on_connect(client, _userdata, _flags, reason_code, _properties=None):
    ok = reason_code == 0
    with lock:
        state["connected"] = ok
        state["error"] = None if ok else str(reason_code)
    if not ok:
        return
    with lock:
        state["connected_at"] = time.time()
        # Rebuilt from what this connection replays, so nothing deleted while
        # the viewer was away is left showing as retained.
        retained.clear()
    with lock:
        state["subscriptions"] = []
        _sub_mids.clear()
    # The reply topic is tracked like any other: a credential that cannot
    # subscribe to it leaves every seek and point-read waiting for an answer
    # that can never arrive, which is the same silence for a different reason.
    _res, _mid = client.subscribe(REPLY_TOPIC, qos=1)
    with lock:
        _sub_mids[_mid] = REPLY_TOPIC
    # **One filter, and `#` by default.** The page used to subscribe to each
    # channel's filter in turn, which showed every channel and no broadcast
    # at all - a topic no channel claims was invisible, and that is exactly
    # the topic somebody is looking for when a publish is going nowhere.
    #
    # Subscribing to everything is safe rather than lucky, and the broker is
    # what makes it so. Measured against a running saguin: `#` is granted at
    # QoS 1 and serves the append and latest channels, replays the append
    # ones from their retention floor, serves topics no channel claims -
    # and does not serve a queue's records. A filter merely crossing a queue
    # is an ordinary subscriber and the queue's work is left out, so this
    # viewer cannot take a job even by asking for everything. Nothing under
    # `$SYS/` or `$saguin/` reaches a wildcard either.
    #
    # **That measurement was taken against a broker with no acl_file**, and it
    # does not hold under one. A credential scoped to a channel - the shape the
    # README's own ACL guidance invites, and the shape the dead-letter feature
    # is built for - is answered `Not authorized` for `#`, in the SUBACK, which
    # is an ordinary acknowledgement rather than an error. The page then shows
    # a connected viewer holding nothing, and the honest reading of that is
    # impossible from the page. So every filter's answer is kept.
    for filt in expand(MQTT_CFG["subscribe"]):
        _res, mid = client.subscribe(filt, qos=1)
        with lock:
            _sub_mids[mid] = filt


def on_subscribe(_client, _userdata, mid, reason_codes, _properties=None):
    """Record what the broker granted, per filter.

    **A refused subscription is a success-shaped failure**, which is the worst
    class of failure here: the SUBACK arrives, the connection is fine, and
    the only symptom is that nothing ever appears. Under an acl_file that is
    the normal answer to `#`, so this is the difference between "the viewer is
    broken" and "this credential may not read that filter"."""
    codes = reason_codes if isinstance(reason_codes, (list, tuple)) else [reason_codes]
    refusals = []
    with lock:
        filt = _sub_mids.pop(mid, None)
        for rc in codes:
            fail = getattr(rc, "is_failure", None)
            refused = bool(fail) if fail is not None else getattr(rc, "value", 0) >= 0x80
            state["subscriptions"].append(
                {"filter": filt, "granted": not refused, "reason": str(rc)})
            if refused:
                refusals.append((filt, str(rc)))
    # **Recorded is not reported, and for a while this only recorded.** The state
    # above has carried `granted: false` since it was written, and nothing said it
    # out loud - so an operator whose acl_file refused a filter saw a page that
    # looked well and features that did nothing. Printed outside the lock.
    for filt, reason in refusals:
        print(f"viewer: the broker refused the subscription to {filt!r}: {reason}",
              flush=True)
        if filt == REPLY_TOPIC:
            # **The one refusal that costs more than it looks.** Every MQTT 5
            # request this page makes names this topic as its Response Topic -
            # point reads of a `latest` channel, a channel seek, the sessions
            # verbs - so refusing it loses every answer while each request is
            # still accepted. This was found on a demo whose ACL
            # granted the data filters and not the reply prefix: thirty-one smoke
            # checks passed over it.
            print(f"viewer: that is this page's reply topic, so point reads, "
                  f"seeks and the sessions verbs will accept your request and "
                  f"never show an answer. An acl_file has to grant this client "
                  f"read on {REPLY_PREFIX!r} - see the viewer's README.",
                  flush=True)


def on_disconnect(_client, _userdata, _flags, reason_code, _properties=None):
    with lock:
        state["connected"] = False
        state["error"] = str(reason_code) if reason_code else None


def _is_replay(entry):
    ts = (entry.get("properties") or {}).get("saguin-timestamp")
    try:
        return float(ts) / 1000.0 < state.get("connected_at", 0)
    except (TypeError, ValueError):
        # Broadcast carries no such property, and broadcast is never
        # replayed - it is stored nowhere.
        return False


def on_message(_client, _userdata, msg):
    if msg.topic == REPLY_TOPIC:
        ticket = getattr(getattr(msg, "properties", None), "CorrelationData", b"") or b""
        with lock:
            seeks[ticket.decode(errors="replace")] = msg.payload.decode(errors="replace")
        return

    up = user_properties(msg)
    ctype = getattr(getattr(msg, "properties", None), "ContentType", "") or ""
    at = time.time()
    entry = {
        "at": at,
        "retained": bool(msg.retain),
        "qos": msg.qos,
        "offset": up.get("saguin-offset"),
        "properties": up,
        # **The publisher's Message Expiry, as the moment it runs out rather
        # than as the seconds left.**
        #
        # The broker decrements the interval by the time the record has
        # waited, as MQTT requires, so what arrives is a countdown from *this
        # delivery*. A page that showed the number would show a value that
        # was true when it was received and is wrong by however long the
        # window has been open - "60 seconds left" on a row from ten minutes
        # ago. Added to the receipt time it becomes a fixed moment, which is
        # the same fact and stays true.
        #
        # None where the publisher set none, which is not the same as zero:
        # absent means the value does not expire on the publisher's clock at
        # all, and a page rendering that as "expires now" would be inventing
        # a deadline.
        "expires_at": message_expires_at(msg, at, up),
        # **Kept beside the payload, because it is half of what
        # deserializing needs.** The Content Type says the format and the `schema`
        # property says where the schema is; a page holding one of the two
        # can render bytes and nothing else.
        "content_type": ctype,
    }
    # One snapshot for both reads, so the channel a record is filed under and
    # the kind that sizes its ring come from the same scrape rather than from
    # two, either side of a replacement.
    #
    # **Resolved before the payload is rendered**, because what a zero-length
    # payload means is a property of the channel it landed on.
    with lock:
        chans = dict(channels)
    ch = channel_of(msg.topic, chans)
    kind = chans.get(ch, ("broadcast", ""))[0]
    entry["payload"] = render_payload(
        msg.payload,
        getattr(getattr(msg, "properties", None), "ContentType", "") or "", kind)
    # **A deletion removes the topic rather than adding an empty row to it.**
    #
    # On a `latest` channel a zero-length payload *is* the delete - RFC 0002 -
    # so a value that has been deleted must stop appearing, or the tree shows
    # a key the broker no longer holds. On a broadcast topic the same publish
    # carrying RETAIN deletes the retained value, which is how MQTT deletes
    # one; the page was leaving the topic in the tree with an empty payload
    # under it, so clearing a retained message from the panel beside it looked
    # like it had done nothing.
    #
    # An empty payload that is neither of those - a broadcast publish with no
    # retain flag - is left alone: it is somebody's data, not a deletion, and
    # dropping the topic would hide traffic that really arrived.
    #
    # **The retain flag cannot be the test.** A delivery to a client that is
    # already subscribed carries `retain = 0` whatever was stored - MQTT sets
    # it only when replaying a stored value to a new subscription - so the
    # delete that this page is watching for arrives looking like an ordinary
    # publish. What identifies it is that the page is holding a retained value
    # for that topic and has just been sent an empty one.
    # **On a `latest` channel the topic goes; on broadcast only the value
    # does.** The distinction is what each view is showing. A latest channel's
    # topic *is* its value, so a deleted key must stop appearing or the tree
    # offers one the broker no longer holds. A broadcast topic's tree entry is
    # the messages that arrived on it, and a retained delete does not unsend
    # those - erasing them threw away history the reader had been watching,
    # which is what deleting one retained value looked like: the topic and its
    # earlier messages vanished together.
    if not msg.payload and kind == "latest":
        with lock:
            topics.pop(msg.topic, None)
        return
    if not msg.payload and kind == "broadcast" and (msg.retain or msg.topic in retained):
        # **The clear is not shown as a message, and the history is kept.**
        #
        # It did arrive, so listing it is defensible - and it reads as though
        # one of the messages below had been deleted, which is the one thing
        # that cannot have happened. A broadcast message is delivered to
        # whoever is subscribed and stored nowhere; the retained value is a
        # separate slot for the topic. Clearing it deletes no message, so a
        # zero-length row sitting among a thousand of them invites the reader
        # to work out which one it removed. None of them.
        #
        # What the reader actually wants to know - is there a retained value
        # now - is answered beside the heading, from the store.
        with lock:
            retained.pop(msg.topic, None)
        return

    with lock:
        t = topics.get(msg.topic)
        if t is None:
            # **One slot for a latest channel, a history for an append one.**
            # A latest channel holds the current value of a topic and nothing
            # else; keeping a log of arrivals there shows the viewer's memory
            # rather than the channel, and an older value sitting under a
            # newer one reads as the broker serving two.
            depth = 1 if kind == "latest" else PER_TOPIC
            t = topics[msg.topic] = {"count": 0, "last": 0.0, "channel": ch,
                                     "kind": kind,
                                     "messages": collections.deque(maxlen=depth)}
        t["count"] += 1
        t["last"] = entry["at"]
        t["messages"].appendleft(entry)
        state["received"] += 1
        feed_seq[0] += 1
        feed.append({**entry, "seq": feed_seq[0], "topic": msg.topic,
                     "channel": ch, "kind": kind,
                     # **Replayed or live, told apart by the broker's own
                     # receipt clock.** An append channel is replayed from
                     # its retention floor the moment this viewer subscribes,
                     # so the first thing a feed shows is history arriving at
                     # speed - which reads as a flood of live traffic unless
                     # it is labelled. A record whose broker timestamp
                     # precedes this connection is one that already existed.
                     "replayed": _is_replay(entry)})
        # The retain flag marks the value the broker keeps for this topic. A
        # zero-length retained publish is how MQTT clears one, so an empty one
        # removes rather than records.
        if msg.retain:
            if msg.payload:
                retained[msg.topic] = {"topic": msg.topic, "at": entry["at"],
                                       "payload": entry["payload"], "qos": entry["qos"]}
            else:
                retained.pop(msg.topic, None)


# **One library, both protocol versions.** paho takes the version as a
# parameter; a 3.1.1 session is asked for with clean_session at construction
# and an MQTT 5 one with clean_start at connect, so the two differ here and
# nowhere else.
if PROTOCOL == mqtt.MQTTv5:
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=CLIENT_ID,
                         protocol=PROTOCOL)
else:
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=CLIENT_ID,
                         protocol=PROTOCOL, clean_session=False)

if MQTT_CFG["username"]:
    client.username_pw_set(MQTT_CFG["username"], MQTT_CFG["password"])

_tls = MQTT_CFG["tls"]
if _tls["enabled"]:
    client.tls_set(ca_certs=_tls["ca_file"] or None,
                   certfile=_tls["cert_file"] or None,
                   keyfile=_tls["key_file"] or None,
                   cert_reqs=ssl.CERT_NONE if _tls["insecure_skip_verify"]
                   else ssl.CERT_REQUIRED)
    if _tls["insecure_skip_verify"]:
        # Asked for by name in the configuration, so it is honoured and said
        # out loud rather than done quietly.
        client.tls_insecure_set(True)
        print("viewer: TLS certificate verification is off, by configuration",
              flush=True)
def on_publish(_client, _userdata, mid, reason_code=None, _props=None):
    """What the broker made of a publish on this connection.

    **A PUBACK that arrives is not a PUBACK that agreed**, and until this
    existed the page could not tell the two apart on its own connection: a
    refused control publish looked exactly like one whose reply had not come
    back yet, and was reported after five seconds as a timeout. "The broker
    is slow" and "your credential may not do that" are not the same sentence,
    and the second is the one somebody can act on.

    `publish_once` has carried this for a publish on a connection of its own
    since the beginning; this is the same rule on the connection the page
    keeps, which is the one every control verb goes out on.
    """
    if reason_code is None:
        return
    with lock:
        pubacks[mid] = reason_code
        # The page sends a handful of control publishes and reads each answer
        # within seconds. Bounded anyway, because a dict nothing empties on a
        # process that runs for weeks is a leak waiting for a busy operator.
        if len(pubacks) > 256:
            for stale in list(pubacks)[:128]:
                del pubacks[stale]


client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_publish = on_publish
client.on_subscribe = on_subscribe
client.on_message = on_message


def run():
    """Learn the channels, then connect, and keep trying at both."""
    props = None
    if PROTOCOL == mqtt.MQTTv5:
        props = Properties(PacketTypes.CONNECT)
        # A session that outlives the connection, which is what gives this
        # client a stored position and therefore something a seek can move.
        # Short rather than long: a viewer holding a position is a consumer
        # like any other, and one parked behind the head drags
        # saguin_channel_consumer_position_min down - the input to the one
        # alert saguin's whole catalogue exists for.
        props.SessionExpiryInterval = int(MQTT_CFG["session_expiry"])
    while True:
        try:
            found, held = catalogue()
            with lock:
                channels.clear()
                channels.update(found)
                holds.clear()
                holds.update(held)
                state["catalogue"] = time.time()
        except Exception as e:                 # noqa: BLE001 - reported, not hidden
            with lock:
                state["error"] = f"cannot read the channel catalogue: {e}"
            print(f"viewer: catalogue: {e!r}", flush=True)
            time.sleep(3)
            continue
        try:
            if PROTOCOL == mqtt.MQTTv5:
                # **Clean start, and it is the whole difference between a
                # viewer that is useful when opened and one that is empty.**
                #
                # An MQTT 5 subscriber with no stored position is served an
                # append channel from its retention floor: the whole replay.
                # One that resumes a session is served only what arrived
                # since - so a viewer keeping its session across restarts
                # got the history exactly once, ever, and every launch after
                # that showed a tree with the append channels missing.
                # Measured: restart with a resumed session and three
                # published events do not come back.
                #
                # A seek still works, because clean_start discards the old
                # position and the session created here still has one to
                # move. What is given up is resuming where the last look
                # ended, which is not a thing anybody wants from a viewer.
                client.connect(HOST, PORT, keepalive=MQTT_CFG["keepalive"],
                               clean_start=True, properties=props)
            else:
                # 3.1.1 has no clean_start and no CONNECT properties; the
                # session was asked for at construction instead. **And it is
                # served no replay**: an append subscription starts at the
                # tail, so a tree that looks empty here is this protocol
                # version rather than a broker that lost anything. The page
                # says which version it connected as, for exactly that
                # reason.
                client.connect(HOST, PORT, keepalive=MQTT_CFG["keepalive"])
            client.loop_forever(retry_first_connection=True)
        except Exception as e:                 # noqa: BLE001 - reported, not hidden
            with lock:
                state["connected"] = False
                state["error"] = f"{type(e).__name__}: {e}"
            print(f"viewer: {e!r}", flush=True)
            time.sleep(2)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def api_state():
    with lock:
        return jsonify({
            **state,
            "client_id": CLIENT_ID,
            "protocol": MQTT_CFG["protocol"],
            "session_expiry": MQTT_CFG["session_expiry"],
            # **How many arrivals a topic keeps, which is one setting for the
            # whole page** rather than a property of whichever topic is open:
            # the rings are resized together. So it belongs in the state the
            # header reads, not in one topic's answer.
            "messages_per_topic": PER_TOPIC,
            "keep_choices": list(KEEP_CHOICES),
            # **What the sidebar counts**, computed here because the page
            # would otherwise ask two routes on a timer to badge two rows.
            # Both are what this viewer is holding rather than what the broker
            # holds - the same bound every list on this page carries - which
            # is why the badge is a count of rows you can actually open.
            "counts": counts(),
            # What the broker granted, per filter asked for. A refused one is
            # why the page can be connected and empty.
            "subscriptions": list(state["subscriptions"]),
            "subscribe": MQTT_CFG["subscribe"],
            "topics": len(topics),
            "channels": [
                {"name": n, "type": k,
                 "read": k in ("append", "latest"),
                 # `tail` where the channel says so, and absent where it
                 # starts from the retention floor, which is the default.
                 "start": starts.get(n),
                 "seekable": k == "append",
                 # The broker's own count, so this page and the dashboard
                 # are quoting one number rather than two.
                 "held": holds.get(n),
                 # Where the channel's records are, which a name no longer
                 # says. The page shows it beside the name so a reader can
                 # see why a topic landed where it did.
                 "filter": f}
                for n, (k, f) in sorted(channels.items())
            ],
        })


@app.get("/api/tree")
def api_tree():
    with lock:
        return jsonify([
            # **`retained` is not reported for a `latest` topic**, and the
            # count is not what a reader should be shown there either.
            #
            # Both describe the arrival rather than the value. A current
            # value reaches a subscriber flagged retained when it was
            # already stored at subscribe, and unflagged when it was
            # published a moment later - the same value either way, and
            # which one you get depends only on when the page connected. As
            # a per-topic badge that made the tree look inconsistent about
            # data that is not: half the stations showed "retained" and half
            # did not, and the count column read as a message count on a
            # channel that holds exactly one message per topic.
            #
            # What is worth showing there is the offset: it says when the
            # value last changed, which is the only thing that varies.
            {"topic": name, "count": t["count"], "last": t["last"],
             "channel": t["channel"], "kind": t["kind"],
             "offset": (t["messages"][0]["offset"] if t["messages"] else None),
             "retained": (t["kind"] != "latest"
                          and bool(t["messages"] and t["messages"][0]["retained"]))}
            for name, t in sorted(topics.items())
        ])


@app.get("/api/metrics")
def api_metrics():
    """Everything the dashboard draws, from one scrape.

    **It is deliberately not a live view.** saguin recomputes its catalogue
    at most once a minute and answers a faster scrape from the previous one,
    so this endpoint reports the last sample the poller took and says when -
    a page refreshing every second over a number that changes every minute
    would be drawing its own polling, not the broker.
    """
    with lock:
        samples = list(latest_samples)
        hist = list(metrics_history)
        st = dict(metrics_state)
        chans = dict(channels)
        described = dict(metrics_help)

    def lbl(name, key="channel"):
        """Every series of `name`, added up per label value.

        **Added up, because a metric may carry a second label.** This kept
        one row per key and let the last win, which is the same number for a
        metric with one series per channel and silently wrong for one that
        splits further: `saguin_storage_commits_total` is four rows per
        provider, one per `closed_by` (records, interval, commit,
        unbatched), so a broker that had committed sixty
        thousand times reported zero - and the reading built on it, records
        per commit, was hidden rather than wrong, which is worse.
        """
        out = {}
        for lb, v in rows(samples, name):
            k = lb.get(key)
            if k is not None:
                out[k] = out.get(k, 0.0) + v
        return out

    records, byts = lbl("saguin_channel_bytes"), None
    byts = records
    records = lbl("saguin_channel_records")
    nxt, floor = lbl("saguin_channel_next_offset"), lbl("saguin_channel_floor_offset")
    pos, cons = lbl("saguin_channel_consumer_position_min"), lbl("saguin_channel_consumers")
    sliced = lbl("saguin_channel_partitioned_consumers")
    depth = lbl("saguin_queue_depth")

    # Which provider holds each channel, and which kind of store that
    # provider is. Both are already on the wire and neither was being read:
    # the channel's own series carries its provider's name, and the
    # provider's series carries its type. So this is a join across two
    # series the page already scrapes rather than anything new to ask the
    # broker for - and without it a row cannot say whether what it counts
    # survives a restart, which is the difference between the two kinds.
    holder = {lb["channel"]: lb.get("provider", "")
              for lb, _ in rows(samples, "saguin_channel_info") if lb.get("channel")}
    kind_of = {lb["provider"]: lb.get("type", "")
               for lb, _ in rows(samples, "saguin_provider_info") if lb.get("provider")}

    channel_rows = []
    for name, (kind, filt) in sorted(chans.items()):
        p, f, n = pos.get(name), floor.get(name), nxt.get(name)
        channel_rows.append({
            "name": name, "type": kind, "filter": filt,
            "provider": holder.get(name) or None,
            "provider_type": kind_of.get(holder.get(name, "")) or None,
            # **A queue's number is its unresolved work, under another
            # name.** `saguin_channel_records` is next minus floor, and a
            # queue has neither: resolution removes from the middle rather
            # than the front. The depth is the number that means something
            # there, the tree beside this page has shown it all along, and
            # this column said "not counted" about a broker that was
            # answering. The row carries its type, so the page can say which
            # of the two it is drawing rather than letting unresolved work
            # be read as a history.
            "records": depth.get(name) if kind == "queue" else records.get(name),
            "bytes": byts.get(name),
            "next": n, "floor": f, "consumers": cons.get(name),
            # **A denominator, and deliberately not a coverage figure.** It
            # says how many subscribers declared a slice of this channel, not
            # which slices they took and not whether they cover the space.
            # The broker cannot answer that last one - a missing index is
            # indistinguishable from a member that has not connected yet - so
            # a panel implying it would read as an alert during every rolling
            # restart. Which client declared what is on the operations
            # sessions route, where a client-chosen number belongs.
            "partitioned": sliced.get(name),
            "position_min": p,
            # **The one alert the whole catalogue exists for.** Retention has
            # passed a stored position, so when that consumer returns it is
            # refused rather than served the oldest surviving record - which
            # is invariant 1 holding, and also somebody's missing afternoon.
            "below_floor": (p is not None and f is not None and f > p),
            # Its warning shot: a consumer far enough behind to be worth
            # looking at before retention reaches it.
            "lag": (None if p is None or n is None else max(n - p, 0)),
            "published": lbl("saguin_published_total").get(name),
            "retention_removed": lbl("saguin_channel_retention_removed_total").get(name),
            "position_lost": lbl("saguin_channel_position_lost_total").get(name),
            "superseded": lbl("saguin_latest_superseded_total").get(name),
        })

    queue_rows = []
    for name, (kind, _f) in sorted(chans.items()):
        if kind != "queue":
            continue
        g = lambda m: lbl(m).get(name, 0)
        queue_rows.append({
            "name": name,
            "depth": g("saguin_queue_depth"), "inflight": g("saguin_queue_inflight"),
            "delivered": g("saguin_queue_delivered_total"),
            "acknowledged": g("saguin_queue_acknowledged_total"),
            "returned": g("saguin_queue_returned_total"),
            "redelivered": g("saguin_queue_redelivered_total"),
            "dead_lettered": g("saguin_queue_dead_lettered_total"),
            "expired": g("saguin_queue_expired_total"),
            "retain_ignored": g("saguin_queue_retain_ignored_total"),
        })

    providers = []
    for lb, _ in rows(samples, "saguin_provider_info"):
        p = lb.get("provider")
        providers.append({
            "name": p, "type": lb.get("type"),
            "bytes": lbl("saguin_provider_bytes", "provider").get(p),
            "max_bytes": lbl("saguin_provider_max_bytes", "provider").get(p),
            "commits": lbl("saguin_storage_commits_total", "provider").get(p),
            "records": lbl("saguin_storage_committed_records_total", "provider").get(p),
            "commit_max": lbl("saguin_provider_publish_commit_max_records", "provider").get(p),
            "errors": lbl("saguin_storage_errors_total", "provider").get(p),
        })

    bridges = []
    for lb, _ in rows(samples, "saguin_bridge_info"):
        b = lb.get("bridge")
        bridges.append({
            "name": b, "peer": lb.get("peer"),
            "connected": lbl("saguin_bridge_connected", "bridge").get(b),
            "stopped": lbl("saguin_bridge_stopped", "bridge").get(b),
            "received": lbl("saguin_bridge_received_total", "bridge").get(b),
            "sent": lbl("saguin_bridge_sent_total", "bridge").get(b),
            "skipped": lbl("saguin_bridge_loops_skipped_total", "bridge").get(b),
            "reconnects": lbl("saguin_bridge_reconnects_total", "bridge").get(b),
            "unsent": lbl("saguin_bridge_unsent_total", "bridge").get(b),
            "unstored": lbl("saguin_bridge_unstored_total", "bridge").get(b),
        })

    build = next((lb for lb, _ in rows(samples, "saguin_build_info")), {})
    return jsonify({
        "taken_at": st["taken"], "interval": st["interval"],
        "error": st["error"], "note": st["note"],
        "scraped": bool(samples),
        "broker": {
            "version": build.get("version"), "id": build.get("broker_id"),
            "uptime": one(samples, "saguin_uptime_seconds"),
            "connections": one(samples, "saguin_connections"),
            "connections_total": one(samples, "saguin_connections_total"),
            "subscriptions": one(samples, "saguin_subscriptions"),
            "max_session_expiry": one(samples, "saguin_max_session_expiry_seconds"),
            "session_expiry_shortened": one(samples, "saguin_session_expiry_shortened_total"),
            "unmatched": one(samples, "saguin_broadcast_unmatched_total"),
            "dropped": one(samples, "saguin_deliveries_dropped_total"),
            "expired": one(samples, "saguin_deliveries_expired_total"),
            "bytes_in": one(samples, "saguin_bytes_received_total"),
            "bytes_out": one(samples, "saguin_bytes_sent_total"),
            "publishes_in": one(samples, "saguin_publishes_received_total"),
            "deliveries_out": one(samples, "saguin_deliveries_sent_total"),
            "deliveries_refused": one(samples, "saguin_deliveries_refused_total"),
                "sessions_offline": one(samples, "saguin_sessions_offline"),
            "sessions_restored": one(samples, "saguin_sessions_restored_total"),
            "wills_waiting": one(samples, "saguin_wills_waiting"),
            "wills_cancelled": one(samples, "saguin_wills_cancelled_total"),
            "session_queue_messages": one(samples, "saguin_session_queue_messages"),
            "session_queue_bytes": one(samples, "saguin_session_queue_bytes"),
            "shares_held": one(samples, "saguin_shares_held_total"),
            "shares_drained": one(samples, "saguin_shares_drained_total"),
            "retained": one(samples, "saguin_retained_messages"),
            "qos2_held": one(samples, "saguin_qos2_held"),
            "qos2_abandoned": one(samples, "saguin_qos2_abandoned_total"),
            "qos2_max_inflight": one(samples, "saguin_qos2_max_inflight_per_client"),
            "go_heap_live": one(samples, "saguin_go_heap_live_bytes"),
            "go_heap_goal": one(samples, "saguin_go_heap_goal_bytes"),
            "go_stack": one(samples, "saguin_go_stack_bytes"),
            "go_gogc": one(samples, "saguin_go_gogc_percent"),
            "go_memory_limit": one(samples, "saguin_go_memory_limit_bytes"),
        },
        # **The fleet mix, by the protocol each client connected with.** How
        # much of the estate is still on the older protocol, and whether that
        # number is coming down. Counted from the client table at each scrape
        # rather than kept by increments, because a gauge maintained on
        # connect and disconnect is one that drifts.
        "protocols": sorted(
            [{"protocol": lb.get("protocol", "?"), "count": v}
             for lb, v in rows(samples, "saguin_connections_by_protocol")],
            key=lambda r: r["protocol"]),
        # **The series behind a fleet in a reconnect loop.** Every
        # connection the broker refused, whatever protocol the client speaks:
        # a CONNECT turned away, or a connection ended for a refusal.
        # Labelled with the same vocabulary as the refusals below, so the two
        # read directly against each other.
        "connections_refused": sorted(
            [{"reason": lb.get("reason", "?"), "count": v}
             for lb, v in rows(samples, "saguin_connections_refused_total")],
            key=lambda r: -r["count"]),
        # Labelled with the specification's name for the code the client was
        # answered with. This is what answers "the fleet's data is not
        # arriving" without reading a log.
        "refusals": sorted(
            [{"reason": lb.get("reason", "?"), "count": v}
             for lb, v in rows(samples, "saguin_publish_refused_total")],
            key=lambda r: -r["count"]),
        # The subscribing side of the same question: a SUBSCRIBE was answered
        # and its filter was refused, so the device gets nothing. Counted per
        # filter, by the code's name, before a 3.1.1 client's 0x80.
        "subscription_refusals": sorted(
            [{"reason": lb.get("reason", "?"), "count": v}
             for lb, v in rows(samples, "saguin_subscriptions_refused_total")],
            key=lambda r: -r["count"]),
        # **Why a delivery never reached a session**, by the broker's own
        # closed set of causes: a session that filled up is not a group with
        # nobody to take its messages, and each has a different fix.
        "session_drops": sorted(
            [{"cause": lb.get("cause", "?"), "count": v}
             for lb, v in rows(samples, "saguin_session_deliveries_dropped_total")],
            key=lambda r: -r["count"]),
        # **And why a group's backlog was given up on**, which is a third
        # question again: a backlog at its bound, a group whose last member
        # session ended so nobody was owed the messages any more, or one held
        # past broker.share.expires_after. The middle cause is the rule that a
        # backlog cannot outlive the sessions it belongs to.
        "share_drops": sorted(
            [{"cause": lb.get("cause", "?"), "count": v}
             for lb, v in rows(samples, "saguin_shares_dropped_total")],
            key=lambda r: -r["count"]),
        # **And why a session itself was not kept**, which is a different
        # question with a different fix: a provider that was full when the
        # client connected, an expiry that passed while the broker was
        # stopped, a subscription this broker's rules now refuse.
        # **What the broker said on a client's behalf, and why it said it.**
        # A Will published because a delay ran out is a device that went
        # quiet; one published at a start is a death the broker could not
        # announce while it was down.
        "wills": sorted(
            [{"cause": lb.get("cause", "?"), "count": v}
             for lb, v in rows(samples, "saguin_wills_published_total")],
            key=lambda r: -r["count"]),
        "session_ends": sorted(
            [{"cause": lb.get("cause", "?"), "count": v}
             for lb, v in rows(samples, "saguin_sessions_dropped_total")],
            key=lambda r: -r["count"]),
        # **The catalogue's own words for each series**, so a card can say
        # what it is drawing without this page keeping a second glossary that
        # goes stale the day a metric's meaning is sharpened upstream.
        "help": described,
        "channels": channel_rows,
        "queues": queue_rows,
        "providers": providers,
        "bridges": bridges,
        "history": hist,
    })


@app.get("/api/config")
def api_config():
    """The configuration this broker resolved, which is not the file on disk.

    **The resolved values, not the written ones**, which is the whole reason
    to ask a running broker rather than read a file: a channel that wrote no
    filter has `<name>/#` here, and one that named no storage has the
    broker-wide default. It is read once at startup, so this is what the
    process is running - a file edited underneath it does not change this
    answer, exactly as it does not change the broker.

    No credential can be in it, and that is the schema's doing rather than a
    filter's: a password file is a path, an acl_file is a path, and a
    bridge's client key is a path.
    """
    sections = request.args.getlist("section")
    q = "?" + "&".join(f"section={s}" for s in sections) if sections else ""
    return jsonify(ops_json("/v1/operations/config" + q))


@app.get("/api/dashboards")
def api_dashboards():
    """The configured dashboards, in the order the config wrote them - one tab
    each, right of Topics. Validated at startup, so this only ever hands back
    specs that loaded; an empty list is a viewer with none configured, and the
    page shows its built-in dashboard instead.
    """
    return jsonify(DASHBOARDS)


@app.get("/api/alarms")
def api_alarms():
    """The alarm record the poll thread keeps: what is firing now, and the
    episodes that fired and cleared within the retention window - so a viewer
    opened after the fact sees what happened while nobody was looking. `firing`
    is newest-started first, `recent` newest-ended first. Empty `defined` means
    no dashboard declares an alarm, so there is nothing to record.
    """
    with lock:
        firing = sorted((dict(e) for e in alarm_episodes if e["ended"] is None),
                        key=lambda e: e["started"], reverse=True)
        recent = sorted((dict(e) for e in alarm_episodes if e["ended"] is not None),
                        key=lambda e: e["ended"], reverse=True)
    return jsonify({"firing": firing, "recent": recent, "defined": len(ALARM_SPECS)})


@app.get("/api/retained")
def api_retained():
    """The retained value the broker keeps for each broadcast topic, as this
    viewer has seen it since it connected - an LWT, a discovery record. Clearing
    one is an empty retained publish through /api/publish, which is how MQTT
    deletes a retained value.
    """
    with lock:
        items = sorted(retained.values(), key=lambda x: x["topic"])
        chans = dict(channels)
    # **The channel each one belongs to, resolved here rather than in the
    # page.** A topic's channel is decided by matching it against every
    # channel's filter, which is the broker's rule and this module already
    # implements it for `/api/route`. The page's tree is keyed by
    # `channel/topic`, so without this a link from here opened a path that
    # names no node - the topic on its own, which reads as a broken link
    # rather than as a missing field.
    # **Broadcast only, which is what a retained message is.** A `latest`
    # channel's current value also arrives with the retain flag set - that is
    # how MQTT carries "this was stored before you subscribed" - so collecting
    # by the flag alone put every state topic in here. On a broker with no
    # retained store at all the panel then listed a hundred rows, every one of
    # them a channel's value, under a heading saying the broker keeps none.
    #
    # The channel is the test rather than the flag: a topic no channel claims
    # is broadcast, and only those can be retained.
    # **How many messages arrived on the topic, beside the one value kept.**
    # A retained value is one slot per topic: publish five and four are
    # delivered and gone, and the fifth is what a new subscriber is handed. A
    # panel showing one row where somebody has just published five times reads
    # as a panel that lost four, and the tree beside it - which lists all five
    # arrivals - makes that look confirmed. So the count comes with the row,
    # and the page can say which number is which.
    with lock:
        seen = {t: v["count"] for t, v in topics.items()}
    out = [{**it, "channel": "broadcast", "seen": seen.get(it["topic"], 0)}
           for it in items if channel_of(it["topic"], chans) is None]
    return jsonify({"items": out})


@app.get("/api/users")
def api_users():
    """Who may connect, and through which door.

    **The names alone are never the answer**, which is why two fields travel
    with them. A door admitting anonymous clients lets in names that are in
    no file at all, and a door requiring a client certificate is wrong in
    both directions at once: none of the names listed can connect, and
    whoever the authority signed can.

    These are the clients, never the operators - the credential guarding
    this route is not on it.
    """
    return jsonify(ops_json("/v1/operations/users"))


@app.get("/api/acl")
def api_acl():
    """What one client may do, and which entry decided it.

    `client_id` is needed only to resolve a rule written with `%c` - but
    when it is given, `client_id_allowed` outranks everything beside it: an
    entry's client_ids refuses at CONNECT before a single rule is consulted,
    so a body listing what a pair may publish while that pair cannot connect
    at all answers the wrong question.
    """
    user = request.args.get("user", "").strip()
    if not user:
        return jsonify({"error": "name a user"}), 400
    q = "?user=" + urllib.parse.quote(user)
    cid = request.args.get("client_id", "").strip()
    if cid:
        q += "&client_id=" + urllib.parse.quote(cid)
    return jsonify(ops_json("/v1/operations/acl" + q))


@app.get("/api/consumers")
def api_consumers():
    """Which durable consumers are behind, and by how much.

    **The catalogue cannot answer this and never will.**
    `saguin_channel_consumer_position_min` is one number per channel, so one
    straggler in a fleet of three hundred reads exactly like a fleet that has
    stopped - and a metric per consumer is impossible, because a client
    chooses its own id and Prometheus keeps a series for every label set it
    has ever seen.

    So it is a route, read when somebody asks rather than every minute by a
    scraper, and this is a viewer asking.
    """
    return jsonify(ops_json("/v1/operations/consumers"))


@app.get("/api/position-lost")
def api_position_lost():
    """Which readers the retention floor passed, and how much each lost.

    **The counter says how much and never which one.**
    `saguin_channel_position_lost_total` is one number per channel, and it
    counts occurrences rather than readers - the floor overtakes the same
    lagging reader thousands of times in a busy run - so a rate that will not
    come down cannot be turned into a list of devices to go and look at. A
    metric keyed by client id is not the answer either, for the reason the
    consumers route exists: a client chooses its own id.

    **`reported` is the field to read first.** A consumer overtaken while it
    was connected and reading cannot be told - MQTT has no way to say it - so
    nobody knows but whoever is looking at this. A durable session whose
    stored position had already been passed when it came back *is* told, with
    Session Present = 0, and starts again knowing it lost its place.

    The record is held in memory and a restart empties it, so a run of zeroes
    here after a restart is a broker that has not seen one since, not a
    broker that never has.
    """
    return jsonify(ops_json("/v1/operations/position-lost"))


@app.get("/api/queue")
def api_queue():
    """What one queue is holding, without taking any of it.

    **The only other way to look is to consume**, and a viewer that consumed
    would take the jobs its workers are meant to do - which is why this page
    lists queues and does not read them. The route leases nothing and returns
    no payloads.
    """
    channel = request.args.get("channel", "")
    if not channel:
        return jsonify({"error": "no channel"}), 400
    # **Checked against the catalogue, then quoted**, which is what the
    # sibling routes already do and this one did neither of. A name goes
    # into a URL path here, so `../consumers` reached a different operations
    # route - nothing a caller on loopback could not ask for directly, and
    # still a path built out of somebody else's string. Quoting settles it;
    # the catalogue check is what turns a wrong name into a sentence rather
    # than a 404 from further away. It is skipped when the catalogue is
    # empty, because a credential reaching /v1 and not /metrics has no
    # catalogue to be checked against and is still entitled to an answer.
    with lock:
        known = dict(channels)
    kind = (known.get(channel) or (None, ""))[0]
    if known and kind != "queue":
        return jsonify({"error": f"{channel!r} is {kind or 'not a channel'} here; "
                                 "only a queue holds work to look at"}), 400
    return jsonify(ops_json("/v1/operations/queues/"
                            + urllib.parse.quote(channel, safe="")))


def ops_json(path):
    """GET one of the operations routes, as JSON.

    Failures come back as a body rather than an exception, because the page
    polls: a viewer that stopped rendering because one panel could not be
    fetched would lose the topic tree as well.
    """
    try:
        return json.loads(ops_get(path, timeout=5).decode())
    except urllib.error.HTTPError as e:
        # 403 is worth saying in words: it means the credential is good and
        # does not reach this route, which is a scope to widen rather than a
        # password to check.
        if e.code == 403:
            # **Named by whatever actually named the caller.** With a client
            # certificate and no password there is no user name, and this
            # read as " does not reach /v1/operations/users" - a sentence
            # with a hole where the answer should be. A certificate saguin
            # verified but has no entry for reaches /metrics and nothing
            # else, which is the rule rather than a misconfiguration.
            if OPS_USER:
                return {"error": f"{OPS_USER} does not reach {path} - widen its scope "
                                 f"with `saguin --passwd scope <file> {OPS_USER} …`"}
            return {"error": f"this client certificate does not reach {path}. A "
                             f"certificate saguin verified but has no password-file "
                             f"entry for reaches /metrics and nothing else; add its "
                             f"Common Name with `saguin --passwd add` and give it a "
                             f"scope, or configure a password."}
        return {"error": f"{path}: HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 - the page must keep rendering
        return {"error": f"{path}: {e}"}


def haystack(m, decode):
    """Everything in one message a reader can see, as one lowercased string.

    **The deserialized record is in it only when deserializing is on.** Matching text
    that is nowhere on the screen would hide a message for a reason the page
    does not show, and a search whose reasons are invisible reads as a
    broken one. With the switch off this searches the bytes as they arrived,
    which is what is on the screen then.

    The topic is not in it. Every message in this view is on the same topic,
    so including it would mean a search matching all of them or none.
    """
    p = m.get("payload") or {}
    parts = [m.get("content_type") or "", p.get("text") or "", p.get("hex") or ""]
    parts += [f"{k}={v}" for k, v in (m.get("properties") or {}).items()]
    if p.get("kind") == "json" and p.get("value") is not None:
        parts.append(json.dumps(p["value"], ensure_ascii=False))
    ref = (m.get("properties") or {}).get("schema")
    if decode and ref and p.get("hex"):
        text, err = schema_text_for(ref)
        if not err:
            ctype = (m.get("content_type") or "").lower().split(";")[0]
            value, _meta, err = decode_with_schema(bytes.fromhex(p["hex"]),
                                                   ctype, ref, text)
            if not err:
                parts.append(json.dumps(value, ensure_ascii=False))
    return "\n".join(parts).lower()


@app.get("/api/messages")
def api_messages():
    topic = request.args.get("topic", "")
    # **Searched here rather than on the page**, so that it is every arrival
    # this viewer holds that is searched and not only the cards that happen
    # to be rendered. Nothing is deserialized unless somebody has typed
    # something: with no search this is the same read it always was.
    q = request.args.get("q", "").strip().lower()
    decode = request.args.get("decode") != "0"
    with lock:
        t = topics.get(topic)
        if not t:
            return jsonify({"kind": "broadcast", "messages": [], "held": 0,
                            "matched": 0, "q": q})
        held = list(t["messages"])
        kind, channel, count = t["kind"], t["channel"], t["count"]
    shown = [m for m in held if q in haystack(m, decode)] if q else held
    # **The cap travels with the answer**, so the page can say that a full
    # list is a window rather than the topic's whole history. A reader
    # counting rows to judge how busy a topic is would otherwise be reading
    # this page's memory and calling it the broker's.
    return jsonify({"kind": kind, "channel": channel, "count": count,
                    "messages": shown, "held": len(held),
                    "matched": len(shown), "q": q, "per_topic": PER_TOPIC})


def point_read(topic, timeout=5):
    """The current value of one topic, to a caller that does not subscribe.

    **This is the one verb on a `latest` channel that is not already
    ordinary MQTT.** SET is a publish, DELETE is a zero-length publish and a
    multi-get is a subscription; reading one value without becoming a
    subscriber is a publish to `$saguin/kv/get` naming the topic, with a
    Response Topic for the answer.

    An empty reply means there is no value - a topic never set and one whose
    value was deleted are the same answer, which is what a zero-length
    payload already means on that channel type. Every refusal rides the
    PUBACK instead, so that the Response Topic only ever carries an answer.
    """
    if PROTOCOL != mqtt.MQTTv5:
        # A point read needs a Response Topic, and 3.1.1 has no such thing.
        return None, "a point read needs MQTT 5: 3.1.1 carries no Response Topic"
    ticket = os.urandom(6).hex()
    props = Properties(PacketTypes.PUBLISH)
    props.ResponseTopic = REPLY_TOPIC
    props.CorrelationData = ticket.encode()
    try:
        info = client.publish("$saguin/kv/get", topic.encode(), qos=1, properties=props)
        info.wait_for_publish(timeout)
    except Exception as e:                     # noqa: BLE001 - shown, not hidden
        return None, f"{type(e).__name__}: {e}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            reply = seeks.pop(ticket, None)
        if reply is not None:
            return reply, None
        time.sleep(0.05)
    # A refused read is answered on the PUBACK and never on the Response
    # Topic, so nothing arriving is what a refusal looks like from here.
    return None, ("no answer within 5s - the broker refuses a read whose key names "
                  "no `latest` channel, holds a wildcard, or is in a reserved space, "
                  "and that refusal rides the PUBACK rather than the reply")


def compiled_message(topic, text):
    """The message class a schema describes, compiled and cached.

    **Compiling needs protoc, which is an optional dependency.** The viewer
    reads and shows a schema with nothing installed; deserializing a
    protobuf payload with it needs `grpcio-tools`, and where that is
    missing this says so rather than showing an empty value.

    **Each schema gets a descriptor pool of its own, and that is not
    tidiness.** protobuf keeps one global pool keyed by fully-qualified
    name, so generating a module for a republished schema fails with
    `duplicate symbol 'iot.WaterMeasurement'` - the second version of a
    message can never be loaded beside the first. A registry exists so that
    schemas can change; a deserializer that dies the first time one does
    would defeat the thing it is reading. So protoc is asked for a descriptor set
    rather than Python source, and each set is added to a private pool where
    its names collide with nothing.
    """
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    with lock:
        hit = schema_types.get(topic)
    if hit and hit[0] == digest:
        return hit[1], None

    try:
        from grpc_tools import protoc
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    except ImportError:
        return None, ("deserializing a protobuf payload needs `grpcio-tools`, which "
                      "is not installed - the schema above is shown without it")

    work = tempfile.mkdtemp(prefix="saguin-viewer-schema-")
    try:
        proto = os.path.join(work, "schema.proto")
        out = os.path.join(work, "schema.pb")
        with open(proto, "w") as fh:
            fh.write(text)
        rc = protoc.main(["protoc", "-I", work, "--descriptor_set_out", out, proto])
        if rc != 0:
            return None, "the schema at that topic is not valid proto3"
        with open(out, "rb") as fh:
            fds = descriptor_pb2.FileDescriptorSet.FromString(fh.read())
    except Exception as e:                     # noqa: BLE001 - reported, not hidden
        return None, f"could not compile the schema: {type(e).__name__}: {e}"
    finally:
        shutil.rmtree(work, ignore_errors=True)

    try:
        pool = descriptor_pool.DescriptorPool()
        for f in fds.file:
            pool.Add(f)
        names = [f"{f.package + '.' if f.package else ''}{m.name}"
                 for f in fds.file for m in f.message_type]
    except Exception as e:                     # noqa: BLE001
        return None, f"could not read the schema: {type(e).__name__}: {e}"

    if not names:
        return None, "that schema defines no message"
    # **One message per schema topic is the convention this follows.** The
    # pointer is a whole topic, so a schema holds the one thing that topic
    # names. Where a file holds several, the one whose name matches the
    # topic wins and the rest are listed rather than guessed between.
    chosen = names[0] if len(names) == 1 else None
    if chosen is None:
        want = [p.lower().replace("_", "") for p in topic.rstrip("/").split("/")[-2:]]
        for n in names:
            if n.rsplit(".", 1)[-1].lower().replace("_", "") in want:
                chosen = n
                break
    if chosen is None:
        return None, ("that schema defines " + ", ".join(sorted(names)) +
                      " and nothing says which describes this message - the "
                      "convention is one message per schema topic")

    try:
        cls = message_factory.GetMessageClass(pool.FindMessageTypeByName(chosen))
    except Exception as e:                     # noqa: BLE001
        return None, f"could not build {chosen}: {type(e).__name__}: {e}"

    with lock:
        if len(schema_types) > 200:
            schema_types.clear()
        schema_types[topic] = (digest, cls)
    return cls, None


PROTOBUF_TYPES = ("application/x-protobuf", "application/protobuf",
                  "application/vnd.google.protobuf")
AVRO_TYPES = ("application/avro", "application/x-avro", "avro/binary",
              "application/vnd.apache.avro+binary")
SCHEMA_TYPES = PROTOBUF_TYPES + AVRO_TYPES


def encoding_of(text):
    """Which reader a schema needs, read off the schema itself.

    **A fallback for a publisher that names a schema and no Content Type**,
    which is the common shape in the wild and is what the bento-connectors
    generator does. It is not a guess about the payload: a `.proto` can only
    be read by protobuf and an Avro schema is JSON, so the schema settles
    the question the missing header would have answered. Where a header is
    present it wins - a publisher saying what it sent is better evidence
    than anything inferred about it.
    """
    head = text.lstrip()[:400]
    if head.startswith("syntax") or "message " in head or "package " in head:
        return PROTOBUF_TYPES[0]
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if isinstance(doc, (dict, list)):
        return AVRO_TYPES[0]
    return None


def decode_with_schema(raw, ctype, topic, text):
    """One payload, read through the schema its headers name.

    **The Content Type chooses the reader and the schema comes from the
    registry**, which is the whole of the convention: nothing here is
    specific to a deployment, and adding a format is adding a branch
    rather than a special case. Where the Content Type is missing the schema
    itself says which reader it needs.
    """
    inferred = False
    if ctype not in SCHEMA_TYPES:
        ctype = encoding_of(text)
        inferred = True
        if ctype is None:
            return None, None, ("no Content Type on the message, and the schema at "
                                "that topic is neither proto3 nor an avro schema - "
                                "so nothing says how to read these bytes")
    if ctype in PROTOBUF_TYPES:
        cls, err = compiled_message(topic, text)
        if err:
            return None, None, err
        try:
            msg = cls()
            msg.ParseFromString(raw)
        except Exception as e:                 # noqa: BLE001 - shown, not hidden
            return None, None, (f"these bytes are not {cls.DESCRIPTOR.name}: "
                                f"{type(e).__name__}: {e}")
        return (MessageToDict(msg, preserving_proto_field_name=True,
                              always_print_fields_with_no_presence=True),
                {"message": cls.DESCRIPTOR.name, "format": ctype,
                 "inferred": inferred},
                None)

    if ctype in AVRO_TYPES:
        try:
            import fastavro
        except ImportError:
            return None, None, ("deserializing an avro payload needs `fastavro`, "
                                "which is not installed - the schema above is "
                                "shown without it")
        try:
            schema = fastavro.parse_schema(json.loads(text))
        except Exception as e:                 # noqa: BLE001
            return None, None, f"that schema is not valid avro: {type(e).__name__}: {e}"
        try:
            value = fastavro.schemaless_reader(io.BytesIO(raw), schema)
        except Exception as e:                 # noqa: BLE001
            return None, None, (f"these bytes are not that avro record: "
                                f"{type(e).__name__}: {e}")
        name = schema.get("name") if isinstance(schema, dict) else "record"
        return value, {"message": name or "record", "format": ctype,
                       "inferred": inferred}, None

    return None, None, (f"content type {ctype or 'unset'} is not one this viewer "
                        f"deserializes with a schema; it deserializes "
                        f"{', '.join(SCHEMA_TYPES)}")


def schema_text_for(topic):
    """The schema at a topic, from the cache or the registry."""
    with lock:
        hit = schema_cache.get(topic)
    if hit:
        return hit["text"], None
    text, err = point_read(topic)
    if err or not text:
        return None, err or "no schema registered at that topic"
    with lock:
        if len(schema_cache) > 200:
            schema_cache.clear()
        schema_cache[topic] = {"topic": topic, "text": text,
                               "bytes": len(text.encode()), "read_at": time.time()}
    return text, None


@app.post("/api/decode")
def api_decode():
    """One payload, read through the schema its headers point at.

    **The pointer comes from the message and nothing here is guessed.** The
    `schema` user property says where the schema is; a Content Type, if the
    publisher sent one, says how to read the bytes, and where it is missing
    the schema at that topic says so itself. That is what makes this work
    against any deployment following the convention rather than against the
    one it was written for. The answer names which of the two chose the
    reader, because "the publisher said so" and "we worked it out" are not
    the same claim.
    """
    body = request.get_json(silent=True) or {}
    topic = str(body.get("schema", "")).strip()
    ctype = str(body.get("content_type", "")).strip().lower().split(";")[0]
    hexed = str(body.get("hex", ""))
    if not topic:
        return jsonify({"error": "the message names no schema"}), 400
    try:
        raw = bytes.fromhex(hexed)
    except ValueError:
        return jsonify({"error": "payload is not hex"}), 400

    text, err = schema_text_for(topic)
    if err:
        return jsonify({"error": err}), 502
    value, meta, err = decode_with_schema(raw, ctype, topic, text)
    if err:
        return jsonify({"error": err}), 422
    return jsonify({"schema": topic, "value": value, **meta})


def as_number(text):
    """The whole of a payload read as one number, or None.

    **The whole of it.** A number inside a sentence is a number that
    happened to be in a sentence, and picking one out of `pressure 28.89 psi`
    is how a chart ends up plotting a device id. `nan` and `inf` parse as
    floats and are not points on a scale - they are also not writable as
    JSON, so one of them reaching the answer would fail the whole request
    rather than one reading.
    """
    try:
        v = float((text or "").strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def numeric_fields(value, prefix=""):
    """Every number in a deserialized payload, by its path.

    **A protobuf int64 arrives as a string**, because JSON cannot hold one
    exactly - so a numeric-looking string is read as a number here or every
    timestamp and counter in a protobuf payload would be unplottable. A
    boolean is not a measurement and is left out: charting a flag as 0 and 1
    invites a line between two states that were never on a scale.
    """
    out = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(numeric_fields(v, f"{prefix}{k}."))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.update(numeric_fields(v, f"{prefix}{i}."))
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        if math.isfinite(value):
            out[prefix.rstrip(".")] = float(value)
    elif isinstance(value, str):
        v = as_number(value)
        if v is not None:
            out[prefix.rstrip(".")] = v
    return out


@app.get("/api/series")
def api_series():
    """The numbers inside a topic's payloads, over the arrivals held here.

    **However the payload was serialized.** JSON needs nothing; a protobuf or
    avro payload is read through the schema its own headers name, which is
    the registry convention doing the work - so a field can be plotted
    without this viewer knowing anything about the deployment.

    It reads the arrivals this page is holding rather than asking the broker
    for more: the ring is what it has, and a chart drawn from it says so.
    """
    topic = request.args.get("topic", "")
    with lock:
        t = topics.get(topic)
        held = list(t["messages"]) if t else []
    if not t:
        return jsonify({"topic": topic, "points": [], "fields": [], "source": "none"})

    points, source, err = [], "none", None
    for m in reversed(held):                   # oldest first, for a line
        p = m.get("payload") or {}
        value = None
        if p.get("kind") == "json":
            value, source = p.get("value"), "json"
        elif p.get("kind") == "text":
            # **A topic carrying nothing but a number is the commonest
            # sensor there is**, and it is the one payload whose chart needs
            # no interpretation at all. The field has no name because the
            # payload gives none: inventing one from a topic level would be
            # the viewer guessing which level was the measurement.
            one = as_number(p.get("text"))
            if one is not None:
                value, source = {"value": one}, "number"
        elif p.get("hex"):
            ref = (m.get("properties") or {}).get("schema")
            ctype = (m.get("content_type") or "").lower().split(";")[0]
            if ref and request.args.get("decode") != "0":
                text, e = schema_text_for(ref)
                if e:
                    err = err or e
                else:
                    value, _meta, e = decode_with_schema(
                        bytes.fromhex(p["hex"]), ctype, ref, text)
                    if e:
                        err = err or e
                    else:
                        source = "schema"
        if value is None:
            continue
        nums = numeric_fields(value)
        if nums:
            points.append({"t": m["at"], "values": nums})

    fields = sorted({k for pt in points for k in pt["values"]})
    return jsonify({"topic": topic, "points": points, "fields": fields,
                    "source": source, "held": len(held), "error": err})


@app.get("/api/route")
def api_route():
    """Where a topic lands, which a name does not say.

    **The question `saguin --route` answers at a shell**, for somebody with
    a credential and no shell. Filters overlap deliberately and the most
    exact of them holds a topic, so this is not a prefix match - and where
    two filters both match, both are named rather than one being picked
    quietly.
    """
    topic = request.args.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "name a topic"}), 400
    if "+" in topic or "#" in topic:
        return jsonify({"error": "that is a filter, not a topic - `+` and `#` are "
                                 "wildcards, and a published topic carries neither"}), 400
    with lock:
        chans = dict(channels)
    matched = []
    for name, (kind, written) in sorted(chans.items()):
        for filt in expand(written or ""):
            if filt and topic_matches_sub(filt, topic):
                matched.append({"channel": name, "type": kind, "filter": filt})
                break
    holder = channel_of(topic, chans)
    return jsonify({
        "topic": topic,
        "channel": holder,
        "type": chans.get(holder, (None, None))[0] if holder else "broadcast",
        "filter": next((m["filter"] for m in matched if m["channel"] == holder), None),
        # Every filter that matches, so an operator can see that two claim it
        # and which one won - the only way an overlap is visible at all.
        "matched": matched,
        "readable": (chans.get(holder, ("", ""))[0] != "queue") if holder else True,
    })


@app.get("/api/schema")
def api_schema():
    """The schema a message points at, read from the registry channel.

    The `schema` property carries a *topic*, so this is a point read of that
    topic and nothing more - the viewer needs no registry of its own, and no
    deployment's schemas compiled into it.
    """
    topic = request.args.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "no schema topic"}), 400
    if request.args.get("refresh") != "1":
        with lock:
            hit = schema_cache.get(topic)
        if hit:
            return jsonify({**hit, "cached": True})

    text, err = point_read(topic)
    if err:
        return jsonify({"topic": topic, "error": err}), 502
    if not text:
        # Absence needs no new vocabulary on this channel type: a topic
        # never set and one whose value was deleted are the same answer.
        return jsonify({"topic": topic,
                        "error": "no value at that topic - the schema is not "
                                 "registered, or was retired"}), 404
    answer = {"topic": topic, "text": text, "bytes": len(text.encode()),
              "read_at": time.time()}
    with lock:
        # Bounded like everything else here: a fleet naming a thousand
        # schemas should not be a thousand held for ever.
        if len(schema_cache) > 200:
            schema_cache.clear()
        schema_cache[topic] = answer
    return jsonify({**answer, "cached": False})


@app.get("/api/feed")
def api_feed():
    """Everything that has arrived, newest last, since a sequence number.

    **Incremental on purpose.** The page holds what it has already been
    given and asks only for what is new, so a feed left open does not
    re-send its whole buffer every second - and the sequence number makes a
    gap visible rather than silently skipped, which matters because the ring
    drops the oldest when a busy broker outruns a reader.

    **A queue's records never appear here**, and that is the broker's doing
    rather than a filter of ours: a filter merely crossing a queue is served
    everything except its work.
    """
    try:
        after = int(request.args.get("after", 0))
    except ValueError:
        after = 0
    with lock:
        rows_out = [m for m in feed if m["seq"] > after]
        oldest = feed[0]["seq"] if feed else 0
        newest = feed_seq[0]
    return jsonify({
        # **The ring is the bound, and it is the only one.** This clipped to a
        # second hard-coded 2000, which was a no-op only for as long as the
        # ring's own bound was hard-coded to the same number: the moment
        # `feed_messages` started being read, a larger ring would have served
        # its newest 2000 and reported `held` for all of them with `dropped`
        # false - a reader silently missing the rest, on the endpoint whose
        # whole job is to make a gap visible.
        "messages": rows_out,
        "seq": newest,
        # A reader that fell behind the ring is told so rather than handed a
        # feed with a hole in it.
        "dropped": after > 0 and oldest > after + 1,
        "held": len(feed), "capacity": feed.maxlen,
    })


# ===========================================================================
# **Dead letters, and putting one back.**
#
# A dead-letter channel is an ordinary `append` channel the broker derives from
# a queue, so the tree has always shown it. What it did not show is the one
# thing an operator opens it for: why each record failed, and a way to put it
# back once the bug is fixed.
#
# **Requeue needs nothing from the broker** - RFC 0003 "Putting dead-lettered
# work back" says it is four lines of any MQTT client, because a queue takes an
# ordinary publish. What it takes is care about three things, all of which this
# page can get wrong quietly:
#
#   * **Where the `__dlq` level sits.** RFC 0003 gives a rule - at the `#` for a
#     filter ending in one, on the end otherwise - and reimplementing that rule
#     is a way to be subtly wrong on a filter shape nobody tested. So it is not
#     reimplemented: the dead-letter channel's *own* filter carries the level at
#     exactly the position to remove (`jobs/__dlq/#`, `iot/+/work/+/__dlq`), and
#     that filter comes from the broker's catalogue. The broker says where it
#     put the level; this takes it out of the same place.
#   * **The payload, byte for byte.** A requeue that re-encodes is a requeue
#     that corrupts, and `render_payload` is a display rendering. Every one of
#     its four kinds is exactly recoverable and `payload_bytes` is the inverse -
#     with a round-trip test over all four, because "looks like text" is where
#     this would go wrong.
#   * **The properties.** The publisher's own go back; `saguin-` ones do not,
#     because the broker refuses them from any client - except `saguin-id`,
#     which RFC 0003 makes the one reserved name a publisher may set and the
#     whole reason to carry properties back at all. Pass it and a consumer can
#     tell this is the same work returning rather than new work that looks like
#     it. Drop it and the round trip is invisible to everyone downstream.
DLQ_LEVEL = "__dlq"


def dlq_level_index(filt):
    """Where the `__dlq` level sits in a dead-letter channel's filter, or None
    if this is not one. The filter is the broker's own, from the catalogue."""
    levels = str(filt or "").split("/")
    return levels.index(DLQ_LEVEL) if DLQ_LEVEL in levels else None


def requeue_topic(topic, filt):
    """The topic a dead letter goes back to: its own, with the `__dlq` level
    taken out at the position the channel's filter puts it.

    Returns (topic, None) or (None, why-not). It refuses rather than guesses:
    a topic that does not carry the level where the filter says it is, is not a
    record this rule describes, and publishing the un-stripped topic would put
    the job into the dead-letter channel it came from."""
    i = dlq_level_index(filt)
    if i is None:
        return None, f"{filt!r} is not a dead-letter channel's filter"
    levels = str(topic).split("/")
    if i >= len(levels) or levels[i] != DLQ_LEVEL:
        return None, (f"{topic!r} does not carry {DLQ_LEVEL!r} at level {i}, where "
                      f"{filt!r} puts it - so this is not a dead letter of that "
                      f"channel and where its job came from cannot be known")
    back = "/".join(levels[:i] + levels[i + 1:])
    if not back or DLQ_LEVEL in back.split("/"):
        return None, (f"stripping {DLQ_LEVEL!r} from {topic!r} leaves {back!r}, which "
                      f"is not a topic a job can go back to")
    return back, None


def payload_bytes(rendered):
    """The exact bytes behind a render_payload result.

    **The inverse, and it has to be exact.** A requeue publishes this, so a
    round trip that re-encodes rather than restores is a corrupted job - and it
    would look fine, because the corruption is in bytes a person reads as text.
    Every kind render_payload produces keeps enough to reconstruct: json and
    text keep the decoded text, bytes keeps hex, empty is empty."""
    kind = (rendered or {}).get("kind")
    if kind in ("json", "text"):
        return (rendered.get("text") or "").encode("utf-8")
    if kind == "bytes":
        return binascii.unhexlify(rendered.get("hex") or "")
    if kind == "empty":
        return b""
    raise ValueError(f"cannot recover the bytes of a {kind!r} payload")


def dlq_channels(chans):
    """The dead-letter channels in a catalogue, as {name: filter}."""
    return {name: written for name, (_kind, written) in chans.items()
            if dlq_level_index(written) is not None}


def counts():
    """How many dead letters and retained values this page can show.

    Both are counts of what **this viewer holds** rather than of what the
    broker holds - the same bound every list on this page carries - so the
    badge counts rows the reader can actually open. Caller holds `lock`; the
    channel table is snapshotted once because `channel_of` walks it per topic
    and the poll thread replaces it wholesale.
    """
    chans = dict(channels)
    dlqs = set(dlq_channels(chans))
    return {
        "deadletters": sum(len(t["messages"]) for t in topics.values()
                           if t.get("channel") in dlqs),
        "retained": sum(1 for t in retained if channel_of(t, chans) is None),
    }


def publish_once(topic, payload, qos, retain, props):
    """Publish one message on a connection of its own and say what the
    broker made of it. Returns (json-able dict, HTTP status).

    **Extracted so a requeue and a hand-written publish cannot drift in
    what they call success.** Every subtlety here was paid for once -
    a PUBACK that carries a refusal, a refusal that closes the
    connection instead of answering, QoS 0 acknowledging itself, a
    3.1.1 PUBACK that cannot carry a reason - and a second copy of it
    is a second place for one of them to be forgotten.
    """
    outcome = {}
    done = threading.Event()

    def on_publish(_c, _u, _mid, reason_code=None, _props=None):
        # **A PUBACK that arrives is not a PUBACK that agreed.** saguin
        # answers a publish into a dead-letter channel with `Not authorized`
        # and a rate-limited one with `Quota exceeded` - both are ordinary
        # acknowledgements carrying a refusal, and reporting the arrival as
        # success would tell an operator their message was stored when the
        # broker had just declined it. Anything at or above 0x80 is a
        # refusal in MQTT 5.
        outcome["reason"] = str(reason_code) if reason_code is not None else "delivered"
        if reason_code is not None:
            fail = getattr(reason_code, "is_failure", None)
            outcome["failed"] = bool(fail) if fail is not None else \
                getattr(reason_code, "value", 0) >= 0x80
        done.set()

    def on_disconnect(_c, _u, _flags, reason_code=None, _props=None):
        # A refusal that ends the connection rather than answering it. This
        # is the one an operator most needs the words for: the broker's log
        # says why and the client is told only that it was hung up on.
        if not done.is_set():
            outcome["disconnected"] = str(reason_code)
            done.set()

    pub = mqtt.Client(CallbackAPIVersion.VERSION2,
                      client_id=f"{CLIENT_ID}-pub-{os.urandom(3).hex()}",
                      protocol=PROTOCOL,
                      **({} if PROTOCOL == mqtt.MQTTv5 else {"clean_session": True}))
    pub.on_publish, pub.on_disconnect = on_publish, on_disconnect
    if MQTT_CFG["username"]:
        pub.username_pw_set(MQTT_CFG["username"], MQTT_CFG["password"])
    if MQTT_CFG["tls"]["enabled"]:
        pub.tls_set(ca_certs=MQTT_CFG["tls"]["ca_file"] or None,
                    certfile=MQTT_CFG["tls"]["cert_file"] or None,
                    keyfile=MQTT_CFG["tls"]["key_file"] or None,
                    cert_reqs=ssl.CERT_NONE if MQTT_CFG["tls"]["insecure_skip_verify"]
                    else ssl.CERT_REQUIRED)
        if MQTT_CFG["tls"]["insecure_skip_verify"]:
            pub.tls_insecure_set(True)

    try:
        pub.connect(HOST, PORT, keepalive=MQTT_CFG["keepalive"])
        pub.loop_start()
        info = pub.publish(topic, payload, qos=qos,
                           retain=bool(retain), properties=props)
        if qos == 0:
            # Nothing is acknowledged at QoS 0, so the honest report is that
            # it went out - not that it arrived. paho calls on_publish here
            # too, with a reason code it made up rather than one the broker
            # sent, so that answer is overwritten rather than trusted:
            # "Success" from a client talking to itself is the exact shape
            # of a false green light.
            info.wait_for_publish(5)
            outcome["reason"] = ("written to the socket - QoS 0 is not "
                                 "acknowledged, so nothing here says the broker "
                                 "kept it")
            outcome["failed"] = False
        else:
            done.wait(5)
            # **A 3.1.1 PUBACK carries no reason code**, so "Success" here is
            # the client library's word rather than the broker's. saguin
            # answers a refusal this protocol cannot be told about by
            # closing the connection instead - which is caught above - so
            # what an acknowledgement means here is only that one arrived.
            if PROTOCOL != mqtt.MQTTv5 and not outcome.get("failed"):
                outcome["reason"] = ("acknowledged - MQTT 3.1.1 carries no reason "
                                     "code on a PUBACK, so nothing here says more "
                                     "than that one arrived")
    except Exception as e:                     # noqa: BLE001 - shown, not hidden
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}, 502
    finally:
        try:
            pub.loop_stop(); pub.disconnect()
        except Exception:                      # noqa: BLE001
            pass

    if "disconnected" in outcome:
        return {"ok": False, "topic": topic,
                        "error": f"the broker closed the connection rather than "
                                 f"answering: {outcome['disconnected']}. Its log says "
                                 f"why; a retained publish to a broadcast topic does "
                                 f"this when no retained store is configured."}, 502
    if "reason" not in outcome:
        return {"ok": False, "topic": topic,
                        "error": "no acknowledgement within 5s"}, 504
    if outcome.get("failed"):
        return {"ok": False, "topic": topic,
                        "error": f"the broker acknowledged and refused it: "
                                 f"{outcome['reason']}"}, 200
    # A successful empty retained publish is a clear, so drop it from the set
    # rather than wait for a reconnect - the delete arrives live with the retain
    # flag off and would otherwise linger in the list.
    if retain and not payload:
        with lock:
            retained.pop(topic, None)
    return {"ok": True, "topic": topic, "bytes": len(payload),
            "reason": outcome["reason"]}, 200


@app.post("/api/publish")
def api_publish():
    """Publish one message, and say what the broker made of it.

    **On a connection of its own, not the viewer's.** A refused publish is
    not always a reason code: saguin *disconnects* a client for a retained
    publish it cannot honour, and MQTT has several other refusals that end
    the connection rather than answer it. Sent on the viewer's own link, one
    mistyped publish would drop the subscription, empty the tree and lose
    everything the page had collected - a form that can wipe the window it
    is in. A short-lived client costs a connection per publish, which is
    nothing for something a person clicks, and it cannot take the page down.

    It also reads correctly in the broker's own logs and in
    /v1/operations/users: the thing that published is named as a publisher
    rather than as the viewer that was watching.
    """
    body = request.get_json(silent=True) or {}
    topic = str(body.get("topic", "")).strip()
    if not topic:
        return jsonify({"ok": False, "error": "no topic"}), 400
    if "+" in topic or "#" in topic:
        # Refused here rather than at the broker, which answers a wildcard
        # topic name by closing the connection: `0x82 Protocol error`. The
        # message is friendlier from this side and costs nobody a link.
        return jsonify({"ok": False,
                        "error": "a topic name cannot contain + or # - those are "
                                 "filter syntax, and publishing one is a protocol "
                                 "error the broker answers by disconnecting"}), 400

    fmt = body.get("format", "text")
    raw = body.get("payload", "")
    try:
        if fmt == "hex":
            payload = bytes.fromhex("".join(str(raw).split()))
        elif fmt == "json":
            # Parsed and re-rendered, so that what goes on the wire is known
            # to be JSON rather than something that merely looked like it.
            payload = json.dumps(json.loads(raw)).encode()
        else:
            payload = str(raw).encode()
    except Exception as e:                     # noqa: BLE001 - shown, not hidden
        return jsonify({"ok": False, "error": f"payload is not {fmt}: {e}"}), 400

    qos = int(body.get("qos", 1))
    if qos not in (0, 1):
        # saguin answers Maximum QoS 1 in its CONNACK; QoS 2 is not offered.
        return jsonify({"ok": False, "error": "QoS is 0 or 1 - saguin answers "
                                              "Maximum QoS 1 in its CONNACK"}), 400

    pairs_in = {k: v for k, v in (body.get("properties") or {}).items() if str(k).strip()}
    if PROTOCOL != mqtt.MQTTv5 and (pairs_in or body.get("content_type")):
        # **Refused rather than dropped.** MQTT 3.1.1 has no user properties
        # and no content type, so these cannot go on the wire - and sending
        # the message without them would acknowledge a publish that is not
        # the one that was asked for. A form that quietly sends something
        # else is worse than one that says it cannot.
        return jsonify({"ok": False,
                        "error": "MQTT 3.1.1 carries no user properties and no "
                                 "content type, so this message cannot be sent as "
                                 "written. Remove them, or connect the viewer as "
                                 "MQTT 5 (broker.mqtt.protocol)."}), 400

    expiry = body.get("message_expiry")
    if expiry not in (None, "", 0):
        try:
            expiry = int(expiry)
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": "message expiry is a whole number of seconds"}), 400
        if expiry < 0 or expiry > 0xFFFFFFFF:
            # Unsigned 32 bits, MQTT's own bound on the field.
            return jsonify({"ok": False,
                            "error": "message expiry is 0 to 4294967295 seconds"}), 400
        if PROTOCOL != mqtt.MQTTv5:
            # Refused rather than dropped, as the user properties above are:
            # 3.1.1 has no such field, and sending the message without it
            # would acknowledge a publish that is not the one asked for.
            return jsonify({"ok": False,
                            "error": "MQTT 3.1.1 carries no message expiry interval, so "
                                     "this message cannot be sent as written. Remove it, "
                                     "or connect the viewer as MQTT 5 "
                                     "(broker.mqtt.protocol)."}), 400
    else:
        expiry = None

    props = None
    if PROTOCOL == mqtt.MQTTv5:
        props = Properties(PacketTypes.PUBLISH)
        if body.get("content_type"):
            props.ContentType = str(body["content_type"])
        if expiry is not None:
            props.MessageExpiryInterval = expiry
        pairs = [(str(k), str(v)) for k, v in pairs_in.items()]
        if pairs:
            # **`saguin-` is reserved**, and a publisher setting one would be
            # writing over what the broker stamps on a delivery.
            bad = [k for k, _ in pairs if k.startswith("saguin-")]
            if bad:
                return jsonify({"ok": False,
                                "error": f"user property {bad[0]!r}: the `saguin-` "
                                         "prefix is reserved for the broker's own "
                                         "properties on a delivery"}), 400
            props.UserProperty = pairs

    resp, status = publish_once(topic, payload, qos, body.get("retain"), props)

    # **A retained publish this page made is recorded here, because nothing
    # else will tell it.** MQTT sets the retain flag on a delivery only when
    # replaying a stored value to a *new* subscription; a message arriving on
    # a live subscription carries `retain = 0` whatever the publisher asked
    # for. So a page that learns about retained values only from the flag
    # cannot see the one it just sent - it publishes with RETAIN, watches the
    # message arrive with the flag clear, and shows an empty panel.
    #
    # What it knows is what it asked for. An empty payload is the delete, so
    # that removes the row rather than adding one.
    #
    # **The limit, said rather than hidden**: a retained value published by
    # somebody else still appears only after this viewer reconnects and is
    # served the store. Nothing on the wire distinguishes it before then.
    if resp.get("ok") and body.get("retain") and channel_of(topic) is None:
        with lock:
            if payload:
                retained[topic] = {"topic": topic, "at": time.time(), "qos": qos,
                                   "payload": render_payload(payload,
                                                             body.get("content_type") or "",
                                                             "broadcast")}
            else:
                # Only the stored value goes. What arrived on this topic
                # before is still what arrived.
                retained.pop(topic, None)
    return jsonify(resp), status


@app.post("/api/keep")
def api_keep():
    """Resize the per-topic rings, the way the feed's `keep` resizes its own.

    **Existing rings are rebuilt rather than left at their old bound.** A
    deque's maxlen cannot be changed, so a page asking for 2000 and getting
    the 25 already configured would be a control that reports success and
    does nothing - and the reader would count rows and conclude the broker had
    sent no more.

    Growing keeps everything held; shrinking drops the oldest, which is what
    the bound means.
    """
    global PER_TOPIC
    want = (request.get_json(silent=True) or {}).get("keep")
    try:
        want = int(want)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "keep is a number of messages"}), 400
    if want not in KEEP_CHOICES:
        return jsonify({"ok": False,
                        "error": f"keep is one of {', '.join(map(str, KEEP_CHOICES))}"}), 400
    with lock:
        PER_TOPIC = want
        for name, t in topics.items():
            if t["kind"] == "latest":
                # One value per topic is what the channel holds, not a
                # preference - resizing it would show a history the broker
                # does not have.
                continue
            t["messages"] = collections.deque(t["messages"], maxlen=want)
    return jsonify({"ok": True, "keep": want})


@app.get("/api/deadletters")
def api_deadletters():
    """The dead-letter channels, and the records held on one of them.

    Assembled from what this viewer has already received rather than from a new
    subscription: a dead-letter channel is an append channel the page is
    subscribed to like any other, so its records are in the per-topic rings
    already. That bounds what this can show to `messages_per_topic` per topic,
    which the reply says out loud rather than presenting a window as the whole.
    """
    with lock:
        chans = dict(channels)
    dlqs = dlq_channels(chans)
    want = request.args.get("channel", "").strip()
    out = {"channels": sorted(dlqs), "per_topic": PER_TOPIC}
    if not want:
        return jsonify(out)
    if want not in dlqs:
        return jsonify({**out, "error": f"{want!r} is not a dead-letter channel here"}), 400
    filt = dlqs[want]
    rows = []
    with lock:
        held = [(t, list(v["messages"])) for t, v in topics.items()
                if v.get("channel") == want]
    for topic, msgs in held:
        back, why = requeue_topic(topic, filt)
        for m in msgs:
            props = m.get("properties") or {}
            rows.append({
                # **Keyed by the broker's own offset for the record on this
                # channel**, not by anything this page invents: the per-topic
                # ring has no sequence number, two dead letters of one job
                # differ only by offset, and a requeue naming the wrong one
                # puts back a different job.
                "topic": topic, "offset": props.get("saguin-offset"), "at": m.get("at"),
                "back": back, "why_not": why,
                # The failure metadata, surfaced rather than left in the
                # property list an operator has to open each record to read.
                "reason": props.get("saguin-dlq-reason"),
                "attempts": props.get("saguin-dlq-attempts"),
                "queue": props.get("saguin-dlq-channel"),
                "failed_at": props.get("saguin-dlq-at"),
                "first": props.get("saguin-dlq-first"),
                "last": props.get("saguin-dlq-last"),
                "id": props.get("saguin-id"),
                "bytes": (m.get("payload") or {}).get("bytes"),
                "content_type": m.get("content_type"),
                # **Whether this page has already put this work back.** Read
                # from the memory rather than from the record, because the
                # record cannot carry it: an append channel's rows never
                # change, and marking the redriven job instead would put the
                # mark on a site's copy that a passive site would not have.
                "redriven": redrive_note(props.get("saguin-id")),
            })
    rows.sort(key=lambda r: (r["at"] or 0), reverse=True)
    # **An empty list has two meanings and they need telling apart.** Nothing
    # has failed, or nothing reached this viewer - and under an acl_file the
    # second is the likely one, because a credential scoped to the dead-letter
    # channel is answered `Not authorized` for the default `#` subscription.
    # An unqualified empty list there reads as a broken feature.
    refused = [x["filter"] for x in state["subscriptions"] if not x["granted"]]
    # Only the filters `subscribe` actually controls belong in the advice; the
    # reply topic is refused by the same ACL and is not what an operator would
    # edit to fix this.
    asked = set(expand(MQTT_CFG["subscribe"]))
    refused_data = [f for f in refused if f in asked]
    hint = None
    if not rows and refused_data:
        hint = (f"This channel exists and no records have reached this viewer. The "
                f"broker refused the subscription to {refused_data[0]!r} - under an "
                f"acl_file that is the usual answer to a wildcard - so nothing is "
                f"arriving to be listed. Narrow broker.mqtt.subscribe to what this "
                f"credential may read, e.g. {filt!r}.")
    elif not rows:
        hint = ("This channel exists and holds nothing this viewer has seen. Records "
                "that were dead-lettered before it connected are not replayed unless "
                "its session is durable and its subscription covers them.")
    return jsonify({**out, "channel": want, "filter": filt, "records": rows,
                    "hint": hint, "refused": refused})


@app.post("/api/requeue")
def api_requeue():
    """Put one dead letter back on the queue it came from.

    **One record, named by topic and sequence number.** There is no drain-all
    and that is deliberate: RFC 0003 names the hazard - redriving into a queue
    whose bug is not fixed dead-letters the job again, and doing it in a loop
    fills a disk. One record per call makes that self-limiting, and an operator
    putting fifty back is fifty deliberate acts.

    **What it costs is said in the reply rather than discovered**: the job goes
    back as a new record with a new offset and a fresh attempt count, and what
    survives is its identity, because `saguin-id` is carried back.
    """
    body = request.get_json(silent=True) or {}
    topic = str(body.get("topic", "")).strip()
    offset = body.get("offset")
    if not topic or offset in (None, ""):
        return jsonify({"ok": False, "error": "name the record: topic and offset"}), 400
    offset = str(offset)

    with lock:
        chans = dict(channels)
        t = topics.get(topic)
        rec = next((m for m in (t or {}).get("messages", [])
                    if str((m.get("properties") or {}).get("saguin-offset")) == offset), None)
        holder = (t or {}).get("channel")
    if rec is None:
        return jsonify({"ok": False,
                        "error": f"no record at offset {offset} on {topic!r} is still held "
                                 f"here - this page keeps the last {PER_TOPIC} per topic, "
                                 f"and a requeue publishes the bytes it is holding"}), 404

    dlqs = dlq_channels(chans)
    if holder not in dlqs:
        return jsonify({"ok": False,
                        "error": f"{topic!r} is on {holder or 'no channel'}, which is not "
                                 f"a dead-letter channel"}), 400
    back, why = requeue_topic(topic, dlqs[holder])
    if back is None:
        return jsonify({"ok": False, "error": why}), 400
    # **The target must be a queue**, checked against the catalogue rather than
    # assumed from the name. A stripped topic that lands anywhere else would
    # publish the job into a broadcast or an append channel - accepted by the
    # broker, and not work anybody will ever run.
    dest = channel_of(back, chans)
    kind = chans.get(dest, (None, ""))[0]
    if kind != "queue":
        return jsonify({"ok": False,
                        "error": f"{back!r} lands on {dest or 'no channel'}"
                                 f"{f' ({kind})' if kind else ''}, not a queue - so "
                                 f"putting this record there would not requeue it"}), 400

    try:
        payload = payload_bytes(rec.get("payload"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    props = None
    if PROTOCOL == mqtt.MQTTv5:
        props = Properties(PacketTypes.PUBLISH)
        if rec.get("content_type"):
            props.ContentType = str(rec["content_type"])
        held = rec.get("properties") or {}
        # The publisher's own properties go back. The broker's do not - it
        # refuses the whole `saguin-` prefix from any client - except
        # `saguin-id`, which RFC 0003 makes the one reserved name a publisher
        # may set, and which is what lets a consumer tell this is the same work
        # coming round again rather than new work that looks like it.
        pairs = [(str(k), str(v)) for k, v in held.items() if not k.startswith("saguin-")]
        if held.get("saguin-id"):
            pairs.append(("saguin-id", str(held["saguin-id"])))
        if pairs:
            props.UserProperty = pairs

    resp, status = publish_once(back, payload, 1, False, props)
    if not resp.get("ok"):
        # **A refusal here has one overwhelmingly likely cause, so say it.**
        # The viewer reads a dead-letter channel with `read`, and putting the
        # job back needs `write` on the *queue* - a different grant on a
        # different channel, which is exactly the shape of ACL an operator
        # writes without noticing. "Not authorized" is the broker's word for
        # it and names neither the grant nor where to look.
        out = {**resp, "requeued_to": back, "queue": dest}
        if "not authorized" in str(resp.get("error", "")).lower():
            out["hint"] = (f"the viewer's MQTT credential may read {holder!r} and has no "
                           f"`write` grant on {dest!r}. Reading dead letters and putting "
                           f"one back are two grants on two channels. Check the acl_file "
                           f"with `saguin --acl <config> "
                           f"{MQTT_CFG['username'] or '<user>'}`; nothing was requeued.")
        return jsonify(out), status
    # **After the acknowledgement, never before it.** A mark recorded on
    # intention would sit on a job the queue never took, and would then argue
    # against the retry that was actually needed.
    sid = (rec.get("properties") or {}).get("saguin-id")
    # The dead-letter channel it came from, so the mark expires on that
    # channel's own retention rather than on a window this page picked.
    note_redrive(sid, MQTT_CFG.get("username") or "anonymous", holder or "")
    return jsonify({
        "ok": True, "from": topic, "to": back, "queue": dest, "bytes": len(payload),
        "id": sid,
        "reason": resp.get("reason"),
        # Said rather than discovered, because both are surprising if you have
        # not read RFC 0003 and both matter to whoever is watching the queue.
        "note": "back as a new record: a new offset and an attempt count starting "
                "at 1. Its saguin-id is carried over, so a consumer can tell it is "
                "the same work returning." if (rec.get("properties") or {}).get("saguin-id")
        else "back as a new record: a new offset and an attempt count starting at 1. "
             "It carried no saguin-id, so downstream it is indistinguishable from "
             "work that had never failed.",
    })


@app.post("/api/seek")
def api_seek():
    """Move this viewer's own position on one append channel.

    A seek is an ordinary publish to a reserved topic, with a Response Topic
    for the answer - there is no saguin API and no client library.
    """
    body = request.get_json(silent=True) or {}
    channel = str(body.get("channel", ""))
    position = str(body.get("position", "")).strip()
    with lock:
        kind = channels.get(channel, (None, ""))[0]
    if kind != "append":
        return jsonify({"ok": False,
                        "error": f"{channel!r} is {kind or 'not a channel'} here; only an "
                                 "append channel has a position to move"}), 400
    if not position:
        return jsonify({"ok": False, "error": "no position given"}), 400

    ticket = os.urandom(6).hex()
    props = None
    if PROTOCOL == mqtt.MQTTv5:
        props = Properties(PacketTypes.PUBLISH)
        props.ResponseTopic = REPLY_TOPIC
        props.CorrelationData = ticket.encode()

    # **Only this channel's topics are forgotten, and only on the page.**
    # The previous version cleared every topic the viewer had ever seen,
    # including channels the seek could not touch - so a replay of one
    # channel looked like it had emptied the others. Nothing here changes
    # what the broker holds.
    with lock:
        for name in [n for n, t in topics.items() if t["channel"] == channel]:
            del topics[name]

    info = client.publish(f"$saguin/consumer/{channel}/seek", position.encode(),
                          qos=1, properties=props)
    if PROTOCOL != mqtt.MQTTv5:
        # **A 3.1.1 seek is accepted and cannot be answered.** There is no
        # Response Topic to carry the reply and no reason code on a 3.1.1
        # PUBACK to carry a refusal, so what the broker made of it is
        # visible only in what arrives next. Saying so is better than
        # waiting five seconds for a reply that cannot come and then
        # reporting a timeout, which would read as a broker that ignored it.
        try:
            info.wait_for_publish(timeout=5)
        except Exception as e:                 # noqa: BLE001 - shown, not hidden
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 502
        return jsonify({"ok": True, "channel": channel, "position": position,
                        "reply": "sent - MQTT 3.1.1 carries no reply to a seek, "
                                 "so watch what arrives"})
    try:
        info.wait_for_publish(timeout=5)
    except Exception as e:                     # noqa: BLE001 - shown, not hidden
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 502

    deadline = time.time() + 5
    while time.time() < deadline:
        with lock:
            reply = seeks.pop(ticket, None)
        if reply is not None:
            return jsonify({"ok": True, "reply": reply, "position": position,
                            "channel": channel})
        time.sleep(0.05)
    rc = getattr(info, "rc", None)
    return jsonify({"ok": False, "channel": channel, "position": position,
                    "error": "no reply within 5s" + (f"; PUBACK reason {rc}" if rc else "")}), 504


@app.get("/api/sessions")
def api_sessions():
    """Who is connected, and who is holding a session with nothing behind it.

    **Two numbers cannot answer this.** `saguin_connections` beside
    `saguin_sessions_offline` says three hundred devices and two hundred
    connected, which is either a rota or a hundred that have stopped calling -
    the counts read the same for both. The route names them.

    Positions are deliberately not folded in here. `/v1/operations/consumers`
    already answers them, keyed by the same client id, and the page joins the
    two: a second answer to one question is a second thing to keep in step.
    """
    return jsonify(ops_json("/v1/operations/sessions"))


@app.post("/api/disconnect")
def api_disconnect():
    """Hang up one connected client.

    **It ends the connection and leaves the session alone**, so the device
    reconnects and resumes at its stored position - the whole cost is one
    reconnection. That is what makes it safe to put behind a button. Ending a
    session would take a durable consumer's position with it, and the broker
    does not offer that.

    **It withdraws nothing on its own.** The device comes back with whatever
    the broker's password file and acl_file say then. Taking access away is
    RFC 0002's "Withdrawing a device's access": edit the two files, signal the
    broker with SIGUSR1 to re-read them, then hang the client up so that it
    connects again and is refused - the acl_file is already in force on the
    open connection, the password file is only read at CONNECT. What the
    button is for on its own is a client that is stuck, and on a queue the
    lease it holds goes back when its connection ends.

    **The outcome is the reply, not the acknowledgement.** The broker answers
    on the Response Topic - `hung-up`, or `no-such-client` when nothing is
    connected under that id - because a PUBACK may leave its reason-code byte
    out and an answer that can vanish is not one. This is the same reply path
    a seek uses, ticket and all.
    """
    body = request.get_json(silent=True) or {}
    client_id = str(body.get("client_id", "")).strip()
    if not client_id:
        return jsonify({"ok": False, "error": "name the client to hang up"}), 400
    if PROTOCOL != mqtt.MQTTv5:
        # **Said rather than attempted.** A 3.1.1 connection cannot set a
        # Response Topic, so the broker refuses the request - and because a
        # 3.1.1 PUBACK carries no reason code, that refusal arrives as this
        # viewer's own connection being closed. Trying it would take the page
        # offline to tell the operator it could not be done.
        return jsonify({"ok": False,
                        "error": "this viewer is connected with MQTT 3.1.1, which cannot "
                                 "carry the Response Topic the broker answers on. Set "
                                 "broker.mqtt.protocol to 5"}), 400
    if client_id == MQTT_CFG.get("client_id"):
        # The broker refuses this too, and says why; answered here so the page
        # does not have to survive its own connection being the one at risk.
        return jsonify({"ok": False,
                        "error": f"{client_id!r} is this viewer's own connection. A client "
                                 f"cannot hang itself up - and the page would lose the "
                                 f"answer with the connection"}), 400

    ticket = os.urandom(6).hex()
    props = Properties(PacketTypes.PUBLISH)
    props.ResponseTopic = REPLY_TOPIC
    props.CorrelationData = ticket.encode()

    info = client.publish("$saguin/sessions/disconnect", client_id.encode(),
                          qos=1, properties=props)
    try:
        info.wait_for_publish(timeout=5)
    except Exception as e:                     # noqa: BLE001 - shown, not hidden
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 502

    # **The refusal and the reply race, and either one is the answer.** A
    # refused request never produces a reply, so waiting for one and calling
    # the silence a timeout reports "the broker is slow" about a broker that
    # said no immediately.
    deadline = time.time() + 5
    rc = None
    while time.time() < deadline:
        with lock:
            reply = seeks.pop(ticket, None)
            rc = pubacks.pop(info.mid, rc)
        if reply is not None:
            return jsonify({"ok": True, "client_id": client_id, "reply": reply,
                            "hung_up": reply == "hung-up"})
        if rc is not None and int(getattr(rc, "value", rc)) >= 0x80:
            break
        time.sleep(0.05)

    if rc is not None and int(getattr(rc, "value", rc)) >= 0x80:
        code = int(getattr(rc, "value", rc))
        out = {"ok": False, "client_id": client_id,
               "error": f"the broker refused it: {rc}"}
        if code == 0x87:
            # **One overwhelmingly likely cause, so say it.** `disconnect` is
            # not a channel verb and no topic grant confers it: it takes a
            # `broker: sessions` rule, which is a rule kind an operator has to
            # have written on purpose.
            out["hint"] = (f"hanging a client up needs a `broker: sessions` rule allowing "
                           f"`disconnect`, which is a rule kind of its own - no channel "
                           f"or topic grant confers it, however wide. Check the acl_file "
                           f"with `saguin --acl <config> "
                           f"{MQTT_CFG['username'] or '<user>'}`; nobody was hung up.")
        return jsonify(out), 403 if code == 0x87 else 502

    return jsonify({"ok": False, "client_id": client_id,
                    "error": "no reply within 5s"}), 504

if __name__ == "__main__":
    # **Started here rather than at import**, so that a test can import this
    # module for its parsing and matching without opening a connection to a
    # broker or beginning to scrape one. A module that connects on import is
    # a module whose tests need a broker.
    threading.Thread(target=run, daemon=True).start()
    threading.Thread(target=poll_metrics, daemon=True).start()

    # **The address comes from the configuration**, and nowhere else: the
    # page shows everything both credentials can read, so which network - or
    # which users, for a Unix socket - can reach it is the one setting an
    # operator cannot afford to have do nothing.
    http_server(CONFIG).serve_forever()
