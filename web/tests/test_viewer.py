#!/usr/bin/env python3
"""What this viewer claims, run rather than written once.

Every case here is a defect this viewer actually had. Two of them shipped:
the label parser cut a `{a,b}` filter in half, and a sample with no labels
was dropped entirely - so the page showed channels and no connection count.
"""

import collections
import io
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.request

import yaml

HERE = pathlib.Path(__file__).resolve().parent      # …/web/tests
VIEWER = HERE.parent                                # …/web
REPO = VIEWER.parent                                # the repository root

# Where the viewer sits inside the repository, which is the prefix every COPY
# in the Dockerfile is written with. Derived rather than spelled out, so that
# moving this directory again is one edit here rather than four below.
PREFIX = VIEWER.relative_to(REPO).as_posix() + "/"

# **saguin's own checkout, which is no longer this repository.** The viewer is
# a client of any broker, so the tests that need a real one - `--route`, which
# answers where a topic lands - cannot be served from here. SAGUIN_REPO names
# the checkout; a sibling directory is what a contributor usually has, so it
# is the fallback rather than a requirement.
SAGUIN = pathlib.Path(os.environ.get("SAGUIN_REPO") or REPO.parent / "saguin")

# Imported with no arguments, so the defaults are what is under test and no
# connection is opened. app.py starts its threads only under __main__.
#
# **The path is added here rather than relied on**, so that this file runs
# the same whether it is discovered from the viewer's directory, from the
# repository root, or by a runner that sets none of it up.
sys.argv = ["app"]
sys.path.insert(0, str(VIEWER))
import app  # noqa: E402


class TheConfigurationExample(unittest.TestCase):
    """The file shipped beside this one, loaded the way the viewer loads it.

    A configuration example nobody parses is a file that drifts from the
    program it configures, and the drift is silent: it still looks like
    YAML.
    """

    def load(self, env=None):
        import yaml
        old = dict(os.environ)
        os.environ.update(env or {})
        try:
            raw = yaml.safe_load((VIEWER / "saguin-viewer.yaml").read_text())
            return app.merge(app.DEFAULTS, raw)
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_it_parses_and_every_key_is_known(self):
        cfg = self.load()
        self.assertEqual(app.bind_target(cfg), ("tcp", "127.0.0.1", 8080, None))
        self.assertEqual(cfg["broker"]["mqtt"]["host"], "127.0.0.1")

    def test_the_commented_unix_block_is_the_one_the_file_says_to_uncomment(self):
        """The `unix` block ships commented out, so nothing above proves it
        is valid yaml with the right keys. Swapped the way the comment
        beside it says - `tcp` out, `unix` in - it serves on the socket;
        with both in, the viewer refuses rather than picking one."""
        import yaml
        text = (VIEWER / "saguin-viewer.yaml").read_text()
        both = text.replace("\n  # unix:", "\n  unix:").replace("\n  #   ", "\n    ")
        self.assertNotEqual(both, text, "the unix block is not there to uncomment")
        with self.assertRaises(SystemExit) as caught:
            app.bind_target(app.merge(app.DEFAULTS, yaml.safe_load(both)))
        self.assertIn("one door or the other", str(caught.exception))
        swapped = both.replace("\n  tcp:", "\n  # tcp:").replace("\n    address:", "\n  #   address:")
        cfg = app.merge(app.DEFAULTS, yaml.safe_load(swapped))
        self.assertEqual(app.bind_target(cfg), ("unix", "/run/saguin-viewer.sock", 0, 0o660))

    def test_types_survive_the_environment(self):
        """A variable arrives as a string, and two of those strings are traps.

        `false` is a non-empty string and therefore true, so a TLS block
        written `${VIEWER_TLS:-false}` would silently turn TLS on.
        """
        cfg = self.load()
        self.assertIsInstance(cfg["broker"]["mqtt"]["port"], int)
        self.assertIsInstance(cfg["broker"]["mqtt"]["keepalive"], int)
        self.assertIsInstance(cfg["messages_per_topic"], int)
        self.assertIs(cfg["broker"]["mqtt"]["tls"]["enabled"], False)
        self.assertIs(cfg["broker"]["operations"]["tls"]["insecure_skip_verify"], False)

    def test_an_exported_variable_wins_and_keeps_its_type(self):
        cfg = self.load({"SAGUIN_MQTT_PORT": "8883", "SAGUIN_MQTT_TLS": "true"})
        self.assertEqual(cfg["broker"]["mqtt"]["port"], 8883)
        self.assertIs(cfg["broker"]["mqtt"]["tls"]["enabled"], True)

    def test_an_unknown_key_is_refused_rather_than_ignored(self):
        with self.assertRaises(SystemExit):
            app.merge(app.DEFAULTS, {"lisen": "127.0.0.1:1"})

    def test_a_strict_variable_that_is_unset_stops_the_viewer(self):
        """An empty password is a credential that fails at the broker, and
        sends somebody to look at the broker."""
        with self.assertRaises(SystemExit):
            app.expand_env("${DEFINITELY_NOT_SET_ANYWHERE}", "broker.operations.password")

    def test_a_fallback_is_taken_when_the_variable_is_unset(self):
        self.assertEqual(app.expand_env("${ALSO_NOT_SET:-fallback}", "k"), "fallback")


class WhereThePageIsServed(unittest.TestCase):
    """`listen` has a saguin listener's shape - a `tcp` block or a `unix`
    block - but one of them: a `listen` naming both doors stops the viewer
    at startup and names the keys, and one naming neither is loopback.

    The socket case is proved by serving over one: a real GET through
    AF_UNIX, against the mode the file asked for. The path is a short one
    under /tmp rather than the test's own directory, because a Unix socket
    path is limited to about a hundred bytes and a checkout can sit deeper
    than that.
    """

    def cfg(self, address="", path="", mode="0660"):
        return dict(app.DEFAULTS, listen={"tcp": {"address": address},
                                          "unix": {"path": path, "mode": mode}})

    def test_a_tcp_address_a_bare_port_and_no_door_at_all(self):
        self.assertEqual(app.bind_target(self.cfg(address="192.0.2.7:9000")),
                         ("tcp", "192.0.2.7", 9000, None))
        # A file naming no door - one that only sets `feed_messages`, say -
        # is a viewer on loopback, not a refusal.
        self.assertEqual(app.bind_target(self.cfg()),
                         ("tcp", "127.0.0.1", 8080, None))
        self.assertEqual(app.bind_target(self.cfg(address=":9000")),
                         ("tcp", "127.0.0.1", 9000, None))

    def test_a_unix_socket_takes_the_mode_written(self):
        self.assertEqual(app.bind_target(self.cfg(path="/run/v.sock")),
                         ("unix", "/run/v.sock", 0, 0o660))
        self.assertEqual(app.bind_target(self.cfg(path="/run/v.sock", mode="0600")),
                         ("unix", "/run/v.sock", 0, 0o600))

    def test_both_doors_neither_door_and_the_wrong_shape_are_refused_by_name(self):
        for address, path, mode, names in (
                ("127.0.0.1:8080", "/run/v.sock", "0660", "one door or the other"),
                ("localhost", "", "0660", "listen.tcp.address"),
                ("127.0.0.1:http", "", "0660", "listen.tcp.address"),
                ("", "/run/v.sock", "rw-rw----", "listen.unix.mode")):
            with self.subTest(address=address, path=path, mode=mode):
                with self.assertRaises(SystemExit) as caught:
                    app.bind_target(self.cfg(address, path, mode))
                self.assertIn(names, str(caught.exception))

    def test_the_page_is_served_over_the_socket_with_the_mode_asked_for(self):
        import http.client
        import socket
        import stat
        import threading

        tmp = tempfile.mkdtemp(prefix="sv-", dir="/tmp")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "v.sock")
        # A stale socket file from an earlier run is unlinked, not tripped
        # over: a viewer that died leaves one behind.
        open(path, "w").close()

        server = app.http_server(self.cfg(path=path, mode="0600"))
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

        class Over(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX)
                self.sock.connect(path)

        conn = Over("localhost")
        # The page itself, which needs no broker; the api routes proxy one.
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200, body[:200])
        self.assertIn(b"saguin", body)


class TheDoorTheBrowserCameThrough(unittest.TestCase):
    """A page on another site must not be able to drive this viewer.

    An attacker's domain that re-resolves to 127.0.0.1 is *same-origin* to a
    viewer on loopback: no preflight, none of the cross-site rules, and the
    operator's own credentials behind every call. The one thing it cannot
    forge is the `Host` header, which carries the name the browser was
    loaded by - so the TCP door answers to an address and refuses a name.
    """

    def setUp(self):
        self.c = app.app.test_client()

    def served(self, path, host, **headers):
        """The status of a request that gets through, response closed.

        Closed because `/` is a file: an unclosed one turns every run of the
        whole suite into a page of ResourceWarnings, and a suite whose
        output nobody reads is a suite that reports nothing.
        """
        r = self.c.get(path, headers=dict(headers, Host=host))
        try:
            return r.status_code
        finally:
            r.close()

    def test_an_address_is_served_and_a_name_is_refused(self):
        for host in ("127.0.0.1:8080", "localhost:8080", "localhost",
                     "[::1]:8080", "192.0.2.9:8080", "198.51.100.7"):
            with self.subTest(host=host):
                self.assertEqual(self.served("/api/state", host), 200)
        # The page itself and not only the api, because a rebinding page
        # reading app.js is how it learns which verbs are here.
        self.assertEqual(self.served("/", "127.0.0.1:8080"), 200)
        for host in ("evil.example", "evil.example:8080", "viewer.example.com",
                     "localhost.evil.example", "127.0.0.1.evil.example"):
            with self.subTest(host=host):
                r = self.c.get("/", headers={"Host": host})
                self.assertEqual(r.status_code, 403)
                # The refusal says what happened and where the other door is,
                # rather than leaving an operator to guess at a 403.
                said = r.get_json()["error"]
                self.assertIn(host, said)
                self.assertIn("listen.unix", said)

    def test_a_foreign_origin_is_refused_even_when_the_name_is_an_address(self):
        # The half a browser sends when a page on another site reaches an
        # address directly. It is refused today by accident - every verb
        # reads its body as JSON and cross-site JSON needs a preflight this
        # viewer does not answer - and an accident is not a defence.
        r = self.c.post("/api/keep", json={"keep": 100},
                        headers={"Host": "127.0.0.1:8080",
                                 "Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("evil.example", r.get_json()["error"])
        # The viewer's own page, which is what an operator's browser sends.
        self.assertEqual(
            self.c.post("/api/keep", json={"keep": 100},
                        headers={"Host": "127.0.0.1:8080",
                                 "Origin": "http://127.0.0.1:8080"}).status_code,
            200)

    def test_every_route_is_behind_it_including_the_ones_added_later(self):
        """Counted from the app's own map, so a verb added tomorrow is covered.

        A test naming today's routes passes over the one somebody adds next
        week, which is exactly the route nobody thought to check.
        """
        rules = [r for r in app.app.url_map.iter_rules()
                 if "GET" in r.methods or "POST" in r.methods]
        self.assertGreater(len(rules), 20, "the route map did not load")
        checked = 0
        for rule in rules:
            # The static catch-all takes a filename; every other rule here is
            # a fixed path.
            path = "/index.html" if rule.arguments else rule.rule
            method = "POST" if "POST" in rule.methods else "GET"
            with self.subTest(path=path, method=method):
                r = self.c.open(path, method=method, json={},
                                headers={"Host": "evil.example"})
                self.assertEqual(r.status_code, 403)
            checked += 1
        self.assertEqual(checked, len(rules))

    def test_the_socket_door_is_not_checked_because_no_browser_can_reach_one(self):
        # A proxy in front of the socket sends whatever name it was asked
        # for, and that deployment is the answer to reaching the page by a
        # name - so checking the header there would refuse the one shape
        # this viewer offers for it.
        with unittest.mock.patch.object(app, "DOOR", "unix"):
            self.assertEqual(self.served("/", "viewer.example.com",
                                         Origin="https://viewer.example.com"), 200)


class TheMetricsParser(unittest.TestCase):
    """Both of these were shipped defects, and both were silent."""

    def test_a_brace_filter_survives(self):
        """`{a,b}` is configuration syntax carried in a label value, so the
        obvious parser - find `}`, split on commas - cuts the filter in
        half. Every topic-to-channel decision is made against these."""
        line = ('saguin_channel_info{channel="w",filter="iot/+/{status,location}/+",'
                'type="append",provider="mem"} 1')
        (name, labels, value), = app.parse_metrics(line)
        self.assertEqual(name, "saguin_channel_info")
        self.assertEqual(labels["filter"], "iot/+/{status,location}/+")
        self.assertEqual(labels["type"], "append")
        self.assertEqual(value, 1.0)

    def test_a_sample_with_no_labels_is_not_dropped(self):
        """Reading from the start of the line takes the *name* as the value.
        Every unlabelled gauge went missing this way while every labelled one
        arrived - so the page showed channels and no connection count."""
        self.assertEqual(app.parse_metrics("saguin_connections 1"),
                         [("saguin_connections", {}, 1.0)])
        self.assertEqual(app.parse_metrics("saguin_uptime_seconds 63.604"),
                         [("saguin_uptime_seconds", {}, 63.604)])

    def test_help_and_type_lines_are_skipped(self):
        self.assertEqual(app.parse_metrics("# HELP saguin_connections Connected now."), [])

    def test_an_unparseable_line_is_skipped_not_fatal(self):
        """A line this does not understand is far more likely to be a metric
        added later than a broker gone wrong."""
        self.assertEqual(app.parse_metrics("saguin_future_metric{a=\"b\"} not-a-number"), [])

    def test_the_catalogue_reads_a_dead_letter_channel(self):
        """Nothing configured it, so nothing else could have listed it - and
        nothing else could have said where its topics are."""
        body = "\n".join([
            'saguin_channel_info{channel="jobs",filter="iot/+/work/+",type="queue",provider="mem"} 1',
            'saguin_channel_info{channel="jobs__dlq",filter="iot/+/work/+/__dlq",type="append",provider="mem"} 1',
            'saguin_queue_depth{channel="jobs"} 4',
            'saguin_channel_records{channel="jobs__dlq"} 2',
        ])
        found, held = app.catalogue(app.parse_metrics(body))
        self.assertEqual(found["jobs__dlq"], ("append", "iot/+/work/+/__dlq"))
        self.assertEqual(held["jobs"], 4)     # unresolved work, not a record count
        self.assertEqual(held["jobs__dlq"], 2)


class WhatTheCatalogueSaysEachMetricMeans(unittest.TestCase):
    """The dashboard shows the broker's own `# HELP` line on each card
    rather than a glossary written here, so this reads the same exposition
    format the sample parser does - where two defects have already lived."""

    def test_the_description_is_the_rest_of_the_line(self):
        help = app.parse_help(
            "# HELP saguin_connections Clients connected now.\n"
            "# TYPE saguin_connections gauge\n"
            "saguin_connections 8\n")
        self.assertEqual(help, {"saguin_connections": "Clients connected now."})

    def test_a_description_carrying_punctuation_survives_whole(self):
        """These sentences carry braces, quotes and dashes - RFC 0005
        writes them that way - and a parser that split on any of them would
        show half a sentence."""
        line = ('# HELP saguin_channel_info Always 1. Carries the filter as '
                'written, `iot/+/{status,location}/+` included \u2014 and "type".')
        (name, text), = app.parse_help(line).items()
        self.assertEqual(name, "saguin_channel_info")
        self.assertTrue(text.endswith('and "type".'))
        self.assertIn("{status,location}", text)

    def test_a_line_with_no_description_is_not_a_metric_with_an_empty_one(self):
        """An empty sentence would draw a marker that says nothing when
        hovered, which is worse than no marker."""
        self.assertEqual(app.parse_help("# HELP saguin_odd\n"), {})
        self.assertEqual(app.parse_help("# TYPE saguin_odd counter\n"), {})
        self.assertEqual(app.parse_help("saguin_odd 1\n"), {})


class ThePayloadRenderer(unittest.TestCase):
    def test_json_is_parsed_and_the_bytes_are_kept(self):
        p = app.render_payload(b'{"a": 1}')
        self.assertEqual(p["kind"], "json")
        self.assertEqual(p["value"], {"a": 1})
        self.assertEqual(p["bytes"], 8)

    def test_text_that_is_not_json_is_still_text(self):
        self.assertEqual(app.render_payload(b"hello")["kind"], "text")

    def test_something_that_starts_like_json_and_is_not(self):
        """Shown as text rather than as a failure: it arrived perfectly and
        merely is not JSON."""
        self.assertEqual(app.render_payload(b"{not json")["kind"], "text")

    def test_bytes_are_shown_as_hex_rather_than_as_an_error(self):
        p = app.render_payload(bytes([0xff, 0xfe]))
        self.assertEqual(p["kind"], "bytes")
        self.assertEqual(p["hex"], "fffe")

    def test_a_zero_length_payload_is_named_not_blank(self):
        """An empty box would hide the only thing that happened - and what it
        means depends on where it landed.

        **The note names the mechanism that applies, or none.** It used to say
        "a delete on a latest channel" over every zero-length payload,
        including broadcast topics that have no latest channel anywhere near
        them: a reader then goes looking for a channel that is not there. On
        broadcast the same publish clears the *retained value*, which deletes
        no message at all.
        """
        for kind, want in (("latest", "delete"),
                           ("broadcast", "retained"),
                           ("append", "empty payload"),
                           ("", "empty payload")):
            with self.subTest(kind=kind):
                p = app.render_payload(b"", "", kind)
                self.assertEqual(p["kind"], "empty")
                self.assertEqual(p["bytes"], 0)
                self.assertIn(want, p["why"])
        # And the one that must never come back: a broadcast topic told about
        # a channel type it has nothing to do with.
        self.assertNotIn("latest", app.render_payload(b"", "", "broadcast")["why"])


class WhenTheValueExpires(unittest.TestCase):
    """RFC 0003: a delivery carries the seconds remaining as of that
    delivery, so a page showing the number shows one that was true when the
    message arrived and is wrong by however long the window has been open.
    The absolute moment is the same fact and stays true."""

    class Msg:
        def __init__(self, left):
            self.properties = unittest.mock.Mock(MessageExpiryInterval=left)

    def test_the_countdown_becomes_a_moment(self):
        self.assertEqual(app.message_expires_at(self.Msg(60), 1_000_000.0),
                         1_000_060.0)

    def test_the_brokers_own_property_wins(self):
        """A channel delivery carries `saguin-expires`, an absolute moment
        that is there whether or not the deadline has passed. MQTT's own
        countdown cannot answer the passed case at all, so where both are
        present the broker's is the one to read."""
        got = app.message_expires_at(self.Msg(60), 1_000_000.0,
                                     {"saguin-expires": "1500000000000"})
        self.assertEqual(got, 1_500_000_000.0)

    def test_an_expired_record_still_has_a_moment(self):
        """The case the property exists for: the standard field is gone
        because the countdown reached zero, and the record is still served."""
        got = app.message_expires_at(self.Msg(None), 1_000_000.0,
                                     {"saguin-expires": "999000000000"})
        self.assertEqual(got, 999_000_000.0)

    def test_a_value_the_page_cannot_read_is_not_guessed_at(self):
        """The broker writes these, so a value that will not parse is a
        broker that changed shape. Inventing a moment would hide it."""
        self.assertIsNone(app.message_expires_at(self.Msg(None), 1_000_000.0,
                                                 {"saguin-expires": "soon"}))

    def test_a_publisher_that_set_none_gets_no_deadline(self):
        """Absent is not zero. A value with no expiry does not expire on the
        publisher's clock at all, and rendering that as "expires now" would
        invent a deadline the publisher never asked for."""
        self.assertIsNone(app.message_expires_at(self.Msg(None), 1_000_000.0))

    def test_a_message_with_no_properties_at_all(self):
        """A 3.1.1 delivery, which carries no properties - so this is asked of
        every message the viewer receives and must not raise on one."""
        bare = unittest.mock.Mock(spec=[])
        self.assertIsNone(app.message_expires_at(bare, 1_000_000.0))


class ThePublishFormsMessageExpiry(unittest.TestCase):
    """The other half of showing an expiry: being able to send one, so the
    behaviour can be driven from the page that displays it.

    The publish itself is not exercised here - it opens a connection of its
    own - so these drive the validation, which is the part that decides what
    reaches the wire."""

    def setUp(self):
        self.c = app.app.test_client()

    def post(self, **kw):
        body = {"topic": "state/lamp", "payload": "on"}
        body.update(kw)
        return self.c.post("/api/publish", json=body)

    def test_a_number_that_is_not_one_is_refused_rather_than_ignored(self):
        r = self.post(message_expiry="soon")
        self.assertEqual(r.status_code, 400)
        self.assertIn("whole number", r.get_json()["error"])

    def test_past_the_field_is_refused(self):
        """Unsigned 32 bits is MQTT's own bound. Sent anyway it wraps, so a
        publisher asking for a very long life would get a very short one."""
        r = self.post(message_expiry=2 ** 32)
        self.assertEqual(r.status_code, 400)
        self.assertIn("4294967295", r.get_json()["error"])

    def test_a_negative_expiry_is_refused(self):
        self.assertEqual(self.post(message_expiry=-1).status_code, 400)

    def test_311_is_told_rather_than_sent_something_else(self):
        """The rule the content type and the user properties already follow:
        3.1.1 has no such field, so a form that sent the message without it
        would acknowledge a publish that is not the one asked for."""
        with unittest.mock.patch.object(app, "PROTOCOL", 4):
            r = self.post(message_expiry=60)
        self.assertEqual(r.status_code, 400)
        self.assertIn("3.1.1", r.get_json()["error"])

    def test_the_property_reaches_the_packet(self):
        """What the form is for. Asserted on the properties handed to the
        publish rather than on the reply, because the reply says what the
        broker made of it and this is about what was sent."""
        seen = {}

        def fake(topic, payload, qos, retain, props):
            seen["props"] = props
            return {"ok": True}, 200

        with unittest.mock.patch.object(app, "publish_once", fake):
            r = self.post(message_expiry=60)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen["props"].MessageExpiryInterval, 60)

    def test_no_expiry_sets_no_property(self):
        """Absent rather than zero, which is the same distinction the page
        makes when it renders one: a publish that set no expiry must not
        arrive claiming one."""
        seen = {}

        def fake(topic, payload, qos, retain, props):
            seen["props"] = props
            return {"ok": True}, 200

        with unittest.mock.patch.object(app, "publish_once", fake):
            self.post()
        self.assertFalse(hasattr(seen["props"], "MessageExpiryInterval"))


class BraceExpansion(unittest.TestCase):
    def test_a_brace_filter_expands_before_it_reaches_the_wire(self):
        """A brace is configuration syntax and not MQTT: subscribing with one
        is granted, treated as a literal level, and matches nothing for ever."""
        self.assertEqual(sorted(app.expand("iot/+/{status,location}/+")),
                         ["iot/+/location/+", "iot/+/status/+"])

    def test_a_plain_filter_is_left_alone(self):
        self.assertEqual(app.expand("iot/+/events/+"), ["iot/+/events/+"])


class WhereATopicLands(unittest.TestCase):
    """The viewer's matching, held to the broker's own answer.

    **It is a mirror and the broker is authoritative.** `saguin --route`
    calls the functions the broker calls; this asserts the two agree, so the
    day they stop agreeing a test says so rather than a badge being quietly
    wrong about which channel holds a topic.
    """

    CONFIG = """
broker:
  id: route-test
  mqtt:
    listen:
      tcp:
        address: 127.0.0.1:1
  storage:
    default: mem
    default_retention_period: none
    default_retention_bytes: none
    providers:
      mem:
        type: memory
        snapshot_dir: none
channels:
  wide:
    type: append
    filter: iot/#
  exact:
    type: latest
    filter: iot/depot/state/+
  plus:
    type: append
    filter: iot/+/events/+
  jobs:
    type: queue
    filter: iot/depot/work/+
"""
    TOPICS = ["iot/depot/state/d1", "iot/depot/events/e1", "iot/other/events/e2",
              "iot/depot/work/w1", "iot/depot/anything/else", "elsewhere/entirely"]

    # **Why, not whether.** A SkipTest raised in setUpClass records one skip for
    # the whole class and leaves its methods uncounted, so a degraded run reported
    # "skipped=2" where nine methods had not run - the count beside an honest
    # sentence saying the opposite of the sentence. The decision is made here and
    # taken in setUp, one method at a time, so the number unittest prints is the
    # number of cases that did not run. The expensive part still happens once.
    why = None

    def setUp(self):
        if self.why:
            self.skipTest(self.why)

    @classmethod
    def setUpClass(cls):
        cls.binary = SAGUIN / "bin" / "saguin"
        if not cls.binary.exists():
            cls.why = (f"no {cls.binary} - this needs saguin's own checkout: build "
                       f"it there with `go build -o bin/saguin ./cmd/saguin`, and "
                       f"set SAGUIN_REPO if it is not beside this one")
            return
        cls.dir = tempfile.mkdtemp()
        cls.path = pathlib.Path(cls.dir) / "route.yaml"
        cls.path.write_text(cls.CONFIG)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "dir"):
            shutil.rmtree(cls.dir, ignore_errors=True)

    def broker_says(self):
        """Every topic in one call, on standard input.

        **The list form, not the single-topic one.** Given one topic on the
        command line `--route` explains it in prose for a person; given a
        list on standard input it answers a line at a time in tab-separated
        columns, which is the form meant to be read by something else.
        """
        out = subprocess.run([str(self.binary), "--route", str(self.path)],
                             input="\n".join(self.TOPICS) + "\n",
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        answers = {}
        for line in out.stdout.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            cols = [c for c in line.split("\t") if c] if "\t" in line else line.split()
            if len(cols) >= 2 and cols[0] in self.TOPICS:
                answers[cols[0]] = None if cols[1] == "-" else cols[1]
        missing = set(self.TOPICS) - set(answers)
        self.assertFalse(missing, f"--route said nothing about {missing}: {out.stdout!r}")
        return answers

    def test_the_viewer_agrees_with_the_broker_on_every_topic(self):
        app.channels.clear()
        app.channels.update({
            "wide": ("append", "iot/#"),
            "exact": ("latest", "iot/depot/state/+"),
            "plus": ("append", "iot/+/events/+"),
            "jobs": ("queue", "iot/depot/work/+"),
        })
        answers = self.broker_says()
        for topic in self.TOPICS:
            with self.subTest(topic=topic):
                self.assertEqual(
                    app.channel_of(topic), answers[topic],
                    f"the viewer and `saguin --route` disagree about {topic}")


class TheChannelTableIsReadUnderTheLock(unittest.TestCase):
    """`channel_of` survives the poll thread replacing the channel table.

    Every scrape the poll thread does `channels.clear()` then
    `channels.update(found)` under `lock`. Two readers walked that table
    without taking it: `on_message`, which is where a record's channel and the
    kind that sizes its ring are decided, and `/api/route`. A replacement
    landing mid-iteration raises `RuntimeError: dictionary changed size during
    iteration` in CPython - out of paho's loop in the first case, which costs a
    reconnect and a replay, and a 500 in the second.

    **Driven rather than reasoned about**, which is why it runs many rounds:
    the real window is one dict-replace a minute against message arrival, and a
    probe cannot hit that on demand. Here the replacement runs flat out beside
    the reader, so the unguarded version fails within a few rounds.
    """

    TABLE = {"state": ("latest", "iot/depot/state/+"),
             "plus": ("append", "iot/+/events/+"),
             "jobs": ("queue", "iot/depot/work/+")}

    def setUp(self):
        with app.lock:
            self.saved = dict(app.channels)
            app.channels.clear(); app.channels.update(self.TABLE)

    def tearDown(self):
        with app.lock:
            app.channels.clear(); app.channels.update(self.saved)

    def test_a_reader_survives_the_table_being_replaced_under_it(self):
        import threading
        stop = threading.Event()
        errors = []

        def churn():
            while not stop.is_set():
                with app.lock:
                    app.channels.clear()
                    app.channels.update(self.TABLE)

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        reads = 0
        try:
            for _ in range(20000):
                try:
                    self.assertEqual(app.channel_of("iot/depot/events/e1"), "plus")
                    reads += 1
                except Exception as e:            # noqa: BLE001 - the point is which
                    errors.append(f"{type(e).__name__}: {e}")
                    break
        finally:
            stop.set(); t.join(timeout=5)
        self.assertEqual(errors, [], "channel_of read the table while it was being replaced")
        self.assertEqual(reads, 20000, "the sweep did not run every round it claims")


class APayloadThatIsOnlyANumber(unittest.TestCase):
    """A sensor publishing `28.89` on its own topic is the commonest thing
    on any broker, and the viewer skipped it: it looked inside JSON objects
    and decoded payloads and nowhere else."""

    def test_the_whole_payload_is_the_number(self):
        self.assertEqual(app.as_number("28.89"), 28.89)
        self.assertEqual(app.as_number("  12  "), 12.0)
        self.assertEqual(app.as_number("-3"), -3.0)

    def test_a_number_inside_a_sentence_is_not_a_measurement(self):
        """Picking one number out of a line is how a chart ends up plotting
        a device id."""
        self.assertIsNone(app.as_number("pressure 28.89 psi"))
        self.assertIsNone(app.as_number("device-01"))
        self.assertIsNone(app.as_number(""))
        self.assertIsNone(app.as_number(None))

    def test_nan_and_infinity_are_not_points_on_a_scale(self):
        """Both parse as floats and neither can be written as JSON, so one
        reaching the answer fails the whole chart request rather than one
        reading - the failure would be a chart that stopped drawing."""
        for text in ("nan", "NaN", "inf", "-inf", "Infinity"):
            with self.subTest(text=text):
                self.assertIsNone(app.as_number(text))
        self.assertEqual(json.dumps(app.numeric_fields({"a": float("nan"),
                                                        "b": 1.5})),
                         '{"b": 1.5}')

    def test_a_boolean_is_left_out_and_an_int64_string_is_not(self):
        """Charting a flag as 0 and 1 draws a line between two states that
        were never on a scale. A protobuf int64 arrives as a string because
        JSON cannot hold one exactly, and every timestamp would be
        unplottable if that were left out."""
        fields = app.numeric_fields({"occupied": True, "co2": 439,
                                     "timestamp_ms": "1788195023591"})
        self.assertEqual(sorted(fields), ["co2", "timestamp_ms"])


class SearchingWhatAMessageHolds(unittest.TestCase):
    """The search runs over every arrival the page holds, in the backend, so
    it is not limited to the cards that happen to be rendered."""

    RAW = {"topic": "iot/room/reading/room-01",
           "content_type": "",
           "properties": {"schema": "schemas/iot/room_reading/v1",
                          "msg_type": "room_reading"},
           "payload": {"kind": "bytes", "bytes": 4, "hex": "0e726f6f"}}
    JSON = {"topic": "t", "content_type": "application/json", "properties": {},
            "payload": {"kind": "json", "bytes": 9,
                        "value": {"device": "trk-9", "temperature": 26.1}}}

    def test_it_reads_the_bytes_the_properties_and_the_parsed_json(self):
        self.assertIn("0e726f", app.haystack(self.RAW, decode=False))
        self.assertIn("msg_type=room_reading", app.haystack(self.RAW, decode=False))
        self.assertIn("trk-9", app.haystack(self.JSON, decode=False))
        self.assertIn("26.1", app.haystack(self.JSON, decode=False))

    def test_the_topic_is_left_out(self):
        """Every message in this view is on the same topic, so including it
        would mean a search matching all of them or none."""
        self.assertNotIn("room-01", app.haystack(self.RAW, decode=False))

    def test_the_decoded_record_counts_only_while_decoding_is_on(self):
        """Matching text that is nowhere on the screen would hide a message
        for a reason the page does not show, which reads as a broken search
        rather than a strict one.

        **The gate is what this proves, so it stands in for the decoder.**
        Written against real avro it skipped wherever fastavro was absent -
        which is a clean checkout - so the rule somebody asked for was
        covered on the machine that wrote it and nowhere else."""
        msg = {"topic": "iot/room/reading/room-77", "content_type": "",
               "properties": {"schema": "schemas/iot/room_reading/v1"},
               "payload": {"kind": "bytes", "bytes": 3, "hex": "abcdef"}}
        text, decoded = app.schema_text_for, app.decode_with_schema
        app.schema_text_for = lambda topic, **kw: ("{}", None)
        app.decode_with_schema = lambda raw, ctype, topic, t: (
            {"device_id": "room-77", "occupied": True},
            {"message": "RoomReading", "format": "application/avro",
             "inferred": True}, None)
        try:
            on = app.haystack(msg, decode=True)
            off = app.haystack(msg, decode=False)
        finally:
            app.schema_text_for, app.decode_with_schema = text, decoded
        self.assertIn("occupied", on)
        self.assertIn("room-77", on)
        self.assertNotIn("occupied", off)
        self.assertIn("abcdef", off)

    def test_an_avro_payload_is_read_through_its_schema(self):
        """The same rule against the real decoder rather than a stand-in.
        This one legitimately needs the library, and says so when it is
        absent - the gate above no longer does."""
        try:
            import fastavro
        except ImportError:                     # pragma: no cover
            self.skipTest("fastavro is not installed")
        schema = {"type": "record", "name": "RoomReading", "namespace": "iot",
                  "fields": [{"name": "device_id", "type": "string"},
                             {"name": "occupied", "type": "boolean"}]}
        buf = io.BytesIO()
        fastavro.schemaless_writer(buf, fastavro.parse_schema(schema),
                                   {"device_id": "room-77", "occupied": True})
        msg = {"topic": "iot/room/reading/room-77", "content_type": "",
               "properties": {"schema": "schemas/iot/room_reading/v1"},
               "payload": {"kind": "bytes", "bytes": len(buf.getvalue()),
                           "hex": buf.getvalue().hex()}}
        original = app.schema_text_for
        app.schema_text_for = lambda topic, **kw: (json.dumps(schema), None)
        try:
            on = app.haystack(msg, decode=True)
            off = app.haystack(msg, decode=False)
        finally:
            app.schema_text_for = original
        self.assertIn("occupied", on)
        self.assertIn("room-77", on)
        self.assertNotIn("occupied", off)


class AMetricWithASecondLabel(unittest.TestCase):
    """`saguin_storage_commits_total` carries `closed_by` as well as
    `provider`, so reading it one row per provider let the last row win. It
    reported zero on a broker that had committed sixty thousand times, and
    the reading built on it - records per commit - was hidden rather than
    wrong, which is worse."""

    CATALOGUE = """
saguin_provider_info{provider="durable",type="sqlite"} 1
saguin_provider_bytes{provider="durable"} 4096
saguin_storage_commits_total{provider="durable",closed_by="records"} 7
saguin_storage_commits_total{provider="durable",closed_by="interval"} 62366
saguin_storage_commits_total{provider="durable",closed_by="unbatched"} 0
saguin_storage_committed_records_total{provider="durable"} 62838
"""

    def test_the_rows_are_added_up_rather_than_the_last_one_kept(self):
        with app.lock:
            app.latest_samples[:] = app.parse_metrics(self.CATALOGUE)
            app.metrics_state.update({"taken": 1.0, "interval": 60,
                                      "error": None, "note": None})
        body = app.app.test_client().get("/api/metrics").get_json()
        durable, = [p for p in body["providers"] if p["name"] == "durable"]
        self.assertEqual(durable["commits"], 7 + 62366 + 0)
        self.assertEqual(durable["records"], 62838)


class TheSubscriptionRefusalsAndTheGoRuntimeReachThePage(unittest.TestCase):
    """The two things RFC 0005 added that the page must carry: a refusal split
    by the code's name, absent until the first, and the Go runtime's ten
    series. The values are the RFC's own example scrape, so this reads a
    figure it did not choose."""

    CATALOGUE = """
saguin_subscriptions_refused_total{reason="topic filter invalid"} 3
saguin_subscriptions_refused_total{reason="not authorized"} 5
saguin_go_heap_live_bytes 1.0514432e+07
saguin_go_heap_goal_bytes 2.1106112e+07
saguin_go_gc_cycles_total 3
saguin_go_gc_cpu_seconds_total 0.004718377
saguin_go_gc_assist_cpu_seconds_total 0.000125117
saguin_go_stack_bytes 917504
saguin_go_allocated_bytes_total 2.4389552e+07
saguin_go_allocated_objects_total 118004
saguin_go_gogc_percent 100
saguin_go_memory_limit_bytes 9.223372036854776e+18
"""

    def body(self, text):
        with app.lock:
            app.latest_samples[:] = app.parse_metrics(text)
            app.metrics_state.update({"taken": 1.0, "interval": 60,
                                      "error": None, "note": None})
        return app.app.test_client().get("/api/metrics").get_json()

    def test_refusals_arrive_by_reason_largest_first(self):
        body = self.body(self.CATALOGUE)
        self.assertEqual(body["subscription_refusals"],
                         [{"reason": "not authorized", "count": 5},
                          {"reason": "topic filter invalid", "count": 3}])

    def test_no_refusal_is_an_empty_list_and_not_a_zero(self):
        self.assertEqual(self.body("saguin_uptime_seconds 1\n")
                         ["subscription_refusals"], [])

    def test_every_go_series_is_read_and_the_counters_keep_history(self):
        body = self.body(self.CATALOGUE)
        b = body["broker"]
        self.assertEqual((b["go_heap_live"], b["go_heap_goal"], b["go_stack"],
                          b["go_gogc"]),
                         (1.0514432e+07, 2.1106112e+07, 917504, 100))
        self.assertGreaterEqual(b["go_memory_limit"], 9.2e18)
        snap = app.snapshot(app.parse_metrics(self.CATALOGUE))
        self.assertEqual((snap["go_gc_cycles"], snap["go_alloc_objects"]),
                         (3, 118004))
        for key in ("go_gc_cpu", "go_gc_assist_cpu", "go_alloc_bytes", "go_stack"):
            self.assertIsNotNone(snap[key], key)

    def test_the_runtime_tab_leads_with_collections_a_second(self):
        cards = [c for c in yaml.safe_load(
            (VIEWER / "dashboards" / "runtime.yaml").read_text())["cards"]
            if c.get("metric")]
        self.assertEqual(cards[0]["metric"], "saguin_go_gc_cycles_total")
        self.assertEqual(cards[0]["mode"], "rate")

    def test_a_limit_of_max_int64_is_drawn_as_none(self):
        js = (VIEWER / "static" / "app.js").read_text()
        self.assertIn('fmt === "bytes_or_none") return v >= 9.2e18 ? "none"', js)


class WhatTheChannelsCardSays(unittest.TestCase):
    """Two things the card was not saying about a broker that was
    answering.

    **Which provider holds a channel, and of which kind.** Both labels are
    already on the wire - the channel's series carries the provider's name
    and the provider's series carries its type - and the page dropped the
    name while building its payload. Without them a row cannot say whether
    what it counts survives a restart.

    **And a queue's holdings.** The column read "not counted" for a queue,
    which was true of `saguin_channel_records` and false of the broker: it
    publishes the depth for every queue channel, zero included. The two are
    different numbers and the row now carries the type that tells them
    apart, so this asserts the depth arrives *and* that it is not one of
    the other numbers published about the same channel.
    """

    CATALOGUE = """
saguin_channel_info{channel="events",filter="iot/+/events/+",type="append",provider="durable"} 1
saguin_channel_info{channel="jobs",filter="iot/+/work/+",type="queue",provider="durable"} 1
saguin_channel_info{channel="state",filter="iot/+/state/+",type="latest",provider="volatile"} 1
saguin_provider_info{provider="durable",type="sqlite"} 1
saguin_provider_info{provider="volatile",type="memory"} 1
saguin_channel_records{channel="events"} 12
saguin_channel_bytes{channel="events"} 1278
saguin_channel_bytes{channel="jobs"} 945
saguin_queue_depth{channel="jobs"} 7
saguin_queue_inflight{channel="jobs"} 2
"""

    def rows(self):
        with app.lock:
            app.latest_samples[:] = app.parse_metrics(self.CATALOGUE)
            app.channels.clear()
            app.channels.update({
                "events": ("append", "iot/+/events/+"),
                "jobs": ("queue", "iot/+/work/+"),
                "state": ("latest", "iot/+/state/+"),
            })
            app.metrics_state.update({"taken": 1.0, "interval": 60,
                                      "error": None, "note": None})
        body = app.app.test_client().get("/api/metrics").get_json()
        by_name = {c["name"]: c for c in body["channels"]}
        # The counter this shape owes: a payload that had lost its channels
        # would agree with every assertion below by having nothing to
        # disagree with.
        self.assertEqual(set(by_name), {"events", "jobs", "state"})
        return by_name

    def test_each_channel_carries_its_provider_and_the_kind_of_store(self):
        by_name = self.rows()
        self.assertEqual((by_name["events"]["provider"],
                          by_name["events"]["provider_type"]),
                         ("durable", "sqlite"))
        self.assertEqual((by_name["state"]["provider"],
                          by_name["state"]["provider_type"]),
                         ("volatile", "memory"))

    def test_a_queue_holds_its_unresolved_work_rather_than_nothing(self):
        by_name = self.rows()
        self.assertEqual(by_name["jobs"]["records"], 7)
        # Not the inflight count and not the byte total: three numbers are
        # published about this channel and only one of them is its depth.
        self.assertNotEqual(by_name["jobs"]["records"], 2)
        self.assertNotEqual(by_name["jobs"]["records"], 945)

    def test_an_append_channel_still_counts_records_rather_than_a_depth(self):
        by_name = self.rows()
        self.assertEqual(by_name["events"]["records"], 12)

    def test_a_latest_channel_stays_blank_rather_than_becoming_a_zero(self):
        """The broker publishes neither number for it. A zero here would
        read as an empty channel rather than as one nobody measures, which
        is the distinction the card's own footnote is about."""
        by_name = self.rows()
        self.assertIsNone(by_name["state"]["records"])
        self.assertIsNone(by_name["state"]["bytes"])


class TheDashboardLoader(unittest.TestCase):
    """The dashboard files the viewer draws as tabs, validated the way it
    validates them. Every case here is one the loader once got wrong: a card it approved that the page then drew wrong or not at
    all, and strictness that stopped at the card rather than the file."""

    def load(self, cards, grid=None, top=None):
        doc = {"title": "T", "cards": cards}
        if grid is not None:
            doc["grid"] = grid
        if top:
            doc.update(top)
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(doc, f)
        try:
            return app.load_dashboard("probe", path)
        finally:
            os.unlink(path)

    def refused(self, cards, grid=None, top=None):
        with self.assertRaises(SystemExit):
            self.load(cards, grid=grid, top=top)

    def test_every_shipped_dashboard_loads(self):
        """Named from the shipped configuration rather than listed here, so a
        dashboard added or renamed is covered without anybody remembering to
        add it - and a tab pointing at a file that is not there is red here
        rather than a viewer that exits on start."""
        raw = yaml.safe_load((VIEWER / "saguin-viewer.yaml").read_text())
        tabs = app.merge(app.DEFAULTS, raw)["dashboard"]
        self.assertTrue(tabs, "the shipped configuration names no dashboards")
        for name, path in tabs.items():
            app.load_dashboard(name, str(VIEWER / path))

    def test_a_stat_may_subtract_single_values_and_nothing_else(self):
        """`minus:` is how the traffic tab draws what shared groups hold now,
        held minus drained minus dropped (RFC 0005), which no one metric
        carries. It takes a list of metrics with a single value, on a stat,
        with no sparkline - each refusal here is one a card could otherwise
        make and then draw wrong."""
        held = "saguin_shares_held_total"
        self.load([{"type": "stat", "metric": held,
                    "minus": ["saguin_shares_drained_total", "saguin_shares_dropped_total"]}])
        for why, card in [
            ("not a list", {"type": "stat", "metric": held, "minus": "saguin_shares_drained_total"}),
            ("an empty list", {"type": "stat", "metric": held, "minus": []}),
            ("a metric the page cannot draw", {"type": "stat", "metric": held, "minus": ["saguin_nothing"]}),
            ("a breakdown-only metric", {"type": "stat", "metric": held, "minus": ["saguin_connections_refused_total"]}),
            ("with a sparkline", {"type": "stat", "metric": "saguin_session_queue_bytes", "sparkline": True,
                                  "minus": ["saguin_shares_drained_total"]}),
            ("on a timeseries", {"type": "timeseries", "metric": "saguin_session_queue_bytes",
                                 "minus": ["saguin_shares_drained_total"]}),
        ]:
            with self.subTest(why):
                self.refused([card])

    def test_a_minus_card_draws_the_difference_or_a_dash(self):
        """The page's own statValue, run by node: held 10, drained 4, dropped 1
        draws 5; any operand unknown draws a dash (null), never a number that
        silently left an operand out; and a stat without `minus` is its metric."""
        if shutil.which("node") is None:
            self.skipTest("no node on PATH - needed to drive static/app.js")
        js = (VIEWER / "static" / "app.js").read_text()
        start = js.index("function statValue(")
        src = js[start:js.index("\n}\n", start) + 3]
        cases = {
            "held": {"metric": "h", "minus": ["r", "p"]},
            "unknown": {"metric": "h", "minus": ["r", "x"]},
            "plain": {"metric": "r"},
        }
        vals = {"h": 10, "r": 4, "p": 1}
        d = tempfile.mkdtemp()
        try:
            f = pathlib.Path(d) / "probe.js"
            f.write_text(src + "\nconst vals = " + json.dumps(vals) + ";\nconst cases = "
                         + json.dumps(cases) + ";\nconst out = {};\n"
                         "for (const k in cases) out[k] = statValue(cases[k], r => r in vals ? vals[r] : null);\n"
                         "process.stdout.write(JSON.stringify(out));")
            run = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            got = json.loads(run.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(got, {"held": 5, "unknown": None, "plain": 4})

    def test_the_shipped_dashboards_draw_every_metric_once(self):
        """**All of them, and none of them twice.** The shipped dashboards are
        a partition of what saguin exports: every family the page can draw
        appears on exactly one tab, so an operator looking for a number has one
        place to look and a metric added to the broker is visibly missing
        rather than quietly absent from all of them.

        The `*_info` families are not in the allow-list and so not here: each
        is the constant 1 with its facts in labels, which is a flat line at one
        on any chart."""
        drawn = {}
        for path in sorted((VIEWER / "dashboards").glob("*.yaml")):
            body = path.read_text()
            # Card lines only. A metric named in a comment is documentation.
            live = "\n".join(l for l in body.splitlines()
                              if not l.strip().startswith("#"))
            for fam in app.ALLOWED_METRICS:
                if re.search(r"\b" + fam + r"\b", live):
                    drawn.setdefault(fam, []).append(path.name)
        missing = sorted(set(app.ALLOWED_METRICS) - set(drawn))
        self.assertEqual(missing, [], "drawn on no dashboard")
        twice = {f: v for f, v in drawn.items() if len(v) > 1}
        self.assertEqual(twice, {}, "drawn on more than one dashboard")

    # The validator must refuse what the renderer cannot draw.

    def test_a_selector_a_metric_ignores_is_refused(self):
        # metricValue reads the aggregate for every non-provider metric, so a
        # {channel=...} selector would be dropped and a fleet total drawn under
        # the operator's per-channel title - a wrong number reported as success.
        self.refused([{"type": "stat",
                       "metric": 'saguin_published_total{channel="x"}'}])

    def test_a_provider_metric_requires_its_selector(self):
        self.refused([{"type": "stat", "metric": "saguin_provider_bytes"}])
        self.load([{"type": "meter",
                    "value": 'saguin_provider_bytes{provider="p"}',
                    "max": 'saguin_provider_max_bytes{provider="p"}'}])

    def test_a_split_of_one_takes_no_selector_and_refuses_one(self):
        """**A breakdown is the other way round**, and it is what lets a
        shipped dashboard draw a per-provider metric at all: a split is over
        every one of them, so it names none - and a dashboard that named one
        would be a dashboard about this broker's providers, empty on anybody
        else's. A selector there asks for a chart of a single bar."""
        self.load([{"type": "breakdown", "metric": "saguin_provider_bytes",
                    "by": "provider"}])
        self.refused([{"type": "breakdown", "by": "provider",
                       "metric": 'saguin_provider_bytes{provider="p"}'}])

    def test_the_readme_number_is_the_allow_list_size(self):
        """The README says how many metrics a card may name. A number written
        in prose is the one kind of claim no test catches by accident - it
        stops being true silently, and every reader after that believes it."""
        # **Every ten the list could plausibly reach**, because the fallback
        # below is `str(tens)`: a map stopping short does not fail, it asks
        # the README for "all 60 of" in prose that spells every other number,
        # and whoever meets that writes the digits to make the test pass.
        words = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
                 60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety"}
        units = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
                 6: "six", 7: "seven", 8: "eight", 9: "nine"}
        n = len(app.ALLOWED_METRICS)
        tens, ones = (n // 10) * 10, n % 10
        spelled = words.get(tens, str(tens))
        if ones:
            spelled += "-" + units[ones]
        readme = (VIEWER / "README.md").read_text()
        self.assertIn(f"all {spelled} of", readme,
                      f"the README does not say there are {n} of them")

    def test_no_shipped_dashboard_names_an_entity(self):
        """The reason the rule above exists. A card naming a provider, a
        channel, a queue or a bridge draws an empty card on every broker but
        the one it was written against - and a dashboard that ships with the
        program is read as an example of what to write."""
        for path in sorted((VIEWER / "dashboards").glob("*.yaml")):
            live = "\n".join(l for l in path.read_text().splitlines()
                              if not l.strip().startswith("#"))
            found = re.findall(r'(provider|channel|queue|bridge)="[^"]*"', live)
            self.assertEqual(found, [], f"{path.name} names one")

    def test_a_breakdown_the_page_cannot_split_is_refused(self):
        # published_total carries a channel label the renderer has no split for;
        # queue_depth is summed rather than split, though the broker labels it.
        self.refused([{"type": "breakdown",
                       "metric": "saguin_published_total", "by": "channel"}])
        self.refused([{"type": "breakdown",
                       "metric": "saguin_queue_depth", "by": "channel"}])

    def test_a_breakdown_only_metric_is_refused_as_a_value(self):
        self.refused([{"type": "stat",
                       "metric": "saguin_connections_by_protocol"}])
        self.refused([{"type": "timeseries",
                       "metric": "saguin_connections_refused_total"}])

    def test_the_documented_cards_still_load(self):
        for fam, by in (("saguin_connections_by_protocol", "protocol"),
                        ("saguin_publish_refused_total", "reason"),
                        ("saguin_connections_refused_total", "reason")):
            self.load([{"type": "breakdown", "metric": fam, "by": by}])
        self.load([{"type": "stat", "metric": "saguin_publish_refused_total"}])

    # Width/height, not the old coordinates.

    def test_width_and_height_are_the_layout(self):
        self.load([{"type": "stat", "metric": "saguin_connections",
                    "width": 3, "height": "small"}])

    def test_the_old_size_and_at_are_refused(self):
        self.refused([{"type": "stat", "metric": "saguin_connections",
                       "size": [3, 1]}])
        self.refused([{"type": "stat", "metric": "saguin_connections",
                       "at": [0, 0]}])

    # Strictness does not stop at the card.

    def test_an_unknown_top_level_key_is_refused(self):
        self.refused([{"type": "stat", "metric": "saguin_connections"}],
                     top={"titel": "x"})

    def test_an_unknown_grid_key_is_refused(self):
        self.refused([{"type": "stat", "metric": "saguin_connections"}],
                     grid={"colums": 24})

    def test_the_dead_row_height_key_is_gone(self):
        self.refused([{"type": "stat", "metric": "saguin_connections"}],
                     grid={"row_height": 88})


class TheConfigIsWrittenAsYAML(unittest.TestCase):
    """`/v1/operations/config` answers JSON, and the page shows YAML - because
    an operator reading it has a YAML file open beside them, and a document in
    braces asks them to translate every line before they can compare.

    **The writer is held to a real parser rather than to a fixture.** What
    matters is not that it produces the text somebody expected once: it is
    that PyYAML reads what it wrote and gets the document back. A quoting rule
    that is wrong for one value shows up here as a document that does not
    match, whatever the text looks like.

    node runs it because it is the page's own function, and the alternative is
    a second copy in Python - which is the thing that drifts.
    """

    @classmethod
    def setUpClass(cls):
        if shutil.which("node") is None:
            raise unittest.SkipTest("no node on PATH - needed to drive static/app.js")
        js = (VIEWER / "static" / "app.js").read_text()
        start = js.find("const YAML_PLAIN")
        if start < 0:
            raise AssertionError("the YAML writer is not in app.js")
        end = js.index("\n}\n", js.index("function toYaml", start)) + 3
        cls.src = js[start:end]

    def written(self, doc):
        d = tempfile.mkdtemp()
        try:
            f = pathlib.Path(d) / "probe.js"
            f.write_text(self.src + "\nprocess.stdout.write(toYaml("
                         + json.dumps(doc) + ", 0));")
            out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def roundtrip(self, doc):
        text = self.written(doc)
        self.assertEqual(yaml.safe_load(text), doc, "\n" + text)
        return text

    def test_a_configuration_shaped_document_comes_back_the_same(self):
        """The shapes this document is made of: nested mappings, a list of
        mappings, numbers, booleans, null, and the sizes and durations saguin
        writes as strings."""
        self.roundtrip({
            "broker": {
                "id": "bento-connectors",
                "limits": {"max_message_size": "1MiB", "max_keepalive": "none",
                           "max_connections": 10000, "write_timeout": "5s"},
                "mqtt": {"listen": {"tcp": {"address": "0.0.0.0:1883"}},
                         "anonymous": False},
            },
            "bridges": {"head-office": {
                "peer": "tcp://mosquitto:1883",
                "topics": [{"channel": "fleet", "filter": "fleet/+/telemetry/+",
                             "topic": "iot/fleet/$2/$1"}],
            }},
            "channels": {"jobs": {"type": "queue", "visibility_timeout": "30s",
                                  "max_attempts": 3}},
        })

    def test_a_string_that_would_read_back_as_something_else_is_quoted(self):
        """The half a writer gets wrong. Each of these is a string in the
        document and would come back as a boolean, a number or null if it were
        written bare - so the round-trip is the assertion, not the quoting."""
        self.roundtrip({"a": "yes", "b": "no", "c": "true", "d": "off",
                        "e": "null", "f": "~", "g": "1234", "h": "1.5",
                        "i": "", "j": "on", "k": "N", "l": "-12"})

    def test_the_characters_yaml_gives_a_meaning_to(self):
        """A topic filter with a `#`, a template with `$`, a path with a colon
        and a space - every one of them ordinary in this document."""
        self.roundtrip({"filter": "events/#", "topic": "iot/$1/$2",
                        "note": "one: two", "path": "/etc/saguin/acl.yaml",
                        "dash": "- not a list", "brace": "{a}", "star": "*x",
                        "amp": "&y", "pct": "%z", "at": "@w", "q": "he said \"hi\""})

    def test_an_empty_mapping_and_an_empty_list_are_written_as_such(self):
        """Absent and empty are different answers about a broker, and a writer
        that dropped the key would turn one into the other."""
        text = self.roundtrip({"channels": {}, "bridges": [], "broker": {"id": "t"}})
        self.assertIn("channels: {}", text)
        self.assertIn("bridges: []", text)


class TheAllowListMatchesTheRenderer(unittest.TestCase):
    """ALLOWED_METRICS in app.py and METRIC_MAP in static/app.js are one fact
    in two languages, and the drift between them is a metric the validator approved that the
    renderer could not draw. This reads the renderer and holds the allow-list to it, so the
    next drift is a red test rather than a wrong number on a dashboard."""

    def setUp(self):
        js = (VIEWER / "static" / "app.js").read_text()
        m = re.search(r"const METRIC_MAP = \{\n(.*?)\n\};", js, re.S)
        self.assertIsNotNone(m, "METRIC_MAP not found in app.js")
        # Split at each 2-space-indented `saguin_…:` key, so a two-line entry
        # (its cur on one line, its breakdown on the next) stays whole.
        self.entries = {}
        # A digit is part of a metric name (`saguin_qos2_held`). Both
        # patterns here would otherwise skip such an entry, and a skipped
        # entry is one this comparison silently agrees about.
        for part in re.split(r"\n(?=  saguin_[a-z_][a-z0-9_]*:)", m.group(1)):
            k = re.match(r"\s*(saguin_[a-z_][a-z0-9_]*):", part)
            if k:
                self.entries[k.group(1)] = part

    def test_the_two_name_the_same_metrics(self):
        self.assertEqual(set(self.entries), set(app.ALLOWED_METRICS))

    def test_capability_flags_match_the_renderer(self):
        for name, body in self.entries.items():
            meta = app.ALLOWED_METRICS[name]
            has_value = any(k in body for k in ("cur:", "hist:", "provider:"))
            self.assertEqual(meta["scalar"], has_value, f"{name}: scalar")
            self.assertEqual(meta["history"], "hist:" in body, f"{name}: history")
            self.assertEqual(meta["breakdown"] is not None,
                             "breakdown:" in body, f"{name}: breakdown")
            self.assertEqual(meta["selector"] is not None,
                             "provider:" in body, f"{name}: selector")


class WhatThePageRemembersBetweenVisits(unittest.TestCase):
    """Every tab the page has can be the one a refresh comes back to.

    The tab has always been written to a cookie. What it was not is *read*
    back: the reader was `cookie(...) === "dashboard" ? "dashboard" : "topics"`,
    written when there were two tabs, and the Alarms tab arrived without
    extending it. So the cookie was saved faithfully and thrown away on the
    next load, and an operator watching the Alarms tab - which is the tab you
    leave open, being the one that is worth leaving open - was put back on
    Topics by a refresh.

    A source check because the fault is a list that stops matching the tabs
    beside it, and that is a thing to compare rather than to click: it reads
    the tab buttons the page renders and holds the restore list to them, so the
    next tab added is red here rather than silently unrestorable.
    """

    def setUp(self):
        self.js = (VIEWER / "static" / "app.js").read_text()

    def test_every_tab_the_page_renders_can_be_restored(self):
        rendered = set(re.findall(r'onClick=\$\{\(\) => setTab\("([a-z]+)"\)\}', self.js))
        self.assertGreaterEqual(len(rendered), 3,
                                f"only found the tab buttons {rendered} - the regex missed some")
        m = re.search(r'const TABS = \[([^\]]*)\]', self.js)
        self.assertIsNotNone(m, "no TABS restore list in app.js")
        restorable = set(re.findall(r'"([a-z]+)"', m.group(1)))
        self.assertEqual(rendered, restorable,
                         f"tabs the page renders but a refresh cannot restore: "
                         f"{sorted(rendered - restorable)}")
        # And the list is what the reader actually consults, rather than a
        # constant sitting beside a hard-coded comparison.
        self.assertIn('TABS.includes(cookie("saguin_viewer_tab"))', self.js)


class WhatNotifyMeDoesInEveryStateTheBrowserCanBeIn(unittest.TestCase):
    """The notify control's decision, driven rather than read.

    This is the most-corrected thing in the page - five defects across three
    rounds - and each fix was guarded by a test that scanned app.js for the
    *text* of the defect it had just removed. What that is worth showed when
    someone rewrote the pre-judgment defect against a cached copy of
    the permission instead of `Notification.permission`, the same decision
    spelled differently, and the whole suite stayed green.

    So the decision lives in three pure functions and this drives them, in
    node, with the browser's world passed in. A regression has to change an
    *answer* to be caught, not a spelling. The table below is the contract:
    every state a browser can present, and what the operator gets.

    node runs them because they are JavaScript and the page is the only place
    they are used; the alternative is a second copy in Python, which is the
    thing that drifts.
    """

    # The functions are self-contained by construction - they take their world
    # as arguments - so the harness slices them out and evaluates just those.
    WANTED = ("notifyWhyRefused", "notifyPlan", "notifyAnswer",
              "notifyDeliveryFailed", "raiseNotification")

    @classmethod
    def setUpClass(cls):
        if shutil.which("node") is None:
            raise unittest.SkipTest("no node on PATH - needed to drive static/app.js")
        js = (VIEWER / "static" / "app.js").read_text()
        cls.src = []
        for name in cls.WANTED:
            m = re.search(r"^function %s\(.*?^\}" % name, js, re.S | re.M)
            if m is None:
                raise AssertionError(f"{name} is not a top-level function in app.js - "
                                     f"the decision has moved back inside the component, "
                                     f"where it cannot be driven")
            cls.src.append(m.group(0))
        cls.src = "\n".join(cls.src)

    def run_js(self, body):
        """Evaluate the extracted functions plus `body`, which prints one JSON
        line. A non-zero exit or unparseable output is the failure, with node's
        own stderr as the message rather than this harness's summary of it."""
        d = tempfile.mkdtemp()
        try:
            f = pathlib.Path(d) / "probe.js"
            f.write_text(self.src + "\n" + body)
            out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout.strip().splitlines()[-1])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_whole_decision_table(self):
        """Every state, and the answer each one owes the operator."""
        cases = [
            # (api, secure, perm) -> expected act, and a phrase the note must carry
            ((False, False, "unsupported"), "explain", "no notifications at all"),
            ((False, True,  "unsupported"), "explain", "no notifications at all"),
            # An insecure origin is settled without asking, and never reported
            # as the operator's own setting - this is the defect that shipped.
            ((True,  False, "denied"),      "explain", "served over plain HTTP"),
            ((True,  False, "default"),     "explain", "served over plain HTTP"),
            ((True,  False, "granted"),     "explain", "served over plain HTTP"),
            ((True,  True,  "granted"),     "arm",     None),
            # A permission is the operator's to change, so every not-granted
            # state on a secure origin is a question, never a verdict. "denied"
            # is the one a rewrite against a cached copy got past.
            ((True,  True,  "default"),     "ask",     "Asking the browser"),
            ((True,  True,  "denied"),      "ask",     "Asking the browser"),
        ]
        probe = "const out = [];\n"
        for (api, secure, perm), _, _ in cases:
            probe += (f"out.push(notifyPlan({{api: {str(api).lower()}, "
                      f"secure: {str(secure).lower()}, perm: {perm!r}}}));\n")
        probe += "console.log(JSON.stringify(out));"
        got = self.run_js(probe.replace("'", '"'))
        self.assertEqual(len(got), len(cases))
        for (state, act, phrase), g in zip(cases, got):
            with self.subTest(state=state):
                self.assertEqual(g["act"], act, f"{state} -> {g}")
                if phrase is None:
                    self.assertIsNone(g["note"], f"{state} -> {g}")
                else:
                    self.assertIn(phrase, g["note"] or "", f"{state} -> {g}")

    def test_the_click_says_something_before_the_browser_answers(self):
        """A quieted prompt never resolves, so the handler that writes every
        other sentence never runs and the click produces nothing - the original
        complaint, through the one gap left in the fix for it. The plan for
        "ask" carries its own note, set before the question is put."""
        got = self.run_js('console.log(JSON.stringify('
                          'notifyPlan({api: true, secure: true, perm: "default"})));')
        self.assertEqual(got["act"], "ask")
        self.assertTrue(got["note"], "the click is silent while the browser is deciding")

    def test_what_each_answer_from_the_browser_becomes(self):
        cases = [("granted", True, None),
                 ("default", False, "dismissed"),
                 ("denied", False, "blocking notifications")]
        probe = "const out = [];\n"
        for p, _, _ in cases:
            probe += f'out.push(notifyAnswer("{p}", {{api: true, secure: true}}));\n'
        probe += "console.log(JSON.stringify(out));"
        got = self.run_js(probe)
        for (p, arm, phrase), g in zip(cases, got):
            with self.subTest(answer=p):
                self.assertEqual(g["arm"], arm, f"{p} -> {g}")
                if phrase is None:
                    self.assertIsNone(g["note"])
                else:
                    self.assertIn(phrase, g["note"] or "")

    def test_an_insecure_origin_that_answers_granted_is_still_refused(self):
        """Chromium was observed resolving requestPermission() as "granted"
        over plain HTTP while delivering nothing. Believing it is the false
        green light this control has been fixed for twice."""
        got = self.run_js('console.log(JSON.stringify('
                          'notifyAnswer("granted", {api: true, secure: false})));')
        self.assertFalse(got["arm"], "a granted answer on an insecure origin armed the control")

    def test_a_refusal_that_does_not_throw_still_turns_the_control_off(self):
        """**The defect this replaces was unreachable code.** The off-switch
        was a `catch`, and Chrome does not throw when it refuses a
        notification: it returns the object and fires `error` on it. Driven on
        the real page - `new Notification(...)` unpermitted returned normally
        and fired `error` - so the control stayed "notifying" over a shut
        channel until a reload.

        The stub here therefore *succeeds* and then fires `error`, which is the
        case a throwing stub passes straight over.
        """
        got = self.run_js("""
          const seen = {event: false, thrown: false};
          global.Notification = class {
            constructor(t, o) { this.t = t; setTimeout(() => this.onerror && this.onerror(), 0); }
          };
          raiseNotification("t", "b", () => { seen.event = true; });
          // and the browser that guards its constructor instead
          global.Notification = class { constructor() { throw new Error("refused"); } };
          raiseNotification("t", "b", () => { seen.thrown = true; });
          setTimeout(() => console.log(JSON.stringify(seen)), 20);
        """)
        self.assertTrue(got["event"],
                        "a refusal delivered as an error event did not turn the control off - "
                        "which is how Chrome refuses, so the off-switch is unreachable")
        self.assertTrue(got["thrown"],
                        "a refusal delivered as an exception did not turn the control off")

    def test_a_failed_delivery_is_explained_by_what_actually_failed(self):
        """Turning the control off on a refused delivery is right; blaming the
        operator's site settings for it is not, when the permission is granted
        and the browser simply would not show it. Two states, two sentences."""
        got = self.run_js("""
          console.log(JSON.stringify({
            granted: notifyDeliveryFailed({api: true, secure: true, perm: "granted"}),
            denied:  notifyDeliveryFailed({api: true, secure: true, perm: "denied"}),
            insecure: notifyDeliveryFailed({api: true, secure: false, perm: "denied"}),
          }));""")
        self.assertIn("has permission and still would not show", got["granted"])
        self.assertNotIn("site settings", got["granted"],
                         "a granted permission sent the operator to site settings")
        self.assertIn("blocking notifications", got["denied"])
        self.assertIn("served over plain HTTP", got["insecure"])

    def test_a_delivery_that_succeeds_does_not_turn_it_off(self):
        """The control against the case above: a notification the browser
        shows must leave the control armed. Without this, `onRefused()` called
        unconditionally would pass every assertion above."""
        got = self.run_js("""
          const seen = {refused: false};
          global.Notification = class { constructor(t, o) { this.t = t; } };
          const n = raiseNotification("t", "b", () => { seen.refused = true; });
          setTimeout(() => console.log(JSON.stringify({...seen, got: !!n})), 20);
        """)
        self.assertFalse(got["refused"], "a delivered notification turned the control off")
        self.assertTrue(got["got"], "raiseNotification did not return the notification")


class EveryChartStrokeIsMeasuredInPixels(unittest.TestCase):
    """A stroke inside a chart does not thicken as the card gets wider.

    Every chart draws into a fixed-unit viewBox with `preserveAspectRatio=
    "none"` and `.chart { width: 100% }` stretches it, so a 1200px card scales
    x by about 3.75 while y stays 1. `stroke-width: 2` then holds only on a
    horizontal run: steep segments fattened with the card and the crosshair,
    being vertical, got the worst of it. `vector-effect: non-scaling-stroke`
    measures the width after the transform, which is what makes it mean the
    same at every card size.

    **A source check, because the effect is geometric and a browser is what
    would show it.** It reads the renderer rather than the stylesheet's word:
    it finds every stroked shape in a chart and holds each one's class to the
    rule, so a new stroked element added without a covered class is red here
    rather than a fat line somebody notices on a wide screen. It counts what
    it read, so a regex that stopped matching cannot pass by comparing
    nothing.
    """

    def setUp(self):
        self.js = (VIEWER / "static" / "app.js").read_text()
        self.css = (VIEWER / "static" / "app.css").read_text()

    def stroked(self):
        """(tag, class) for every SVG shape in app.js that carries a stroke,
        inline or through a class the stylesheet strokes."""
        css_strokes = {c for c in re.findall(r"\.chart \.([a-z]+)\s*\{[^}]*stroke", self.css)}
        out = []
        for tag, attrs in re.findall(r"<(path|line|circle|polyline|polygon)\b([^>]*)>", self.js):
            cls = re.search(r'class="([a-z]+)"', attrs)
            name = cls.group(1) if cls else None
            if "stroke" in attrs or name in css_strokes:
                out.append((tag, name))
        return out

    def test_every_stroked_shape_carries_a_class_the_rule_covers(self):
        rule = re.search(r"((?:\.chart \.[a-z]+,?\s*)+)\{[^}]*vector-effect:\s*non-scaling-stroke",
                         self.css)
        self.assertIsNotNone(rule, "no non-scaling-stroke rule in app.css")
        covered = set(re.findall(r"\.chart \.([a-z]+)", rule.group(1)))
        found = self.stroked()
        # Six today: TimeLine's line, its two grid rules and its crosshair, and
        # Spark's line and crosshair. The floor is what stops a regex that
        # quietly stopped matching from passing this by comparing nothing.
        self.assertGreaterEqual(len(found), 6, f"only found {found} - the regex missed some")
        for tag, name in found:
            self.assertIsNotNone(name, f"a stroked <{tag}> with no class, so no rule reaches it")
            self.assertIn(name, covered,
                          f"<{tag} class=\"{name}\"> is stroked and not in the "
                          f"non-scaling-stroke rule, so it thickens with the card")

    def test_no_round_shape_is_drawn_inside_a_stretched_viewbox(self):
        """The other half of the same cause, and the half that stayed hidden.

        non-scaling-stroke fixes a stroke's *width* after the transform. It
        does nothing to a shape's geometry, so a `<circle>` in these viewBoxes
        is painted as an ellipse - wider and flatter the wider the card. Both
        charts drew their point marker as one, and nobody saw it because the
        line was fat enough to cover it; thinning the line is what put it on
        screen. So the rule is the stronger one: a shape whose roundness is the
        point does not go inside a `preserveAspectRatio="none"` viewBox at all.
        The markers are HTML positioned over the chart now.
        """
        shapes = re.findall(r"<(circle|ellipse)\b", self.js)
        self.assertEqual(shapes, [],
                         f"{shapes} drawn inside a chart: a circle in a stretched "
                         f"viewBox is an ellipse. Position an HTML .chartmark over "
                         f"the svg instead.")
        # And the marker that replaced them is real, in both charts, over a
        # relatively-positioned wrapper - without which `top`/`left` would be
        # measured from the page.
        self.assertEqual(self.js.count('class="chartmark'), 2,
                         "both TimeLine and Spark should mark their point")
        self.assertIn(".chartmark { position: absolute;", self.css)
        self.assertEqual(self.js.count('<div style=${{ position: "relative" }}>'), 2,
                         "a .chartmark needs a positioned wrapper to sit in")


class TheHistoryRing(unittest.TestCase):
    """The metrics ring, persisted to SQLite across a viewer restart. Off unless
    `history_file` is set; a file that is not a database, or a row that is not
    JSON, degrades to less history rather than a crash, and the ring is never
    overrun by a large store."""

    def setUp(self):
        self.saved_file = app.HISTORY_FILE
        self.saved_ring = list(app.metrics_history)
        self.tmp = tempfile.mkdtemp()
        app.HISTORY_FILE = os.path.join(self.tmp, "history.db")
        app.metrics_history.clear()

    def tearDown(self):
        app.HISTORY_FILE = self.saved_file
        app.metrics_history.clear()
        app.metrics_history.extend(self.saved_ring)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rawdb(self):
        import sqlite3
        conn = sqlite3.connect(app.HISTORY_FILE)
        conn.execute(app._HISTORY_DDL)
        return conn

    def test_off_by_default_writes_nothing(self):
        app.HISTORY_FILE = ""
        app.metrics_history.append({"t": 1.0})
        app.save_history()
        self.assertFalse(os.listdir(self.tmp))

    def test_the_ring_round_trips(self):
        rows = [{"t": 1.0, "connections": 3}, {"t": 2.0, "connections": 4}]
        for r in rows:                       # a scrape appends one row and saves
            app.metrics_history.append(r)
            app.save_history()
        app.metrics_history.clear()
        app.load_history()
        self.assertEqual(list(app.metrics_history), rows)

    def test_a_file_that_is_not_a_database_degrades(self):
        with open(app.HISTORY_FILE, "w") as f:
            f.write("this is not a sqlite database")
        app.metrics_history.clear()
        app.load_history()      # must not raise
        self.assertEqual(list(app.metrics_history), [])

    def test_a_row_that_is_not_json_is_skipped(self):
        conn = self._rawdb()
        with conn:
            conn.execute("INSERT INTO history VALUES (1.0, ?)",
                         (json.dumps({"t": 1.0, "connections": 1}),))
            conn.execute("INSERT INTO history VALUES (2.0, 'not json')")
        conn.close()
        app.metrics_history.clear()
        app.load_history()
        self.assertEqual(list(app.metrics_history), [{"t": 1.0, "connections": 1}])

    def test_a_large_store_is_clipped_to_the_ring(self):
        n = app.metrics_history.maxlen + 50
        conn = self._rawdb()
        with conn:
            conn.executemany("INSERT INTO history VALUES (?, ?)",
                             [(float(i), json.dumps({"t": float(i)})) for i in range(n)])
        conn.close()
        app.metrics_history.clear()
        app.load_history()
        self.assertEqual(len(app.metrics_history), app.metrics_history.maxlen)
        self.assertEqual(app.metrics_history[0]["t"], 50.0)   # oldest dropped

    def test_save_trims_the_on_disk_table_below_the_ring(self):
        # save_history's on-disk DELETE had no guard -
        # replacing it with `pass` left the whole suite green while the table
        # grew unbounded. Seed rows older than the ring's window; a save must
        # delete everything below the ring's oldest, not only insert the newest.
        conn = self._rawdb()
        with conn:
            conn.executemany("INSERT INTO history VALUES (?, ?)",
                             [(float(i), json.dumps({"t": float(i)})) for i in range(1, 101)])
        conn.close()                             # 100 rows on disk, t = 1..100
        app.metrics_history.clear()
        for i in range(50, 60):                  # the ring's oldest is t = 50
            app.metrics_history.append({"t": float(i)})
        app.save_history()
        conn = self._rawdb()
        left = [r[0] for r in conn.execute("SELECT t FROM history ORDER BY t")]
        conn.close()
        self.assertFalse([t for t in left if t < 50.0],
                         "rows older than the ring survived the on-disk trim")
        self.assertEqual(min(left), 50.0)        # nothing below the ring's oldest

    def test_trim_by_age_drops_samples_older_than_retention(self):
        # The ring is bounded by age, not only maxlen: a slower scrape_interval
        # keeps fewer than a ring's worth across five days, so a sample past the
        # retention is dropped after each scrape rather than lingering to maxlen.
        now = time.time()
        app.metrics_history.clear()
        app.metrics_history.append({"t": now - app.HISTORY_RETENTION - 120})   # stale
        app.metrics_history.append({"t": now - 60})                            # fresh
        app.trim_history_by_age()
        kept = [r["t"] for r in app.metrics_history]
        self.assertEqual(len(kept), 1)                                         # stale one gone
        self.assertTrue(all(t >= now - app.HISTORY_RETENTION for t in kept))

    def test_the_file_is_created_if_it_does_not_exist(self):
        self.assertFalse(os.path.exists(app.HISTORY_FILE))
        app.open_history()
        self.assertTrue(os.path.exists(app.HISTORY_FILE))
        # and it is a usable database with the table
        conn = self._rawdb()
        conn.execute("SELECT count(*) FROM history")   # would raise if unusable
        conn.close()

    def test_a_path_whose_directory_is_missing_stops_loudly(self):
        app.HISTORY_FILE = os.path.join(self.tmp, "nope", "history.db")
        with self.assertRaises(SystemExit):
            app.open_history()

    def test_a_file_that_is_not_a_database_stops_loudly(self):
        with open(app.HISTORY_FILE, "w") as f:
            f.write("this is not a sqlite database")
        with self.assertRaises(SystemExit):
            app.open_history()

    def test_empty_history_file_needs_no_file(self):
        app.HISTORY_FILE = ""
        app.open_history()                 # must not raise
        self.assertFalse(os.listdir(self.tmp))


class TheFeedIsBoundedByItsConfiguredSize(unittest.TestCase):
    """`feed_messages` bounds the feed, and the endpoint says the same number.

    **In a fresh interpreter, because the bound is fixed at import.** The ring
    was built twice - once from the configuration and once more, later, with
    the default written in by hand - and the later line won, so the key did
    nothing from the viewer's first commit to this one. Asserting it at the
    default proves nothing: the two numbers were the same there, which is why
    it went unnoticed. So this configures a size that is not
    the default and drives the endpoint whose reply names the capacity.
    """

    PROBE = r"""
import json, sys
cfg, viewer, sent = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.argv = ["app", cfg]
sys.path.insert(0, viewer)
import app
for i in range(sent):
    app.feed_seq[0] += 1
    app.feed.append({"seq": app.feed_seq[0], "topic": "t/%d" % i, "at": 0.0})
body = app.app.test_client().get("/api/feed").get_json()
print(json.dumps({"configured": app.CONFIG["feed_messages"],
                  "maxlen": app.feed.maxlen,
                  "capacity": body["capacity"], "held": body["held"],
                  "served": len(body["messages"]),
                  "seq": body["seq"]}))
"""

    def run_with(self, feed_messages, sent):
        d = tempfile.mkdtemp()
        try:
            cfg = pathlib.Path(d) / "viewer.yaml"
            cfg.write_text(f"feed_messages: {feed_messages}\n")
            probe = pathlib.Path(d) / "probe.py"
            probe.write_text(self.PROBE)
            out = subprocess.run(
                [sys.executable, str(probe), str(cfg), str(VIEWER), str(sent)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout.strip().splitlines()[-1])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_configured_size_is_the_size(self):
        r = self.run_with(5, sent=8)
        self.assertEqual(r["configured"], 5)
        self.assertEqual(r["maxlen"], 5, "the ring ignored feed_messages")
        self.assertEqual(r["capacity"], 5, "/api/feed reported a capacity it does not have")
        self.assertEqual(r["held"], 5, "eight messages held past a bound of five")
        self.assertEqual(r["served"], 5)
        self.assertEqual(r["seq"], 8)      # the eight did arrive; five were kept

    def test_a_ring_larger_than_the_old_clip_is_served_whole(self):
        """The sibling of the same cause, and it needed a ring big enough to
        reach it: `/api/feed` clipped its reply to a second hard-coded 2000,
        which was a no-op only while the ring itself was stuck at 2000. So this
        fills a ring of 2500 past that clip - 2100 held, 2100 served, none of
        them silently dropped from a reply whose `dropped` flag says false.
        Eight messages in a ring of 2500 would pass against the defect, which
        is what the first draft of this test did."""
        r = self.run_with(2500, sent=2100)
        self.assertEqual(r["maxlen"], 2500)
        self.assertEqual(r["capacity"], 2500)
        self.assertEqual(r["held"], 2100)
        self.assertEqual(r["served"], 2100,
                         "the reply clipped what the ring is holding, with no gap reported")


class PuttingDeadLetteredWorkBack(unittest.TestCase):
    """Requeue: where the `__dlq` level is, and the bytes that go back.

    RFC 0003 gives a rule for where the level sits - at the `#` for a filter
    ending in one, on the end otherwise - and reimplementing that rule is a way
    to be subtly wrong on a filter shape nobody tested. It is not
    reimplemented: the dead-letter channel's own filter carries the level at
    the position to remove, and that filter is the broker's. Both shapes are
    here because they are the two the broker produces, and they were taken from
    a running broker's catalogue rather than from the prose:

        jobs/#         ->  jobs/__dlq/#
        iot/+/work/+   ->  iot/+/work/+/__dlq
    """

    JOBS = "jobs/__dlq/#"
    WORK = "iot/+/work/+/__dlq"

    def test_the_level_is_found_where_the_filter_puts_it(self):
        self.assertEqual(app.dlq_level_index(self.JOBS), 1)
        self.assertEqual(app.dlq_level_index(self.WORK), 4)
        self.assertIsNone(app.dlq_level_index("jobs/#"))
        self.assertIsNone(app.dlq_level_index(""))

    def test_both_filter_shapes_strip_back_to_the_job(self):
        self.assertEqual(app.requeue_topic("jobs/__dlq/j1", self.JOBS), ("jobs/j1", None))
        self.assertEqual(app.requeue_topic("iot/site/work/j7/__dlq", self.WORK),
                         ("iot/site/work/j7", None))
        # deeper topics under a `#` filter keep every level but the one
        self.assertEqual(app.requeue_topic("jobs/__dlq/a/b/c", self.JOBS), ("jobs/a/b/c", None))

    def test_a_topic_without_the_level_where_the_filter_says_is_refused(self):
        """**Refused rather than guessed.** Publishing an un-stripped topic
        would put the job straight back into the dead-letter channel it came
        from, which reads as a successful requeue and moves no work."""
        back, why = app.requeue_topic("jobs/j1", self.JOBS)
        self.assertIsNone(back)
        self.assertIn("__dlq", why)
        back, why = app.requeue_topic("jobs/__dlq/j1", self.WORK)   # wrong channel's filter
        self.assertIsNone(back)
        back, why = app.requeue_topic("jobs/__dlq/j1", "jobs/#")     # not a dlq filter
        self.assertIsNone(back)
        self.assertIn("not a dead-letter channel", why)

    def test_every_rendered_payload_round_trips_to_the_same_bytes(self):
        """**A requeue publishes these bytes, so the inverse has to be exact.**
        A re-encode would corrupt the job while still looking right, because
        the corruption is in bytes a person reads as text. Every kind
        render_payload can produce is here, including the two that reach the
        hex branch by different routes."""
        cases = [
            (b'{"work": 1}', "application/json"),
            (b'{"work": 1}', ""),
            # **The three below are the point of this case list.** Replacing the
            # json branch with `json.dumps(json.loads(text))` - the "tidy it before publishing"
            # regression this test's docstring warns of - and the suite stayed
            # green, because `{"work": 1}` already carries Python's canonical
            # spacing and survives the round trip unchanged. A sample that
            # cannot tell the two implementations apart asserts nothing. These
            # differ under a re-encode: compact spacing, unsorted keys, and a
            # float whose repr is not its source text.
            (b'{"work":1}', "application/json"),
            (b'{"b":2,"a":1}', ""),
            (b'{"n": 1.10}', ""),
            (b"plain text, not json", ""),
            (b"\xff\xfe\x00\x01", ""),                  # not valid UTF-8
            (b"printable-but-declared-binary", "application/x-protobuf"),
            (b"", ""),
            ("a payload with wide characters: ação, 日本語".encode("utf-8"), ""),
        ]
        for raw, ctype in cases:
            with self.subTest(payload=raw[:20], content_type=ctype):
                rendered = app.render_payload(raw, ctype)
                self.assertEqual(app.payload_bytes(rendered), raw,
                                 f"{rendered.get('kind')} did not round-trip")

    def test_a_dead_letter_channel_is_recognised_by_its_filter_not_its_name(self):
        """The `__dlq` suffix on the channel name is a convention; the filter
        is what the broker actually routes by, and it is what this reads."""
        chans = {"jobs": ("queue", "jobs/#"),
                 "jobs__dlq": ("append", "jobs/__dlq/#"),
                 "work__dlq": ("append", "iot/+/work/+/__dlq"),
                 "notdlq": ("append", "audit/__dlqish/#")}
        self.assertEqual(set(app.dlq_channels(chans)), {"jobs__dlq", "work__dlq"})


class TheRequeueEndpointRefusesBeforeItPublishes(unittest.TestCase):
    """Everything /api/requeue checks before a byte goes on the wire.

    Each of these would otherwise be a publish that succeeds and moves no work,
    or moves it somewhere no worker reads - the worst shape there is,
    because it reports success."""

    def setUp(self):
        self.saved = (dict(app.channels), dict(app.topics))
        app.channels.clear()
        app.channels.update({"jobs": ("queue", "jobs/#"),
                             "jobs__dlq": ("append", "jobs/__dlq/#"),
                             "notes": ("append", "notes/#"),
                             "notes__dlq": ("append", "notes/__dlq/#")})
        app.topics.clear()
        app.topics["jobs/__dlq/j1"] = {
            "count": 1, "last": 0.0, "channel": "jobs__dlq", "kind": "append",
            "messages": [{"at": 1.0, "content_type": "application/json",
                          "properties": {"saguin-offset": "7", "saguin-id": "id-1",
                                         "saguin-dlq-reason": "attempts_exhausted",
                                         "saguin-dlq-channel": "jobs", "origin": "probe"},
                          "payload": app.render_payload(b'{"work": 1}', "application/json")}]}
        # a dead letter whose stripped topic lands on an append channel
        app.topics["notes/__dlq/n1"] = {
            "count": 1, "last": 0.0, "channel": "notes__dlq", "kind": "append",
            "messages": [{"at": 1.0, "content_type": "",
                          "properties": {"saguin-offset": "1"},
                          "payload": app.render_payload(b"x", "")}]}
        self.c = app.app.test_client()

    def tearDown(self):
        chans, tops = self.saved
        app.channels.clear(); app.channels.update(chans)
        app.topics.clear(); app.topics.update(tops)

    def post(self, **body):
        return self.c.post("/api/requeue", json=body)

    def test_a_record_this_page_is_not_holding(self):
        r = self.post(topic="jobs/__dlq/j1", offset=999)
        self.assertEqual(r.status_code, 404)
        self.assertIn("still held here", r.get_json()["error"])

    def test_a_topic_that_is_not_on_a_dead_letter_channel(self):
        app.topics["jobs/j1"] = {"count": 1, "last": 0.0, "channel": "jobs", "kind": "queue",
                                 "messages": [{"at": 1.0, "properties": {"saguin-offset": "1"},
                                               "payload": app.render_payload(b"x", "")}]}
        r = self.post(topic="jobs/j1", offset=1)
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a dead-letter channel", r.get_json()["error"])

    def test_a_stripped_topic_that_does_not_land_on_a_queue(self):
        """`notes__dlq` is a dead-letter channel by filter, but `notes/n1` is
        an append channel - publishing there is accepted by the broker and
        read by no worker, so it is refused here with the reason."""
        r = self.post(topic="notes/__dlq/n1", offset=1)
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a queue", r.get_json()["error"])

    def test_naming_no_record_at_all(self):
        self.assertEqual(self.post(topic="jobs/__dlq/j1").status_code, 400)
        self.assertEqual(self.post(offset=1).status_code, 400)

    def test_the_listing_surfaces_the_failure_metadata_and_the_way_back(self):
        b = self.c.get("/api/deadletters").get_json()
        self.assertEqual(b["channels"], ["jobs__dlq", "notes__dlq"])
        b = self.c.get("/api/deadletters?channel=jobs__dlq").get_json()
        self.assertEqual(len(b["records"]), 1)
        r = b["records"][0]
        self.assertEqual(r["reason"], "attempts_exhausted")
        self.assertEqual(r["queue"], "jobs")
        self.assertEqual(r["id"], "id-1")
        self.assertEqual(r["offset"], "7")
        self.assertEqual(r["back"], "jobs/j1")

    def test_a_channel_that_is_not_a_dead_letter_one(self):
        r = self.c.get("/api/deadletters?channel=jobs")
        self.assertEqual(r.status_code, 400)


class WhatTheBrokerGrantedIsKeptAndSaid(unittest.TestCase):
    """A refused subscription is recorded, not swallowed.

    **The success-shaped failure this feature was one of.** A SUBACK carries a
    reason code per filter, and a refusal is an ordinary acknowledgement: the
    connection is fine, `connected` is true, and the only symptom is that
    nothing ever arrives. Under an `acl_file` that is the *normal* answer to
    the default `subscribe: "#"` - driving it showed that a credential
    granted `read` on one dead-letter channel is answered `Not authorized` for
    `#`, and the page showed a connected viewer holding nothing with no way to
    read why from the page.

    The code comment above the subscribe loop said `#` "is granted at QoS 1,
    measured against a running saguin". It was - against one with no acl_file.
    """

    class Code:
        """A paho reason code, as far as this cares: it knows if it failed."""
        def __init__(self, name, failure):
            self.name, self.is_failure = name, failure
        def __str__(self):
            return self.name

    def setUp(self):
        self.saved = list(app.state["subscriptions"])
        app.state["subscriptions"] = []
        app._sub_mids.clear()

    def tearDown(self):
        app.state["subscriptions"] = self.saved
        app._sub_mids.clear()

    def test_a_refused_filter_is_recorded_with_what_the_broker_said(self):
        app._sub_mids[7] = "#"
        app.on_subscribe(None, None, 7, [self.Code("Not authorized", True)])
        self.assertEqual(app.state["subscriptions"],
                         [{"filter": "#", "granted": False, "reason": "Not authorized"}])

    def test_a_granted_filter_is_recorded_too(self):
        """Both halves: a test that only ever saw refusals would pass against a
        recorder that marked everything refused."""
        app._sub_mids[3] = "jobs/__dlq/#"
        app.on_subscribe(None, None, 3, [self.Code("Granted QoS 1", False)])
        self.assertEqual(app.state["subscriptions"],
                         [{"filter": "jobs/__dlq/#", "granted": True,
                           "reason": "Granted QoS 1"}])

    def test_the_state_endpoint_carries_them(self):
        app._sub_mids[1] = "#"
        app.on_subscribe(None, None, 1, [self.Code("Not authorized", True)])
        body = app.app.test_client().get("/api/state").get_json()
        self.assertEqual([x["filter"] for x in body["subscriptions"]], ["#"])
        self.assertFalse(body["subscriptions"][0]["granted"])
        self.assertEqual(body["subscribe"], app.MQTT_CFG["subscribe"])

    def test_the_state_offers_no_retained_store_detection(self):
        """RFC 0002 "Retained messages on a broadcast topic": the broker's
        retained store always exists - `broker.retained` only changes which
        provider holds it and for how long - and a retained publish is
        refused `0x9A` only where a client's roles deny `retained`.

        This viewer inferred "no retained store" from the resolved
        configuration omitting the optional block - which the broker leaves
        out whenever nobody wrote it - and then told an operator a retained
        publish "will be refused 0x9A", measured false on the wire. Nothing
        the configuration route answers can say the store is absent, so
        nothing here may claim to know."""
        body = app.app.test_client().get("/api/state").get_json()
        self.assertNotIn("retained_store", body)
        js = (VIEWER / "static" / "app.js").read_text()
        for name in ("retainOK", "retained_store"):
            self.assertNotIn(name, js)

    def test_the_sidebar_counts_hold_what_the_panels_would_list(self):
        """The badge is what those two panels can open, so it counts held rows
        and not the broker's totals. A dead letter is a message on a `__dlq`
        channel's topic; a retained value counts only where no channel claims
        the topic, which is the same test the retained panel applies."""
        chans = dict(app.channels)
        tops = dict(app.topics)
        ret = dict(app.retained)
        app.channels.clear()
        app.channels.update({"jobs__dlq": ("append", "jobs/__dlq/#"),
                             "readings": ("latest", "sensors/+")})
        app.topics.clear()
        app.topics.update({
            "jobs/__dlq/a": {"channel": "jobs__dlq", "kind": "append",
                             "messages": [1, 2, 3]},
            "jobs/__dlq/b": {"channel": "jobs__dlq", "kind": "append",
                             "messages": [4]},
            "sensors/x": {"channel": "readings", "kind": "latest",
                          "messages": [5]},          # a channel, not a dead letter
        })
        app.retained.clear()
        app.retained.update({"news/today": {}, "news/sport": {},
                             "sensors/x": {}})       # a channel value, not retained
        try:
            body = app.app.test_client().get("/api/state").get_json()
            self.assertEqual(body["counts"], {"deadletters": 4, "retained": 2})
        finally:
            app.channels.clear(); app.channels.update(chans)
            app.topics.clear(); app.topics.update(tops)
            app.retained.clear(); app.retained.update(ret)

    def test_the_sidebar_counts_are_zero_rather_than_absent(self):
        """A missing badge and a badge saying nought are read differently: the
        first says the page does not know, and this page does."""
        chans = dict(app.channels)
        tops = dict(app.topics)
        ret = dict(app.retained)
        app.channels.clear(); app.topics.clear(); app.retained.clear()
        try:
            body = app.app.test_client().get("/api/state").get_json()
            self.assertEqual(body["counts"], {"deadletters": 0, "retained": 0})
        finally:
            app.channels.clear(); app.channels.update(chans)
            app.topics.clear(); app.topics.update(tops)
            app.retained.clear(); app.retained.update(ret)

    def test_the_dead_letter_list_says_why_it_is_empty(self):
        """An empty list has two meanings under an acl_file and the likelier one
        is a refused subscription. Naming it is the difference between "the
        feature is broken" and "this credential may not read that filter"."""
        saved = dict(app.channels)
        app.channels.clear()
        app.channels.update({"jobs__dlq": ("append", "jobs/__dlq/#")})
        try:
            app._sub_mids[1] = app.MQTT_CFG["subscribe"]
            app.on_subscribe(None, None, 1, [self.Code("Not authorized", True)])
            b = app.app.test_client().get("/api/deadletters?channel=jobs__dlq").get_json()
            self.assertEqual(b["records"], [])
            self.assertIn("refused the subscription", b["hint"])
            self.assertIn("jobs/__dlq/#", b["hint"])       # what to narrow it to
        finally:
            app.channels.clear(); app.channels.update(saved)

    def test_the_reply_topic_is_not_what_the_advice_names(self):
        """The reply topic is refused by the same ACL and is not what an
        operator would edit to fix this - naming it sends them to the wrong
        setting. Only filters `subscribe` controls belong in the sentence."""
        saved = dict(app.channels)
        app.channels.clear()
        app.channels.update({"jobs__dlq": ("append", "jobs/__dlq/#")})
        try:
            app._sub_mids[1] = "viewer-reply/abc123"
            app.on_subscribe(None, None, 1, [self.Code("Not authorized", True)])
            b = app.app.test_client().get("/api/deadletters?channel=jobs__dlq").get_json()
            self.assertNotIn("viewer-reply", b["hint"] or "")
        finally:
            app.channels.clear(); app.channels.update(saved)


class TheImageCarriesWhatTheConfigNeeds(unittest.TestCase):
    """The Dockerfile copies every file the shipped configuration reads.

    **The image did not start for a day and nobody noticed**, because nothing
    ran it. `dashboards/` was added the day after the Dockerfile was last
    edited, the Dockerfile enumerates what it copies, and a dashboard file that
    is not there stops the viewer at startup - so every run exited on
    `dashboard "Default": no such file`, including the `bento-viewer` container
    in examples/bento-connectors, which builds from the same file.

    This is the check that would have caught it, and it is an ordinary test
    rather than a Makefile target so it cannot be skipped by somebody who runs
    the suite. It reads the shipped `saguin-viewer.yaml` for the paths the
    program will actually open, and holds the Dockerfile to them - so the next
    directory added beside app.py is red here rather than a container that
    exits on start.

    It does not need Docker: what is being checked is that two files in the
    repository agree.
    """

    def setUp(self):
        self.dockerfile = (VIEWER / "Dockerfile").read_text()
        raw = yaml.safe_load((VIEWER / "saguin-viewer.yaml").read_text())
        self.cfg = app.merge(app.DEFAULTS, raw)

    def copied(self):
        """The repo-relative paths the Dockerfile copies into the image."""
        out = []
        for line in self.dockerfile.splitlines():
            line = line.strip()
            if not line.upper().startswith("COPY "):
                continue
            parts = line.split()
            # COPY <src>... <dest> - every argument but the last is a source.
            out.extend(parts[1:-1])
        return out

    def test_it_copies_something_at_all(self):
        """Count what was read, so a parser that stopped matching cannot pass
        this by comparing an empty list against an empty list."""
        self.assertGreaterEqual(len(self.copied()), 4,
                                f"only found {self.copied()} - the COPY parse missed some")

    def test_every_dashboard_the_config_names_is_in_the_image(self):
        dashboards = [p for p in (self.cfg.get("dashboard") or {}).values()
                      if p != "builtin"]
        self.assertTrue(dashboards, "the shipped config names no dashboard file to check")
        copied = self.copied()
        for path in dashboards:
            # The program resolves a relative path beside app.py, which in the
            # image is /viewer - so the file has to arrive under the same name.
            src = f"{PREFIX}{path}"
            covered = any(src == c or src.startswith(c.rstrip("/") + "/") for c in copied)
            with self.subTest(dashboard=path):
                self.assertTrue(covered,
                                f"{path!r} is named by saguin-viewer.yaml and no COPY in "
                                f"the Dockerfile brings it into the image, so the "
                                f"container exits at startup with "
                                f'dashboard "…": no such file. Copied: {copied}')

    def test_the_program_and_its_static_files_are_in_the_image(self):
        copied = self.copied()
        for needed in (f"{PREFIX}app.py",
                       f"{PREFIX}static",
                       f"{PREFIX}saguin-viewer.yaml",
                       f"{PREFIX}requirements.txt"):
            self.assertIn(needed, copied, f"the image does not copy {needed}")

    def test_the_shipped_config_loads_from_only_what_is_copied(self):
        """The end-to-end form of the two above: assemble exactly the file set
        the Dockerfile copies, in the layout it builds, and load the config the
        way the program does. This is what was run to reproduce the defect."""
        d = tempfile.mkdtemp()
        try:
            root = pathlib.Path(d)
            for src in self.copied():
                if not src.startswith(PREFIX):
                    continue
                rel = src[len(PREFIX):]
                s, dst = VIEWER / rel, root / rel
                if s.is_dir():
                    shutil.copytree(s, dst, dirs_exist_ok=True)
                elif s.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s, dst)
            raw = yaml.safe_load((root / "saguin-viewer.yaml").read_text())
            cfg = app.merge(app.DEFAULTS, raw)
            for name, path in (cfg.get("dashboard") or {}).items():
                if path == "builtin":
                    continue
                with self.subTest(dashboard=name):
                    self.assertTrue((root / path).exists(),
                                    f"dashboard {name!r} points at {path!r}, which the "
                                    f"image's file set does not contain")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TheRetainedSet(unittest.TestCase):
    """Which retained value the broker keeps per topic, as the viewer learns it
    from the retain flag on delivery. An empty retained message clears it - the
    way MQTT deletes one - and a live (non-retained) message records none."""

    class FakeMsg:
        def __init__(self, topic, payload, retain):
            self.topic, self.payload, self.retain = topic, payload, retain
            self.qos, self.properties = 0, None

    def setUp(self):
        app.retained.clear()
        self.seen = []

    def tearDown(self):
        app.retained.clear()
        for t in self.seen:
            app.topics.pop(t, None)

    def deliver(self, topic, payload, retain):
        self.seen.append(topic)
        app.on_message(None, None, self.FakeMsg(topic, payload, retain))

    def test_a_retained_message_is_recorded(self):
        self.deliver("probe/ret/a", b'{"x":1}', True)
        self.assertIn("probe/ret/a", app.retained)
        self.assertEqual(app.retained["probe/ret/a"]["topic"], "probe/ret/a")

    def test_an_empty_retained_message_clears_it(self):
        self.deliver("probe/ret/b", b"value", True)
        self.assertIn("probe/ret/b", app.retained)
        self.deliver("probe/ret/b", b"", True)
        self.assertNotIn("probe/ret/b", app.retained)

    def test_a_live_message_records_no_retained(self):
        self.deliver("probe/ret/c", b"live", False)
        self.assertNotIn("probe/ret/c", app.retained)


class TheAlarmRecord(unittest.TestCase):
    """Alarm episodes recorded by the poll thread: opened when a threshold
    starts firing, closed when it stops, persisted to the same SQLite database
    as the ring and trimmed to the same five days - so a viewer opened after the
    fact sees what fired while nobody was looking. Evaluated on the backend, so
    the record and the page's live red card share one threshold."""

    def setUp(self):
        self.saved = (app.ALARM_SPECS, app.ALARM_KEYS, app.HISTORY_FILE,
                      list(app.alarm_episodes), dict(app.alarm_firing), app._alarm_next_id)
        self.tmp = tempfile.mkdtemp()
        app.HISTORY_FILE = os.path.join(self.tmp, "history.db")
        app.ALARM_SPECS = [{"dash": "D", "title": "Refused",
                            "metric": "saguin_publish_refused_total", "expr": "> 0"}]
        app.ALARM_KEYS = {("D", "Refused")}
        app.alarm_episodes.clear()
        app.alarm_firing.clear()
        app._alarm_next_id = 1

    def tearDown(self):
        specs, keys, hf, eps, fir, nid = self.saved
        app.ALARM_SPECS, app.ALARM_KEYS, app.HISTORY_FILE = specs, keys, hf
        app.alarm_episodes.clear(); app.alarm_episodes.extend(eps)
        app.alarm_firing.clear(); app.alarm_firing.update(fir)
        app._alarm_next_id = nid
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _s(self, v):
        return [("saguin_publish_refused_total", (), float(v))]

    def test_eval_alarm_grammar(self):
        self.assertTrue(app.eval_alarm("> 0", 1))
        self.assertFalse(app.eval_alarm("> 0", 0))
        self.assertTrue(app.eval_alarm(">= 5", 5))
        self.assertTrue(app.eval_alarm("< 3", 2))
        self.assertTrue(app.eval_alarm("!= 0", 4))
        self.assertFalse(app.eval_alarm("> 0", None))     # absent never fires
        self.assertFalse(app.eval_alarm("nonsense", 9))   # unparseable never fires

    def test_alarm_value_sums_the_family_and_absent_is_none(self):
        s = [("saguin_publish_refused_total", (), 2.0),
             ("saguin_publish_refused_total", ("ch",), 3.0)]
        self.assertEqual(app.alarm_value("saguin_publish_refused_total", s), 5.0)
        self.assertIsNone(app.alarm_value("saguin_publish_refused_total", []))

    def test_a_selector_alarm_opens_on_the_named_provider_and_not_the_other(self):
        """A `{provider="…"}` alarm is recorded, and against the one it names.

        This ran end to end rather than against `alarm_value` alone because
        the grammar half already agreed and the bug was in the value half:
        the recorder matched a sample name against the *whole* reference,
        selector text included, so no sample could ever equal it - the value
        was always None, None never fires, and the episode the red card
        implied was never opened. Two providers here, only the named one past
        the threshold, so a fix that merely summed the family would open on
        the wrong number and this would still be red.
        """
        app.ALARM_SPECS = [{"dash": "D", "title": "Mem store",
                            "metric": 'saguin_provider_bytes{provider="mem"}',
                            "expr": ">= 4000"}]
        app.ALARM_KEYS = {("D", "Mem store")}
        samples = [("saguin_provider_bytes", {"provider": "mem"}, 4096.0),
                   ("saguin_provider_bytes", {"provider": "sqlite"}, 12.0)]
        self.assertEqual(app.alarm_value('saguin_provider_bytes{provider="mem"}',
                                         samples), 4096.0)
        self.assertEqual(app.alarm_value('saguin_provider_bytes{provider="sqlite"}',
                                         samples), 12.0)
        self.assertIsNotNone(app.evaluate_alarms(samples))
        self.assertEqual(len(app.alarm_episodes), 1)
        ep = app.alarm_episodes[0]
        self.assertIsNone(ep["ended"])
        self.assertEqual(ep["peak"], 4096.0)      # the named provider's, not the sum
        # And the one it does not name cannot open it: drop mem below the
        # threshold while sqlite stays where it was, and the episode closes.
        cleared = [("saguin_provider_bytes", {"provider": "mem"}, 1.0),
                   ("saguin_provider_bytes", {"provider": "sqlite"}, 12.0)]
        self.assertIsNotNone(app.evaluate_alarms(cleared))
        self.assertIsNotNone(app.alarm_episodes[0]["ended"])

    def test_a_selector_naming_a_provider_that_is_absent_never_fires(self):
        """Absent stays None rather than becoming zero, selector and all - so
        `< n` on a provider the broker is not running does not fire."""
        app.ALARM_SPECS = [{"dash": "D", "title": "Gone",
                            "metric": 'saguin_provider_bytes{provider="nope"}',
                            "expr": "< 1"}]
        app.ALARM_KEYS = {("D", "Gone")}
        samples = [("saguin_provider_bytes", {"provider": "mem"}, 4096.0)]
        self.assertIsNone(app.alarm_value('saguin_provider_bytes{provider="nope"}',
                                          samples))
        self.assertIsNone(app.evaluate_alarms(samples))
        self.assertEqual(app.alarm_episodes, [])

    def test_an_episode_opens_firing_and_closes_when_it_clears(self):
        self.assertIsNone(app.evaluate_alarms(self._s(0)))     # not firing, no change
        self.assertEqual(app.alarm_episodes, [])
        self.assertIsNotNone(app.evaluate_alarms(self._s(2)))  # fires -> opens
        self.assertEqual(len(app.alarm_episodes), 1)
        self.assertIsNone(app.alarm_episodes[0]["ended"])
        self.assertIsNone(app.evaluate_alarms(self._s(3)))     # steady -> no edge, no write
        self.assertEqual(app.alarm_episodes[0]["peak"], 3.0)   # peak still tracked
        self.assertIsNotNone(app.evaluate_alarms(self._s(0)))  # clears -> closes
        self.assertIsNotNone(app.alarm_episodes[0]["ended"])
        self.assertEqual(app.alarm_firing, {})

    def test_a_closed_episode_past_five_days_is_trimmed_but_an_open_one_is_kept(self):
        now = time.time()
        app.alarm_episodes.append({"id": 1, "dash": "D", "title": "Refused",
            "metric": "m", "started": now - app.HISTORY_RETENTION - 1000,
            "ended": now - app.HISTORY_RETENTION - 500, "peak": 1})   # old, closed
        old_open = {"id": 2, "dash": "D", "title": "Refused", "metric": "m",
                    "started": now - app.HISTORY_RETENTION - 1000, "ended": None, "peak": 1}
        app.alarm_episodes.append(old_open)
        app.alarm_firing[("D", "Refused")] = old_open
        app.evaluate_alarms(self._s(1))                          # still firing
        self.assertEqual([e["id"] for e in app.alarm_episodes], [2])  # closed one gone, open kept

    def test_persist_and_reload_keeps_a_firing_episode_whole(self):
        app.save_alarms(app.evaluate_alarms(self._s(2)))        # opens and persists
        app.alarm_episodes.clear(); app.alarm_firing.clear(); app._alarm_next_id = 1
        app.load_alarms()                                       # restart
        self.assertEqual(len(app.alarm_episodes), 1)
        self.assertIn(("D", "Refused"), app.alarm_firing)       # reloaded as still firing
        self.assertIsNone(app.evaluate_alarms(self._s(3)))      # continues, does not split
        self.assertEqual(len(app.alarm_episodes), 1)

    def _db_ended(self):
        import sqlite3
        conn = sqlite3.connect(app.HISTORY_FILE)
        try:
            return conn.execute("SELECT ended FROM alarms").fetchone()[0]
        finally:
            conn.close()

    def test_reload_persists_the_close_of_an_orphaned_episode(self):
        # An episode still firing when its alarm is removed
        # from the config was closed in memory but never on disk, so the db row's
        # ended stayed NULL - the load filter always matched it (never aged out)
        # and its clear time was re-stamped every restart. The close must be
        # persisted; asserting only the in-memory object (as the first version of
        # this test did) is an in-memory-only hole.
        app.save_alarms(app.evaluate_alarms(self._s(2)))        # opens, persists ended NULL
        app.ALARM_SPECS, app.ALARM_KEYS = [], set()             # alarm removed from config
        app.alarm_episodes.clear(); app.alarm_firing.clear()
        app.load_alarms()                                       # closes the orphan
        self.assertEqual(len(app.alarm_episodes), 1)
        self.assertEqual(app.alarm_firing, {})
        self.assertIsNotNone(app.alarm_episodes[0]["ended"])    # closed in memory
        self.assertIsNotNone(self._db_ended(), "orphan close not persisted; db ended still NULL")
        # ...and stable: a second restart does not re-stamp the clear time.
        first = app.alarm_episodes[0]["ended"]
        app.alarm_episodes.clear(); app.alarm_firing.clear()
        app.load_alarms()
        self.assertEqual(app.alarm_episodes[0]["ended"], first)

    def test_an_orphaned_episode_past_five_days_no_longer_reloads(self):
        # Once its close is persisted, an orphan ages out like any closed episode
        # - the durability half of the same fix: a NULL ended would always reload.
        app.save_alarms(app.evaluate_alarms(self._s(2)))
        app.ALARM_SPECS, app.ALARM_KEYS = [], set()
        app.alarm_episodes.clear(); app.alarm_firing.clear()
        app.load_alarms()                                       # closes + persists
        # push its persisted clear time past the window, then reload
        import sqlite3
        conn = sqlite3.connect(app.HISTORY_FILE)
        with conn:
            conn.execute("UPDATE alarms SET ended = ?", (time.time() - app.HISTORY_RETENTION - 10,))
        conn.close()
        app.alarm_episodes.clear(); app.alarm_firing.clear()
        app.load_alarms()
        self.assertEqual(app.alarm_episodes, [])               # aged out, not reloaded

    def test_the_endpoint_reports_firing_and_recent(self):
        app.evaluate_alarms(self._s(2))                         # one firing
        app.evaluate_alarms(self._s(2))                         # (steady)
        # a second alarm that fired and cleared, for the recent list
        app.alarm_episodes.append({"id": 99, "dash": "D", "title": "Old",
            "metric": "m", "started": time.time() - 100, "ended": time.time() - 10, "peak": 1})
        body = app.app.test_client().get("/api/alarms").get_json()
        self.assertEqual(body["defined"], 1)
        self.assertEqual(len(body["firing"]), 1)
        self.assertEqual(body["firing"][0]["title"], "Refused")
        self.assertEqual(len(body["recent"]), 1)
        self.assertEqual(body["recent"][0]["title"], "Old")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TheBrokersViewsAreOneListRatherThanTwo(unittest.TestCase):
    """The folding sidebar reads its group from `opsViews`, and so does its
    count and the row it keeps visible while folded.

    Written down because the failure is quiet: a tenth view added as a
    literal button beside the list would render, and would be missing from
    the count, and would vanish when the group is folded even while its own
    panel is open beside it. Nothing would be red and the page would simply
    be wrong about where you are.
    """

    def setUp(self):
        self.js = (VIEWER / "static" / "app.js").read_text()

    def test_no_broker_view_is_written_outside_the_list(self):
        literal = re.findall(r'sidebarButton\("', self.js)
        self.assertEqual(literal, [],
                         "a sidebar button is written with a literal key rather than coming "
                         "from opsViews: folded, the group will not count it and will not "
                         "keep it visible when it is the one selected")

    def test_the_list_holds_the_views_and_is_what_the_group_renders(self):
        m = re.search(r"const opsViews = \[(.*?)\n  \];", self.js, re.S)
        self.assertIsNotNone(m, "no opsViews list in app.js")
        keys = re.findall(r'\["([a-z]+)",', m.group(1))
        self.assertGreaterEqual(len(keys), 9,
                                f"only found {keys} in opsViews - the regex missed some")
        # Every key names a panel the right-hand side can actually show, so a
        # typo is a button that opens nothing rather than a silent no-op.
        for key in keys:
            self.assertIn(f'view === "{key}"', self.js,
                          f"opsViews offers {key!r} and nothing renders it")
        # And the group is rendered from the list rather than from a copy.
        #
        # **The icon rides along and is not what this pins.** The property is
        # that the group renders from the one list, so an entry added there
        # appears in the sidebar and nowhere else has to be edited. Spelled
        # as an exact source string, that property went red when each row
        # gained an icon - and stayed red unnoticed, because the target
        # running this suite could not fail.
        self.assertIn(
            "opsViews.map(([key, label, icon]) => sidebarButton(key, label, icon))",
            self.js)
        self.assertIn("opsViews.filter(([key]) => view === key)", self.js)

    def test_the_group_starts_folded_and_is_remembered(self):
        # Folded unless the cookie says otherwise: the complaint this answers
        # is about what the first paint looks like.
        self.assertIn('cookie("saguin_viewer_ops_open") === "1"', self.js)
        self.assertIn('cookie("saguin_viewer_ops_open", v ? "1" : "0")', self.js)


class TheDisconnectEndpointRefusesBeforeItPublishes(unittest.TestCase):
    """What /api/disconnect settles without a byte going on the wire.

    Each of these would otherwise be a publish that cannot be answered - and
    the first one takes the page's own connection down to say so, which reads
    as the viewer having crashed rather than as a request it should not have
    made.
    """

    def setUp(self):
        self.c = app.app.test_client()

    def test_no_client_id_is_refused(self):
        r = self.c.post("/api/disconnect", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("name the client", r.get_json()["error"])

    def test_naming_the_viewers_own_connection_is_refused(self):
        # The broker refuses this too, and its refusal arrives on the very
        # connection it is about - so the page settles it first and keeps the
        # answer it would otherwise lose.
        r = self.c.post("/api/disconnect", json={"client_id": app.MQTT_CFG["client_id"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cannot hang itself up", r.get_json()["error"])

    def test_a_3_1_1_viewer_says_so_rather_than_trying(self):
        saved = app.PROTOCOL
        app.PROTOCOL = app.mqtt.MQTTv311
        try:
            r = self.c.post("/api/disconnect", json={"client_id": "somebody"})
            self.assertEqual(r.status_code, 400)
            said = r.get_json()["error"]
            self.assertIn("3.1.1", said)
            # And it says which setting moves it, rather than only what is wrong.
            self.assertIn("protocol", said)
        finally:
            app.PROTOCOL = saved


class WhatTheBrokerSaysAboutAPublishOnTheMainConnection(unittest.TestCase):
    """A refusal on the page's own connection is read, rather than waited out.

    **This is the defect the endpoint was written with.** A refused control
    publish produces no reply, so waiting for one and calling the silence a
    timeout reports "the broker is slow" about a broker that said no
    immediately - and the operator is left with five seconds and nothing to
    act on. Driven here: the page's `on_publish` records what the broker
    answered, keyed by the message id the publish returned.
    """

    def setUp(self):
        app.pubacks.clear()

    def tearDown(self):
        app.pubacks.clear()

    def test_a_reason_code_is_kept_for_the_publish_it_belongs_to(self):
        from paho.mqtt.reasoncodes import ReasonCode
        app.on_publish(None, None, 7,
                       reason_code=ReasonCode(app.PacketTypes.PUBACK, "Not authorized"))
        self.assertIn(7, app.pubacks)
        self.assertGreaterEqual(int(app.pubacks[7].value), 0x80)

    def test_a_publish_with_no_reason_code_is_not_recorded(self):
        # QoS 0, and MQTT 3.1.1 at any QoS: there is no reason code to keep,
        # and an entry under the message id would read as an answer.
        app.on_publish(None, None, 9, reason_code=None)
        self.assertNotIn(9, app.pubacks)

    def test_the_record_does_not_grow_without_bound(self):
        # A page that runs for weeks sends control publishes for weeks. This
        # is a dict nothing else empties, so it empties itself.
        from paho.mqtt.reasoncodes import ReasonCode
        for mid in range(400):
            app.on_publish(None, None, mid,
                           reason_code=ReasonCode(app.PacketTypes.PUBACK, "Success"))
        self.assertLessEqual(len(app.pubacks), 256)
        # And what it keeps is the recent end, which is what a caller waiting
        # on an answer is about to ask for.
        self.assertIn(399, app.pubacks)


class WhatThePageRemembersAboutPuttingWorkBack(unittest.TestCase):
    """The mark that says a dead letter has already been redriven.

    **It cannot live on the record.** A dead-letter channel is an `append`
    channel and a record written once never changes; marking the redriven job
    instead would put the mark on the active site's copy and not on a passive
    site's. So it is kept here, and everything below is about it being kept
    correctly: counted, expired with the record it is about, and written down
    so a restart does not turn a handled row back into an unhandled one.
    """

    def setUp(self):
        app.redriven.clear()
        self.saved = app.HISTORY_FILE

    def tearDown(self):
        app.redriven.clear()
        app.HISTORY_FILE = self.saved

    def test_a_second_redrive_counts_rather_than_replaces(self):
        # The count is the decision: a job on its third trip through the queue
        # is one whose bug is not fixed.
        with unittest.mock.patch.object(app, "dlq_retention", return_value=None):
            app.note_redrive("id-1", "alice", "jobs__dlq")
            app.note_redrive("id-1", "bob", "jobs__dlq")
        note = app.redrive_note("id-1")
        self.assertEqual(note["count"], 2)
        self.assertEqual(note["by"], "bob", "the most recent hand is the one to name")

    def test_work_with_no_identity_is_not_marked(self):
        # A record with no saguin-id cannot be followed across the round trip,
        # and inventing a key would mark one arbitrary row.
        with unittest.mock.patch.object(app, "dlq_retention", return_value=None):
            app.note_redrive("", "alice", "jobs__dlq")
        self.assertEqual(app.redriven, {})
        self.assertIsNone(app.redrive_note(""))

    def test_a_mark_expires_with_the_record_it_is_about(self):
        """**The window is the dead-letter channel's, not this page's.**

        A mark outliving its record is a row nobody can see; one expiring first
        is a row that silently forgets it was put back. So it is measured
        against that channel's own `dlq_retention_period`, and two channels on
        one broker need not agree.
        """
        app.redriven.update({
            "old": {"at": time.time() - 1000, "by": "a", "count": 1, "channel": "jobs__dlq"},
            "new": {"at": time.time() - 10, "by": "a", "count": 1, "channel": "jobs__dlq"},
            "other": {"at": time.time() - 1000, "by": "a", "count": 1, "channel": "slow__dlq"},
        })
        windows = {"jobs__dlq": 900, "slow__dlq": 86400}
        with unittest.mock.patch.object(app, "dlq_retention", windows.get):
            app.prune_redriven()
        self.assertNotIn("old", app.redriven, "a mark outlived its channel's retention")
        self.assertIn("new", app.redriven)
        self.assertIn("other", app.redriven,
                      "a channel with a longer retention lost its mark to another's window")

    def test_a_channel_that_keeps_everything_is_capped_instead(self):
        # `retention: none` means the broker keeps dead letters for ever, and
        # this page must not - so the size cap is the backstop, oldest first.
        now = time.time()
        for i in range(app.REDRIVE_MAX + 50):
            app.redriven[f"id-{i}"] = {"at": now + i, "by": "a", "count": 1,
                                       "channel": "forever__dlq"}
        with unittest.mock.patch.object(app, "dlq_retention", return_value=None):
            app.prune_redriven()
        self.assertLessEqual(len(app.redriven), app.REDRIVE_MAX)
        self.assertNotIn("id-0", app.redriven, "the oldest should go first")
        self.assertIn(f"id-{app.REDRIVE_MAX + 49}", app.redriven,
                      "the newest marks are the ones somebody is about to look at")

    def test_the_marks_survive_a_restart(self):
        """**Written down, or a restart turns a handled row back into an
        unhandled one** - and the operator puts the same work back twice,
        which is the whole thing this prevents."""
        with tempfile.TemporaryDirectory() as d:
            app.HISTORY_FILE = os.path.join(d, "history.db")
            with unittest.mock.patch.object(app, "dlq_retention", return_value=None):
                app.note_redrive("id-kept", "alice", "jobs__dlq")
                # The restart: nothing in memory, and the file is all there is.
                app.redriven.clear()
                self.assertIsNone(app.redrive_note("id-kept"))
                app.load_redrives()
            note = app.redrive_note("id-kept")
            self.assertIsNotNone(note, "the mark did not survive a restart")
            self.assertEqual((note["count"], note["by"]), (1, "alice"))

    def test_with_no_history_file_it_still_works_for_this_run(self):
        # Persistence is a configuration, not a requirement: a viewer with no
        # history_file keeps its marks in memory and says nothing about it.
        app.HISTORY_FILE = ""
        with unittest.mock.patch.object(app, "dlq_retention", return_value=None):
            app.note_redrive("id-mem", "alice", "jobs__dlq")
        self.assertEqual(app.redrive_note("id-mem")["count"], 1)
        app.load_redrives()          # a no-op rather than an error
        self.assertEqual(app.redrive_note("id-mem")["count"], 1)


class TheRetentionAMarkIsMeasuredAgainst(unittest.TestCase):
    """`dlq_retention` reads the queue's own key, per channel.

    A dead-letter channel is derived and has no configuration block of its
    own, so its retention is written on the queue as `dlq_retention_period` -
    which is the one place this page has to undo the broker's naming rule.
    """

    def answer(self, doc):
        return unittest.mock.patch.object(app, "ops_json", return_value=doc)

    def test_it_reads_the_queues_key_for_the_derived_channel(self):
        with self.answer({"channels": {"jobs": {"dlq_retention_period": "900s"}}}):
            self.assertEqual(app.dlq_retention("jobs__dlq"), 900)

    def test_none_means_keep_the_mark(self):
        # The broker keeps those dead letters for ever; the size cap is what
        # bounds the page instead.
        with self.answer({"channels": {"jobs": {"dlq_retention_period": "none"}}}):
            self.assertIsNone(app.dlq_retention("jobs__dlq"))

    def test_a_channel_it_cannot_resolve_keeps_the_mark(self):
        """Forgetting that a job was put back is the failure this exists to
        prevent, so an unanswerable question holds the mark rather than
        dropping it. Holding one too long costs a row of text."""
        for doc in ({"error": "not authorized"}, {"channels": {}},
                    {"channels": {"jobs": {}}}):
            with self.subTest(doc=doc), self.answer(doc):
                self.assertIsNone(app.dlq_retention("jobs__dlq"))


class WhoLostRecordsSaysWhatTheCounterCannot(unittest.TestCase):
    """The page for `/v1/operations/position-lost`, and the two things it must
    not quietly leave out.

    **A reader that was never told is the only row on the page nothing else
    in the system will ever report.** A consumer overtaken by the retention
    floor while connected and reading cannot be told - MQTT has no way to say
    it - so the device believes it is up to date and whoever opens this page
    is the only party that knows. Drawn as one column among eight it is the
    easiest thing on the page to miss, so the row carries a class of its own
    and the stylesheet spends the loss colour on it.

    **And the three numbers travel together.** `returned` is what the body
    carried, `tracked` what the broker is holding, `beyond` what never fit in
    the record at all. A page that draws rows and says neither of the other
    two has an operator believe they have seen the whole fleet - which is the
    failure `beyond` exists in RFC 0005 to stop.
    """

    # The contract's own shape, which is what the broker answers. Kept whole
    # rather than trimmed to the fields a case reads: a shape this page is
    # wrong about is the defect, and a fixture edited down to fit cannot show
    # it.
    #
    # **Two of these three rows are fixtures and nothing else.** A broker
    # under retention pressure produces `kind: "consumer"` with
    # `reported: false` by the hundred - that arm has been watched live. A
    # `session` row needs a durable session to reconnect after its position
    # was passed, and a `bridge` row needs an outbound bridge rule
    # configured; neither happens by itself, so they are held down here
    # rather than claimed to have been seen.
    BODY = {
        "readers": [
            {"reader": "mqtt:device-7", "channel": "events", "kind": "consumer",
             "reported": False, "count": 2154, "records_missed": 1852128,
             "last_position": 412, "last_floor": 5820,
             "last_seen": "2026-09-23T08:21:45Z"},
            {"reader": "bridge:head-office", "channel": "events", "kind": "bridge",
             "reported": False, "count": 3, "records_missed": 4100,
             "last_position": 1720, "last_floor": 5820,
             "last_seen": "2026-09-23T08:21:02Z"},
            {"reader": "mqtt:stocktake", "channel": "events", "kind": "session",
             "reported": True, "count": 1, "records_missed": 90,
             "last_position": 5730, "last_floor": 5820,
             "last_seen": "2026-09-23T08:20:11Z"},
        ],
        "returned": 3, "tracked": 41, "beyond": 7,
    }

    def setUp(self):
        self.js = (VIEWER / "static" / "app.js").read_text()
        m = re.search(r"\nfunction PositionLost\(\) \{(.*?)\n\}\n", self.js, re.S)
        self.assertIsNotNone(m, "no PositionLost component in app.js")
        self.panel = m.group(1)

    def test_the_endpoint_asks_the_route_and_hands_the_body_back_whole(self):
        asked = []

        def fake(path):
            asked.append(path)
            return self.BODY

        with unittest.mock.patch.object(app, "ops_json", fake):
            body = app.app.test_client().get("/api/position-lost").get_json()
        self.assertEqual(asked, ["/v1/operations/position-lost"])
        self.assertEqual(body, self.BODY)

    def test_a_credential_that_cannot_reach_it_degrades_rather_than_raising(self):
        """`ops_json` answers a 403 with a sentence, and this route must pass
        that through like its siblings - the panel says why it is empty rather
        than the page losing the tree beside it."""
        said = {"error": "viewer does not reach /v1/operations/position-lost"}
        with unittest.mock.patch.object(app, "ops_json", return_value=said):
            r = app.app.test_client().get("/api/position-lost")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), said)

    def test_the_view_is_offered_in_the_sidebar_and_rendered(self):
        self.assertIn('["positionlost", "Who lost records"', self.js)
        self.assertIn('view === "positionlost" ? html`<${PositionLost} />', self.js)

    def test_an_untold_reader_is_marked_and_the_stylesheet_marks_it(self):
        self.assertIn('class=${r.reported ? "" : "silent"}', self.panel)
        css = (VIEWER / "static" / "app.css").read_text()
        self.assertIn("table.ops tr.silent td", css,
                      "the untold row carries a class the stylesheet does not draw, "
                      "so silent loss is a row that reads like every other")

    def test_all_three_of_the_brokers_numbers_reach_the_page(self):
        """Counted rather than spot-checked: a panel that surfaces two of the
        three passes every assertion written about the two."""
        missing = [n for n in ("returned", "tracked", "beyond")
                   if f"data.{n}" not in self.panel]
        self.assertEqual(missing, [],
                         f"the panel never reads {missing} - rows the broker holds or "
                         f"never kept would be invisible to whoever is reading it")

    # **The identifier is `reader` and it carries a scheme.** A position is
    # stored under `mqtt:<client id>` or `bridge:<rule name>`, because a
    # client may legally call itself `bridge:head-office` and without the
    # scheme a device and a bridge link would share one row. The same
    # prefixed string keys `/v1/operations/consumers`, so it is the one an
    # operator copies from here to look a reader up in *Who is behind*.
    # Reading `client_id` instead draws a blank cell - the key is not in the
    # body - and a blank cell in an identity column reads as a reader with no
    # name rather than as a bug.
    IDENTITY_SITES = (
        ("the export's first column", 'rows.map(r => [r.reader,'),
        ("the row's React key", 'key=${r.reader + "\\u0000" + r.channel}'),
        ("the cell an operator copies", '<td><code>${r.reader}</code>'),
    )

    def test_every_place_the_panel_names_a_reader_reads_the_reader_field(self):
        """Counted, and each site named: a panel that fixed the cell and left
        the export on the old key passes any assertion written about the
        cell."""
        missing = [what for what, src in self.IDENTITY_SITES
                   if src not in self.panel]
        self.assertEqual(
            missing, [],
            f"{missing} do not read `reader`; the broker sends no `client_id` "
            f"on this route, so each of those is blank or undefined")
        self.assertEqual(len(self.IDENTITY_SITES), 3,
                         "the sweep stopped naming all three identity sites")
        self.assertNotIn(
            "client_id", self.panel,
            "this route's rows have no `client_id` - reading one draws an "
            "empty identity cell, which reads as a nameless reader rather "
            "than as a bug")

    def test_the_scheme_is_not_stripped_off_what_an_operator_copies(self):
        """**The prefix is the join, so tidying it away breaks it.** The
        consumers route keys its rows by the same prefixed string; a cell
        showing the bare id gives an operator something to paste that matches
        nothing there, and re-creates the collision between a device called
        `bridge:head-office` and the bridge rule of that name."""
        tidying = ["split(\":\")", "indexOf(\":\")", "replace(/^", "slice(5)",
                   "substring(", "\"mqtt:\"", "\"bridge:\""]
        found = [idiom for idiom in tidying if idiom in self.panel]
        self.assertEqual(len(tidying), 7, "the sweep lost one of its idioms")
        self.assertEqual(
            found, [],
            f"the panel is taking the scheme apart with {found}; the whole "
            f"`reader` string is what joins this view to Who is behind")

    def test_the_export_carries_the_whole_row_the_broker_sent(self):
        """node runs the panel's own `exportCsv`, so what the button writes is
        what is asserted rather than a Python copy of it that can drift. The
        `bridge` and `session` rows here are fixtures: this broker's data is
        all untold consumers."""
        if shutil.which("node") is None:
            self.skipTest("no node on PATH - needed to drive static/app.js")
        start = self.panel.index("const exportCsv")
        src = self.panel[start:self.panel.index("]));", start) + 4]
        probe = ("let got = null;\n"
                 "function downloadCsv(kind, header, rows) "
                 "{ got = { kind, header, rows }; }\n"
                 "const rows = " + json.dumps(self.BODY["readers"]) + ";\n"
                 + src + "\nexportCsv();\n"
                 "process.stdout.write(JSON.stringify(got));")
        d = tempfile.mkdtemp()
        try:
            f = pathlib.Path(d) / "probe.js"
            f.write_text(probe)
            out = subprocess.run(["node", str(f)], capture_output=True,
                                 text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            got = json.loads(out.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(got["kind"], "position-lost")
        self.assertEqual(got["header"][0], "reader")
        self.assertEqual([row[0] for row in got["rows"]],
                         ["mqtt:device-7", "bridge:head-office",
                          "mqtt:stocktake"],
                         "the export wrote something other than the broker's "
                         "own prefixed reader, so a spreadsheet of these rows "
                         "cannot be joined back to the consumers route")
        self.assertEqual(got["rows"][0],
                         ["mqtt:device-7", "events", "consumer", "no",
                          1852128, 2154, 412, 5820, "2026-09-23T08:21:45Z"])
        self.assertEqual(got["rows"][2][3], "yes",
                         "a reader that was told is exported as told")

    def test_each_kind_is_explained_in_its_own_terms(self):
        """The tooltip says why a reader knows or does not, and the three
        answers are different. **Only the untold-consumer arm has been seen
        live** - a broker under retention pressure makes those by the
        hundred; a told `session` and a `bridge` are fixtures here, so this
        case is what holds them."""
        if shutil.which("node") is None:
            self.skipTest("no node on PATH - needed to drive static/app.js")
        start = self.panel.index("const told = r =>")
        src = self.panel[start:self.panel.index("\n  return html", start)]
        probe = (src + "\nconst rows = "
                 + json.dumps(self.BODY["readers"]) + ";\n"
                 "process.stdout.write(JSON.stringify(rows.map(told)));")
        d = tempfile.mkdtemp()
        try:
            f = pathlib.Path(d) / "probe.js"
            f.write_text(probe)
            out = subprocess.run(["node", str(f)], capture_output=True,
                                 text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            said = json.loads(out.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(len(said), 3)
        self.assertIn("MQTT has no way to tell it", said[0],
                      "an untold consumer must say why nothing can reach it")
        self.assertNotIn("MQTT has no way", said[1],
                         "a bridge rule is not a device MQTT could have told")
        self.assertIn("Session Present = 0", said[2],
                      "a told reader must say what told it")
        self.assertEqual(len(set(said)), 3,
                         "two kinds share one explanation, so the page says "
                         "the same thing about a reader that knows and one "
                         "that never will")

    def test_it_does_not_offer_the_record_as_history(self):
        """The broker keeps this in memory on purpose and a restart empties
        it, so an empty page is "nothing since the last start" rather than
        "nothing ever" - and a page that does not say so is read as the
        second."""
        self.assertIn("restart empties it", self.panel)


class TheCsvExportDoesNotHandFormulasToASpreadsheet(unittest.TestCase):
    """**Whose strings fill these cells decides how careful the writer has
    to be.**

    The sessions export writes `client_id` and `user`, and the consumer
    export writes a client id again - and a client id is any string a
    device chose for itself at CONNECT. A field starting with `=`, `+`,
    `-`, `@`, a tab or a carriage return is read as a formula by a
    spreadsheet opening the CSV, so a device that can reach the broker
    could name itself as one, wait in the sessions table, and run on the
    operator's machine when somebody opens the export. Quoting is no help:
    the `=` is still the first character once the field is unquoted.

    node runs the page's own function rather than a copy of it in Python,
    for the reason the YAML writer's tests give: the copy is the thing
    that drifts.
    """

    @classmethod
    def setUpClass(cls):
        if shutil.which("node") is None:
            raise unittest.SkipTest("no node on PATH - needed to drive static/app.js")
        js = (VIEWER / "static" / "app.js").read_text()
        start = js.find("function csvField")
        if start < 0:
            raise AssertionError("csvField is not in app.js")
        cls.src = js[start:js.index("\n}\n", start) + 3]

    def written(self, values):
        d = tempfile.mkdtemp()
        try:
            f = pathlib.Path(d) / "probe.js"
            f.write_text(self.src + "\nprocess.stdout.write(JSON.stringify("
                         + json.dumps(values) + ".map(csvField)));")
            out = subprocess.run(["node", str(f)], capture_output=True, text=True,
                                 timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_field_a_spreadsheet_would_run_is_defused(self):
        dangerous = ["=cmd|' /C calc'!A0", "+2+5", "-1+1", "@SUM(A1)",
                     "\t=1+1", "\r=1+1", '=HYPERLINK("http://x","open")']
        for value, out in zip(dangerous, self.written(dangerous)):
            stripped = out[1:-1].replace('""', '"') if out.startswith('"') else out
            self.assertTrue(
                stripped.startswith("'"),
                "{!r} was written as {!r}: a spreadsheet opening this file runs "
                "it, and the strings in these columns are chosen by whoever "
                "connected".format(value, out))

    def test_a_number_is_still_a_number(self):
        """The defusing turns a cell into text, so it must not reach values
        that are only leading a `-` because they are negative. A column of
        numbers that stopped adding up would be the fix causing its own
        bug."""
        numbers = ["-42", "-1.5", "+7", "0", "3.5"]
        self.assertEqual(self.written(numbers), numbers)

    def test_ordinary_quoting_is_unchanged(self):
        self.assertEqual(
            self.written(["plain", "has,comma", 'has"quote', "has\nnewline"]),
            ["plain", '"has,comma"', '"has""quote"', '"has\nnewline"'])


class TheViewerTargetCannotSwallowItsSuite(unittest.TestCase):
    """**An ordinary test rather than a Makefile target**, so it cannot be
    skipped by somebody who runs the suite but not the Makefile - and
    because the thing it checks is a Makefile that was checking itself.

    `make viewer` used to run the suite through `grep`. make judges a
    recipe line by the last command in the pipeline, so the suite's own
    exit was thrown away: the target printed `FAILED (failures=1)` and
    exited 0, and the CI job that runs it verbatim was green over a red
    suite. It had been unable to fail since the day it was written, and
    the first failure it ever had, it hid.
    """

    def setUp(self):
        self.makefile = (REPO / "Makefile").read_text()

    def recipe(self):
        """The viewer target's recipe lines, which are the tab-indented
        ones after `viewer:` and before the next target."""
        lines, inside = [], False
        for line in self.makefile.splitlines():
            if line.startswith("viewer:"):
                inside = True
                continue
            if inside:
                if line.startswith("\t"):
                    lines.append(line)
                elif line.strip() and not line.startswith("#"):
                    break
        return lines

    def test_the_suite_is_run_without_a_pipe_after_it(self):
        recipe = self.recipe()
        self.assertTrue(recipe, "no recipe found for the viewer target")
        running = [l for l in recipe if "unittest" in l]
        self.assertEqual(
            len(running), 1,
            "expected exactly one line running the suite, found {}: this check "
            "reports on what it counted, and a pattern that stopped matching "
            "would pass by comparing nothing".format(len(running)))
        self.assertNotIn(
            "|", running[0],
            "the suite's output is piped, so make takes the exit of the last "
            "command in the pipeline and the suite's own is discarded - a "
            "failing suite would leave this target, and CI, green")


class TheMetricListDoesNotDriftFromTheBroker(unittest.TestCase):
    """**Nothing checked this list until now.** `ALLOWED_METRICS` in app.py is
    the viewer's own copy of saguin's metric catalogue, and a copy with no
    check is a copy that is already wrong: a metric the broker started
    publishing is one the page silently cannot draw, and a metric the broker
    stopped publishing is a card that draws nothing with no error anywhere.

    Four lists, and RFC 0005 is the oracle for all of them. Its catalogue is
    closed - "a name here is a promise; a name not here is not published" - so
    it is what the broker is held to, and what the viewer is held to, rather
    than either being read to judge the other.

    The live broker is here because a specification the code does not follow
    would let both halves of a comparison against it agree and both be wrong.
    It is driven before it is scraped, and that order is the point:
    `min_scrape_interval` has a floor of a minute and answers an early scrape
    from the previous computation, so a test that drove the broker after its
    first scrape would read a cached body and conclude the metrics were
    missing. Everything happens, and then the first scrape of that process is
    taken.

    **One metric is exempt and it is exempt for what it is.**
    `saguin_storage_errors_total` has, in RFC 0005's own words, "no series
    until a provider has failed", so a healthy broker publishes none - and
    breaking a store to produce one would be a test about the harness. Every
    other name in the catalogue is driven out of a real broker below.
    """

    # The four `*_info` families are the documented exclusion: each is the
    # constant 1 with its facts in labels, so there is no number to draw. The
    # comment in app.py under ALLOWED_METRICS says so. Naming them here rather
    # than pattern-matching `_info` keeps the exclusion a list somebody chose,
    # which a new `*_info` metric has to be added to on purpose.
    INFO_FAMILIES = {"saguin_build_info", "saguin_channel_info",
                     "saguin_provider_info", "saguin_bridge_info"}

    UNREACHABLE = {"saguin_storage_errors_total"}

    CONFIG = """\
broker:
  id: drift-test
  log_level: error
  mqtt:
    listen:
      tcp:
        address: 127.0.0.1:{mqtt}
    password_file: {passwd}
  operations:
    listen:
      tcp:
        address: 127.0.0.1:{ops}
  limits:
    # Small, so that the capped provider below can be filled by a handful of
    # publishes rather than by two megabytes of them.
    max_message_size: 4KiB
  storage:
    default: mem
    default_retention_period: none
    default_retention_bytes: none
    providers:
      - mem:
          type: memory
          snapshot_dir: none
        tiny:
          type: memory
          snapshot_dir: none
          # A provider holds back one largest message plus headers for the
          # operations that free it, so this is the smallest a 4KiB message
          # size allows.
          max_bytes: 64KiB
        disk:
          type: sqlite
          file_path: {db}
channels:
  - events:
      type: append
      filter: iot/+/events/+
      # On the sqlite provider, so that the storage counters have a provider
      # that commits.
      storage: disk
    state:
      type: latest
      filter: iot/+/state/+
    full:
      type: append
      filter: iot/+/full/+
      storage: tiny
    jobs:
      type: queue
      filter: iot/+/work/+
bridges:
  head-office:
    # Nothing listens there. A bridge that cannot connect still publishes
    # every bridge series, which is what this is for - and the one thing an
    # operator most wants a metric for is a link that is down.
    peer: tcp://127.0.0.1:{peer}
    client_id: drift-bridge
    topics:
      - filter: upstream/#
        topic: iot/depot/events/$#
        direction: in
"""

    @classmethod
    def free_port(cls):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    # Decided here, taken in setUp - see WhereATopicLands for why the count
    # matters. This is the class the count was most wrong about: eight methods
    # reported as one skip.
    why = None

    def setUp(self):
        if self.why:
            self.skipTest(self.why)

    @classmethod
    def setUpClass(cls):
        cls.binary = SAGUIN / "bin" / "saguin"
        cls.rfc = SAGUIN / "docs" / "rfcs" / "0005-operations.md"
        for path, how in ((cls.binary, "build it with `go build -o bin/saguin ./cmd/saguin`"),
                          (cls.rfc, "it is the oracle for every list here")):
            if not path.exists():
                cls.why = (f"no {path} - this needs saguin's own checkout: {how}, "
                           f"and set SAGUIN_REPO if it is not beside this one")
                return
        try:
            import paho.mqtt.client  # noqa: F401
        except ImportError:                                   # pragma: no cover
            cls.why = "paho-mqtt is not installed"
            return

        cls.dir = pathlib.Path(tempfile.mkdtemp())
        cls.mqtt_port, cls.ops_port = cls.free_port(), cls.free_port()
        passwd = cls.dir / "clients.passwd"
        subprocess.run([str(cls.binary), "--passwd", "add", str(passwd),
                        "device", "secret"],
                       capture_output=True, text=True, timeout=30, check=True)
        cfg = cls.dir / "saguin.yaml"
        cfg.write_text(cls.CONFIG.format(
            mqtt=cls.mqtt_port, ops=cls.ops_port, passwd=passwd,
            db=cls.dir / "saguin.db", peer=cls.free_port()))
        cls.log = open(cls.dir / "broker.log", "w+")
        cls.broker = subprocess.Popen([str(cls.binary), "--config", str(cfg)],
                                      stdout=cls.log, stderr=subprocess.STDOUT)
        try:
            cls.await_health()
            cls.drive()
            cls.body = cls.scrape("/metrics")
        except Exception:
            cls.tearDownClass()
            raise
        cls.served = cls.families(cls.body)

    @classmethod
    def tearDownClass(cls):
        for client in getattr(cls, "clients", []):
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        broker = getattr(cls, "broker", None)
        if broker is not None:
            broker.terminate()
            try:
                broker.wait(timeout=10)
            except subprocess.TimeoutExpired:                 # pragma: no cover
                broker.kill()
        log = getattr(cls, "log", None)
        if log is not None:
            log.close()
        if hasattr(cls, "dir"):
            shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def broker_log(cls):
        try:
            cls.log.flush()
            return (cls.dir / "broker.log").read_text()
        except Exception:                                     # pragma: no cover
            return "(no log)"

    @classmethod
    def url(cls, path):
        return f"http://127.0.0.1:{cls.ops_port}{path}"

    @classmethod
    def scrape(cls, path):
        with urllib.request.urlopen(cls.url(path), timeout=30) as r:
            return r.read().decode()

    @classmethod
    def await_health(cls):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if cls.broker.poll() is not None:                 # pragma: no cover
                raise AssertionError(
                    f"the broker exited with {cls.broker.returncode} before it "
                    f"served /health. It said:\n{cls.broker_log()}")
            try:
                cls.scrape("/health")
                return
            except Exception:
                time.sleep(0.05)
        raise AssertionError(                                 # pragma: no cover
            f"the broker never served /health. It said:\n{cls.broker_log()}")

    @classmethod
    def client(cls, client_id, **kw):
        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                        protocol=mqtt.MQTTv5)
        c.username_pw_set(kw.pop("user", "device"), kw.pop("password", "secret"))
        cls.clients.append(c)
        return c

    @classmethod
    def drive(cls):
        """Everything the catalogue counts, before the first scrape.

        Each step is here because one series does not exist until it happens.
        A broker doing nothing publishes most of the seventy-eight names,
        and a test that scraped that would agree with a viewer missing the rest.
        """
        import paho.mqtt.client as mqtt
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties
        cls.clients = []
        host = "127.0.0.1"

        # A refused CONNECT, for saguin_connections_refused_total{reason}.
        wrong = cls.client("wrong", password="not-the-password")
        try:
            wrong.connect(host, cls.mqtt_port, 30)
            wrong.loop_start()
            time.sleep(0.4)
            wrong.loop_stop()
        except Exception:
            pass

        # A SUBSCRIBE whose filter is refused, for
        # saguin_subscriptions_refused_total{reason}. A plain filter lying wholly
        # inside a queue's is refused in the SUBACK. (Paho will not send a
        # malformed filter, so this is the refusal it can drive.)
        refused = cls.client("refused")
        refused.connect(host, cls.mqtt_port, 30)
        refused.loop_start()
        refused.subscribe("iot/depot/work/w9", qos=1)
        time.sleep(0.4)
        refused.loop_stop()

        # A durable consumer that reads, for saguin_channel_consumer_position_min
        # and saguin_channel_consumers. It stays connected, so
        # saguin_connections_by_protocol has a client to count.
        props = Properties(PacketTypes.CONNECT)
        props.SessionExpiryInterval = 3600
        reader = cls.client("reader")
        reader.connect(host, cls.mqtt_port, 30, clean_start=False, properties=props)
        reader.loop_start()
        reader.subscribe("iot/+/events/+", qos=1)
        time.sleep(0.4)

        writer = cls.client("writer")
        writer.connect(host, cls.mqtt_port, 30)
        writer.loop_start()
        for topic in ("iot/depot/events/e1", "iot/depot/events/e2",
                      "iot/depot/state/s1", "iot/depot/work/w1"):
            writer.publish(topic, b"payload-bytes", qos=1).wait_for_publish()
        # A retain a queue ignores, and one a latest channel keeps.
        writer.publish("iot/depot/work/w2", b"retained", qos=1,
                       retain=True).wait_for_publish()
        writer.publish("iot/depot/state/s2", b"kept", qos=1,
                       retain=True).wait_for_publish()
        # Fill the capped provider until it refuses, for
        # saguin_publish_refused_total{reason="quota exceeded"}.
        blob = b"x" * 4000
        for i in range(30):
            writer.publish(f"iot/depot/full/f{i}", blob, qos=1).wait_for_publish()
        time.sleep(0.6)

        # A Will that fires, for the three saguin_wills_* series: connect with
        # one and drop the socket, which is the death a DISCONNECT is not.
        dying = cls.client("dying")
        dying.will_set("iot/depot/state/gone", b"bye", qos=1)
        dying.connect(host, cls.mqtt_port, 30)
        dying.loop_start()
        time.sleep(0.4)
        sock = dying.socket()
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()
        dying.loop_stop()
        time.sleep(1.0)

    @staticmethod
    def families(body):
        """The metric names a scrape carries, which are the lines that are
        neither HELP nor TYPE nor blank."""
        out = set()
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"([a-zA-Z_][a-zA-Z0-9_]*)[{ ]", line)
            if m:
                out.add(m.group(1))
        return out

    @classmethod
    def catalogue(cls):
        """The metric names RFC 0005's catalogue promises.

        The catalogue is a run of Markdown tables between two headings, one
        metric per row, each row opening with the name in backticks and its
        labels in braces. Bounded by the headings rather than read from the
        whole document, so the example scrape above it and the alert below it
        cannot contribute a name the catalogue does not.

        **The bound catches nothing today, and that was measured rather than
        assumed.** Removing it leaves every case in this class passing, because
        all seventy-eight metric rows in RFC 0005's tables are inside this section -
        the example scrape's lines are samples rather than table rows, and the
        alert's are in a code block. It stays as defence against that stopping
        being true, which a table of metrics anywhere else in the document would
        do. Whoever removes it should know it is belt-and-braces rather than
        load-bearing.
        """
        text = cls.rfc.read_text()
        start = text.index("\n### The catalogue\n")
        # To the section after, not the next heading: "The Go runtime" is a
        # bold title under "The one alert", and its ten rows are part of the
        # catalogue. The alert's own lines are a code block, which no row
        # pattern matches.
        end = text.index("\n### Labels, and where the catalogue stops", start)
        rows = re.findall(r"^\| `(saguin_[a-z0-9_]+)",
                          text[start:end], re.M)
        return rows

    def test_the_catalogue_was_read(self):
        """Count what the parse examined. A heading renamed in the RFC or a
        table written differently would hand every comparison below an empty
        set, and an empty set agrees with everything."""
        rows = self.catalogue()
        self.assertGreaterEqual(
            len(rows), 60,
            f"only {len(rows)} metrics parsed out of RFC 0005's catalogue - the "
            f"section bounds or the row shape have moved, and every comparison "
            f"in this class is reading a short list")
        self.assertEqual(len(rows), len(set(rows)),
                         "the catalogue names a metric twice")

    def test_the_allow_list_was_read(self):
        """The same question of the other list: app.py is walked as a syntax
        tree rather than matched as text, so a reformatting cannot shorten
        it silently."""
        self.assertGreaterEqual(
            len(self.allowed()), 50,
            f"only {len(self.allowed())} names came out of ALLOWED_METRICS")
        self.assertEqual(self.allowed(), set(app.ALLOWED_METRICS),
                         "the syntax tree and the imported module disagree about "
                         "what ALLOWED_METRICS holds")

    @staticmethod
    def allowed():
        """ALLOWED_METRICS' keys, read off app.py's syntax tree."""
        import ast
        tree = ast.parse((VIEWER / "app.py").read_text())
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "ALLOWED_METRICS"
                       for t in node.targets):
                continue
            return {k.value for k in node.value.keys}
        raise AssertionError("no ALLOWED_METRICS assignment in app.py")

    def test_the_broker_serves_every_metric_the_catalogue_promises(self):
        """The RFC is the oracle, so this is the test that keeps it one: a
        catalogue the broker does not actually serve would let the viewer
        agree with a document and disagree with the broker."""
        missing = set(self.catalogue()) - self.served - self.UNREACHABLE
        self.assertEqual(
            missing, set(),
            f"RFC 0005's catalogue promises these and the scrape does not carry "
            f"them: {sorted(missing)}. Either the broker no longer publishes "
            f"them, or `drive` above no longer reaches the state that makes "
            f"them exist.\nThe broker said:\n{self.broker_log()}")

    def test_the_broker_serves_nothing_the_catalogue_does_not_name(self):
        extra = self.served - set(self.catalogue())
        self.assertEqual(
            extra, set(),
            f"the broker publishes {sorted(extra)}, which RFC 0005's catalogue "
            f"does not name - and the catalogue is closed, so either it has to "
            f"gain a row or the broker has to stop")

    def test_the_page_may_draw_every_metric_the_broker_serves(self):
        """The first of the two directions the drift runs in: a metric arrives
        in the scrape and no card may name it, so it is on no dashboard and
        nothing says why."""
        undrawable = self.served - self.allowed() - self.INFO_FAMILIES
        self.assertEqual(
            undrawable, set(),
            f"the broker serves {sorted(undrawable)} and ALLOWED_METRICS does "
            f"not name them, so no dashboard can draw them and a card that "
            f"tried would be refused at startup")

    def test_the_page_draws_no_metric_the_broker_does_not_serve(self):
        """The other direction: a name the viewer would accept on a card that
        the broker has stopped publishing, which draws nothing and reports
        nothing. Against the catalogue rather than the scrape, because
        `saguin_storage_errors_total` is a real promise with no series until a
        provider fails."""
        invented = self.allowed() - set(self.catalogue())
        self.assertEqual(
            invented, set(),
            f"ALLOWED_METRICS names {sorted(invented)}, which RFC 0005's "
            f"catalogue does not - a card on one draws an empty panel")

    def test_the_info_families_are_the_whole_of_what_the_page_leaves_out(self):
        """The exclusion cannot grow quietly. app.py says the four `*_info`
        families are left out because each is the constant 1 with its facts in
        labels; anything else absent from the list is drift wearing that
        reason."""
        self.assertEqual(
            set(self.catalogue()) - self.allowed(), self.INFO_FAMILIES,
            "what the catalogue promises and the page will not draw is no longer "
            "exactly the four *_info families")

    def test_every_metric_the_page_may_draw_is_on_exactly_one_dashboard(self):
        """The fourth list. `saguin-viewer.yaml` says the shipped dashboards
        "are a partition of what the broker exports: every metric it publishes
        a number for is on exactly one of them - so a number has one place to
        be looked for", and that is a claim nothing checked either.

        A name in `ALLOWED_METRICS` and on no dashboard is a metric the broker
        reports and the shipped viewer never shows. A name on two is a number
        an operator finds in one place, and then again somewhere else with no
        reason given.

        Read off each dashboard's parsed cards through app.py's own reference
        parser, rather than by matching the file as text: a name inside a
        comment is prose about a card, not a card, and a comment is exactly
        where a metric that was taken off a dashboard leaves its name behind.
        """
        where = collections.defaultdict(list)
        files = sorted((VIEWER / "dashboards").glob("*.yaml"))
        self.assertGreaterEqual(len(files), 5,
                                f"only found {files} to read")
        for path in files:
            spec = yaml.safe_load(path.read_text())
            cards = spec.get("cards") or []
            self.assertTrue(cards, f"{path.name} declares no cards")
            for card in cards:
                for field in ("metric", "value", "max"):
                    ref = card.get(field)
                    if not isinstance(ref, str):
                        continue
                    m = app.METRIC_REF.match(ref)
                    self.assertIsNotNone(
                        m, f"{path.name}: {ref!r} is not a reference app.py "
                           f"can read, so this test cannot count it")
                    where[m.group(1)].append(path.name)

        drawn = set(where)
        self.assertGreaterEqual(
            len(drawn), 50,
            f"only {len(drawn)} metrics were found on the shipped dashboards - "
            f"the card walk is reading less than the dashboards hold")

        nowhere = self.allowed() - drawn
        self.assertEqual(
            nowhere, set(),
            f"{sorted(nowhere)} may be drawn and no shipped dashboard draws "
            f"them, so the broker reports a number the shipped viewer never "
            f"shows")

        twice = {m: sorted(set(f)) for m, f in where.items() if len(set(f)) > 1}
        self.assertEqual(
            twice, {},
            f"{twice} - the shipped dashboards are meant to be a partition, so "
            f"a number has one place to be looked for")

        self.assertEqual(
            drawn - self.allowed(), set(),
            "a shipped dashboard names a metric ALLOWED_METRICS does not, which "
            "the viewer would refuse at startup")


class TheServedLibrariesAreAttributed(unittest.TestCase):
    """**static/lib/ is third-party code this repository redistributes**, and for
    the viewer's whole life nothing said whose it was.

    Two things were wrong when this was written. `htm.js` carried no notice at
    all - minification stripped it, so a file was served to every browser with
    nothing naming its author or its licence. And React's own header says its
    licence is "in the LICENSE file in the root directory of this source tree",
    where the LICENSE file is saguin-viewer's Apache-2.0 rather than React's MIT,
    so the one pointer that did exist resolved to the wrong licence.

    This holds the notices file to the directory's contents: a fourth library kept
    here fails until its licence is there too. Vendoring is a two-second copy and
    nothing else says an obligation came with it.
    """

    NOTICES = REPO / "cmd" / "saguin-viewer" / "THIRD-PARTY-NOTICES.md"

    def setUp(self):
        if not self.NOTICES.exists():
            self.fail(f"no {self.NOTICES} - the served libraries' licences have "
                      f"nowhere to be")
        self.notices = self.NOTICES.read_text()
        self.served = sorted(p.name for p in (VIEWER / "static" / "lib").iterdir()
                             if p.is_file())

    def test_something_is_being_checked(self):
        """Count what the sweep found. An empty directory would pass every case
        below by having nothing to disagree about."""
        self.assertGreaterEqual(
            len(self.served), 3,
            f"only {self.served} in static/lib - the page loads three libraries, "
            f"so this sweep is reading less than is there")

    def test_every_file_served_is_named_in_the_notices(self):
        for name in self.served:
            with self.subTest(library=name):
                self.assertIn(
                    f"web/static/lib/{name}", self.notices,
                    f"{name} is served to the browser from this repository and "
                    f"the notices do not name it")

    def test_the_notices_carry_a_licence_for_each(self):
        """The two licences the three files are under, by name. A file named with
        no licence beside it is attribution that says nothing."""
        for want in ("MIT License", "Apache License"):
            self.assertIn(want, self.notices,
                          f"the notices name no {want!r}")
        # React's copyright holder and htm's, which are what the licences require
        # to be carried rather than the version.
        for want in ("Copyright (c) Facebook, Inc. and its affiliates.",
                     "Copyright 2018 Google Inc."):
            self.assertIn(want, self.notices,
                          f"the notices do not carry {want!r}")

    def test_the_react_header_is_still_the_one_the_notices_explain(self):
        """The notices explain that React's own header points at a LICENSE file
        which in this repository is a different licence. If a future React build
        stops saying that, the explanation becomes a claim about nothing - so this
        fails and the paragraph can go.
        """
        react = (VIEWER / "static" / "lib" / "react.production.min.js").read_text()
        self.assertIn("LICENSE file in the root directory", react,
                      "React's build no longer points at a root LICENSE file, so "
                      "the notices' paragraph about that pointer is now about "
                      "nothing and should be removed")
        root = (REPO / "LICENSE").read_text()
        self.assertIn("Apache License", root,
                      "this repository's LICENSE is no longer Apache-2.0, so the "
                      "notices' account of why React's pointer misleads is stale")


class TheReadmeDocumentsTheCardsThatExist(unittest.TestCase):
    """**Three fields were missing from that table and nothing said so.** A card
    field the validator accepts and the README's table omits is a feature nobody
    can find: `note` on a timeseries, `severity` on a breakdown and `label` on a
    meter were all accepted, all rendered, and all undocumented.

    The table is the dashboard file's documentation. This holds it to
    `CARD_FIELDS`, which is what the validator reads.
    """

    def setUp(self):
        self.readme = (VIEWER / "README.md").read_text()

    def row(self, card):
        """The table row for one card type, which is where its fields are listed."""
        prefix = f"| `{card}` |"
        for line in self.readme.splitlines():
            if line.startswith(prefix):
                return line
        return None

    def test_every_card_type_has_a_row(self):
        """Counted, because a table this walk cannot find would let every case
        below pass by looking at nothing."""
        rows = [c for c in app.CARD_TYPES if self.row(c)]
        self.assertGreaterEqual(
            len(rows), 8,
            f"only {len(rows)} of {len(app.CARD_TYPES)} card types have a table "
            f"row - the table's shape has moved and this check is reading almost "
            f"nothing: {sorted(set(app.CARD_TYPES) - set(rows))}")

    def test_every_field_the_validator_takes_is_in_its_cards_row(self):
        for card, fields in sorted(app.CARD_FIELDS.items()):
            row = self.row(card)
            if row is None:
                continue
            for field in sorted(fields):
                with self.subTest(card=card, field=field):
                    self.assertRegex(
                        row, r"\b" + re.escape(field) + r"\b",
                        f"a {card} card takes {field!r} and its row in the README "
                        f"does not name it, so it is a field nobody can find")

    def test_the_readme_invents_no_field(self):
        """The other direction: a field the README promises and the validator
        refuses is worse than an undocumented one, because somebody writes it and
        the viewer will not start."""
        for card, fields in sorted(app.CARD_FIELDS.items()):
            row = self.row(card)
            if row is None:
                continue
            allowed = fields | COMMON_LIKE
            # **The fields column only.** The row's first cell is the card's own
            # type name, which is not one of its fields - reading the whole row
            # reported `timeseries` as a field of a timeseries.
            cells = [c for c in row.split("|") if c.strip()]
            for named in re.findall(r"`([a-z_]+)(?::|`)", cells[-1]):
                with self.subTest(card=card, field=named):
                    self.assertIn(
                        named, allowed,
                        f"the README's {card} row names {named!r} and the "
                        f"validator does not take it on that card")


# The fields allowed on every card, plus the values the table writes in the same
# backticks as a field name - `bars`, `stacked`, `level`, `rate` and the rest are
# what a field is set to rather than fields.
COMMON_LIKE = {"type", "width", "height", "title", "bars", "stacked", "level",
               "rate", "duration", "bytes", "number", "percent", "warning",
               "critical", "msgs_rate", "bytes_rate"}


class EveryDashboardExampleInTheReadmeLoads(unittest.TestCase):
    """**The README is the dashboard file's documentation, and one of its examples
    could not be copied.** `### The file` ended its card list with a literal
    `- ...`, so a reader who pasted it was told "card 2: a card is a mapping" -
    a refusal about the placeholder rather than about anything they did.

    An example that stops loading is worse than no example: it sends somebody to
    debug the prose.
    """

    def blocks(self):
        text = (VIEWER / "README.md").read_text()
        return re.findall(r"^```yaml\n(.*?)^```", text, re.S | re.M)

    def test_the_blocks_were_found(self):
        self.assertGreaterEqual(
            len(self.blocks()), 5,
            "the yaml fence pattern found almost nothing, so the cases below are "
            "checking nothing")

    def test_every_dashboard_example_loads(self):
        """A block is a dashboard when it has `cards`, whole or wrapped. The
        configuration fragments are not dashboards and are left alone: they are
        checked against the shipped `saguin-viewer.yaml` elsewhere.
        """
        checked = 0
        for i, block in enumerate(self.blocks(), 1):
            try:
                doc = yaml.safe_load(block)
            except yaml.YAMLError as e:
                self.fail(f"README block {i} is not valid YAML: {e}")
            body = None
            if isinstance(doc, dict) and "cards" in doc:
                body = block
            elif isinstance(doc, list) and doc and isinstance(doc[0], dict) \
                    and "type" in doc[0]:
                # A card, or a few, shown without the dashboard around them.
                body = 'title: "x"\ncards:\n' + "\n".join(
                    "  " + l for l in block.splitlines())
            if body is None:
                continue
            checked += 1
            d = tempfile.mkdtemp()
            try:
                path = pathlib.Path(d) / "dashboard.yaml"
                path.write_text(body)
                with self.subTest(block=i):
                    try:
                        app.load_dashboard(f"README block {i}", str(path))
                    except SystemExit as e:
                        self.fail(f"a README example does not load: {e}\n"
                                  f"--- as offered to the loader ---\n{body}")
            finally:
                shutil.rmtree(d, ignore_errors=True)
        self.assertGreaterEqual(
            checked, 2,
            f"only {checked} dashboard examples were recognised in the README - "
            f"either they have gone or this case no longer recognises one")


class EveryCountWrittenInProseAgrees(unittest.TestCase):
    """**A number written out in words is the one claim that stops being true
    silently**, and this repository has now got the same one wrong three times: the
    shipped dashboards were "five" after a sixth arrived, the drawable metrics were
    "sixty" after four more, and `Dockerfile`'s comment said the configuration named
    "four" dashboard files where it names six. Each was fixed as an instance; this
    is the sweep.

    It reads every file that could carry such a claim, finds a spelled number
    standing next to the word `dashboard`, and holds it to how many there are. A
    comment is not code, which is exactly why nothing else here would notice.
    """

    WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
             "twelve": 12}

    # Where a claim about how many dashboards there are could be written.
    FILES = ("README.md", "app.py", "Dockerfile", "saguin-viewer.yaml")

    def setUp(self):
        self.shipped = sorted((VIEWER / "dashboards").glob("*.yaml"))
        raw = yaml.safe_load((VIEWER / "saguin-viewer.yaml").read_text())
        cfg = app.merge(app.DEFAULTS, raw)
        self.named = [p for p in (cfg.get("dashboard") or {}).values()
                      if p != "builtin"]

    def test_the_two_counts_are_the_same_thing(self):
        """The files in `dashboards/` and the files the configuration names. If
        these ever differ the claims below are about two different numbers and this
        class has to say which."""
        self.assertEqual(
            len(self.shipped), len(self.named),
            f"{len(self.shipped)} files in dashboards/ and {len(self.named)} named "
            f"by the configuration")
        self.assertGreaterEqual(len(self.shipped), 5,
                                "too few dashboards for this sweep to mean much")

    def test_no_file_claims_the_wrong_number_of_dashboards(self):
        truth = len(self.shipped)
        pattern = re.compile(
            r"\b(" + "|".join(self.WORDS) + r")\b[^.\n]{0,40}?\bdashboard",
            re.I)
        examined = 0
        for name in self.FILES:
            path = VIEWER / name
            if not path.exists():
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                for m in pattern.finditer(line):
                    word = m.group(1).lower()
                    said = self.WORDS[word]
                    # "one dashboard file", "a dashboard or two" and the like are
                    # not claims about how many ship; only a number that matches
                    # or contradicts the real count is.
                    if said == 1:
                        continue
                    examined += 1
                    with self.subTest(file=name, line=n, said=word):
                        self.assertEqual(
                            said, truth,
                            f"{name}:{n} says {word!r} where {truth} dashboards "
                            f"ship: {line.strip()!r}")
        # Count what the sweep examined, so a pattern that stopped matching cannot
        # pass this by finding nothing to check.
        self.assertGreaterEqual(
            examined, 2,
            f"only {examined} spelled dashboard counts were found across "
            f"{self.FILES} - the pattern has stopped matching and this sweep is "
            f"reporting on nothing")

    def test_the_configurations_dashboard_comment_agrees(self):
        """**The case above missed the instance that prompted it**, which mutating
        it showed rather than reading it: `saguin-viewer.yaml` said "so all five
        run" with the word `dashboard` nowhere near the number and a line break in
        between, so a rule keyed on proximity could not see it.

        The comment block immediately above `dashboard:` is entirely about the
        dashboards, so every spelled number in it is a claim about how many there
        are. Read as a block rather than by proximity.
        """
        lines = (VIEWER / "saguin-viewer.yaml").read_text().splitlines()
        at = next((i for i, l in enumerate(lines) if l.startswith("dashboard:")), None)
        self.assertIsNotNone(at, "saguin-viewer.yaml has no `dashboard:` key")
        block, i = [], at - 1
        while i >= 0 and lines[i].lstrip().startswith("#"):
            block.append((i + 1, lines[i]))
            i -= 1
        self.assertGreaterEqual(
            len(block), 5,
            f"only {len(block)} comment lines above `dashboard:` - the block this "
            f"reads has moved and it is checking almost nothing")

        # **"the six" and "all six", not every number in the block.** Read whole,
        # the block also says "none is on two" - two dashboards, of which no metric
        # is on both - which is not a claim about how many ship. A total is asserted
        # with "the" or "all" in front of it; anything else is arithmetic about
        # them.
        truth, examined = len(self.shipped), 0
        claim = re.compile(r"\b(?:the|all)\s+(" + "|".join(self.WORDS) + r")\b", re.I)
        for n, line in block:
            for word in claim.findall(line):
                said = self.WORDS[word.lower()]
                if said == 1:
                    continue
                examined += 1
                with self.subTest(line=n, said=word):
                    self.assertEqual(
                        said, truth,
                        f"saguin-viewer.yaml:{n} says {word!r} where {truth} "
                        f"dashboards ship: {line.strip()!r}")
        self.assertGreaterEqual(
            examined, 1,
            "no spelled number in the comment above `dashboard:` - that block is "
            "where this repository has twice written the wrong count, so a sweep "
            "finding none there is a sweep that would have missed both")


class ARefusedReplyTopicIsNotSilent(unittest.TestCase):
    """**Every answer this page asks for arrives on one topic, and an `acl_file`
    that does not grant it loses them all while refusing nothing.**

    Found driving `examples/home-automation`, whose ACL granted the
    data filters and not `viewer-reply/+`: the subscribe was refused `0x87`, every
    point read, seek and sessions verb was accepted and answered nowhere, and
    thirty-one smoke checks passed over it. `/api/state` had carried
    `granted: false` for that filter all along - recording is not reporting.

    Sharper than a missed case: `/api/deadletters` already knew the reply topic
    gets refused by the same ACL and deliberately filtered it *out* of its advice,
    correctly, because it is not what an operator edits to fix a dead-letter
    listing. That left it excluded from the one place refusals surfaced and with
    nowhere of its own.
    """

    class Code:
        def __init__(self, name, failure):
            self.name, self.is_failure = name, failure

        def __str__(self):
            return self.name

    def setUp(self):
        self.saved = list(app.state["subscriptions"])
        app.state["subscriptions"] = []
        app._sub_mids.clear()

    def tearDown(self):
        app.state["subscriptions"] = self.saved
        app._sub_mids.clear()

    def refuse(self, filt):
        """Refuse one filter and return what the viewer printed."""
        app._sub_mids[11] = filt
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            app.on_subscribe(None, None, 11, [self.Code("Not authorized", True)])
        return out.getvalue()

    def test_the_prefix_and_the_topic_are_one_fact(self):
        """The advice, the README and the subscription have to name the same
        thing, so the prefix is a constant rather than a string in three places."""
        self.assertTrue(app.REPLY_TOPIC.startswith("viewer-reply/"))
        self.assertEqual(app.REPLY_PREFIX, "viewer-reply/+")
        readme = (VIEWER / "README.md").read_text()
        self.assertIn(app.REPLY_PREFIX, readme,
                      "the README does not name the prefix an acl_file has to "
                      "grant, which is the half of this a reader can act on")
        self.assertIn("read", readme)

    def test_a_refused_reply_topic_says_what_it_costs(self):
        said = self.refuse(app.REPLY_TOPIC)
        self.assertIn(app.REPLY_TOPIC, said, "the refusal does not name the topic")
        for phrase in ("reply topic", "never show an answer", app.REPLY_PREFIX):
            self.assertIn(phrase, said,
                          f"the refusal does not say {phrase!r}, so a reader is "
                          f"told a filter was refused and not what it costs")

    def test_an_ordinary_filter_is_reported_without_the_reply_advice(self):
        """Both halves. A refusal of a data filter is still said out loud - that
        was silent too - but the paragraph about lost answers belongs to one
        topic, and a message that gave it for every filter would be noise."""
        said = self.refuse("iot/#")
        self.assertIn("refused the subscription to 'iot/#'", said)
        self.assertNotIn("reply topic", said)

    def test_a_granted_filter_says_nothing(self):
        """The instrument proving itself: a recorder that printed on every SUBACK
        would satisfy the cases above and drown the log."""
        app._sub_mids[12] = "iot/#"
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            app.on_subscribe(None, None, 12, [self.Code("Granted QoS 1", False)])
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(app.state["subscriptions"],
                         [{"filter": "iot/#", "granted": True,
                           "reason": "Granted QoS 1"}])

    def test_the_page_reads_the_refusal_rather_than_only_the_state_carrying_it(self):
        """The page's half. `/api/state` has always carried `granted: false`; what
        was missing is anything reading it, so this holds app.js to having a
        function that finds a refused reply filter and a pill that draws it."""
        js = (VIEWER / "static" / "app.js").read_text()
        self.assertIn("function replyRefused", js,
                      "nothing on the page looks for a refused reply topic")
        self.assertIn("replies refused", js,
                      "the page has no visible marker for it")
        self.assertIn('startsWith("viewer-reply/")', js,
                      "the page's check does not match the topic the viewer "
                      "subscribes, so it would never fire")
        # And the state it reads is actually served, which is what makes the
        # function's input real rather than assumed.
        self.assertIn('"subscriptions": list(state["subscriptions"])',
                      (VIEWER / "app.py").read_text(),
                      "/api/state does not carry subscriptions, so the page's "
                      "check has nothing to read")


class AnAclFileDecidesWhetherAnswersArrive(unittest.TestCase):
    """The end-to-end sibling of `ARefusedReplyTopicIsNotSilent`, against a real
    broker with a real `acl_file`.

    **Those five cases drive a recorded SUBACK; this one drives a broker.** The
    defect they were written for was found in a configuration, not in code - a
    role granting the data filters and not `viewer-reply/+` - so the guard that
    would have caught it has to be a broker refusing a subscription for the reason
    an `acl_file` refuses it, rather than a reason code this suite constructed.

    Both directions, because a case that only ever saw the refusal would pass
    against a broker that refused everything, and one that only saw the grant would
    pass against a broker with no `acl_file` at all - which is exactly the
    configuration that hid this.
    """

    CONFIG = """\
broker:
  id: acl-arm
  log_level: error
  mqtt:
    listen:
      tcp:
        address: 127.0.0.1:{mqtt}
    password_file: {passwd}
    acl_file: {acl}
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
"""

    # The role a viewer needs, with and without the one rule this is about. Written
    # as saguin's acl_file is written: `topic:` because viewer-reply/<id> is a
    # broadcast topic that no channel claims, and `allow:` rather than `verbs:`.
    ACL = """\
roles:
  viewer:
    - channel: events
      allow: [read, seek]
{reply}
users:
  operator: [viewer]
"""
    REPLY_RULE = """    - topic: viewer-reply/+
      allow: [read]
"""

    @classmethod
    def setUpClass(cls):
        cls.why = None
        cls.binary = SAGUIN / "bin" / "saguin"
        if not cls.binary.exists():
            cls.why = (f"no {cls.binary} - this needs saguin's own checkout: build "
                       f"it there with `go build -o bin/saguin ./cmd/saguin`, and "
                       f"set SAGUIN_REPO if it is not beside this one")
            return
        try:
            import paho.mqtt.client  # noqa: F401
        except ImportError:                                   # pragma: no cover
            cls.why = "paho-mqtt is not installed"

    def setUp(self):
        if self.why:
            self.skipTest(self.why)
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def broker(self, grant_reply):
        """Start a broker whose acl_file grants the reply prefix, or does not.
        Returns the MQTT port."""
        passwd = self.dir / f"clients-{grant_reply}.passwd"
        subprocess.run([str(self.binary), "--passwd", "add", str(passwd),
                        "operator", "s3cret"],
                       capture_output=True, text=True, timeout=30, check=True)
        acl = self.dir / f"acl-{grant_reply}.yaml"
        acl.write_text(self.ACL.format(reply=self.REPLY_RULE if grant_reply else ""))
        port = self.free_port()
        cfg = self.dir / f"saguin-{grant_reply}.yaml"
        cfg.write_text(self.CONFIG.format(mqtt=port, passwd=passwd, acl=acl))

        # **The configuration is checked before it is started.** An acl_file this
        # broker refuses would otherwise look like a refused subscription, which is
        # the very thing being measured.
        check = subprocess.run([str(self.binary), "--check-config", str(cfg)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(check.returncode, 0,
                         f"the broker refused this test's own configuration:\n"
                         f"{check.stdout}{check.stderr}")

        log = open(self.dir / f"broker-{grant_reply}.log", "w+")
        proc = subprocess.Popen([str(self.binary), "--config", str(cfg)],
                                stdout=log, stderr=subprocess.STDOUT)

        def stop():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:                 # pragma: no cover
                proc.kill()
            log.close()
        self.addCleanup(stop)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:                       # pragma: no cover
                self.fail(f"the broker exited with {proc.returncode}. It said:\n"
                          f"{(self.dir / f'broker-{grant_reply}.log').read_text()}")
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return port
            time.sleep(0.05)
        self.fail("the broker never accepted a connection")   # pragma: no cover

    def suback_for(self, port, filt):
        """Subscribe one filter as the viewer's own user and return what the
        broker answered - which is the whole measurement."""
        import paho.mqtt.client as mqtt
        answered = []
        done = threading.Event()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id="acl-arm-viewer", protocol=mqtt.MQTTv5)
        c.username_pw_set("operator", "s3cret")

        def on_subscribe(_c, _u, _mid, codes, _props=None):
            answered.extend(codes if isinstance(codes, (list, tuple)) else [codes])
            done.set()

        c.on_subscribe = on_subscribe
        c.connect("127.0.0.1", port, 30)
        c.loop_start()
        self.addCleanup(c.loop_stop)
        c.subscribe(filt, qos=1)
        self.assertTrue(done.wait(20), f"no SUBACK for {filt!r} in 20s")
        c.disconnect()
        self.assertEqual(len(answered), 1, f"{len(answered)} reason codes for one filter")
        return answered[0]

    def test_a_role_granting_the_prefix_is_granted(self):
        port = self.broker(grant_reply=True)
        rc = self.suback_for(port, app.REPLY_TOPIC)
        self.assertFalse(
            rc.is_failure,
            f"a role granting {app.REPLY_PREFIX} was refused {rc} for "
            f"{app.REPLY_TOPIC} - the rule the README tells operators to write "
            f"does not work")

    def test_a_role_omitting_the_prefix_is_refused_exactly_as_the_reporting_says(self):
        port = self.broker(grant_reply=False)
        rc = self.suback_for(port, app.REPLY_TOPIC)
        self.assertTrue(
            rc.is_failure,
            f"a role that does not grant {app.REPLY_PREFIX} was answered {rc}, so "
            f"this arm cannot prove the refusal it exists for")
        self.assertEqual(
            str(rc), "Not authorized",
            "the refusal is not the one an acl_file gives, so the reporting's "
            "cases are matching a code no broker sends here")

        # And the reporting says the right thing about the code a real broker just
        # sent - which is what ties the five recorded-SUBACK cases to this one.
        app.state["subscriptions"] = []
        app._sub_mids.clear()
        app._sub_mids[99] = app.REPLY_TOPIC
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            app.on_subscribe(None, None, 99, [rc])
        said = out.getvalue()
        self.assertIn(app.REPLY_PREFIX, said,
                      "given the code a real broker sends, the viewer does not "
                      "name the rule an operator has to add")
        self.assertIn("never show an answer", said)

    def test_the_data_filters_are_granted_either_way(self):
        """The control. Both arms grant the channel, so a broker refusing
        everything - a wrong password, a role that matched nothing - would fail
        here rather than being read as the refusal this class is about."""
        for grant in (True, False):
            with self.subTest(reply_granted=grant):
                rc = self.suback_for(self.broker(grant_reply=grant), "iot/+/events/+")
                self.assertFalse(rc.is_failure,
                                 f"the channel filter was refused {rc}, so this "
                                 f"broker refuses more than the reply prefix and "
                                 f"neither arm measures what it claims")
