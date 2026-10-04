// The page. One file, no build step: htm gives template literals where JSX
// would need a toolchain, and React comes from the lib directory beside
// this one.
//
// It polls rather than streaming. A demonstration that needs a WebSocket to
// show a topic tree has more moving parts than the thing it is demonstrating,
// and at one second the tree is as live as anybody watching it needs.

const { createElement: h, useState, useEffect, useCallback, useRef, Fragment } = React;
const html = htm.bind(h);

// **Everything the page remembers lives in a cookie**, read here before
// first paint in index.html for the theme and here for the rest: which tab,
// where you were in the tree, the sidebar width, the seek settings, how
// payloads are shown and copied.
const cookie = (name, value) => {
  if (value === undefined) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  }
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=31536000; SameSite=Strict`;
};

// **A fetch that fails is an answer too.** This is a program somebody runs
// in a terminal and stops with Ctrl-C, and when it went away the page kept
// polling into a dead socket: an unhandled rejection every second in the
// console, and on screen the last data it happened to hold, still ticking
// its "taken Ns ago" counter as though it were current. Stale data that
// looks live is the failure worth catching here - every caller now gets a
// shaped answer with a null body, and the header says the page is talking
// to nothing.
let reachable = true;
// **The number on the wire is not the name of the protocol.** CONNECT
// carries 4 for MQTT 3.1.1 and 3 for 3.1, and 5 is the only version that is
// its own number. Showing the raw byte tells an operator their client speaks
// "MQTT 4", which is not a thing. Everywhere a protocol is displayed goes
// through here so no one of them can drift back.
const mqttName = v => ({ "3": "3.1", "4": "3.1.1", "5": "5" })[String(v)] || String(v);

const api = (path, opts) =>
  fetch(path, opts)
    .then(r => r.json().then(j => ({ ok: r.ok, body: j })))
    .catch(() => ({ ok: false, body: null, unreachable: true }))
    .then(r => {
      const now = !r.unreachable;
      if (now !== reachable) {
        reachable = now;
        window.dispatchEvent(new CustomEvent("saguin-reachable"));
      }
      return r;
    });

// Whether the viewer itself is answering, which is a different question
// from whether the broker is - one is this page's own process, the other is
// what that process connects to, and a reader needs to know which is gone.
const useReachable = () => {
  const [ok, setOk] = useState(reachable);
  useEffect(() => {
    const on = () => setOk(reachable);
    window.addEventListener("saguin-reachable", on);
    return () => window.removeEventListener("saguin-reachable", on);
  }, []);
  return ok;
};

// Flat topics into a tree: the channel that holds each one, then its topic
// levels beneath.
//
// **The channel is put there rather than found there.** It used to be the
// first level of the topic - a channel claimed the topics under its own
// name - so keying by topic level made the top of this tree exactly the
// channel list, for free. A channel now carries a topic filter and claims
// whatever it matches, so `iot/water/location/wq-001` and
// `iot/water/measurement/wq-001` share their first three levels and belong
// to two different channels. Keyed by topic alone the top of this tree
// would be a single node called `iot`, and every affordance hanging off
// depth 0 - the type badge, the held count, the replay form - would hang
// off nothing.
//
// So the broker's answer is used instead: `/api/tree` says which channel
// holds each topic, resolved by the same code the broker resolves a publish
// with. A path is therefore `<channel>/<topic>` and the topic is everything
// after the first level.
function toTree(rows, channels) {
  const root = {};
  // **Every channel the broker has, before any traffic.** The tree is built
  // from received topics, and this page deliberately never subscribes to a
  // queue - so a queue had no rows and appeared nowhere, while the header
  // said "queues are listed and not read" and the docstring said they
  // appear in the tree. Both were describing a page that showed five
  // channels out of six.
  //
  // A channel with no rows is an empty node rather than a missing one,
  // which is the honest thing to draw: the broker says it exists, and this
  // page says what it is and why it is not read.
  for (const name of Object.keys(channels || {})) {
    root[name] = root[name] || { _children: {}, _count: 0 };
  }
  for (const row of rows) {
    let node = root;
    const parts = [row.channel || "broadcast", ...row.topic.split("/")];
    parts.forEach((part, i) => {
      node[part] = node[part] || { _children: {}, _count: 0 };
      node[part]._count += row.count;
      if (i === parts.length - 1) {
        node[part]._leaf = row;   // carries kind, offset and retained
      }
      node = node[part]._children;
    });
  }
  return root;
}

// The topic a tree path names, which is the path without the channel level
// the tree puts in front of it.
const topicOf = path => path.split("/").slice(1).join("/");

function Node({ name, node, path, depth, open, toggle, selected, select, showQueue }) {
  const kids = Object.keys(node._children).sort();
  const isOpen = open.has(path);
  // At depth 0 the name is a channel, or the word this page uses for a
  // topic no channel's filter claims.
  const chan = depth === 0
    ? (window.__channels ? window.__channels[name] : null) || "broadcast"
    : null;
  const held = depth === 0 && window.__held ? window.__held[name] : null;
  return html`
    <div class="node">
      <div class=${"row" + (selected === path ? " on" : "")}
           style=${{ paddingLeft: depth * 12 + "px" }}
           onClick=${() => {
             // **A queue channel opens *and* answers.** It used to only
             // toggle, because a queue is listed and never read here - a
             // subscriber would take the workers' jobs. So selecting one
             // showed nothing, which is exactly the gap the route fills.
             if (depth === 0 && chan === "queue" && showQueue) showQueue(name);
             // **A level can be both a topic and a parent**, and this used to
             // be able to do only one thing about it: `a/b` carrying a message
             // and `a/b/c` arriving under it drew the arrow and then refused
             // to open, because a node with a leaf only ever selected. Now the
             // row selects the value it has, and the arrow beside it opens
             // what is underneath - so neither reading of a click is lost.
             node._leaf ? select(path) : toggle(path);
           }}>
        <span class=${"twist" + (isOpen ? " open" : "")}
              onClick=${e => { if (kids.length) { e.stopPropagation(); toggle(path); } }}
              title=${kids.length ? (isOpen ? "Collapse" : "Expand") : null}
              style=${kids.length ? { cursor: "pointer" } : null}
          >${kids.length ? "▶" : ""}</span>
        <span class="label">${name}</span>
        ${chan && html`<span class=${"tag " + chan}>${chan}</span>`}
        ${chan === "queue" && html`<span class="tag">not read</span>`}
        ${depth === 0 && window.__starts && window.__starts[name] === "tail" && html`
          <span class="tag muted"
                title="start: tail - a subscriber with no stored position is served only what arrives next, so there is no replay here. The channel decides this, not the client's protocol."
            >tail</span>`}
        ${node._leaf && node._leaf.retained && html`<span class="tag retained">retained</span>`}

        ${depth === 0
          ? html`<span class="leafcount" title=${chan === "queue"
                ? "unresolved work, from saguin_queue_depth - a queue publishes no record count, because resolution removes from the middle rather than the front"
                : chan === "latest"
                  ? "how many topics hold a current value - a latest channel keeps one per topic, so that count is what it holds"
                  : "what the broker says this channel holds - the same number the dashboard draws"}>
              ${held == null
                ? (chan === "latest" ? "one value per topic" : "")
                : held.toLocaleString() + (chan === "queue" ? " unresolved" : " held")}</span>`
          : node._leaf && node._leaf.kind === "latest"
            ? html`<span class="leafcount">offset ${node._leaf.offset}</span>`
            : html`<span class="leafcount" title="messages this page has seen since it connected">
                ${node._count.toLocaleString()} seen</span>`}
      </div>
      ${isOpen && kids.map(k => html`
        <${Node} key=${k} name=${k} node=${node._children[k]} path=${path + "/" + k}
                 depth=${depth + 1} open=${open} toggle=${toggle}
                 selected=${selected} select=${select} />`)}
    </div>`;
}

function Seek({ channel, onDone }) {
  // **The four shapes the broker accepts, as four things to pick between.**
  // A bare integer goes on meaning an offset, and a duration means *ago*
  // whether or not it carries a minus sign - so a single free-text box
  // would let somebody type 1763000000 meaning a Unix time and be moved to
  // offset 1,763,000,000 instead, read from there in order, and be told it
  // worked. Splitting the shape from the number is what stops that.
  const [mode, setMode] = useState(() => cookie("saguin_viewer_seek_mode") || "ago");
  const [amount, setAmount] = useState(() => cookie("saguin_viewer_seek_amount") || "2");
  const [unit, setUnit] = useState(() => cookie("saguin_viewer_seek_unit") || "m");
  const [reply, setReply] = useState(null);
  const [busy, setBusy] = useState(false);
  const [why, setWhy] = useState(false);

  useEffect(() => { cookie("saguin_viewer_seek_mode", mode); }, [mode]);
  useEffect(() => { cookie("saguin_viewer_seek_amount", amount); }, [amount]);
  useEffect(() => { cookie("saguin_viewer_seek_unit", unit); }, [unit]);

  // `d` is the broker's own suffix; the rest go through Go's duration
  // parser, which knows nothing above an hour.
  const position = mode === "ago"    ? `-${amount || 0}${unit}`
                 : mode === "offset" ? String(amount || 0)
                 : mode === "held"   ? "0"
                 : "-1";

  const submit = async e => {
    e.preventDefault();
    setBusy(true); setReply(null);
    const { body } = await api("/api/seek", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel, position }),
    });
    setReply(body); setBusy(false); onDone && onDone();
  };

  return html`
    <div class="card seek">
      <h3 style=${{ display: "flex", alignItems: "center", gap: "10px" }}>
        Replay this channel
        <button class="chip" style=${{ cursor: "pointer", fontWeight: "normal" }}
                onClick=${() => setWhy(w => !w)}>
          ${why ? "hide what this does" : "what this does"}</button>
      </h3>
      ${why && html`<p class="why">
        Moves <em>this viewer's</em> stored position on${" "}
        <strong>${channel}</strong> and nothing else - every other consumer
        keeps reading from where it was, and no other channel is touched. A
        position is held per client id, which is what makes a replay button
        safe to put on a page. It is an ordinary publish to${" "}
        <code>$saguin/consumer/${channel}/seek</code>, and there is no API.
        This page forgets what it has collected for ${channel} so the replay
        is visible as it arrives; nothing is removed from the broker.
      </p>`}
      <form onSubmit=${submit}>
        <select value=${mode} onChange=${e => setMode(e.target.value)}>
          <option value="ago">a time ago</option>
          <option value="offset">an offset</option>
          <option value="held">everything still held</option>
          <option value="now">only what arrives from now</option>
        </select>
        ${(mode === "ago" || mode === "offset") && html`
          <input type="number" min="0" step="1" style=${{ width: "110px" }}
                 value=${amount} onInput=${e => setAmount(e.target.value)} />`}
        ${mode === "ago" && html`
          <select value=${unit} onChange=${e => setUnit(e.target.value)}>
            <option value="s">seconds</option>
            <option value="m">minutes</option>
            <option value="h">hours</option>
            <option value="d">days</option>
          </select>`}
        <button class="primary" disabled=${busy}>${busy ? "seeking…" : "Seek"}</button>
        <span class="chip">sends <b>${position}</b></span>
      </form>
      ${reply && html`
        <div class=${"reply " + (reply.ok ? "ok" : "bad")}>
          ${reply.ok
            ? html`The broker answered: <code>${reply.reply}</code>`
            : html`Refused: ${reply.error}`}
        </div>`}
    </div>`;
}

// **One preference, shared by every view that shows a payload.** Kept in a
// cookie and broadcast on a window event so the topic pane and the feed
// agree the moment either is changed - two independent copies of one
// setting is a setting that disagrees with itself.
function usePref(name, fallback) {
  const key = "saguin_viewer_" + name;
  const read = () => cookie(key) ?? fallback;
  const [value, set] = useState(read);
  useEffect(() => {
    const onChange = () => set(read());
    window.addEventListener("saguin-pref", onChange);
    return () => window.removeEventListener("saguin-pref", onChange);
  }, []);
  return [value, next => {
    cookie(key, next);
    window.dispatchEvent(new CustomEvent("saguin-pref"));
  }];
}

const useDecode = () => {
  const [v, set] = usePref("decode", "1");
  return [v !== "0", next => set(next ? "1" : "0")];
};

const usePretty = () => {
  const [v, set] = usePref("pretty", "1");
  return [v !== "0", next => set(next ? "1" : "0")];
};

function PayloadControls({ pretty, setPretty, copyAs, setCopyAs, decode, setDecode }) {
  return html`
    <${Fragment}>
      <label class="chip" style=${{ cursor: "pointer" }}
             title=${pretty
               ? "Showing JSON indented. Switch to the bytes exactly as they arrived."
               : "Showing the payload exactly as it arrived. Switch to indented JSON."}>
        <input type="checkbox" checked=${pretty} style=${{ width: "auto", marginRight: "5px" }}
               onChange=${e => setPretty(e.target.checked)} />
        pretty JSON
      </label>
      <label class="chip" style=${{ cursor: "pointer" }}
             title=${"Read a payload through the schema its headers name, from saguin's "
                     + "schema registry - a latest channel holding the schemas, and a "
                     + "`schema` property on each message naming the topic its own lives at"}>
        <input type="checkbox" checked=${decode} style=${{ width: "auto", marginRight: "5px" }}
               onChange=${e => setDecode(e.target.checked)} />
        deserialize with the registry
      </label>
      <label class="chip" title="How the payload is encoded when you copy a message">
        copy payload as
        <select value=${copyAs} onChange=${e => setCopyAs(e.target.value)}
                style=${{ padding: "0 2px", border: "none", background: "transparent",
                          font: "inherit", color: "var(--text)", marginLeft: "4px" }}>
          <option value="text">as-is</option>
          <option value="base64">base64</option>
          <option value="hex">hex</option>
        </select>
      </label>
    <//>`;
}

// **One timestamp format everywhere, to the millisecond.** A record's
// position in a second matters when several share one - which on a broker
// is most of them - and `toLocaleTimeString` rounds that away and varies by
// whose machine is looking. This is local time, matching the clock of
// whoever is reading and the broker's own log lines.
const stamp = t => {
  const d = new Date((t || 0) * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
};

// **The encoding is always named in the copied object.** A clipboard payload
// that does not say how it is encoded is one the receiver has to guess at,
// and a guess between UTF-8 and base64 silently succeeds on the wrong
// answer often enough to be dangerous.
const hexToBytes = hex => (hex.match(/../g) || []).map(x => parseInt(x, 16));
const bytesToBase64 = bs => btoa(bs.map(b => String.fromCharCode(b)).join(""));
const bytesToHex = bs => bs.map(b => b.toString(16).padStart(2, "0")).join("");
// The bytes as they were on the wire, whichever form the page happens to
// hold: a text payload was decoded from UTF-8, so it re-encodes exactly.
const bytesOf = p => p.hex != null ? hexToBytes(p.hex)
                   : [...new TextEncoder().encode(p.text || "")];

const envelopeOf = m => ({
  topic: m.topic,
  channel: m.channel || null,
  kind: m.kind || "broadcast",
  received_at: stamp(m.at),
  qos: m.qos,
  retained: !!m.retained,
  offset: m.offset ?? null,
  user_properties: m.properties || {},
});

function messageAsJSON(m, copyAs) {
  const p = m.payload || {};
  let payload;
  if (p.kind === "empty") {
    payload = { bytes: 0, encoding: "none",
                // The reason comes from the answer, which knows which channel
                // this landed on. A sentence written here was a second copy
                // and said "a delete on a latest channel" over broadcast
                // topics that have no latest channel anywhere near them.
                note: p.why || "zero-length" };
  } else if (copyAs === "base64") {
    payload = { bytes: p.bytes, encoding: "base64", value: bytesToBase64(bytesOf(p)) };
  } else if (copyAs === "hex") {
    payload = { bytes: p.bytes, encoding: "hex", value: bytesToHex(bytesOf(p)) };
  } else if (p.kind === "json") {
    // **The parsed object, not the object beside its own source.** Carrying
    // both made every JSON message twice, and left a reader to work out
    // which of the two was the one to use.
    payload = { bytes: p.bytes, encoding: "json", value: p.value };
  } else if (p.text != null) {
    payload = { bytes: p.bytes, encoding: "utf-8", value: p.text };
  } else {
    // **As-is is not always possible.** These bytes are not valid UTF-8, so
    // there is no as-is text form of them and base64 is used instead. The
    // `encoding` field is the whole of what a receiver needs to know; a
    // sentence beside it is prose in a data structure.
    payload = { bytes: p.bytes, encoding: "base64", value: bytesToBase64(bytesOf(p)) };
  }
  return JSON.stringify({ ...envelopeOf(m), payload }, null, 2);
}

// **The same envelope, with the bytes read through the schema.** A
// deserialized payload copied on its own is an object with no topic, no
// timestamp and no user properties on it, and whoever receives it has to be
// told separately what it was. `format_from` is there because "the
// publisher declared it" and "we read it off the schema" are different
// claims, and a paste that does not distinguish them invites the reader to
// assume the first.
function decodedAsJSON(m, schemaTopic, d) {
  return JSON.stringify({
    ...envelopeOf(m),
    payload: {
      bytes: (m.payload || {}).bytes,
      format: d.format || null,
      format_from: d.inferred ? "the schema" : "the message's content type",
      schema: schemaTopic,
      message: d.message,
      value: d.value,
    },
  }, null, 2);
}

function CopyButton({ text, title }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try {
      // The clipboard API exists only in a secure context, which loopback
      // is. Over plain HTTP on a routable address it is not, so there is a
      // fallback rather than a button that quietly does nothing.
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
        document.body.appendChild(ta); ta.select();
        document.execCommand("copy"); document.body.removeChild(ta);
      }
      setDone(true); setTimeout(() => setDone(false), 1200);
    } catch (e) { setDone(false); }
  };
  return html`
    <button class="chip" onClick=${copy} title=${title || "Copy this message as JSON"}
            style=${{ cursor: "pointer", marginLeft: "auto",
                      color: done ? "var(--good)" : "var(--dim)" }}>
      ${done ? "copied" : "copy"}
    </button>`;
}

// A payload, however it turned out: JSON indented or exactly as it arrived,
// text as text, bytes as hex, and a zero-length payload named rather than
// shown as nothing - on a `latest` channel that is how a value is deleted,
// so an empty box would hide the only thing that happened.
//
// **The bytes are shown in every case**, because "could not be decoded"
// printed in place of a record tells you about the viewer rather than about
// what arrived. It knows no schemas: a viewer that deserialized one deployment's
// protobuf would be a viewer for that deployment.
function Payload({ p, pretty }) {
  if (!p) return null;
  const body = p.kind === "json"  ? (pretty === false ? p.text
                                                      : JSON.stringify(p.value, null, 2))
             : p.kind === "text"  ? p.text
             : p.kind === "bytes" ? p.hex
             : null;
  return html`
    <div>
      ${p.why ? html`<div class="why payloadwhy">${p.why}</div>` : null}
      ${body === null ? null : html`<pre>${body}</pre>`}
    </div>`;
}

// **Deserialized through saguin's schema registry**, which is a `latest`
// channel and an agreement rather than a thing the broker runs. The Content
// Type says the format, a `schema` user property names the *topic* the schema
// lives at, and this point-reads that topic and reads the payload through
// it. Nothing here knows any deployment: the pointer comes from the message
// and the schema from the broker.
// Keyed by schema and bytes, so a page re-rendering once a second asks the
// server once. The server caches the compiled type behind it.
const decodeCache = new Map();

function SchemaBlock({ topic, contentType, hex, decode, setDecode, message,
                      pretty }) {
  const [showSchema, setShowSchema] = useState(false);
  const [schema, setSchema] = useState(null);
  const key = topic + "|" + (hex || "");
  const [decoded, setDecoded] = useState(() => decodeCache.get(key) || null);

  const ctype = (contentType || "").toLowerCase().split(";")[0];
  // **Whether these bytes can be read is the backend's question, not this
  // one's.** This used to require a Content Type from the list below, which
  // meant a publisher that named a schema and no Content Type - the common
  // shape, and what the bento-connectors generator does - was never even
  // offered to the deserializer. The backend reads the format off the schema
  // when no header declares one, so the only thing to check here is that
  // there are bytes and a schema to read them with.
  const decodable = !!hex;

  useEffect(() => {
    if (!decode || !decodable) return;
    const hit = decodeCache.get(key);
    if (hit) { setDecoded(hit); return; }
    let live = true;
    api("/api/decode", { method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ schema: topic, content_type: ctype, hex }),
    }).then(r => {
      if (!live || !r.body) return;
      if (decodeCache.size > 300) decodeCache.clear();
      decodeCache.set(key, r.body);
      setDecoded(r.body);
    });
    return () => { live = false; };
  }, [key, decode, decodable]);

  const loadSchema = refresh => {
    setSchema(null);
    api("/api/schema?topic=" + encodeURIComponent(topic) + (refresh ? "&refresh=1" : ""))
      .then(r => r.body && setSchema(r.body));
  };

  return html`
    <div class="schemablock">
      ${decode && decodable && decoded && !decoded.error && html`
        <div class="decoded">
          <div class="decodedhead">
            <span class="tag schema">schema registry</span>
            <span>read as <b class="mono">${decoded.message}</b> using${" "}
              <code>${topic}</code>${decoded.inferred
                ? " - the message declared no content type, so the " +
                  "serialization format was read off the schema itself"
                : ""}</span>
            ${message && html`
              <${CopyButton} title="Copy this message deserialized, as JSON"
                             text=${decodedAsJSON(message, topic, decoded)} />`}
          </div>
          <pre>${pretty === false
            ? JSON.stringify(decoded.value)
            : JSON.stringify(decoded.value, null, 2)}</pre>
        </div>`}
      ${decode && decodable && decoded && decoded.error && html`
        <div class="decoded warn">
          <div class="decodedhead">
            <span class="tag schema">schema registry</span>
            <span>${decoded.error}</span>
          </div>
        </div>`}
      ${decode && decodable && !decoded && html`
        <div class="why">Reading <code>${topic}</code> from the registry…</div>`}
      ${!decode && decodable && html`
        <div class="decoded off">
          <div class="decodedhead">
            <span class="tag schema">schema registry</span>
            <span>These bytes can be read with the schema this message names,
              and <b>deserializing is switched off</b>.</span>
            <button class="chip" style=${{ cursor: "pointer", marginLeft: "auto" }}
                    onClick=${() => setDecode(true)}>decode them</button>
          </div>
        </div>`}

      <button class="chip" style=${{ cursor: "pointer" }}
              title=${decodable && !decode
                ? "Deserializing is off - this shows the schema text only"
                : "Show the schema this message points at"}
              onClick=${() => { const n = !showSchema; setShowSchema(n);
                                if (n && !schema) loadSchema(false); }}>
        ${showSchema ? "▾" : "▶"} schema <b>${topic}</b>
      </button>
      ${showSchema && html`
        <div style=${{ marginTop: "6px" }}>
          ${!schema ? html`<div class="why">Point-reading ${topic}…</div>`
            : schema.error ? html`<div class="why" style=${{ color: "var(--warning)" }}>${schema.error}</div>`
            : html`
              <div class="msgmeta">
                <span class="chip">${schema.bytes} bytes</span>
                <span class="chip">${schema.cached ? "cached" : "read now"}</span>
                <button class="chip" style=${{ cursor: "pointer" }}
                        onClick=${() => loadSchema(true)}>read again</button>
                <span class="why" style=${{ margin: 0 }}>
                  Cached until the pointer changes, which is what the convention
                  says to cache - the pointer is the whole topic.
                </span>
              </div>
              <pre>${schema.text}</pre>`}
        </div>`}
    </div>`;
}

// Whether this topic has a retained value right now, asked of the store
// rather than inferred from the messages - which cannot answer it.
function RetainedBadge({ topic }) {
  const [held, setHeld] = useState(null);
  useEffect(() => {
    let live = true;
    const load = () => api("/api/retained").then(r => {
      if (!live || !r.body) return;
      const it = (r.body.items || []).find(x => x.topic === topic);
      setHeld(it ? (it.payload || {}) : false);
    });
    load();
    const id = setInterval(load, 5000);
    return () => { live = false; clearInterval(id); };
  }, [topic]);
  if (held === null || held === false) return null;
  return html`
    <span class="tag retained"
          title=${"This topic has a retained value, which is a separate slot from the "
                 + "messages below: those were delivered to whoever was subscribed and "
                 + "stored nowhere. Clearing the retained value deletes none of them."}
      >retained value held${held.bytes != null ? ` · ${held.bytes} bytes` : ""}</span>`;
}

function Messages({ topic, q, onCounts }) {
  const [pretty] = usePretty();
  const [decode, setDecode] = useDecode();
  const [copyAs] = usePref("copy_as", "text");
  const [data, setData] = useState({ messages: [] });
  // **How many to hold is the header's setting; how many to draw is this
  // list's.** The ring is resized for every topic at once, so the selector
  // belongs where it applies - beside the topic's name, in the strip that
  // stays put - and what stays here is the count, which is about this topic.
  // There are no page numbers: what sits on page two of a topic that is
  // still receiving changes every second, so older ones are reached by
  // growing downwards from the newest.
  const [visible, setVisible] = useState(PAGE);
  useEffect(() => { setVisible(PAGE); }, [topic, q]);
  useEffect(() => {
    let live = true;
    const tick = () => api("/api/messages?topic=" + encodeURIComponent(topic)
                           + (q ? "&q=" + encodeURIComponent(q) : "")
                           + (decode ? "" : "&decode=0"))
      .then(r => live && r.body && setData(r.body));
    tick();
    const id = setInterval(tick, 1000);
    return () => { live = false; clearInterval(id); };
  }, [topic, decode, q]);

  const msgs = data.messages || [];
  const page = msgs.slice(0, visible);
  // **The counts are drawn beside the control they are about**, which is in
  // the strip above this list - so they are reported up rather than rendered
  // here. A count sitting under a list is a count you have to scroll away
  // from the `keep` selector to read, which is the one moment you want both.
  useEffect(() => {
    if (onCounts) onCounts({ shown: page.length, held: msgs.length,
                             cap: data.per_topic, kind: data.kind });
  }, [page.length, msgs.length, data.per_topic, data.kind]);
  if (!msgs.length)
    return html`<div class="empty">${q
      ? html`No message this page is holding contains <code>${q}</code>${
          decode ? "" : " - and deserializing is off, so only the bytes as they " +
                        "arrived were searched"}.`
      : "Nothing here yet."}</div>`;

  // **A latest channel has one value, and the page says so rather than
  // showing a list of length one.** Keeping a scrolling history of a latest
  // topic showed the viewer's own memory: an older value sat under a newer
  // one and read as the broker holding two.
  const latest = data.kind === "latest";
  return html`
    <div>
      ${q && html`
        <p class="why"><b>${data.matched}</b> of the ${data.held} arrival(s)
          this page is holding contain <code>${q}</code>${decode
            ? html`, searching the deserialized record as well as the bytes`
            : html`, searching the bytes as they arrived - deserializing is off`}.
        </p>`}
      ${latest && html`
        <p class="why">
          A <code>latest</code> channel holds one value per topic - this is
          that value, as the broker would hand it to any client subscribing
          now. It has been replaced ${(data.count || 1) - 1} time(s) since
          this page connected.
        </p>`}
      ${page.map((m, i) => html`
        <div class="msg" key=${i}>
          <div class="msgmeta">
            <span class="chip">${stamp(m.at)}</span>
            ${m.offset && html`<span class="chip">offset <b>${m.offset}</b></span>`}
            <span class="chip">QoS <b>${m.qos}</b></span>
            <span class="chip"><b>${m.payload && m.payload.bytes}</b> bytes</span>
            ${m.retained && html`<span class="tag retained">${latest ? "current value" : "retained"}</span>`}
            ${m.expires_at && html`
              <span class="chip"
                    title=${"The publisher's Message Expiry Interval, as the moment it "
                            + "runs out. The broker sends the seconds remaining as of "
                            + "each delivery, so a number here would be right when the "
                            + "message arrived and wrong by however long this window has "
                            + "been open."}>
                expires <b>${stamp(m.expires_at)}</b></span>`}
            ${Object.entries(m.properties || {})
              // saguin-offset has a chip of its own above, and
              // saguin-expires is rendered as a readable moment rather than
              // as the raw milliseconds it arrives in.
              .filter(([k]) => k !== "saguin-offset" && k !== "saguin-expires")
              .map(([k, v]) => html`<span class="chip" key=${k}>${k}=<b>${v}</b></span>`)}
            <${CopyButton} text=${messageAsJSON({ ...m, topic, channel: data.channel,
                                                  kind: data.kind }, copyAs)} />
          </div>
          <${Payload} p=${m.payload} pretty=${pretty} />
          ${(m.properties || {}).schema && html`
            <${SchemaBlock} topic=${m.properties.schema}
                               contentType=${m.content_type || ""}
                               hex=${(m.payload || {}).hex}
                               message=${{ ...m, topic, channel: data.channel,
                                           kind: data.kind }}
                               pretty=${pretty}
                               decode=${decode} setDecode=${setDecode} />`}
        </div>`)}
      ${page.length < msgs.length && html`
        <button style=${{ width: "100%" }}
                onClick=${() => setVisible(v => v + PAGE)}>
          Show ${Math.min(PAGE, msgs.length - page.length)} older
          - ${msgs.length - page.length} more held
        </button>`}
    </div>`;
}

// **Publishing, on a connection of its own.** The backend opens a
// short-lived client for each one rather than using the viewer's, because
// several MQTT refusals end the connection instead of answering it - and a
// form that could drop the subscription would be a form that wipes the
// window it sits in.
function Publish({ topic, channel, kind, standalone }) {
  const [open, setOpen] = useState(!!standalone);
  const [to, setTo] = useState(topic || "");
  const [payload, setPayload] = useState("");
  const [fmtAs, setFmtAs] = useState("text");
  const [qos, setQos] = useState(1);
  const [retain, setRetain] = useState(false);
  const [contentType, setContentType] = useState("");
  const [expiry, setExpiry] = useState("");
  const [props, setProps] = useState("");
  const [reply, setReply] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { setTo(topic || ""); setReply(null); }, [topic]);

  const send = e => {
    e.preventDefault();
    // Written as `k=v` a line at a time, because a pair of inputs per
    // property is a widget to manage and this is a thing people paste.
    const properties = {};
    for (const line of props.split("\n")) {
      const i = line.indexOf("=");
      if (i > 0) properties[line.slice(0, i).trim()] = line.slice(i + 1).trim();
    }
    setBusy(true);
    api("/api/publish", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ topic: to, payload, format: fmtAs, qos: Number(qos),
                             retain, content_type: contentType,
                             message_expiry: expiry, properties }),
    }).then(r => { setReply(r.body); setBusy(false); });
  };

  return html`
    <div class="card">
      <h3 style=${{ display: "flex", alignItems: "center", gap: "8px" }}>
        ${!standalone && html`
          <button class="iconbtn" style=${{ width: "22px", height: "22px", fontSize: "10px" }}
                  onClick=${() => setOpen(!open)}>${open ? "▾" : "▶"}</button>`}
        Publish
        <span class="sub" style=${{ fontWeight: 400 }}>
          as a client of its own, so a refusal cannot drop this page's subscription
        </span>
      </h3>
      ${standalone && html`
        <p class="why">
          Any topic, whether or not it exists yet. A topic no channel's filter
          claims is ordinary broadcast: it is delivered to whoever is
          subscribed at that moment and stored nowhere, so it appears in the
          tree as it arrives and is gone when this page restarts.
        </p>`}
      ${kind === "queue" && html`
        <p class="why">This topic is in a <code>queue</code> channel: publishing
          here creates work a worker will be given.</p>`}
      ${open && html`
        <form onSubmit=${send}>
          <div style=${{ display: "flex", gap: "8px", width: "100%", marginBottom: "8px" }}>
            <input style=${{ flex: 1, fontFamily: "var(--mono)" }} value=${to}
                   onInput=${e => setTo(e.target.value)} placeholder="topic" />
          </div>
          <textarea style=${{ width: "100%", minHeight: "90px", fontFamily: "var(--mono)",
                              fontSize: "12px", marginBottom: "8px", resize: "vertical" }}
                    value=${payload} onInput=${e => setPayload(e.target.value)}
                    placeholder=${fmtAs === "hex" ? "deadbeef"
                                 : fmtAs === "json" ? '{"key": "value"}' : "payload"} />
          <div style=${{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
            <select value=${fmtAs} onChange=${e => setFmtAs(e.target.value)}>
              <option value="text">text</option>
              <option value="json">JSON</option>
              <option value="hex">hex</option>
            </select>
            <select value=${qos} onChange=${e => setQos(e.target.value)}>
              <option value="1">QoS 1</option>
              <option value="0">QoS 0</option>
            </select>
            <label class="sub" style=${{ display: "flex", alignItems: "center", gap: "5px" }}
                   title="Publish with the RETAIN flag">
              <input type="checkbox" checked=${retain} style=${{ width: "auto" }}
                     onChange=${e => setRetain(e.target.checked)} /> retain
            </label>
            <input style=${{ width: "150px" }} value=${contentType}
                   onInput=${e => setContentType(e.target.value)}
                   placeholder="content type" />
            <input style=${{ width: "110px" }} value=${expiry} inputmode="numeric"
                   onInput=${e => setExpiry(e.target.value)}
                   title=${"Message Expiry Interval, in seconds. On a broadcast topic "
                           + "it deletes the retained value when it runs out; on a "
                           + "channel it is reported to consumers and deletes nothing, "
                           + "and on a queue it ends the job."}
                   placeholder="expiry (s)" />
            <button class="primary" type="submit" disabled=${busy}>
              ${busy ? "Publishing…" : "Publish"}
            </button>
          </div>
          <textarea style=${{ width: "100%", minHeight: "44px", marginTop: "8px",
                              fontFamily: "var(--mono)", fontSize: "12px", resize: "vertical" }}
                    value=${props} onInput=${e => setProps(e.target.value)}
                    placeholder="user properties, one per line: name=value" />
          <p class="why" style=${{ marginTop: "6px", marginBottom: 0 }}>
            RETAIN on a broadcast topic is honoured: the broker's retained
            store always exists, and <code>broker.retained</code> only chooses
            which provider holds it and for how long. It is refused${" "}
            <code>0x9A</code> only for a client whose roles deny${" "}
            <code>retained</code>. On a channel topic the channel is the
            store, and a <code>latest</code> channel is the same idea with a
            bound on it.
          </p>
        </form>
        ${reply && html`
          <div class=${"reply " + (reply.ok ? "ok" : "bad")}>
            ${reply.ok
              ? `Published ${reply.bytes} bytes to ${reply.topic} - ${reply.reason}`
              : `Refused: ${reply.error}`}
          </div>`}`}
    </div>`;
}

// **Which consumers are behind, and by how much.**
//
// The catalogue cannot say. `saguin_channel_consumer_position_min` is one
// number per channel, so one straggler in a fleet of three hundred reads
// exactly like a fleet that has stopped - and no metric can name them,
// because a client chooses its own id and Prometheus keeps a series for
// every label set it has ever seen. So it is a route, read when somebody
// asks, and this is somebody asking.
function Consumers() {
  const [data, setData] = useState(null);
  useEffect(() => {
    let live = true;
    const load = () => api("/api/consumers")
      .then(r => live && r.body && setData(r.body));
    load();
    const id = setInterval(load, 5000);
    return () => { live = false; clearInterval(id); };
  }, []);

  if (!data) return html`<p class="empty">Asking…</p>`;
  if (data.error) return html`<p class="empty">${data.error}</p>`;
  const channels = (data.channels || []).filter(c => c.consumers > 0);
  // **One export per channel, because that is what a table is here.** The
  // page draws a separate table per channel rather than one long list with a
  // channel column, so a single button at the top would either merge tables
  // nobody asked to see combined or export only the first one silently.
  const exportOf = c => () => downloadCsv(`consumers-${c.channel}`,
    ["consumer", "at offset", "behind", "last seen"],
    c.positions.map(p => [p.reader, p.offset, p.behind, p.last_seen]));
  return html`
    <h2>Who is behind</h2>
    <p class="sub">Each channel's durable consumers, furthest behind first.
      A hundred rows at most, and the count beside them is how many there
      are - a hundred of three hundred read as the whole fleet otherwise.</p>
    ${channels.length === 0
      ? html`<p class="empty">No channel has a durable consumer yet.</p>`
      : channels.map(c => html`
          <div key=${c.channel} class="opsblock">
            <h3 style=${{ display: "flex", alignItems: "center", gap: "8px" }}>
              <span>${c.channel}
                <span class="sub">${" "}${c.consumers}${" consumers, next offset "}${c.next_offset}</span>
              </span>
              <${ExportCsvButton} onClick=${exportOf(c)} />
            </h3>
            <table class="ops">
              <thead><tr><th>consumer</th><th>at offset</th><th>behind</th><th>last seen</th></tr></thead>
              <tbody>
                ${c.positions.map(p => html`
                  <tr key=${p.reader} class=${p.behind > 0 ? "behind" : ""}>
                    <td><code>${p.reader}</code></td>
                    <td>${p.offset}</td>
                    <td>${p.behind}</td>
                    <td class="sub">${p.last_seen}</td>
                  </tr>`)}
              </tbody>
            </table>
            ${c.returned < c.consumers && html`
              <p class="sub">${"showing " + c.returned + " of " + c.consumers}</p>`}
          </div>`)}`;
}

// **Which readers lost records, and how many - the question the counter
// cannot answer.**
//
// `saguin_channel_position_lost_total` is one number per channel counting
// occurrences rather than readers, so a rate that will not come down says
// nothing about which of three hundred devices to go and look at. The route
// names them, worst first by records lost.
//
// **The identifier carries its scheme, and the page shows it whole.** A
// position is stored under `mqtt:<client id>` for a session and
// `bridge:<rule name>` for an outbound bridge rule, because a client may
// legally call itself `bridge:head-office` and without the scheme a device
// and a bridge link would share one row. So the cell, the key and the export
// all carry the prefixed string: it is what `/v1/operations/consumers` keys
// its rows by, and an operator who copies it out of here can find the same
// reader in *Who is behind* by pasting it. Stripping the prefix to make the
// cell tidier would break that join and reintroduce the collision the scheme
// exists to stop. `kind` is drawn under it and is not the same thing - two
// kinds, `session` and `consumer`, share the `mqtt:` scheme.
//
// **The untold rows are the ones to draw differently.** A durable session
// whose position had been passed when it reconnected is told so - Session
// Present = 0 - and starts again knowing it lost its place. A consumer
// overtaken while it was connected and reading cannot be told at all: MQTT
// has no way to say it, so the device believes it is up to date and nobody
// but whoever opens this page ever knows. One column among eight would bury
// the only row on the page that nothing else in the system will report.
function PositionLost() {
  const [data, setData] = useState(null);
  useEffect(() => {
    let live = true;
    const load = () => api("/api/position-lost")
      .then(r => live && r.body && setData(r.body));
    load();
    const id = setInterval(load, 5000);
    return () => { live = false; clearInterval(id); };
  }, []);

  if (!data) return html`<p class="empty">Asking…</p>`;
  if (data.error) return html`<p class="empty">${data.error}</p>`;
  const rows = data.readers || [];
  const silent = rows.filter(r => !r.reported).length;
  const exportCsv = () => downloadCsv("position-lost",
    ["reader", "channel", "kind", "told", "records lost", "times",
     "position", "floor", "last passed"],
    rows.map(r => [r.reader, r.channel, r.kind, r.reported ? "yes" : "no",
      r.records_missed, r.count, r.last_position, r.last_floor, r.last_seen]));
  // Why this reader was told or was not, in its own terms. The kind decides
  // it: only a session has a door to be told through.
  const told = r => r.reported
    ? "This reader knows: it came back to Session Present = 0 and started again."
    : r.kind === "consumer"
    ? "It was overtaken while connected and reading, and MQTT has no way to "
      + "tell it. The device believes it is up to date and cannot ask for "
      + "what it missed."
    : "Nothing told this reader, so it cannot ask for what it missed.";
  return html`
    <div style=${{ display: "flex", alignItems: "center", gap: "8px" }}>
      <h2 style=${{ margin: 0 }}>Who lost records
        ${rows.length > 0 && html`<span class="sub">${" "}${silent}${
          silent === 1 ? " of these readers was never told" : " of these readers were never told"}</span>`}
      </h2>
      <${ExportCsvButton} onClick=${exportCsv} />
    </div>
    <p class="sub">Retention moved a channel's floor - its oldest readable
      offset - past a reader's stored position, so the records between are
      gone and that reader's claim on them with them. Worst first by records
      lost. One row per reader and channel; <b>times</b> is how often the
      floor passed it, which on a straggler is thousands. A reader is named
      with its scheme - <code>mqtt:</code> a client id, <code>bridge:</code> a
      rule name - because a client may call itself <code>bridge:anything</code>;
      that whole string is the one <i>Who is behind</i> uses, so it is what to
      copy when looking a reader up there.</p>
    <p class="sub">This is kept in memory and a restart empties it. An empty
      page is a broker that has lost nothing since it started, which is not
      the same as one that never has.</p>
    ${rows.length === 0
      ? html`<p class="empty">No reader has been passed by a retention floor
          since this broker started.</p>`
      : html`
        <table class="ops">
          <thead><tr>
            <th>reader</th><th>channel</th><th>told</th><th>records lost</th>
            <th>times</th><th>position</th><th>floor</th><th>last passed</th>
          </tr></thead>
          <tbody>
            ${rows.map(r => html`
              <tr key=${r.reader + "\u0000" + r.channel}
                  class=${r.reported ? "" : "silent"}>
                <td><code>${r.reader}</code>
                  <div class="sub">${r.kind}</div></td>
                <td>${r.channel}</td>
                <td>${r.reported
                      ? html`<span title=${told(r)}>told</span>`
                      : html`<span title=${told(r)}>never told</span>`}</td>
                <td>${(r.records_missed || 0).toLocaleString()}</td>
                <td>${(r.count || 0).toLocaleString()}</td>
                <td>${r.last_position}</td>
                <td>${r.last_floor}</td>
                <td class="sub">${r.last_seen}</td>
              </tr>`)}
          </tbody>
        </table>
        ${/* **Three numbers, and the third is the one easy to leave out.**
              `tracked` past `returned` is rows the broker is holding that
              this body did not carry; `beyond` is rows that never fit in the
              record at all and were never kept. A page that showed a hundred
              rows and said neither would have an operator believe they had
              seen the whole fleet. */""}
        ${data.returned < data.tracked && html`
          <p class="sub">${"showing " + data.returned + " of " + data.tracked +
            " the broker is holding - worst first, so what is cut off lost least"}</p>`}
        ${data.beyond > 0 && html`
          <p class="sub">${"and " + data.beyond + " more never fit in the record " +
            "and were not kept at all, so they are not in the count above"}</p>`}`}`;
}

// **Who is connected, who is merely held, and the one verb an operator
// takes.**
//
// The counts on the dashboard cannot answer this: three hundred devices with
// two hundred connected is either a rota or a hundred that have stopped
// calling, and `saguin_connections` beside `saguin_sessions_offline` reads
// the same for both.
//
// **The lag column is joined from `/api/consumers` rather than asked for
// again.** The broker answers positions on one route, keyed by the same
// client id, and a second route answering them would be a second thing to
// keep in step with the first.
function Sessions() {
  const [data, setData] = useState(null);
  const [lag, setLag] = useState(null);
  const [busy, setBusy] = useState(null);
  const [said, setSaid] = useState(null);

  const load = () => {
    api("/api/sessions").then(r => r.body && setData(r.body));
    api("/api/consumers").then(r => r.body && setLag(r.body));
  };
  useEffect(() => {
    let live = true;
    const tick = () => { if (live) load(); };
    tick();
    const id = setInterval(tick, 5000);
    return () => { live = false; clearInterval(id); };
  }, []);

  if (!data) return html`<p class="empty">Asking…</p>`;
  if (data.error) return html`<p class="empty">${data.error}</p>`;

  // Each client's worst position, across every channel it reads. One number
  // per row, because the row is about the client rather than the channel -
  // and "Who is behind" is one click away for the per-channel answer.
  const behind = {};
  for (const c of (lag && lag.channels) || []) {
    for (const p of c.positions || []) {
      const id = String(p.reader || "").replace(/^mqtt:/, "");
      if (behind[id] === undefined || p.behind > behind[id]) behind[id] = p.behind;
    }
  }

  const hangUp = id => {
    setBusy(id); setSaid(null);
    api("/api/disconnect", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ client_id: id }),
    })
      .then(r => {
        const b = r.body || {};
        setSaid({ id, ok: !!b.ok, text: b.ok
          ? (b.hung_up ? "hung up - it will reconnect and resume where it was"
                       : "nothing was connected under that id")
          : (b.error || "the viewer could not reach the broker"),
          hint: b.hint });
        load();
      })
      .finally(() => setBusy(null));
  };

  const rows = data.sessions || [];
  const exportCsv = () => downloadCsv("sessions",
    ["client", "user", "state", "behind", "subs", "keepalive", "expiry"],
    rows.map(s => [s.client_id, s.user || "", s.connected ? "connected" : "held",
      behind[s.client_id] === undefined ? "" : behind[s.client_id], s.subscriptions,
      s.keepalive ? s.keepalive + "s" : "none",
      s.expires_after ? s.expires_after + "s" : "with the connection"]));
  return html`
    <div style=${{ display: "flex", alignItems: "center", gap: "8px" }}>
      <h2 style=${{ margin: 0 }}>Sessions
        <span class="sub">${data.connected}${" connected, "}${data.offline}${" held"}</span>
      </h2>
      <${ExportCsvButton} onClick=${exportCsv} />
    </div>
    <p class="sub">Every client this broker knows: the ones with a connection,
      and the ones holding a session with nothing behind it. A held session is
      what a session is for - a device offline keeps its place - so a number
      that only climbs is sessions nothing is coming back for, and each says
      how long it has left.</p>
    <p class="sub">Hanging a client up ends the connection and leaves the
      session alone: it reconnects and resumes at its stored position - and
      it comes back with whatever the broker's password file and acl_file say
      then. So this button withdraws nothing on its own. Taking a device's
      access away is: edit those two files, signal the broker
      (<code>SIGUSR1</code>) to re-read them, then hang the client up so it
      connects again and is refused. On its own the button is for a client
      that is stuck - and on a queue the jobs it was holding go back when its
      connection ends.</p>

    ${said && html`
      <div class=${"reply " + (said.ok ? "ok" : "bad")}>
        <code>${said.id}</code>${" - "}${said.text}
        ${said.hint && html`<div class="sub" style=${{ marginTop: "4px" }}>${said.hint}</div>`}
      </div>`}

    ${rows.length === 0
      ? html`<p class="empty">No client has connected to this broker yet.</p>`
      : html`
        <table class="ops">
          <thead><tr>
            <th>client</th><th>user</th><th>state</th><th>behind</th>
            <th>subs</th><th>keepalive</th><th>expiry</th><th></th>
          </tr></thead>
          <tbody>
            ${rows.map(s => html`
              <tr key=${s.client_id} class=${s.connected ? "" : "behind"}>
                <td><code>${s.client_id}</code>
                  <div class="sub">${s.listener}${" · MQTT "}${mqttName(s.protocol)}</div></td>
                <td>${s.user || html`<span class="sub">anonymous</span>`}</td>
                <td>${s.connected
                      ? "connected"
                      : html`<span title="Holding a session with nothing connected to it">held</span>`}</td>
                <td>${behind[s.client_id] === undefined
                      ? html`<span class="sub">-</span>` : behind[s.client_id]}</td>
                <td>${s.subscriptions}</td>
                <td>${s.keepalive ? s.keepalive + "s" : html`<span class="sub">none</span>`}</td>
                <td>${s.expires_after
                      ? s.expires_after + "s"
                      : html`<span class="sub" title="Session Expiry Interval 0: the session ends with the connection">with the connection</span>`}</td>
                <td>${s.connected
                      ? html`<button class="chip" disabled=${busy === s.client_id}
                                     title="End this connection. The session survives, so it reconnects and resumes."
                                     onClick=${() => hangUp(s.client_id)}>
                               ${busy === s.client_id ? "hanging up…" : "hang up"}
                             </button>`
                      : html`<span class="sub" title="Nothing is connected under this id, so there is nothing to hang up">-</span>`}</td>
              </tr>`)}
          </tbody>
        </table>
        ${data.returned < data.total && html`
          <p class="sub">${"showing " + data.returned + " of " + data.total +
            " - held sessions first, so what is cut off is the fleet behaving"}</p>`}`}`;
}

// **What a queue is holding, without taking any of it.**
//
// This page lists queues and does not read them, because a subscriber would
// take the jobs the workers are meant to do. That left the one question
// somebody actually has - which of these is stuck - with nowhere to go. The
// route leases nothing and returns no payloads, so asking costs the workers
// nothing.
function QueueView({ channel }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    let live = true;
    const load = () => api("/api/queue?channel=" + encodeURIComponent(channel))
      .then(r => live && r.body && setData(r.body));
    load();
    const id = setInterval(load, 5000);
    return () => { live = false; clearInterval(id); };
  }, [channel]);

  if (!data) return html`<p class="empty">Asking…</p>`;
  if (data.error) return html`<p class="empty">${data.error}</p>`;
  return html`
    <h2>Unresolved work in <span class="topicname">${channel}</span></h2>
    <p class="sub">Oldest first. Nothing here was consumed to show it, and no
      payload is carried - reading records is MQTT's job.</p>
    ${data.unresolved === 0
      ? html`<p class="empty">Nothing unresolved: every job has been acknowledged.</p>`
      : html`
        <table class="ops">
          <thead><tr><th>offset</th><th>topic</th><th>attempts</th><th>state</th><th>holder</th><th>first seen</th></tr></thead>
          <tbody>
            ${(data.records || []).map(r => html`
              <tr key=${r.offset} class=${r.attempts > 1 ? "behind" : ""}>
                <td>${r.offset}</td>
                <td><code>${r.topic}</code></td>
                <td>${r.attempts}</td>
                <td>${r.state}</td>
                <td class="sub">${r.holder || ""}</td>
                <td class="sub">${r.first_seen || ""}</td>
              </tr>`)}
          </tbody>
        </table>
        ${data.returned < data.unresolved && html`
          <p class="sub">${"showing " + data.returned + " of " + data.unresolved}</p>`}`}`;
}

// **Two numbers, not one.** How many to keep is a memory bound; how many to
// draw is a rendering one, and they are different sizes - ten thousand rows
// of payload in the DOM will make the page crawl long before the array
// troubles the machine. So the buffer is capped, the newest slice is drawn,
// and older ones are reached by asking for more.
//
// **And no page numbers.** What sits on "page 2" of a live stream changes
// every second, so a reader can never finish it. Pause is what makes older
// messages hold still; this only ever grows downwards from the newest.
const KEEP = [100, 500, 1000, 2000];
const PAGE = 200;

// Everything in one arrival a reader can see, for the filter box to search.
// The topic is in it here - unlike the topic view, this list is every topic
// - and the deserialized record only when deserializing is on and a card has already
// asked for it. The cache is what the rendered cards fill.
const feedHaystack = (m, decode) => {
  const p = m.payload || {};
  const parts = [m.topic || "", m.content_type || "", p.text || "", p.hex || ""];
  for (const [k, v] of Object.entries(m.properties || {})) parts.push(k + "=" + v);
  if (p.kind === "json" && p.value !== undefined) parts.push(JSON.stringify(p.value));
  const ref = (m.properties || {}).schema;
  if (decode && ref && p.hex) {
    const hit = decodeCache.get(ref + "|" + p.hex);
    if (hit && !hit.error) parts.push(JSON.stringify(hit.value));
  }
  return parts.join("\n").toLowerCase();
};

// **What is flowing, rather than what is on one topic.** The tree answers
// the second question and cannot answer the first: a topic nobody thought
// to click is exactly the one worth seeing when a publish is going
// somewhere unexpected.
// **`showTopic` is passed in rather than reached for**, because what
// clicking a topic here does - narrow the tree, select the topic, move the
// right-hand panel off this feed - is all state the page owns and none of
// it is this component's.
function Feed({ showTopic }) {
  const [pretty, setPretty] = usePretty();
  const [decode, setDecode] = useDecode();
  const [copyAs, setCopyAs] = usePref("copy_as", "text");
  const [msgs, setMsgs] = useState([]);
  const [keep, setKeep] = useState(() => Number(cookie("saguin_viewer_feed_keep")) || 500);
  const [visible, setVisible] = useState(PAGE);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState(() => cookie("saguin_viewer_feed_filter") || "");
  const [meta, setMeta] = useState({});
  const seq = useRef(0);

  useEffect(() => { cookie("saguin_viewer_feed_filter", filter); }, [filter]);
  useEffect(() => { cookie("saguin_viewer_feed_keep", keep); }, [keep]);

  useEffect(() => {
    if (paused) return;
    let live = true;
    const tick = () => api("/api/feed?after=" + seq.current).then(r => {
      if (!live || !r.body) return;
      const b = r.body;
      seq.current = b.seq;
      setMeta({ dropped: b.dropped, held: b.held, capacity: b.capacity });
      if (b.messages.length) {
        setMsgs(prev => [...b.messages.reverse(), ...prev].slice(0, keep));
      }
    });
    tick();
    const id = setInterval(tick, 1000);
    return () => { live = false; clearInterval(id); };
  }, [paused, keep]);

  const matching = filter
    ? msgs.filter(m => feedHaystack(m, decode).includes(filter.toLowerCase()))
    : msgs;
  const shown = matching.slice(0, visible);

  return html`
    <div>
      <div class="panelhead">
        <h2>Everything, as it arrives</h2>
        <div style=${{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
          <button class=${paused ? "primary" : ""} onClick=${() => setPaused(!paused)}>
            ${paused ? "Resume" : "Pause"}
          </button>
          <input value=${filter} onInput=${e => setFilter(e.target.value)}
                 placeholder="filter by topic, payload or property"
                 style=${{ flex: 1, minWidth: "160px",
                                                         fontFamily: "var(--mono)" }} />
          ${filter && html`
            <button class="chip" style=${{ cursor: "pointer" }}
                    title="Empty the filter box"
                    onClick=${() => setFilter("")}>clear filter</button>`}
          <button title=${"Throw away the arrivals this page has collected. "
                          + "Broadcast messages are not stored, so what is "
                          + "dropped here is gone."}
                  onClick=${() => { setMsgs([]); setVisible(PAGE); }}>
            Clear messages</button>
          <label class="chip">keep
            <select value=${keep} onChange=${e => setKeep(Number(e.target.value))}
                    style=${{ padding: "0 2px", border: "none", background: "transparent",
                              font: "inherit", color: "var(--text)" }}>
              ${KEEP.map(n => html`<option key=${n} value=${n}>${n}</option>`)}
            </select></label>
          <span class="chip">showing <b>${shown.length}</b> of ${matching.length}</span>
          <span class="chip">broker buffer <b>${meta.held || 0}</b>/${meta.capacity || 0}</span>
          <${PayloadControls} pretty=${pretty} setPretty=${setPretty}
                              copyAs=${copyAs} setCopyAs=${setCopyAs}
                              decode=${decode} setDecode=${setDecode} />
        </div>
        ${meta.dropped && html`
          <p class="why" style=${{ color: "var(--warning)", marginTop: "8px", marginBottom: 0 }}>
            Messages arrived faster than this page read them, and the oldest
            were dropped from the buffer. Nothing is missing from the broker -
            only from this window.
          </p>`}
        ${paused && html`
          <p class="why" style=${{ marginTop: "8px", marginBottom: 0 }}>
            Paused. Messages keep arriving and the buffer keeps filling; resume
            to see what came in.
          </p>`}
      </div>
      <p class="why">
        Every message this viewer receives, newest first, whatever topic it is
        on. It needs no extra subscription - the viewer already watches${" "}
        <code>${"#"}</code>. <b>A queue's records never appear here</b>, and
        that is the broker's doing rather than a filter of ours: a filter
        merely crossing a queue is served everything except its work, so this
        page cannot take a job even by watching everything.
      </p>
      ${shown.length === 0
        ? html`<div class="empty">
            ${filter ? "Nothing matching that filter yet." : "Nothing has arrived yet."}
          </div>`
        : shown.map(m => html`
            <div class="msg" key=${m.seq}>
              <div class="msgmeta">
                <span class="chip">${stamp(m.at)}</span>
                <a class="topicname"
                   href=${"#" + encodeURIComponent((m.channel || "broadcast") + "/" + m.topic)}
                   title="Show this topic in the tree, and its messages here"
                   onClick=${e => {
                     // The href is what makes this copyable and openable in
                     // a new tab. The page reads that fragment when it
                     // loads rather than when it changes, so following it
                     // in place is this handler's job.
                     e.preventDefault();
                     showTopic && showTopic(m.channel, m.topic);
                   }}>${m.topic}</a>
                ${m.channel
                  ? html`<span class=${"tag " + m.kind}>${m.channel}</span>`
                  : html`<span class="tag broadcast">broadcast</span>`}
                ${m.replayed && html`<span class="tag muted"
                  title="its broker timestamp is older than this viewer's connection - an append channel is replayed from its retention floor on subscribe"
                  >replayed</span>`}
                ${m.offset && html`<span class="chip">offset <b>${m.offset}</b></span>`}
                <span class="chip">QoS <b>${m.qos}</b></span>
                <span class="chip"><b>${m.payload && m.payload.bytes}</b> bytes</span>
                ${m.retained && html`<span class="tag retained">retained</span>`}
                ${Object.entries(m.properties || {})
                  .filter(([k]) => k !== "saguin-offset")
                  .map(([k, v]) => html`<span class="chip" key=${k}>${k}=<b>${v}</b></span>`)}
                <${CopyButton} text=${messageAsJSON(m, copyAs)} />
              </div>
              <${Payload} p=${m.payload} pretty=${pretty} />
              ${(m.properties || {}).schema && html`
                <${SchemaBlock} topic=${m.properties.schema}
                               contentType=${m.content_type || ""}
                               hex=${(m.payload || {}).hex}
                               message=${m}
                               pretty=${pretty}
                               decode=${decode} setDecode=${setDecode} />`}
            </div>`)}
      ${shown.length < matching.length && html`
        <button style=${{ width: "100%" }}
                onClick=${() => setVisible(v => v + PAGE)}>
          Show ${Math.min(PAGE, matching.length - shown.length)} older
          - ${matching.length - shown.length} more held
        </button>`}
    </div>`;
}

// ---------------------------------------------------------------- charts --
//
// Hand-rolled SVG rather than a charting library, for the reason the lib
// directory exists: this page has no build step and must render on a
// machine with no route to the internet. The palette is the validated
// reference instance in app.css - categorical slots where series are the
// subject, one sequential hue for magnitude, a fixed status set never
// reused as "series 5".

const fmt = n => n == null ? "-" : n >= 1e9 ? (n / 1e9).toFixed(1) + "G"
                : n >= 1e6 ? (n / 1e6).toFixed(1) + "M"
                : n >= 1e3 ? (n / 1e3).toFixed(1) + "k"
                : String(Math.round(n * 100) / 100);

const bytes = n => n == null ? "-" : n >= 1 << 30 ? (n / (1 << 30)).toFixed(1) + " GiB"
                : n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + " MiB"
                : n >= 1024 ? (n / 1024).toFixed(1) + " KiB" : n + " B";

const duration = s => s == null ? "-"
                : s >= 86400 ? Math.floor(s / 86400) + "d " + Math.floor(s % 86400 / 3600) + "h"
                : s >= 3600 ? Math.floor(s / 3600) + "h " + Math.floor(s % 3600 / 60) + "m"
                : s >= 60 ? Math.floor(s / 60) + "m" : Math.round(s) + "s";

// A stat tile, which is the right form for a single current value - a
// one-bar bar chart is not.
function Info({ text }) {
  if (!text) return null;
  return html`<span class="info" title=${text} aria-label=${text}>i</span>`;
}

function Tile({ k, v, u, alarm, series, help }) {
  return html`
    <div class=${"tile" + (alarm ? " alarm" : "")}>
      <div class="k">${k}<${Info} text=${help} /></div>
      <div class="v">${v}${u && html` <span class="u">${u}</span>`}</div>
      ${series && series.length > 1 && html`<${Spark} points=${series} />`}
    </div>`;
}

// **A sparkline with no time on it answers half the question.** The shape
// says something changed; it does not say when, and "when" is the whole of
// what an operator does next with it. So the caption under it carries the
// span by default and the moment under the pointer while hovering - the
// same trade the larger charts make, at a size that has no room for an
// axis. The last point is marked, because otherwise which end is *now* is
// a guess.
function Spark({ points }) {
  const [at, setAt] = useState(null);
  const w = 160, hh = 26, pad = 2;
  const vals = points.map(p => p.v);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = hi - lo || 1;
  const X = i => (i / (points.length - 1)) * w;
  const Y = v => hh - pad - ((v - lo) / span) * (hh - pad * 2);
  const d = points.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.v).toFixed(1)}`).join("");
  const last = points[points.length - 1];
  const covered = Math.max(1, Math.round((last.t - points[0].t) / 60));

  const markAt = at == null ? points.length - 1 : at;
  return html`
    <div style=${{ position: "relative" }}>
      <svg class="chart" viewBox=${`0 0 ${w} ${hh}`} height="26"
           preserveAspectRatio="none"
           onMouseLeave=${() => setAt(null)}
           onMouseMove=${e => {
             const r = e.currentTarget.getBoundingClientRect();
             const i = Math.round(((e.clientX - r.left) / r.width) * (points.length - 1));
             setAt(Math.max(0, Math.min(points.length - 1, i)));
           }}>
        <path class="line" d=${d} stroke="var(--seq)" />
        ${at != null && html`
          <line class="grid" x1=${X(at)} y1="0" x2=${X(at)} y2=${hh} stroke="var(--dim)" />`}
      </svg>
      <i class="chartmark small" style=${{
        left: `${(X(markAt) / w) * 100}%`,
        top: `${Y(at == null ? last.v : points[at].v)}px`,
        background: "var(--seq)" }} />
      <div class="sparkfoot">
        ${at == null
          ? html`<span>${covered >= 60 ? Math.round(covered / 60) + "h" : covered + "m"}</span>
                 <span class="mono">${lo === hi ? "flat at " + fmt(lo) : fmt(lo) + " – " + fmt(hi)}</span>`
          : html`<span>${stamp(points[at].t).slice(11, 19)}</span>
                 <span class="mono"><b>${fmt(points[at].v)}</b></span>`}
      </div>
    </div>`;
}

// Magnitude, low to high: one hue, and the length carries the number.
// **Rows with their names on them**, which is what a breakdown is for.
//
// This drew an SVG with `preserveAspectRatio="none"` and reserved 34 units of
// width for a label it never wrote - so every bar was anonymous and the only
// way to read one was to hover it, one at a time. Fine for three refusal
// reasons; useless for a broker with eight channels, which is a chart of
// eight unnamed bars. The stretched viewBox is why no text was there: any
// glyph in it would have been squashed with the bars.
//
// So it is ordinary HTML now. The name and the number are always visible, the
// bar is proportional to the largest row, and nothing is stretched.
function Bars({ rows, unit }) {
  if (!rows.length) return html`<div class="empty">Nothing to show.</div>`;
  const max = Math.max(...rows.map(r => r.value), 1);
  const show = v => (unit === "bytes" ? bytes(v) : fmt(v));
  return html`
    <div class="bars">
      ${rows.map(r => html`
        <div class="barrow" key=${r.label} title=${`${r.label}: ${show(r.value)}`}>
          <span class="barlabel">${r.label}</span>
          <span class="bartrack">
            ${/* **A floor of one pixel, so a row worth nothing is still a
                  row.** A channel with no dead letters and a channel the
                  scrape did not reach must not look the same, and an absent
                  bar reads as an absent channel. */
              html`<span class="barfill" style=${{
                width: `max(1px, ${(r.value / max) * 100}%)`,
                background: r.color || "var(--seq)" }} />`}
          </span>
          <span class="barvalue">${show(r.value)}</span>
        </div>`)}
    </div>`;
}

// Part-to-whole across a handful of named outcomes: a stacked bar, with a
// gap between segments and every segment directly labelled - four series is
// where labels stop being optional, and two of the light slots sit under
// 3:1 on the surface, which obliges them anyway.
function Stacked({ segments }) {
  const total = segments.reduce((a, s) => a + s.value, 0);
  if (!total) return html`<div class="empty">Nothing has been delivered yet.</div>`;
  let x = 0;
  const parts = segments.filter(s => s.value > 0).map(s => {
    const wpc = (s.value / total) * 100, at = x; x += wpc;
    return { ...s, x: at, w: wpc };
  });
  return html`
    <div>
      <svg class="chart" viewBox="0 0 100 14" height="18" preserveAspectRatio="none">
        ${parts.map(p => html`
          <rect key=${p.label} x=${p.x} y="0" height="14" rx="1"
                width=${Math.max(p.w - 0.4, 0.2)} fill=${p.color}>
            <title>${p.label}: ${fmt(p.value)}</title>
          </rect>`)}
      </svg>
      <div class="legend">
        ${parts.map(p => html`
          <span key=${p.label}><i style=${{ background: p.color }}></i>
            ${p.label} <b class="mono">${fmt(p.value)}</b></span>`)}
      </div>
    </div>`;
}

// A single ratio against a limit is a meter, not a pie of two slices.
function Meter({ value, max, label }) {
  const pc = max ? Math.min(value / max, 1) * 100 : null;
  return html`
    <div>
      <div class="msgmeta" style=${{ justifyContent: "space-between", marginBottom: "4px" }}>
        <span>${label}</span>
        <span class="mono">${bytes(value)}${max ? " of " + bytes(max) : ""}</span>
      </div>
      ${pc == null
        ? html`<p class="why" style=${{ margin: 0 }}>No bound configured - the
            provider grows until the disk does.</p>`
        : html`<div class="meter"><i style=${{ width: pc + "%" }}></i></div>`}
    </div>`;
}

// Trend over time. One series is the point here, so there is no legend and
// the label names it; the crosshair reads a value rather than printing one
// on every point.
// **`format` is how a rate gets its unit**, and why it is a prop rather
// than a rounding at the two call sites: the range under the line and the
// readout under the cursor are the same number in two places, and a chart
// whose label said "659 B/s" while hovering it said "659.0410051075250"
// would be one of them lying about the other.
// **Averaging into buckets so a five-day line stays a smooth draw.** The
// widest window holds thousands of samples; past a few hundred an SVG path
// gains nothing a screen can show and costs the browser to draw each frame.
// Each bucket becomes one point at its mean value and its middle time, so the
// shape and the hover stay honest while the point count drops. This is the
// line, not the data - the numbers behind it are untouched.
function downsample(points, max) {
  if (points.length <= max) return points;
  const out = [], step = points.length / max;
  for (let b = 0; b < max; b++) {
    const s = Math.floor(b * step), e = Math.max(s + 1, Math.floor((b + 1) * step));
    let sum = 0, n = 0;
    for (let i = s; i < e && i < points.length; i++) { sum += points[i].v; n++; }
    const mid = points[Math.min(points.length - 1, (s + e) >> 1)];
    out.push({ t: mid.t, v: n ? sum / n : mid.v });
  }
  return out;
}

function TimeLine({ points, label, color, empty, format }) {
  const show = format || fmt;
  points = downsample(points, 500);   // a wide window is thousands of points; a screen shows ~500
  const [at, setAt] = useState(null);
  if (points.length < 2)
    return html`<div class="empty">${empty || html`A line needs two samples.
      The broker recomputes at most once a minute, so this fills in as they
      arrive.`}</div>`;
  const w = 320, hh = 90, pad = 4;
  const lo = Math.min(...points.map(p => p.v)), hi = Math.max(...points.map(p => p.v));
  const span = hi - lo || 1;
  const X = i => pad + i / (points.length - 1) * (w - pad * 2);
  const Y = v => hh - pad - (v - lo) / span * (hh - pad * 2);
  const d = points.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.v).toFixed(1)}`).join("");
  return html`
    <div style=${{ position: "relative" }}>
      <svg class="chart" viewBox=${`0 0 ${w} ${hh}`} height=${hh}
           preserveAspectRatio="none"
           onMouseLeave=${() => setAt(null)}
           onMouseMove=${e => {
             const r = e.currentTarget.getBoundingClientRect();
             const i = Math.round((e.clientX - r.left) / r.width * (points.length - 1));
             setAt(Math.max(0, Math.min(points.length - 1, i)));
           }}>
        <line class="grid" x1="0" y1=${Y(hi)} x2=${w} y2=${Y(hi)} />
        <line class="grid" x1="0" y1=${Y(lo)} x2=${w} y2=${Y(lo)} />
        <path class="line" d=${d} stroke=${color || "var(--seq)"} />
        ${at != null && html`
          <line class="grid" x1=${X(at)} y1="0" x2=${X(at)} y2=${hh} stroke="var(--dim)" />`}
      </svg>
      ${at != null && html`<i class="chartmark" style=${{
        left: `${(X(at) / w) * 100}%`, top: `${Y(points[at].v)}px`,
        background: color || "var(--seq)" }} />`}
      <div class="msgmeta" style=${{ justifyContent: "space-between", marginTop: "2px" }}>
        <span>${label}</span>
        <span class="mono">${at == null
          ? `${show(lo)} – ${show(hi)}`
          : `${new Date(points[at].t * 1000).toLocaleTimeString()} · ${show(points[at].v)}`}</span>
      </div>
    </div>`;
}

// **A refused reply topic loses every answer while accepting every request.**
// The state has carried `granted: false` per filter since it was written and
// nothing on the page read it, so an acl_file that granted the data filters and
// not this prefix produced a page that looked well and features that silently did
// nothing - which was found on a demo, under thirty-one passing checks.
// Returns the refused filter, so the pill can name it.
function replyRefused(state) {
  const subs = (state && state.subscriptions) || [];
  const bad = subs.find(s => !s.granted && s.filter &&
                             s.filter.startsWith("viewer-reply/"));
  return bad ? bad.filter : null;
}

const WINDOWS = [["15m", 15], ["30m", 30], ["1h", 60], ["2h", 120], ["4h", 240],
                 ["8h", 480], ["12h", 720], ["1d", 1440], ["2d", 2880],
                 ["3d", 4320], ["4d", 5760], ["5d", 7200]];
// **Following the broker, rather than a number somebody picked.** The
// catalogue is recomputed at most once a minute and a scrape arriving
// sooner is answered from the previous one, so a page polling every five
// seconds fetches the same sample eleven times out of twelve. It instead
// asks again just after the next sample is due, and the "taken Ns ago"
// counter ticks locally with no request at all.
const REFRESH = [["follow the broker", -1], ["2m", 120], ["5m", 300], ["off", 0]];

// **Not Prometheus, and the page says so.** saguin holds no history: every
// scrape is a snapshot, so any line here begins when this viewer began.
// Current values are right on the first scrape because the counters are
// cumulative; a rate needs a second.
// ===========================================================================
// Dashboards written in a file (see README "Dashboards"). The built-in
// Dashboard() below is unchanged and is what shows when none are configured;
// these components draw a validated spec fetched from /api/dashboards.

// **Minimal markdown for text cards** - headings, bold, italic, code, links,
// line breaks, and nothing else. A caption, not a document. Escaped first, so
// a payload pasted into a note cannot become markup.
function mdEscape(s) {
  return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function mdInline(s) {
  return s.replace(/`([^`]+)`/g, "<code>$1</code>")
          .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
          .replace(/_([^_]+)_/g, "<em>$1</em>")
          .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
                   '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function mdToHtml(src) {
  // Line by line, so a heading followed straight by a sentence - with no blank
  // line between - is a heading and a paragraph, not one heading swallowing
  // both. A blank line ends a paragraph; a `#`/`##` line is its own heading;
  // consecutive text lines join into one paragraph with soft breaks.
  const out = [];
  let para = [];
  const flush = () => {
    if (para.length) { out.push("<p>" + para.map(l => mdInline(mdEscape(l))).join("<br>") + "</p>"); para = []; }
  };
  for (const line of String(src).replace(/\r/g, "").split("\n")) {
    const t = line.trim();
    if (!t) { flush(); continue; }
    if (/^##\s+/.test(t)) { flush(); out.push("<h3>" + mdInline(mdEscape(t.replace(/^##\s+/, ""))) + "</h3>"); }
    else if (/^#\s+/.test(t)) { flush(); out.push("<h2>" + mdInline(mdEscape(t.replace(/^#\s+/, ""))) + "</h2>"); }
    else para.push(t);
  }
  flush();
  return out.join("");
}

// **The name a card writes, resolved to a number in the scrape.** The metric
// families and the fields poll_metrics() keeps go by different names, so this
// is the one place that maps between them - the JS mirror of ALLOWED_METRICS.
const _last = (hist, k) => {
  for (let i = hist.length - 1; i >= 0; i--) if (hist[i][k] != null) return hist[i][k];
  return null;
};
// **One family read two ways from one list**, so a stat and a breakdown of
// the same metric cannot end up counting different things. The first argument
// is the key in the scrape - `queues`, `channels`, `providers`, `bridges` -
// and every one of those lists names its rows in a field called `name`.
//
// **A missing number is skipped rather than counted as nought.** A provider
// that reports no errors and one the scrape did not reach are not the same,
// and only one of them should read as zero - so a list with nothing to sum
// answers `null`, which the cards draw as a dash.
const rowsOf = (d, list, field) =>
  (d[list] || []).filter(r => typeof r[field] === "number");
const sumOf = (list, field) => (b, h, d) => {
  const rows = rowsOf(d, list, field);
  return rows.length ? rows.reduce((a, r) => a + r[field], 0) : null;
};
const splitOf = (list, field) => d =>
  rowsOf(d, list, field).map(r => ({ label: r.name, value: r[field] }));

const METRIC_MAP = {
  saguin_connections:                    { hist: "connections",    cur: b => b.connections },
  saguin_subscriptions:                  { hist: "subscriptions",  cur: b => b.subscriptions },
  saguin_uptime_seconds:                 { cur: b => b.uptime, fmt: "duration" },
  saguin_published_total:                { hist: "published",      cur: (b, h) => _last(h, "published") },
  saguin_publishes_received_total:       { hist: "publishes_in",   cur: (b, h) => _last(h, "publishes_in") },
  saguin_deliveries_sent_total:          { hist: "deliveries_out", cur: (b, h) => _last(h, "deliveries_out") },
  saguin_bytes_received_total:           { hist: "bytes_in",       cur: (b, h) => _last(h, "bytes_in"), fmt: "bytes" },
  saguin_bytes_sent_total:               { hist: "bytes_out",      cur: (b, h) => _last(h, "bytes_out"), fmt: "bytes" },
  saguin_queue_depth:                    { hist: "queue_depth",    cur: (b, h) => _last(h, "queue_depth") },
  saguin_connections_total:              { cur: b => b.connections_total },
  saguin_broadcast_unmatched_total:      { hist: "unmatched",      cur: b => b.unmatched },
  saguin_deliveries_dropped_total:       { hist: "dropped",        cur: b => b.dropped },
  saguin_deliveries_refused_total:       { cur: b => b.deliveries_refused },
  saguin_deliveries_expired_total:       { cur: b => b.expired },
  saguin_session_expiry_shortened_total: { cur: b => b.session_expiry_shortened },
  saguin_sessions_offline:               { cur: b => b.sessions_offline },
  // What the last start did to a fleet's sessions. Neither carries a line:
  // both move once, at the start, and a line of a number that changes once
  // is a flat line with a step nobody is watching for.
  saguin_sessions_restored_total:        { cur: b => b.sessions_restored },
  // Wills. The published total splits by what made each due; waiting carries
  // a line because it rises and falls with a fleet's link quality, and
  // cancelled only climbs.
  saguin_wills_published_total:          { cur: (b, h, d) => (d.wills || []).reduce((a, r) => a + r.count, 0),
                                           breakdown: d => (d.wills || []).map(r => ({ label: r.cause, value: r.count })) },
  saguin_wills_cancelled_total:          { cur: b => b.wills_cancelled },
  saguin_wills_waiting:                  { hist: "wills_waiting", cur: b => b.wills_waiting },
  saguin_sessions_dropped_total:         { cur: (b, h, d) => (d.session_ends || []).reduce((a, r) => a + r.count, 0),
                                           breakdown: d => (d.session_ends || []).map(r => ({ label: r.cause, value: r.count })) },
  saguin_session_queue_messages:         { hist: "session_queue_messages", cur: b => b.session_queue_messages },
  saguin_session_queue_bytes:            { hist: "session_queue_bytes", cur: b => b.session_queue_bytes, fmt: "bytes" },
  saguin_session_deliveries_dropped_total: { cur: (b, h, d) => (d.session_drops || []).reduce((a, r) => a + r.count, 0),
                                             breakdown: d => (d.session_drops || []).map(r => ({ label: r.cause, value: r.count })) },
  // Shared groups: what was put on a group's list, what was handed to a
  // member, and what was let go otherwise - held minus the other two is what
  // the groups hold now. No line on any of the three - all counters that only
  // climb.
  saguin_shares_held_total:              { cur: b => b.shares_held },
  saguin_shares_drained_total:           { cur: b => b.shares_drained },
  saguin_shares_dropped_total:           { cur: (b, h, d) => (d.share_drops || []).reduce((a, r) => a + r.count, 0),
                                           breakdown: d => (d.share_drops || []).map(r => ({ label: r.cause, value: r.count })) },
  saguin_retained_messages:              { cur: b => b.retained },
  // Exactly-once. `hist` on the first two because both are worth a line: one
  // should fall back to nothing between bursts, the other only climbs. The
  // allowance is configured, so a line of it is flat by construction.
  saguin_qos2_held:                      { hist: "qos2_held",      cur: b => b.qos2_held },
  saguin_qos2_abandoned_total:           { hist: "qos2_abandoned", cur: b => b.qos2_abandoned },
  saguin_qos2_max_inflight_per_client:   { cur: b => b.qos2_max_inflight },
  saguin_publish_refused_total:          { cur: (b, h, d) => (d.refusals || []).reduce((a, r) => a + r.count, 0),
                                           breakdown: d => (d.refusals || []).map(r => ({ label: r.reason, value: r.count })) },
  saguin_subscriptions_refused_total:    { cur: (b, h, d) => (d.subscription_refusals || []).reduce((a, r) => a + r.count, 0),
                                           breakdown: d => (d.subscription_refusals || []).map(r => ({ label: r.reason, value: r.count })) },
  // The Go runtime. Counters keep a series so a card can draw a rate; the heap
  // pair and the two settings are read as they stand.
  saguin_go_gc_cycles_total:             { hist: "go_gc_cycles",     cur: (b, h) => _last(h, "go_gc_cycles") },
  saguin_go_gc_cpu_seconds_total:        { hist: "go_gc_cpu",        cur: (b, h) => _last(h, "go_gc_cpu") },
  saguin_go_gc_assist_cpu_seconds_total: { hist: "go_gc_assist_cpu", cur: (b, h) => _last(h, "go_gc_assist_cpu") },
  saguin_go_heap_live_bytes:             { cur: b => b.go_heap_live, fmt: "bytes" },
  saguin_go_heap_goal_bytes:             { cur: b => b.go_heap_goal, fmt: "bytes" },
  saguin_go_stack_bytes:                 { hist: "go_stack",        cur: b => b.go_stack, fmt: "bytes" },
  saguin_go_allocated_bytes_total:       { hist: "go_alloc_bytes",   cur: (b, h) => _last(h, "go_alloc_bytes"), fmt: "bytes" },
  saguin_go_allocated_objects_total:     { hist: "go_alloc_objects", cur: (b, h) => _last(h, "go_alloc_objects") },
  saguin_go_gogc_percent:                { cur: b => b.go_gogc },
  saguin_go_memory_limit_bytes:          { cur: b => b.go_memory_limit, fmt: "bytes_or_none" },
  saguin_connections_by_protocol:        { breakdown: d => (d.protocols || []).map(p => ({ label: "MQTT " + mqttName(p.protocol), value: p.count })) },
  saguin_connections_refused_total:      { breakdown: d => (d.connections_refused || []).map(r => ({ label: r.reason, value: r.count })) },
  // **Named for a value, split for a chart.** A meter comparing one store's
  // fill with its own bound has to name which store; a breakdown of the same
  // family is over every one of them and must not, or a shipped dashboard
  // would name a provider that exists on the broker it was written against
  // and on no other.
  saguin_provider_bytes:                 { provider: p => p.bytes, fmt: "bytes", breakdown: splitOf("providers", "bytes") },
  saguin_provider_max_bytes:             { provider: p => p.max_bytes, fmt: "bytes", breakdown: splitOf("providers", "max_bytes") },
  saguin_provider_publish_commit_max_records: { provider: p => p.commit_max, breakdown: splitOf("providers", "commit_max") },
  saguin_max_session_expiry_seconds:     { cur: b => b.max_session_expiry, fmt: "duration" },

  // **The per-entity families: summed for a stat, split for a breakdown.**
  // The broker labels these by queue, channel, provider or bridge, and both
  // readings answer a different question - how much work is there, and which
  // one is holding it. `sumOf` and `splitOf` build both from one list, so the
  // two can never disagree about what they are counting.
  //
  // **Written out one line each rather than generated**, because the suite
  // reads this table by its keys to hold the Python allow-list to it: a name
  // that only exists inside a helper's arguments is a name that check cannot
  // see, and an unchecked half of a two-language fact is how the two drifted
  // the first time.
  saguin_queue_inflight:           { hist: "queue_inflight", cur: sumOf("queues", "inflight"),        breakdown: splitOf("queues", "inflight") },
  saguin_queue_delivered_total:    { cur: sumOf("queues", "delivered"),     breakdown: splitOf("queues", "delivered") },
  saguin_queue_acknowledged_total: { cur: sumOf("queues", "acknowledged"),  breakdown: splitOf("queues", "acknowledged") },
  saguin_queue_redelivered_total:  { cur: sumOf("queues", "redelivered"),   breakdown: splitOf("queues", "redelivered") },
  saguin_queue_returned_total:     { cur: sumOf("queues", "returned"),      breakdown: splitOf("queues", "returned") },
  saguin_queue_expired_total:      { cur: sumOf("queues", "expired"),       breakdown: splitOf("queues", "expired") },
  saguin_queue_dead_lettered_total: { cur: sumOf("queues", "dead_lettered"), breakdown: splitOf("queues", "dead_lettered") },
  saguin_queue_retain_ignored_total: { cur: sumOf("queues", "retain_ignored"), breakdown: splitOf("queues", "retain_ignored") },

  saguin_channel_records:          { cur: sumOf("channels", "records"),     breakdown: splitOf("channels", "records") },
  saguin_channel_bytes:            { cur: sumOf("channels", "bytes"), fmt: "bytes", breakdown: splitOf("channels", "bytes") },
  saguin_channel_consumers:        { cur: sumOf("channels", "consumers"),   breakdown: splitOf("channels", "consumers") },
  saguin_channel_partitioned_consumers: { cur: sumOf("channels", "partitioned"), breakdown: splitOf("channels", "partitioned") },
  saguin_channel_position_lost_total: { cur: sumOf("channels", "position_lost"), breakdown: splitOf("channels", "position_lost") },
  saguin_channel_retention_removed_total: { cur: sumOf("channels", "retention_removed"), breakdown: splitOf("channels", "retention_removed") },
  saguin_latest_superseded_total:  { cur: sumOf("channels", "superseded"),  breakdown: splitOf("channels", "superseded") },
  // **Offsets are never summed.** One channel's next offset added to
  // another's is a number about no channel at all, so these three have a
  // split and no value - and the allow-list says `scalar: false`, so a stat
  // naming one is refused rather than drawn confidently wrong.
  saguin_channel_next_offset:      { breakdown: splitOf("channels", "next") },
  saguin_channel_floor_offset:     { breakdown: splitOf("channels", "floor") },
  saguin_channel_consumer_position_min: { breakdown: splitOf("channels", "position_min") },

  saguin_storage_commits_total:    { cur: sumOf("providers", "commits"),    breakdown: splitOf("providers", "commits") },
  saguin_storage_committed_records_total: { cur: sumOf("providers", "records"), breakdown: splitOf("providers", "records") },
  saguin_storage_errors_total:     { cur: sumOf("providers", "errors"),     breakdown: splitOf("providers", "errors") },

  saguin_bridge_received_total:    { cur: sumOf("bridges", "received"),     breakdown: splitOf("bridges", "received") },
  saguin_bridge_sent_total:        { cur: sumOf("bridges", "sent"),         breakdown: splitOf("bridges", "sent") },
  saguin_bridge_loops_skipped_total: { cur: sumOf("bridges", "skipped"),    breakdown: splitOf("bridges", "skipped") },
  saguin_bridge_reconnects_total:  { cur: sumOf("bridges", "reconnects"),   breakdown: splitOf("bridges", "reconnects") },
  saguin_bridge_unsent_total:      { cur: sumOf("bridges", "unsent"),       breakdown: splitOf("bridges", "unsent") },
  saguin_bridge_unstored_total:    { cur: sumOf("bridges", "unstored"),     breakdown: splitOf("bridges", "unstored") },
  saguin_bridge_connected:         { cur: sumOf("bridges", "connected"),    breakdown: splitOf("bridges", "connected") },
  saguin_bridge_stopped:           { cur: sumOf("bridges", "stopped"),      breakdown: splitOf("bridges", "stopped") },
};

// Sensible width (of 12) and height when a card names none - so a dashboard
// reads well written as `{type, metric}` and only says a size to change one.
const DEF_W = { stat: 2, timeseries: 6, breakdown: 12, meter: 4, text: 12,
                heading: 12, channels: 12, storage: 6, bridges: 6, queues: 6,
                alerts: 12, break: 12 };
const DEF_H = { stat: "small", timeseries: "medium", breakdown: "medium",
                meter: "small", text: "auto", heading: "auto", channels: "large",
                storage: "medium", bridges: "medium", queues: "medium",
                alerts: "auto", break: "auto" };

function parseRef(ref) {
  // A digit is part of a metric name - `saguin_qos2_held`. The server's
  // METRIC_REF carries the same pattern and the two must agree: a card this
  // rejects is one the page draws nothing for, with no error anywhere.
  const m = String(ref).match(/^([a-z_][a-z0-9_]*)(?:\{[a-z_][a-z0-9_]*="([^"]*)"\})?$/);
  return m ? { family: m[1], sel: m[2] } : { family: String(ref), sel: undefined };
}
// statValue is a stat card's number: its metric, less every metric `minus:`
// names. One operand unknown makes the difference unknown - a dash - rather
// than a wrong number.
function statValue(cfg, valueOf) {
  let v = valueOf(cfg.metric);
  for (const ref of cfg.minus || []) {
    const m = valueOf(ref);
    v = v == null || m == null ? null : v - m;
  }
  return v;
}
function metricValue(ref, b, hist, d) {
  const { family, sel } = parseRef(ref);
  const m = METRIC_MAP[family];
  if (!m) return null;
  if (m.provider) {
    const p = (d.providers || []).find(x => x.name === sel);
    return p ? m.provider(p) : null;
  }
  if (m.cur) return m.cur(b, hist, d);
  if (m.hist) return _last(hist, m.hist);
  return null;
}
function metricFmt(ref) {
  const { family } = parseRef(ref);
  return (METRIC_MAP[family] || {}).fmt || "number";
}

function formatVal(v, fmt) {
  if (v == null) return "-";
  if (fmt === "duration") return duration(v);
  if (fmt === "bytes") return bytes(v);
  if (fmt === "bytes_rate") return bytes(Math.round(v)) + "/s";
  if (fmt === "msgs_rate") return fmt0(v) + "/s";
  if (fmt === "percent") return (v * 100).toFixed(0) + "%";
  // GOMEMLIMIT is math.MaxInt64 when none is set, which a float64 reads as
  // 9.223372036854776e18: no limit, not nine exabytes.
  if (fmt === "bytes_or_none") return v >= 9.2e18 ? "none" : bytes(v);
  return fmt0(v);
}
const fmt0 = v => (typeof v === "number" ? fmt(v) : v);

// ===========================================================================
// **"Notify me", as three pure functions rather than a decision spread through
// a component.** It is the most-corrected thing in this page - five defects
// across three rounds, every one of them the same shape: a control that says
// something other than what the browser will do. They are out here, taking
// their world as arguments and returning what to show, because that is the
// difference between a test that reads the source for a spelling and one that
// drives the decision. A guard asserting the *text* of yesterday's defect passes over the same decision written differently, and
// one did - the early return moved from `Notification.permission` to a cached
// copy of it and the whole suite stayed green.
//
// The states, and why each is answered the way it is:
//
//   no API      - nothing to turn on, and nothing to ask.
//   insecure    - settled without asking, and the only thing that is.
//                 Notifications are gated on a secure context by
//                 specification, so no prompt can change the answer, and
//                 asking is not neutral: Chromium was observed resolving
//                 requestPermission() as "granted" over plain HTTP to a LAN
//                 address while delivering nothing.
//   granted     - arm, no question needed.
//   anything    - ask. A permission is the operator's to change, so it is
//   else          never pre-judged; the browser is asked every time and its
//                 answer is what gets reported.
function notifyWhyRefused({ api, secure }) {
  if (!api) return "This browser has no notifications at all, so there is nothing to turn on.";
  if (!secure)
    return "This page is served over plain HTTP, and browsers refuse notifications "
      + "from a page that is not on HTTPS (localhost excepted) - so this is the "
      + "address, not a setting you have wrong. Reach the viewer over HTTPS, or "
      + "at http://localhost:8080 on the machine it runs on, and click again.";
  return "Your browser is blocking notifications for this page. Allow them in its "
    + "site settings, then click again.";
}

// What a click should do, and what to say while it happens. `act` is one of
// "explain" (say why, stay off), "arm" (turn on), "ask" (put the question to
// the browser, then call notifyAnswer with what it says).
//
// **The note for "ask" is not empty, and that is a fix rather than a detail.**
// A Chrome profile that quiets permission prompts leaves requestPermission()
// pending - not resolved, pending, for as long as you care to wait - so the
// handler that writes every other sentence here never runs and the click
// produces nothing at all. That is the original complaint, restored through
// the one gap in the fix for it. Saying "asking…" up front costs one line and
// covers every browser that declines to answer.
function notifyPlan({ api, secure, perm }) {
  if (!api || !secure) return { act: "explain", note: notifyWhyRefused({ api, secure }) };
  if (perm === "granted") return { act: "arm", note: null };
  return { act: "ask", note: "Asking the browser… if no prompt appears, look for "
                             + "a blocked-notifications icon at the right of the "
                             + "address bar, or check this site's notification "
                             + "permission in the browser's settings." };
}

function notifyAnswer(perm, { api, secure }) {
  // **"granted" from somewhere that cannot deliver is still a refusal.**
  // notifyPlan does not ask on an insecure origin, so this is a second lock on
  // the same door - and it earned itself the first time it ran: Chromium
  // answers "granted" over plain HTTP, and one caller reaching here without
  // the plan's gate is all it would take to light the control up over nothing.
  if (!api || !secure) return { arm: false, note: notifyWhyRefused({ api, secure }) };
  if (perm === "granted") return { arm: true, note: null };
  if (perm === "default")
    return { arm: false, note: "The prompt was dismissed, so nothing can be delivered "
                               + "yet. Click again to ask once more." };
  return { arm: false, note: notifyWhyRefused({ api, secure }) };
}

// Why a delivery the browser accepted and then would not show has failed.
//
// **Not the same sentence as a refused permission, and it was.** Turning the
// control off on a failed delivery is right - an operator is not being notified
// and must not be told they are - but sending them to site settings when the
// permission is granted describes a fault that is not there. Reading the
// permission *here* is not the two-sources problem that made this control arm
// and disarm itself: the refusal has already happened and is not in doubt; this
// only chooses how to explain it.
function notifyDeliveryFailed({ api, secure, perm }) {
  if (perm === "granted")
    return "The browser has permission and still would not show a notification, so "
      + "nothing is being delivered and notifying is off. A browser or desktop "
      + "setting that suppresses notifications is the usual cause.";
  return notifyWhyRefused({ api, secure });
}

// Raise one notification, and call `onRefused` if the browser will not show it.
//
// **A refused construction does not throw in Chrome - it returns an object and
// fires an `error` event on it.** The previous version listened for an
// exception, so the off-switch was unreachable code: revoke the permission
// mid-session and the page went on showing "🔔 notifying" over a shut channel
// until a reload. Driven on the real page before this was written: `new
// Notification(...)` with permission not granted returned normally and fired
// `error`. Both paths are handled - the event is how Chrome refuses, the throw
// is how a browser that guards the constructor does - and the returned object
// is the caller's signal either way.
function raiseNotification(title, body, onRefused) {
  let n = null;
  try {
    n = new Notification(title, { body });
  } catch (err) {
    onRefused();
    return null;
  }
  if (n) n.onerror = onRefused;
  return n;
}

function evalAlarm(expr, v) {
  if (v == null) return false;
  const m = String(expr).match(/^(>=|<=|>|<|==|!=)\s*(-?\d+(?:\.\d+)?)$/);
  if (!m) return false;
  const n = Number(m[2]);
  switch (m[1]) {
    case ">": return v > n; case "<": return v < n;
    case ">=": return v >= n; case "<=": return v <= n;
    case "==": return v === n; case "!=": return v !== n;
  }
  return false;
}

// **One CSV writer for every table that offers one**, so each of them stays
// "export what is on screen" rather than growing its own idea of what a
// download looks like. `kind` names the table in the file - the same word
// the sidebar uses - so a reader with several of these in a downloads folder
// can tell them apart without opening one.
function csvField(v) {
  const s = String(v);
  // **A cell a spreadsheet would run is defused before it is written.** A
  // field starting with `=`, `+`, `-`, `@`, a tab or a carriage return is
  // read as a formula by every spreadsheet that opens a CSV, and quoting
  // does not help: the `=` is still the first character once the field is
  // unquoted.
  //
  // It matters here because of whose strings fill these cells. The
  // sessions export writes `client_id` and `user`, and the consumer export
  // writes a client id again - and a client id is any string a device
  // chose for itself at CONNECT. So a device that can reach the broker can
  // name itself as a formula, sit in the sessions table, and run on the
  // operator's machine the moment somebody opens the export. The viewer is
  // exactly the tool whose CSV ends up in a spreadsheet.
  //
  // An apostrophe is the standard defusing: spreadsheets read it as "the
  // rest is text" and do not show it. A plain number is left alone, so a
  // negative one stays a number rather than becoming text - `-1` is a
  // value, `-1+1` is a formula.
  const guarded = /^[=+\-@\t\r]/.test(s) && !/^[+-]?\d+(?:\.\d+)?$/.test(s)
    ? "'" + s
    : s;
  return /[",\n]/.test(guarded) ? '"' + guarded.replace(/"/g, '""') + '"' : guarded;
}
function downloadCsv(kind, header, rows) {
  const csv = [header, ...rows].map(r => r.map(csvField).join(",")).join("\r\n") + "\r\n";
  const p = n => String(n).padStart(2, "0");
  const now = new Date();
  const stamp = `${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}`
              + `${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}`;
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `saguin-viewer-${kind}-${stamp}.csv`;
  // Firefox only honours `download` on a link that is in the document.
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
function ExportCsvButton({ onClick, title }) {
  return html`<button class="chip" onClick=${onClick}
    title=${title || "Save what this table is showing as a CSV file"}>
    ⬇ export csv</button>`;
}

// **The Alarms tab reads what the backend recorded, not what this browser
// saw.** The poll thread evaluates every dashboard alarm each scrape and stores
// each firing episode, so one that fired while no page was open is here too.
// Firing now on top, then the ones that cleared within the five-day window.
function AlarmsView({ log, notify, notifyNote, toggleNotify }) {
  // **Remembered like the other list filters**, and off by default: a page
  // that hides history on first sight is one somebody reads as shorter than
  // it is.
  const [hideCleared, setHideCleared] = useState(
    () => cookie("saguin_viewer_alarms_hide_cleared") === "1");
  useEffect(() => { cookie("saguin_viewer_alarms_hide_cleared", hideCleared ? "1" : "0"); },
            [hideCleared]);
  // **Called before either early return below**, like every other hook in
  // this file: a hook after a `return` runs on some renders and not others,
  // and the page answers that by rendering nothing at all.
  const headRef = usePinnedHead();
  if (!log) return html`<div class="empty">Loading…</div>`;
  const { firing = [], recent = [], defined = 0 } = log;
  const shownRecent = hideCleared ? [] : recent;
  if (!defined)
    return html`<div class="empty">No dashboard declares an <code>alarm</code>,
      so there is nothing to record. Add an <code>alarm:</code> threshold to a
      stat card and a firing one will appear here.</div>`;
  const when = t => new Date(t * 1000).toLocaleString();
  const forr = e => duration((e.ended || Date.now() / 1000) - e.started);
  // **What downloads is what is on screen.** Firing first, then whatever
  // "hide cleared" is currently letting through - a reader who just hid the
  // history and then exports gets the firing-only file that name implies,
  // not the five days they told the page to stop showing them.
  const exportCsv = () => {
    const lineOf = (e, live) => [live ? "firing" : "cleared", e.title, e.metric, e.dash,
      when(e.started), live ? "" : when(e.ended), forr(e)];
    downloadCsv("alarms", ["State", "Alarm", "Metric", "Dashboard", "Started", "Cleared", "For"],
      [...firing.map(e => lineOf(e, true)), ...shownRecent.map(e => lineOf(e, false))]);
  };
  const rowOf = (e, live) => html`
    <tr class=${live ? "alarm-firing" : ""} key=${e.id}>
      <td>${live
        ? html`<span class="alarm-dot"></span> firing`
        : html`<span class="dim">cleared</span>`}</td>
      <td><b>${e.title}</b></td>
      <td class="mono dim">${e.metric}</td>
      <td>${e.dash}</td>
      <td class="mono">${when(e.started)}</td>
      <td class="mono">${live ? "-" : when(e.ended)}</td>
      <td class="mono">${forr(e)}</td>
    </tr>`;
  return html`
    <div>
    <div class="panelhead panelhead-table alarmsview" ref=${headRef}>
      <div class="alarmsview-head">
        ${firing.length
          ? html`<span class="hasalarm-text">⚠ ${firing.length} firing now</span>`
          : html`<span class="dim">Nothing firing.</span>`}
        <span class="dim"> · ${defined} alarm${defined === 1 ? "" : "s"} watched · last five days</span>
        <label class="sub" style=${{ display: "flex", alignItems: "center", gap: "6px",
                                     whiteSpace: "nowrap", cursor: "pointer" }}
               title="Hide alarms that are no longer firing. The record stays on the broker; this only changes what this page shows.">
          <input type="checkbox" checked=${hideCleared}
                 onChange=${e => setHideCleared(e.target.checked)} />
          hide cleared${recent.length ? ` (${recent.length})` : ""}
        </label>
        <button class="chip alarmnotify" onClick=${toggleNotify}
          title="Pop a browser notification when an alarm starts firing, while this page is open">
          ${notify ? "🔔 notifying" : "🔕 notify me"}</button>
        <${ExportCsvButton} onClick=${exportCsv} />
      </div>
      ${notifyNote && html`<div class="notifynote">${notifyNote}</div>`}
    </div>
      <table class="ops alarmtable undertheh">
        <thead><tr><th>State</th><th>Alarm</th><th>Metric</th><th>Dashboard</th>
          <th>Started</th><th>Cleared</th><th>For</th></tr></thead>
        <tbody>
          ${firing.length === 0 && shownRecent.length === 0
            ? html`<tr><td colspan="7" class="empty">${
                hideCleared && recent.length
                  ? "Nothing firing. Untick “hide cleared” to see what fired in the last five days."
                  : "Nothing has fired in the last five days."}</td></tr>`
            : ""}
          ${firing.map(e => rowOf(e, true))}
          ${shownRecent.map(e => rowOf(e, false))}
        </tbody>
      </table>
    </div>`;
}

// **The composite panels, drawn from the scrape.** These are the built-in
// dashboard's own panels as standalone widgets a card can place. They read
// the same /api/metrics `d` and stand on their own so the config path does
// not reach into Dashboard()'s render.
function saysOf(d, ...names) {
  return names.map(x => (d.help || {})[x]).filter(Boolean).join("\n\n");
}
function WAlerts({ d }) {
  const behind = (d.channels || []).filter(c => c.below_floor);
  if (behind.length === 0) return null;
  return html`<div class="card" style=${{ borderColor: "var(--critical)" }}>
    <h3 style=${{ color: "var(--critical)" }}>Retention has passed a consumer's position</h3>
    <p class="why">On ${behind.map(c => c.name).join(", ")}, the retention floor is above
      the lowest stored position - somebody's missing data, and the one alert the
      whole catalogue exists for.</p></div>`;
}
function WChannels({ d }) {
  return html`<div class="card"><h3>Channels<${Info}
      text=${saysOf(d, "saguin_channel_info", "saguin_channel_records", "saguin_channel_consumer_position_min")} /></h3>
    <table class="ops"><thead><tr><th>channel</th><th>type</th><th>provider</th><th>holds</th>
      <th>bytes</th><th>consumers</th><th>worst lag</th><th>removed</th><th>reads refused</th></tr></thead>
      <tbody>${(d.channels || []).map(c => html`<tr key=${c.name} class=${c.below_floor ? "behind" : ""}>
        <td class="mono">${c.name}</td>
        <td><span class=${"tag " + c.type}>${c.type}</span></td>
        <td class="mono">${c.provider || "-"} ${c.provider_type && html`<span class="tag muted">${c.provider_type}</span>`}</td>
        <td>${c.records == null ? html`<span class="sub">-</span>` : fmt(c.records)}</td>
        <td>${c.bytes == null ? html`<span class="sub">-</span>` : bytes(c.bytes)}</td>
        <td>${fmt(c.consumers)}</td>
        <td>${c.lag == null ? html`<span class="sub">-</span>` : fmt(c.lag)}</td>
        <td>${fmt(c.retention_removed)}</td>
        <td>${c.position_lost > 0 ? html`<span style=${{ color: "var(--critical)" }}>${fmt(c.position_lost)}</span>` : fmt(c.position_lost)}</td>
      </tr>`)}</tbody></table></div>`;
}
function WStorage({ d }) {
  return html`<div class="card"><h3>Storage<${Info}
      text=${saysOf(d, "saguin_provider_info", "saguin_storage_commits_total", "saguin_storage_errors_total")} /></h3>
    ${(d.providers || []).length === 0 ? html`<div class="empty">None configured.</div>`
      : d.providers.map(p => html`<div key=${p.name} style=${{ marginBottom: "14px" }}>
      <${Meter} value=${p.bytes} max=${p.max_bytes} label=${`${p.name} · ${p.type}`} />
      ${p.commits > 0 && html`<p class="why" style=${{ marginTop: "6px", marginBottom: 0 }}>
        ${fmt(p.records / p.commits)} records per commit, against a ceiling of ${fmt(p.commit_max)}.</p>`}
      ${p.errors > 0 && html`<p class="why" style=${{ color: "var(--critical)" }}>${fmt(p.errors)} storage calls have failed.</p>`}
    </div>`)}</div>`;
}
function WBridges({ d }) {
  return html`<div class="card"><h3>Bridges<${Info}
      text=${saysOf(d, "saguin_bridge_info", "saguin_bridge_connected", "saguin_bridge_received_total")} /></h3>
    ${(d.bridges || []).length === 0 ? html`<div class="empty">None configured.</div>`
      : html`<table class="ops"><thead><tr><th>bridge</th><th>peer</th><th>state</th>
          <th>in</th><th>out</th><th>skipped</th><th>reconnects</th></tr></thead>
        <tbody>${d.bridges.map(br => html`<tr key=${br.name}><td class="mono">${br.name}</td>
          <td class="mono sub">${br.peer}</td>
          <td>${br.stopped ? html`<span style=${{ color: "var(--critical)" }}>halted</span>`
            : br.connected ? html`<span style=${{ color: "var(--good)" }}>connected</span>`
            : html`<span style=${{ color: "var(--warning)" }}>link down</span>`}</td>
          <td>${fmt(br.received)}</td><td>${fmt(br.sent)}</td>
          <td>${fmt(br.skipped)}</td><td>${fmt(br.reconnects)}</td></tr>`)}</tbody></table>`}</div>`;
}
function WQueues({ d }) {
  if ((d.queues || []).length === 0) return null;
  return html`${d.queues.map(q => html`<div class="card" key=${q.name}>
    <h3>Queue <span class="mono">${q.name}</span></h3>
    <div class="tiles" style=${{ gridTemplateColumns: "1fr 1fr" }}>
      <${Tile} k="Unresolved" v=${fmt(q.depth)} />
      <${Tile} k="Out with a worker" v=${fmt(q.inflight)} /></div>
    <${Stacked} segments=${[
      { label: "acknowledged", value: q.acknowledged, color: "var(--series-3)" },
      { label: "returned", value: q.returned, color: "var(--series-4)" },
      { label: "redelivered", value: q.redelivered, color: "var(--series-2)" },
      { label: "dead-lettered", value: q.dead_lettered, color: "var(--series-1)" }]} />
  </div>`)}`;
}
const WIDGETS = { channels: WChannels, storage: WStorage, bridges: WBridges,
                  queues: WQueues, alerts: WAlerts };

// **One card of a spec dashboard.** Resolves the metric from the scrape,
// draws the primitive its type names, and self-hides when there is nothing to
// show - the same rule the built-in panels follow.
function SpecCard({ cfg, d, hist, series, rate }) {
  const b = d.broker || {};
  const title = cfg.title
    ? html`<h3>${cfg.title}</h3>` : null;

  // Widgets self-hide when empty so no blank cell is left on the grid.
  if (cfg.type === "alerts")
    return (d.channels || []).some(c => c.below_floor) ? html`<${WAlerts} d=${d} />` : null;
  if (cfg.type === "queues")
    return (d.queues || []).length ? html`<${WQueues} d=${d} />` : null;
  if (WIDGETS[cfg.type]) return html`<${WIDGETS[cfg.type]} d=${d} />`;
  if (cfg.type === "break") return html`<div class="dashbreak" aria-hidden="true"></div>`;
  if (cfg.type === "heading") return html`<div class="specheading"><h2>${cfg.text}</h2></div>`;
  if (cfg.type === "text")
    return html`<div class="specprose">${cfg.title && html`<h3>${cfg.title}</h3>`}
      <div dangerouslySetInnerHTML=${{ __html: mdToHtml(cfg.markdown) }} /></div>`;

  if (cfg.type === "stat") {
    const v = statValue(cfg, ref => metricValue(ref, b, hist, d));
    const hk = (METRIC_MAP[parseRef(cfg.metric).family] || {}).hist;
    const spark = cfg.sparkline && hk ? series(hk) : null;
    // **A card with nothing to show says so rather than disappearing.**
    // Several families carry no series until something happens -
    // `saguin_storage_errors_total` has none until a provider has actually
    // failed, which is deliberate: a rate over a series that does not exist
    // is nothing, and nothing has gone wrong. A card that vanished there left
    // the reader unable to tell "nothing has failed" from "I have mistyped
    // the metric", and that is a question this page should never raise. So
    // the tile stays, with a dash and a line saying why.
    if (v == null)
      return html`<${Tile} k=${cfg.title || cfg.metric} v="-"
        u=${cfg.note || "not reported yet - this broker publishes no series for it"}
        help=${saysOf(d, parseRef(cfg.metric).family)} />`;
    return html`<${Tile} k=${cfg.title || cfg.metric} u=${cfg.note}
      v=${formatVal(v, cfg.format || metricFmt(cfg.metric))}
      alarm=${cfg.alarm ? evalAlarm(cfg.alarm, v) : false}
      series=${spark} help=${saysOf(d, parseRef(cfg.metric).family)} />`;
  }
  if (cfg.type === "timeseries") {
    const hk = METRIC_MAP[parseRef(cfg.metric).family].hist;
    const pts = cfg.mode === "rate" ? rate(hk) : series(hk);
    const fmtv = v => formatVal(v, cfg.format || (cfg.mode === "rate" ? "number" : metricFmt(cfg.metric)));
    const nowv = pts.length ? fmtv(pts[pts.length - 1].v) : "-";
    return html`<div class="card">${title}<${TimeLine} points=${pts}
      color=${cfg.color ? "var(--" + cfg.color + ")" : undefined}
      format=${fmtv} label=${cfg.note || ("now " + nowv)} /></div>`;
  }
  if (cfg.type === "breakdown") {
    const rows = (METRIC_MAP[parseRef(cfg.metric).family].breakdown || (() => []))(d);
    const warn = cfg.severity === "warning" || cfg.severity === "critical";
    // The same rule as the stat above: a split with nothing in it keeps its
    // card and says the broker has published no series, rather than leaving a
    // gap the reader has to account for.
    if (rows.length === 0)
      return html`<div class="card">${title || html`<h3>${cfg.metric}</h3>`}
        <div class="empty">Nothing reported - this broker publishes no series
          for it yet.</div></div>`;
    const body = cfg.style === "stacked"
      ? html`<${Stacked} segments=${rows.map((r, i) => ({ ...r, color: `var(--series-${(i % 4) + 1})` }))} />`
      : html`<${Bars} rows=${rows.map(r => warn ? { ...r, color: "var(--warning)" } : r)} />`;
    return html`<div class="card" style=${warn ? { borderColor: "var(--warning)" } : {}}>
      ${title || html`<h3>${cfg.metric}</h3>`}${body}</div>`;
  }
  if (cfg.type === "meter") {
    const v = metricValue(cfg.value, b, hist, d), mx = metricValue(cfg.max, b, hist, d);
    if (v == null || mx == null) return null;
    return html`<div class="card">${title}<${Meter} value=${v} max=${mx} label=${cfg.label || ""} /></div>`;
  }
  return null;
}

// **One spec dashboard's cards on the responsive grid.** Pure: the scrape,
// window and refresh belong to the Dashboards container and are shared across
// every sub-tab, because they all read the one ring of history.
function DashGrid({ spec, d, hist, series, rate }) {
  const g = spec.grid;
  return html`<div class="dashgrid" style=${{ "--cols": g.columns, gap: `${g.gap}px` }}>
    ${spec.cards.map((cfg, i) => {
      // Called as a function so a self-hidden card (null) leaves no cell.
      const el = SpecCard({ cfg, d, hist, series, rate });
      if (!el) return null;
      const w = cfg.width || DEF_W[cfg.type] || 6;
      const h = cfg.height || DEF_H[cfg.type] || "medium";
      return html`<div key=${i} class=${"cell h-" + h} style=${{ "--w": w }}>${el}</div>`;
    })}
  </div>`;
}

// **The Dashboard tab.** A fixed row of the configured dashboards under the top
// tabs, one shared set of controls - window and refresh - that governs all of
// them because they read a single scrape, and the chosen dashboard's cards
// below. A `builtin` dashboard is drawn by the viewer's own code.
function Dashboards({ dashboards }) {
  const [sel, setSelRaw] = useState(() => {
    const c = cookie("saguin_viewer_dash");
    return dashboards.some(x => x.name === c) ? c : dashboards[0].name;
  });
  const setSel = n => { cookie("saguin_viewer_dash", n); setSelRaw(n); };
  const [d, setD] = useState(null);
  const [win, setWin] = useState(() => Number(cookie("saguin_viewer_window")) || 120);
  const [every, setEvery] = useState(() => {
    const c = cookie("saguin_viewer_refresh"); return c == null ? -1 : Number(c);
  });
  const [again, setAgain] = useState(0);
  useEffect(() => { cookie("saguin_viewer_window", win); }, [win]);
  useEffect(() => { cookie("saguin_viewer_refresh", every); }, [every]);
  useEffect(() => {
    let live = true, timer = null;
    api("/api/metrics").then(r => {
      if (!live || !r.body) return;
      setD(r.body);
      if (every === 0) return;
      const wait = every > 0 ? every
        : Math.max(5, (r.body.taken_at + r.body.interval + 2) - Date.now() / 1000);
      timer = setTimeout(() => live && setAgain(n => n + 1), wait * 1000);
    });
    return () => { live = false; if (timer) clearTimeout(timer); };
  }, [every, again]);

  const spec = dashboards.find(x => x.name === sel) || dashboards[0];
  const age = d ? Math.round(Date.now() / 1000 - d.taken_at) : null;

  const body = () => {
    if (spec.builtin) return html`<${Dashboard} />`;
    if (!d) return html`<div class="empty">Reading the catalogue…</div>`;
    if (d.error) return html`<div class="empty">The catalogue could not be read: ${d.error}</div>`;
    if (!d.scraped) return html`<div class="empty">Waiting for the first scrape.</div>`;
    const hist = (d.history || []).slice(-win);
    const series = k => hist.map(x => ({ t: x.t, v: x[k] })).filter(x => x.v != null);
    const rate = k => {
      const out = [];
      for (let i = 1; i < hist.length; i++) {
        const a = hist[i - 1], b = hist[i];
        if (a[k] == null || b[k] == null) continue;
        const dt = b.t - a.t;
        if (dt > 0 && b[k] >= a[k]) out.push({ t: b.t, v: (b[k] - a[k]) / dt });
      }
      return out;
    };
    return html`<${DashGrid} spec=${spec} d=${d} hist=${hist} series=${series} rate=${rate} />`;
  };

  // **No alarm strip here, and no firing count on the tab.** A card that is
  // firing is already red where it is drawn, and everything else about alarms
  // - what is firing, what fired while nobody looked, and the control that
  // asks to be told - is on the Alarms tab. Two places saying it meant two
  // evaluations of the same thresholds, and one of them was wrong: the page's
  // read honoured a `{provider="…"}` selector and the recorder's did not, so
  // the strip counted an alarm the record never held.
  return html`<div class="dashwrap">
    <nav class="subtabs">
      ${dashboards.map(x => html`<button key=${x.name}
        class=${x.name === sel ? "on" : ""} onClick=${() => setSel(x.name)}>${x.name}</button>`)}
    </nav>
    ${!spec.builtin && html`<div class="stale">
      ${age != null && html`<span class="chip">sample taken <b>${age}s</b> ago</span>`}
      ${d && html`<span class="chip" title="The viewer scrapes on this interval. It cannot go lower: below it the broker answers from the previous catalogue.">scraped every <b>${d.interval}s</b></span>`}
      <label class="chip">show
        <select value=${win} onChange=${e => setWin(Number(e.target.value))}
          style=${{ border: "none", background: "transparent", font: "inherit", color: "var(--text)" }}>
          ${WINDOWS.map(([t, v]) => html`<option key=${t} value=${v}>${t}</option>`)}
        </select></label>
      <button class="chip" style=${{ cursor: "pointer" }} onClick=${() => setAgain(n => n + 1)}>refresh now</button>
      <label class="chip">refresh
        <select value=${every} onChange=${e => setEvery(Number(e.target.value))}
          style=${{ border: "none", background: "transparent", font: "inherit", color: "var(--text)" }}>
          ${REFRESH.map(([t, v]) => html`<option key=${t} value=${v}>${t}</option>`)}
        </select></label>
    </div>`}
    <div class="dashbody">${body()}</div>
  </div>`;
}

function Dashboard() {
  const [d, setD] = useState(null);
  const [win, setWin] = useState(() => Number(cookie("saguin_viewer_window")) || 120);
  const [every, setEvery] = useState(() => {
    const c = cookie("saguin_viewer_refresh");
    return c == null ? -1 : Number(c);
  });
  const [, tickNow] = useState(0);
  const [again, setAgain] = useState(0);

  useEffect(() => { cookie("saguin_viewer_window", win); }, [win]);
  useEffect(() => { cookie("saguin_viewer_refresh", every); }, [every]);
  useEffect(() => {
    const id = setInterval(() => tickNow(n => n + 1), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let live = true, timer = null;
    api("/api/metrics").then(r => {
      if (!live) return;
      if (!r.body) return;
      setD(r.body);
      if (every === 0) return;
      const wait = every > 0 ? every
        : Math.max(5, (r.body.taken_at + r.body.interval + 2) - Date.now() / 1000);
      timer = setTimeout(() => live && setAgain(n => n + 1), wait * 1000);
    });
    return () => { live = false; if (timer) clearTimeout(timer); };
  }, [every, again]);

  if (!d) return html`<div class="empty">Reading the catalogue…</div>`;
  if (d.error) return html`<div class="empty">The catalogue could not be read: ${d.error}</div>`;
  if (!d.scraped) return html`<div class="empty">Waiting for the first scrape.</div>`;

  const b = d.broker;
  // The window is applied here rather than server-side: one ring serves
  // whoever is looking, and a per-browser setting that resized a shared
  // buffer would let one reader shorten another's history.
  const hist = (d.history || []).slice(-win);
  const series = k => hist.map(x => ({ t: x.t, v: x[k] })).filter(x => x.v != null);
  // **A counter drawn as itself is a line that only ever climbs**, which
  // says nothing about how busy the broker is now. A rate is the difference
  // between two readings over the seconds between them, so it needs one
  // more sample than a level does.
  //
  // **A fall means the broker restarted**, because these counters begin
  // again at zero there. That pair is dropped rather than drawn as a
  // negative throughput, which is not a thing that happens.
  const rate = k => {
    const out = [];
    for (let i = 1; i < hist.length; i++) {
      const a = hist[i - 1], b = hist[i];
      if (a[k] == null || b[k] == null) continue;
      const dt = b.t - a.t;
      if (dt > 0 && b[k] >= a[k]) out.push({ t: b.t, v: (b[k] - a[k]) / dt });
    }
    return out;
  };
  // Rounded before it is formatted: a rate is a division, and bytes()
  // prints anything under a kibibyte as the number it was given - which for
  // a division is fifteen digits of it.
  // **Both formatters already scale**, which is why a rate needs nothing
  // beyond them: bytes() moves through B, KiB, MiB and GiB, and fmt()
  // through k, M and G, so the same card reads 626 B/s on a quiet broker
  // and 41.8 MiB/s on a busy one without being told which to expect.
  const bytesPerSecond = v => bytes(Math.round(v)) + "/s";
  const msgsPerSecond = v => fmt(v) + "/s";
  const now = (pts, how) => pts.length === 0 ? "-" : how(pts[pts.length - 1].v);
  // The series itself, times and all - a sparkline that was handed only
  // values could draw the shape and never say when.
  const age = Math.round(Date.now() / 1000 - d.taken_at);
  const behind = d.channels.filter(c => c.below_floor);
  // The catalogue's own description of a series, or of the first of several
  // a card draws. Absent for a metric this broker does not publish, in which
  // case the marker is simply not drawn.
  const says = (...names) => names.map(x => (d.help || {})[x])
                                  .filter(Boolean).join("\n\n");
  const totalRefused = d.refusals.reduce((a, r) => a + r.count, 0);

  return html`
    <div>
      <div class="stale">
        <span class="chip">sample taken <b>${age}s</b> ago</span>
        <span class="chip" title="The viewer scrapes on this interval. It cannot go lower: below it the broker answers from the previous catalogue.">
          scraped every <b>${d.interval}s</b></span>
        <label class="chip">show
          <select value=${win} onChange=${e => setWin(Number(e.target.value))}
                  style=${{ padding: "0 2px", border: "none", background: "transparent",
                            font: "inherit", color: "var(--text)" }}>
            ${WINDOWS.map(([t, v]) => html`<option key=${t} value=${v}>${t}</option>`)}
          </select></label>
        <button class="chip" style=${{ cursor: "pointer" }}
                title="Ask again now" onClick=${() => setAgain(n => n + 1)}>refresh now</button>
        <label class="chip">refresh
          <select value=${every} onChange=${e => setEvery(Number(e.target.value))}
                  style=${{ padding: "0 2px", border: "none", background: "transparent",
                            font: "inherit", color: "var(--text)" }}>
            ${REFRESH.map(([t, v]) => html`<option key=${t} value=${v}>${t}</option>`)}
          </select></label>
        <span>${d.note || `${hist.length} of ${(d.history || []).length} samples - `
          + "lines begin when this viewer started, because the broker keeps no history."}</span>
      </div>

      ${behind.length > 0 && html`
        <div class="card" style=${{ borderColor: "var(--critical)" }}>
          <h3 style=${{ color: "var(--critical)" }}>
            Retention has passed a consumer's position</h3>
          <p class="why">
            On ${behind.map(c => c.name).join(", ")}, the retention floor is
            above the lowest stored position. When that consumer returns it is
            refused rather than served the oldest surviving record - which is
            correct, and is also somebody's missing data. This is the one alert
            the whole catalogue exists for.
          </p>
        </div>`}

      <div class="tiles">
        <${Tile} k="Connections" v=${fmt(b.connections)} series=${series("connections")}
                 help=${says("saguin_connections")} />
        <${Tile} k="Subscriptions" v=${fmt(b.subscriptions)} series=${series("subscriptions")}
                 help=${says("saguin_subscriptions")} />
        <${Tile} k="Uptime" v=${duration(b.uptime)}
                 help=${says("saguin_uptime_seconds")} />
        <${Tile} k="Published" v=${fmt(hist.length ? hist[hist.length - 1].published : null)}
                 series=${series("published")} help=${says("saguin_published_total")} />
        <${Tile} k="Publishes refused" v=${fmt(totalRefused)} alarm=${totalRefused > 0}
                 help=${says("saguin_publish_refused_total")} />
        <${Tile} k="Broadcast to nobody" u="unmatched" v=${fmt(b.unmatched)}
                 alarm=${b.unmatched > 0} help=${says("saguin_broadcast_unmatched_total")} />
        <${Tile} k="Deliveries dropped" v=${fmt(b.dropped)} alarm=${b.dropped > 0}
                 u="the subscriber's queue was full"
                 help=${says("saguin_deliveries_dropped_total")} />
        <${Tile} k="Deliveries refused" v=${fmt(b.deliveries_refused)}
                 u="its window was full" alarm=${b.deliveries_refused > 0}
                 help=${says("saguin_deliveries_refused_total")} />
        <${Tile} k="Sessions offline" v=${fmt(b.sessions_offline)}
                 u="held, nothing connected"
                 help=${says("saguin_sessions_offline")} />
        <${Tile} k="Retained messages" v=${fmt(b.retained)}
                 u=${b.retained == null ? "no retained store" : "on broadcast topics"}
                 help=${says("saguin_retained_messages")} />
        <${Tile} k="Connections accepted" v=${fmt(b.connections_total)}
                 help=${says("saguin_connections_total")} />
        <${Tile} k="Deliveries expired" v=${fmt(b.expired)}
                 u="the publisher's own instruction"
                 help=${says("saguin_deliveries_expired_total")} />
        <${Tile} k="Sessions shortened" v=${fmt(b.session_expiry_shortened)}
                 u=${"cap " + duration(b.max_session_expiry)}
                 alarm=${b.session_expiry_shortened > 0}
                 help=${says("saguin_session_expiry_shortened_total",
                             "saguin_max_session_expiry_seconds")} />
      </div>

      <div class="grid2">
        <div class="card">
          <h3>Written to the broker<${Info} text=${says("saguin_bytes_received_total")} /></h3>
          <${TimeLine} points=${rate("bytes_in")} color="var(--series-1)"
                       format=${bytesPerSecond}
                       label=${"now " + now(rate("bytes_in"), bytesPerSecond)}
                       empty=${html`A rate needs two samples to subtract, so this
                         fills in one scrape later than the levels above.`} />
        </div>
        <div class="card">
          <h3>Read from the broker<${Info} text=${says("saguin_bytes_sent_total")} /></h3>
          <${TimeLine} points=${rate("bytes_out")} color="var(--series-2)"
                       format=${bytesPerSecond}
                       label=${"now " + now(rate("bytes_out"), bytesPerSecond)}
                       empty=${html`A rate needs two samples to subtract, so this
                         fills in one scrape later than the levels above.`} />
        </div>
      </div>
      <div class="grid2">
        <div class="card">
          <h3>Publishes arriving<${Info} text=${says("saguin_publishes_received_total")} /></h3>
          <${TimeLine} points=${rate("publishes_in")} color="var(--series-1)"
                       format=${msgsPerSecond}
                       label=${"now " + now(rate("publishes_in"), msgsPerSecond)}
                       empty=${html`A rate needs two samples to subtract, so this
                         fills in one scrape later than the levels above.`} />
        </div>
        <div class="card">
          <h3>Deliveries leaving<${Info} text=${says("saguin_deliveries_sent_total")} /></h3>
          <${TimeLine} points=${rate("deliveries_out")} color="var(--series-2)"
                       format=${msgsPerSecond}
                       label=${"now " + now(rate("deliveries_out"), msgsPerSecond)}
                       empty=${html`A rate needs two samples to subtract, so this
                         fills in one scrape later than the levels above.`} />
        </div>
      </div>
      <p class="why" style=${{ marginTop: "-6px" }}>
        The bytes are everything crossing the MQTT listeners in each
        direction: payloads, the acknowledgements behind them, keepalives,
        connections being set up. That is the number to size a link against,
        and it is not how much payload the fleet sent - <em>written</em> is
        what clients sent the broker and <em>read</em> is what the broker sent
        them, so a fleet that only publishes still shows traffic both ways.
        A bridge's link to an upstream broker is in neither.
        <br />
        The two below them count publishes rather than bytes. Arrivals include
        the ones this broker went on to refuse, so read against${" "}
        <em>publishes over time</em>${" "}they say how much of what a fleet
        sends is landing. Deliveries include a queue record handed out a second
        time, and sitting above arrivals is ordinary: one publish reaching
        three subscribers is three deliveries. Deliveries at zero while
        arrivals move is a fleet publishing to nobody.
      </p>

      <div class="grid2">
        <div class="card">
          <h3>Publishes over time<${Info} text=${says("saguin_published_total")} /></h3>
          <${TimeLine} points=${series("published")} label="cumulative, all channels" />
        </div>
        <div class="card">
          <h3>Unresolved queue work<${Info} text=${says("saguin_queue_depth")} /></h3>
          <${TimeLine} points=${series("queue_depth")} label="across every queue"
                       color="var(--series-2)" />
        </div>
      </div>

      ${(d.protocols || []).length > 0 && html`
        <div class="card">
          <h3>Which protocol the fleet connects with<${Info} text=${says("saguin_connections_by_protocol")} /></h3>
          <p class="why">
            Counted from the client table at each scrape rather than kept by
            increments - a gauge maintained on connect and disconnect is a
            gauge that drifts.
          </p>
          <${Stacked} segments=${d.protocols.map(p => ({
            label: "MQTT " + mqttName(p.protocol), value: p.count,
            color: p.protocol === "5" ? "var(--series-1)" : "var(--series-4)" }))} />
          ${d.protocols.some(p => p.protocol !== "5" && p.count > 0) && html`
            <p class="why" style=${{ marginTop: "10px", marginBottom: 0 }}>
              A 3.1.1 client is served an append channel from the tail rather
              than replayed from the retention floor, and is told no offsets.
              An application that depends on the replay depends on connecting
              as MQTT 5.
            </p>`}
        </div>`}

      ${(d.connections_refused || []).length > 0 && html`
        <div class="card" style=${{ borderColor: "var(--warning)" }}>
          <h3 style=${{ color: "var(--warning)" }}>Connections refused
            <${Info} text=${says("saguin_connections_refused_total")} /></h3>
          <p class="why">
            Every connection this broker turned away, whatever protocol the
            client speaks: a <code>CONNECT</code> refused, or a connection
            ended for a refusal. Read against the refusals below - of the
            publishes refused for a reason, how many cost a device its
            connection. <b>This is the series behind a fleet in a reconnect
            loop.</b>
          </p>
          <${Bars} rows=${d.connections_refused.map(r => ({
            label: r.reason, value: r.count, color: "var(--warning)" }))} />
          <div class="legend">
            ${d.connections_refused.map(r => html`
              <span key=${r.reason}><i style=${{ background: "var(--warning)" }}></i>
                ${r.reason} <b class="mono">${fmt(r.count)}</b></span>`)}
          </div>
        </div>`}

      ${d.refusals.length > 0 && html`
        <div class="card">
          <h3>Why publishes were refused<${Info} text=${says("saguin_publish_refused_total")} /></h3>
          <p class="why">Labelled with the specification's name for the code the
            client was answered with. This is what answers "the fleet's data is
            not arriving" without reading a log.</p>
          <${Bars} rows=${d.refusals.map(r => ({ label: r.reason, value: r.count }))} />
          <div class="legend">
            ${d.refusals.map(r => html`
              <span key=${r.reason}><i style=${{ background: "var(--seq)" }}></i>
                ${r.reason} <b class="mono">${fmt(r.count)}</b></span>`)}
          </div>
        </div>`}

      ${(d.subscription_refusals || []).length > 0 && html`
        <div class="card">
          <h3>Why subscriptions were refused<${Info} text=${says("saguin_subscriptions_refused_total")} /></h3>
          <p class="why">One count for each filter a SUBACK refused, by the
            specification's name for its code, read before a 3.1.1 client's
            answer becomes the one code that protocol has. This is what answers
            "why is this device not getting anything".</p>
          <${Bars} rows=${d.subscription_refusals.map(r => ({ label: r.reason, value: r.count }))} />
          <div class="legend">
            ${d.subscription_refusals.map(r => html`
              <span key=${r.reason}><i style=${{ background: "var(--seq)" }}></i>
                ${r.reason} <b class="mono">${fmt(r.count)}</b></span>`)}
          </div>
        </div>`}

      <div class="card">
        <h3>Channels<${Info} text=${says("saguin_channel_info", "saguin_channel_records", "saguin_queue_depth", "saguin_channel_consumer_position_min")} /></h3>
        <table class="ops">
          <thead><tr><th>channel</th><th>type</th><th>provider</th><th>holds</th><th>bytes</th>
            <th>consumers</th><th>worst lag</th><th>removed</th>
            <th>reads refused</th></tr></thead>
          <tbody>
            ${d.channels.map(c => html`
              <tr key=${c.name} class=${c.below_floor ? "behind" : ""}>
                <td class="mono">${c.name}</td>
                <td><span class=${"tag " + c.type}>${c.type}</span></td>
                <td class="mono">${c.provider || html`<span class="sub">-</span>`}${" "}${
                  c.provider_type && html`<span class="tag muted"
                    title="the kind of store this provider is. A memory provider holds what it holds until the broker stops; a sqlite one survives a restart."
                  >${c.provider_type}</span>`}</td>
                <td>${c.records == null
                  ? html`<span class="sub">not counted</span>`
                  : html`<span title=${c.type === "queue"
                      ? "unresolved work, from saguin_queue_depth - a queue publishes no record count, because resolution removes from the middle rather than the front"
                      : "records held, as the next offset minus the retention floor"}
                    >${fmt(c.records)}</span>`}${
                  c.type === "queue" && c.records != null
                    && html`<span class="sub"> unresolved</span>`}</td>
                <td>${c.bytes == null ? html`<span class="sub">-</span>` : bytes(c.bytes)}</td>
                <td>${fmt(c.consumers)}</td>
                <td>${c.lag == null ? html`<span class="sub">-</span>` : fmt(c.lag)}</td>
                <td>${fmt(c.retention_removed)}</td>
                <td>${c.position_lost > 0
                  ? html`<span style=${{ color: "var(--critical)" }}
                          title="reads refused for being below the retention floor - invariant 1 firing"
                        >${fmt(c.position_lost)}</span>`
                  : fmt(c.position_lost)}</td>
              </tr>`)}
          </tbody>
        </table>
        <p class="why" style=${{ marginTop: "10px", marginBottom: 0 }}>
          A blank is a series the broker does not publish rather than a zero.
          A <code>latest</code> channel never publishes bytes, on either
          provider: a write there replaces a value, so measuring would mean
          reading the outgoing one first, on every write. What it holds is a
          different matter and both providers report it, because only a
          topic arriving or leaving moves that number. A queue's${" "}
          <em>holds</em>${" "}is its unresolved work rather than a record count,
          since resolution removes from the middle rather than the front and
          there is no floor to subtract from. So that column carries two
          different numbers and every row says which of them it is showing.
        </p>
      </div>

      ${d.queues.length > 0 && html`
        <div class="grid2">
          ${d.queues.map(q => html`
            <div class="card" key=${q.name}>
              <h3>Queue <span class="mono">${q.name}</span>
                <${Info} text=${says("saguin_queue_depth", "saguin_queue_inflight",
                                     "saguin_queue_dead_lettered_total")} /></h3>
              <div class="tiles" style=${{ gridTemplateColumns: "1fr 1fr" }}>
                <${Tile} k="Unresolved" v=${fmt(q.depth)} />
                <${Tile} k="Out with a worker" v=${fmt(q.inflight)} />
              </div>
              <${Stacked} segments=${[
                { label: "acknowledged", value: q.acknowledged, color: "var(--series-3)" },
                { label: "returned", value: q.returned, color: "var(--series-4)" },
                { label: "redelivered", value: q.redelivered, color: "var(--series-2)" },
                { label: "dead-lettered", value: q.dead_lettered, color: "var(--series-1)" },
              ]} />
              <p class="why" style=${{ marginTop: "10px", marginBottom: 0 }}>
                Every delivery that ended lands in exactly one of these, so they
                sum to <b>${fmt(q.delivered)}</b> delivered less${" "}
                <b>${fmt(q.inflight)}</b> still in flight. ${q.expired > 0
                  ? html`${fmt(q.expired)} aged out unresolved, counted here as
                    well as in whichever released it.` : ""}
              </p>
            </div>`)}
        </div>`}

      <div class="grid2">
        <div class="card">
          <h3>Storage<${Info} text=${says("saguin_provider_info", "saguin_storage_commits_total", "saguin_storage_errors_total")} /></h3>
          ${d.providers.map(p => html`
            <div key=${p.name} style=${{ marginBottom: "14px" }}>
              <${Meter} value=${p.bytes} max=${p.max_bytes}
                        label=${`${p.name} · ${p.type}`} />
              ${p.type === "memory" && html`
                <p class="why" style=${{ marginTop: "6px", marginBottom: 0 }}>
                  Memory-backed: as durable as the next clean shutdown.
                </p>`}
              ${p.commits > 0 && html`
                <p class="why" style=${{ marginTop: "6px", marginBottom: 0 }}>
                  ${fmt(p.records / p.commits)} records per commit, against a
                  ceiling of ${fmt(p.commit_max)}. A batch near one under a
                  ceiling of hundreds is slower than not collecting at all.
                </p>`}
              ${p.errors > 0 && html`
                <p class="why" style=${{ color: "var(--critical)" }}>
                  ${fmt(p.errors)} storage calls have failed.</p>`}
            </div>`)}
        </div>
        <div class="card">
          <h3>Bridges<${Info} text=${says("saguin_bridge_info", "saguin_bridge_connected", "saguin_bridge_received_total")} /></h3>
          ${d.bridges.length === 0
            ? html`<div class="empty">None configured.</div>`
            : html`<table class="ops">
                <thead><tr><th>bridge</th><th>peer</th><th>state</th>
                  <th>in</th><th>out</th><th>skipped</th><th>reconnects</th></tr></thead>
                <tbody>${d.bridges.map(br => html`
                  <tr key=${br.name}>
                    <td class="mono">${br.name}</td>
                    <td class="mono sub">${br.peer}</td>
                    <td>${br.stopped
                      ? html`<span style=${{ color: "var(--critical)" }}>halted itself</span>`
                      : br.connected
                      ? html`<span style=${{ color: "var(--good)" }}>connected</span>`
                      : html`<span style=${{ color: "var(--warning)" }}>link down</span>`}</td>
                    <td>${fmt(br.received)}</td>
                    <td>${fmt(br.sent)}</td>
                    <td>${fmt(br.skipped)}</td>
                    <td>${fmt(br.reconnects)}</td>
                  </tr>`)}</tbody>
              </table>`}
        </div>
      </div>
    </div>`;
}

// **A refusal that is a scope rather than a password.** saguin answers a
// good credential that does not reach a route with 403 precisely so a
// reader does not retry as though the password were wrong. Every panel here
// can meet that, so they share one way of saying it.
function RouteError({ error }) {
  return html`<div class="empty" style=${{ textAlign: "left" }}>${error}</div>`;
}

function useRoute(path, deps) {
  const [d, setD] = useState(null);
  useEffect(() => {
    let live = true;
    setD(null);
    if (!path) return () => { live = false; };
    // **Fetched when the panel is opened, never on a timer.** These routes
    // answer with a list rather than a number, and RFC 0005 says outright
    // they must not be scraped: a client id is a string a client chose, so
    // a series keyed by one is a series count chosen by whoever connects.
    api(path).then(r => live && r.body && setD(r.body));
    return () => { live = false; };
  }, deps || [path]);
  return d;
}

// **Where a topic lands, which a name does not say.** Filters overlap on
// purpose and the most exact of them holds a topic, so this is the question
// `saguin --route` answers at a shell - asked here by somebody with a
// credential and no shell.
function RouteLookup() {
  const [topic, setTopic] = useState("");
  const [d, setD] = useState(null);
  const ask = e => {
    e.preventDefault();
    if (!topic.trim()) return;
    setD(null);
    api("/api/route?topic=" + encodeURIComponent(topic.trim()))
      .then(r => r.body && setD(r.body));
  };
  return html`
    <div>
      <div class="panelhead"><h2 style=${{ margin: 0 }}>Where does a topic land?</h2></div>
      <p class="why">
        A channel claims whatever its topic filter matches, and filters overlap
        deliberately - so a name says nothing about where a record goes, and
        the most exact filter is the one that holds it. Type a topic as a
        publisher would send it, wildcards and all left out.
      </p>
      <div class="card">
        <form onSubmit=${ask} style=${{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
          <input value=${topic} onInput=${e => setTopic(e.target.value)}
                 placeholder="iot/depot/events/order-1" style=${{ flex: 1, minWidth: "220px",
                                                                  fontFamily: "var(--mono)" }} />
          <button class="primary" type="submit">Ask</button>
        </form>
      </div>
      ${d && d.error && html`<div class="empty" style=${{ textAlign: "left" }}>${d.error}</div>`}
      ${d && !d.error && html`
        <div class="card">
          ${d.channel
            ? html`
              <h3><span class="mono">${d.topic}</span> is held by
                <span class="topicname">${d.channel}</span>
                <span class=${"tag " + d.type}>${d.type}</span></h3>
              <p class="why">Claimed by the filter <code>${d.filter}</code>.
                ${d.type === "queue"
                  ? html` <b>A queue's records reach one worker</b>, so nothing
                      subscribing here - this page included - is served them.`
                  : ""}</p>`
            : html`
              <h3><span class="mono">${d.topic}</span> is broadcast
                <span class="tag broadcast">no channel</span></h3>
              <p class="why">No channel's filter matches it, so it is delivered
                live to whoever is subscribed at that moment and stored nowhere.
                <b>That is also what a mistyped topic looks like</b> - the
                broker counts these in${" "}
                <code>saguin_broadcast_unmatched_total</code>.</p>`}
          ${(d.matched || []).length > 1 && html`
            <table class="ops">
              <thead><tr><th>filter</th><th>channel</th><th>type</th><th></th></tr></thead>
              <tbody>${d.matched.map(m => html`
                <tr key=${m.filter}>
                  <td class="mono">${m.filter}</td>
                  <td class="mono">${m.channel}</td>
                  <td>${m.type}</td>
                  <td>${m.channel === d.channel
                    ? html`<b>holds it</b>` : html`<span class="sub">also matches</span>`}</td>
                </tr>`)}</tbody>
            </table>
            <p class="why" style=${{ marginTop: "10px", marginBottom: 0 }}>
              More than one filter matches. The most exact wins: a spelled-out
              level beats <code>+</code>, <code>+</code> beats <code>#</code>,
              and a filter that ends there beats one continuing with${" "}
              <code>#</code>.
            </p>`}
        </div>`}
    </div>`;
}

// **A number out of the payloads, over the arrivals this page is holding.**
// Whatever the format: JSON needs nothing, and a protobuf or avro payload
// is read through the schema its own headers name - the registry doing the
// work, so a field can be charted without this viewer knowing the
// deployment.
function FieldChart({ topic }) {
  // **The same switch as the message cards.** Two controls for one decision
  // is a page that disagrees with itself: the chart used to deserialize while
  // the messages beneath it showed raw bytes, which reads as one of the two
  // being broken.
  const [decode, setDecode] = useDecode();
  const [d, setD] = useState(null);
  const [field, setField] = useState(null);
  useEffect(() => {
    let live = true;
    const tick = () => api("/api/series?topic=" + encodeURIComponent(topic)
                           + (decode ? "" : "&decode=0"))
      .then(r => { if (live && r.body) setD(r.body); });
    tick();
    const id = setInterval(tick, 3000);
    return () => { live = false; clearInterval(id); };
  }, [topic, decode]);

  if (!d || !d.fields || !d.fields.length) {
    if (d && d.source === "none" && d.held > 0 && !decode)
      return html`
        <div class="card">
          <div class="decodedhead" style=${{ margin: 0 }}>
            <span class="tag schema">schema registry</span>
            <span>These payloads can be read with the schema they name, and${" "}
              <b>deserializing is switched off</b> - so there is nothing to plot.</span>
            <button class="chip" style=${{ cursor: "pointer", marginLeft: "auto" }}
                    onClick=${() => setDecode(true)}>decode them</button>
          </div>
        </div>`;
    return null;
  }
  const chosen = (field && d.fields.includes(field)) ? field : d.fields[0];
  const points = d.points
    .filter(p => p.values[chosen] != null)
    .map(p => ({ t: p.t, v: p.values[chosen] }));

  return html`
    <div class="card">
      <h3 style=${{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" }}>
        Values over time
        ${d.fields.length > 1
          ? html`<select value=${chosen} onChange=${e => setField(e.target.value)}>
              ${d.fields.map(f => html`<option key=${f} value=${f}>${f}</option>`)}
            </select>`
          : html`<b class="mono">${chosen}</b>`}
        <span class="chip">${d.source === "schema"
          ? "deserialized with the schema registry"
          : d.source === "number"
          ? "the payload is the number"
          : "from the JSON payload"}</span>
      </h3>
      <${TimeLine} points=${points} label=${chosen}
                   empty=${html`A line needs two arrivals. This page has
                     ${points.length} for this topic so far.`} />
      <p class="why" style=${{ marginTop: "10px", marginBottom: 0 }}>
        Drawn from ${points.length < d.held
          ? html`${points.length} of the ${d.held} arrival(s) this page is
              holding for this topic - the rest carry no <code>${chosen}</code>`
          : html`the ${d.held} arrival(s) this page is holding for this topic`},
        not from the broker - saguin keeps no history, and this ring is bounded
        by <code>messages_per_topic</code>. A boolean is left out: charting a
        flag as 0 and 1 draws a line between two states that were never on a
        scale.
      </p>
    </div>`;
}

function Users() {
  const d = useRoute("/api/users");
  if (!d) return html`<div class="empty">Asking the broker…</div>`;
  if (d.error) return html`<${RouteError} error=${d.error} />`;
  const doors = Object.entries(d.listeners || {});
  const exportCsv = () => downloadCsv("users",
    ["listener", "certificate", "anonymous", "names"],
    doors.map(([name, l]) => [name, l.certificate, l.anonymous_allowed ? "allowed" : "no",
      (l.users || []).join(", ")]));
  return html`
    <div>
      <div class="panelhead" style=${{ display: "flex", alignItems: "center", gap: "8px" }}>
        <h2 style=${{ margin: 0 }}>Who may connect</h2>
        <${ExportCsvButton} onClick=${exportCsv} />
      </div>
      <p class="why">
        Taken from the files this broker loaded at startup rather than read
        again, so the answer cannot disagree with who actually gets in - which
        a re-read would, the moment somebody edited a file under a running
        process. These are the clients, never the operators.
      </p>
      <div class="card">
        <h3>Every door</h3>
        <table class="ops">
          <thead><tr><th>listener</th><th>certificate</th><th>anonymous</th><th>names</th></tr></thead>
          <tbody>
            ${doors.map(([name, l]) => html`
              <tr key=${name}>
                <td class="mono">${name}</td>
                <td>${l.certificate === "required"
                      ? html`<span style=${{ color: "var(--warning)" }}>required</span>`
                      : l.certificate}</td>
                <td>${l.anonymous_allowed
                      ? html`<span style=${{ color: "var(--warning)" }}>allowed</span>`
                      : "no"}</td>
                <td class="mono">${(l.users || []).join(", ") || "-"}</td>
              </tr>`)}
          </tbody>
        </table>
        <p class="why" style=${{ marginTop: "10px", marginBottom: 0 }}>
          A door requiring a certificate is wrong in both directions at once:
          none of the names listed can connect, because they hold passwords and
          the handshake wants a certificate - and whoever the authority signed
          can, with no password and no entry anywhere. saguin cannot list who
          holds one; the authority mints those.
        </p>
      </div>
    </div>`;
}

function Acl() {
  const [user, setUser] = useState("");
  const [cid, setCid] = useState("");
  const [q, setQ] = useState(null);
  // **The names this broker admits, offered but not enforced.** A datalist
  // rather than a select, because the two lists are not the same set: this
  // one is who may connect, and an acl_file is written in *patterns* - so
  // `device-99` against a file naming `device-*` is a legitimate question
  // about a name that appears in no list. A dropdown that refused it would
  // refuse the question the route exists to answer.
  const known = useRoute("/api/users");
  const names = (known && !known.error && known.users) || [];
  const d = useRoute(q ? `/api/acl?user=${encodeURIComponent(q.user)}` +
                         (q.cid ? `&client_id=${encodeURIComponent(q.cid)}` : "") : null,
                     [q && q.user, q && q.cid]);
  return html`
    <div>
      <div class="panelhead"><h2 style=${{ margin: 0 }}>What may a client do</h2></div>
      <p class="why">
        The question <code>saguin --acl</code> answers at a shell: what this
        client may do, and which entry decided it. A client id is needed only
        to resolve a rule written with <code>%c</code>.
      </p>
      ${known && known.error
        ? html`<p class="why">The list of names this broker admits needs${" "}
            <code>/v1/operations/users</code>, which this credential does not
            reach - type a name instead.</p>`
        : names.length
        ? html`<p class="why">
            The ${names.length} name(s) this broker admits are in the list.
            <b>Any other name is still a fair question</b>: an acl_file is
            written in patterns, so a name in no list may still be granted
            something - or nothing, which is an answer rather than an error.
          </p>`
        : known
        ? html`<p class="why">This broker admits no named users - every door
            it has takes anonymous clients or a certificate.</p>`
        : null}
      <div class="card">
        <form onSubmit=${e => { e.preventDefault(); setQ({ user, cid }); }}>
          <div style=${{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
            <select value=${names.includes(user) ? user : ""}
                    disabled=${!names.length}
                    onChange=${e => setUser(e.target.value)}
                    style=${{ fontFamily: "var(--mono)" }}>
              ${names.length
                ? html`<${Fragment}>
                    <option value="">pick a name…</option>
                    ${names.map(n => html`<option key=${n} value=${n}>${n}</option>`)}
                  </${Fragment}>`
                : html`<option value="">${known && known.error
                    ? "the users list needs a wider credential"
                    : "no named users on this broker"}</option>`}
            </select>
            <input value=${user} onInput=${e => setUser(e.target.value)}
                   placeholder="user name" list="saguin-known-users"
                   style=${{ fontFamily: "var(--mono)" }} />
            <datalist id="saguin-known-users">
              ${names.map(n => html`<option key=${n} value=${n}></option>`)}
            </datalist>
            <input value=${cid} onInput=${e => setCid(e.target.value)}
                   placeholder="client id (optional)" style=${{ fontFamily: "var(--mono)" }} />
            <button class="primary" type="submit">Ask</button>
          </div>
        </form>
      </div>
      ${q && !d && html`<div class="empty">Asking the broker…</div>`}
      ${d && d.error && html`<${RouteError} error=${d.error} />`}
      ${d && !d.error && html`
        <div class="card">
          ${d.everything_allowed
            ? html`<p class="why">This broker has no <code>acl_file</code>, so
                every authenticated client may do anything.</p>`
            : html`
              ${d.client_id_allowed === false && html`
                <p class="why" style=${{ color: "var(--critical)" }}>
                  <b>This pair cannot connect at all.</b> An entry's${" "}
                  <code>client_ids</code> refuses at CONNECT with <code>0x86</code>
                  - the same code a wrong password gets - before a single rule is
                  consulted. Everything below is what it would be allowed if it
                  could.
                </p>`}
              <table class="ops">
                <tbody>
                  <tr><th>acl_file</th><td class="mono">${d.acl_file || "none"}</td></tr>
                  <tr><th>pattern applied</th>
                      <td class="mono">${d.pattern_applied || "nothing matched"}</td></tr>
                  <tr><th>patterns matched</th>
                      <td class="mono">${(d.patterns_matched || []).join(", ") || "-"}</td></tr>
                  <tr><th>patterns in file</th>
                      <td class="mono">${(d.patterns_in_file || []).join(", ") || "-"}</td></tr>
                </tbody>
              </table>
              <p class="why" style=${{ marginTop: "10px" }}>
                <b>The pattern applied is the one that decides</b>, and the others
                matched and did nothing: one entry applies, the one spelling the
                name out most exactly. That is the only way an acl_file can take
                something away without saying so.
              </p>
              ${(d.grants || []).length === 0
                ? html`<div class="empty">Granted nothing. A name nothing matches
                    is an answer rather than an error - users are patterns, so
                    there is no register of real names to check against.</div>`
                : html`<table class="ops">
                    <thead><tr><th>role</th><th>kind</th><th>subject</th><th>verbs</th></tr></thead>
                    <tbody>${d.grants.map((g, i) => html`
                      <tr key=${i}>
                        <td>${g.role}</td><td>${g.kind}</td>
                        <td class="mono">${g.subject}</td>
                        <td class="mono">${(g.verbs || []).join(", ")}</td>
                      </tr>`)}</tbody>
                  </table>`}`}
        </div>`}
    </div>`;
}

// **The resolved configuration, written the way it is configured.** The
// operations route answers JSON because that is what an HTTP API answers, and
// an operator reading it has a YAML file open beside them - so a page showing
// braces and quotes asks them to translate every line back before they can
// compare. This writes the same document out as YAML.
//
// It is a writer for what this document actually contains - mappings, lists,
// strings, numbers, booleans and null - and not a general YAML library. A
// key or value that would not survive being written bare is quoted: anything
// with a character YAML gives a meaning to, anything that would read back as
// a number or a boolean when it is a string, and the empty string.
const YAML_PLAIN = /^[A-Za-z0-9_][A-Za-z0-9_.\/@+-]*$/;
const YAML_RESERVED = /^(y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF|null|Null|NULL|~)$/;
function yamlScalar(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : JSON.stringify(String(v));
  const t = String(v);
  if (t === "" || YAML_RESERVED.test(t) || !YAML_PLAIN.test(t) ||
      /^-?\d+(\.\d+)?$/.test(t)) {
    return JSON.stringify(t);          // double quotes, with escapes, as YAML allows
  }
  return t;
}
function toYaml(value, indent) {
  const pad = "  ".repeat(indent);
  if (Array.isArray(value)) {
    if (value.length === 0) return pad + "[]\n";
    return value.map(v => {
      if (v !== null && typeof v === "object") {
        const inner = toYaml(v, indent + 1);
        // The first line of the child rides on the dash, the rest line up
        // under it - which is what makes a list of mappings readable.
        return pad + "- " + inner.slice((indent + 1) * 2);
      }
      return pad + "- " + yamlScalar(v) + "\n";
    }).join("");
  }
  if (value !== null && typeof value === "object") {
    const keys = Object.keys(value);
    if (keys.length === 0) return pad + "{}\n";
    return keys.map(k => {
      const v = value[k];
      const key = pad + yamlScalar(k) + ":";
      if (v !== null && typeof v === "object" &&
          (Array.isArray(v) ? v.length : Object.keys(v).length)) {
        return key + "\n" + toYaml(v, indent + 1);
      }
      if (v !== null && typeof v === "object") {
        return key + " " + (Array.isArray(v) ? "[]" : "{}") + "\n";
      }
      return key + " " + yamlScalar(v) + "\n";
    }).join("");
  }
  return pad + yamlScalar(value) + "\n";
}

// **Colour by role, not by syntax highlighting a generic grammar.** Every
// line this writer produces has one shape: indentation, an optional
// `- `, an optional key, and an optional value - so splitting a line back
// into those parts is a handful of cases against a writer we already
// control, not a YAML parser. A key never carries an unescaped colon (the
// `YAML_PLAIN` it is checked against forbids one), so the first colon
// outside a quoted key is always the separator.
function yamlLineParts(line) {
  const m = /^(\s*)(- )?/.exec(line);
  const prefix = m[0];
  const rest = line.slice(prefix.length);
  if (rest.startsWith('"')) {
    let i = 1;
    while (i < rest.length && rest[i] !== '"') { if (rest[i] === "\\") i++; i++; }
    const token = rest.slice(0, i + 1);
    const after = rest.slice(i + 1);
    if (after === ":") return { prefix, key: token };
    if (after.startsWith(": ")) return { prefix, key: token, value: after.slice(2) };
    return { prefix, value: rest };
  }
  const idx = rest.indexOf(":");
  if (idx === -1) return rest === "" ? { prefix } : { prefix, value: rest };
  const after = rest.slice(idx + 1);
  if (after === "") return { prefix, key: rest.slice(0, idx) };
  return { prefix, key: rest.slice(0, idx), value: after.startsWith(" ") ? after.slice(1) : after };
}
function highlightYaml(text) {
  const lines = text.replace(/\n$/, "").split("\n");
  return html`<${Fragment}>${lines.map((line, i) => {
    const { prefix, key, value } = yamlLineParts(line);
    return html`<${Fragment} key=${i}>${i > 0 ? "\n" : ""}${prefix}${
      key !== undefined ? html`<span class="yaml-key">${key}</span>:` : ""}${
      key !== undefined && value !== undefined ? " " : ""}${
      value !== undefined ? html`<span class="yaml-value">${value}</span>` : ""
    }</${Fragment}>`;
  })}</${Fragment}>`;
}

function ConfigView() {
  const d = useRoute("/api/config");
  if (!d) return html`<div class="empty">Asking the broker…</div>`;
  if (d.error) return html`<${RouteError} error=${d.error} />`;
  return html`
    <div>
      <div class="panelhead">
        <h2 style=${{ margin: 0 }}>The configuration this broker resolved</h2>
      </div>
      <p class="why">
        The resolved values, not the written ones - a channel that wrote no
        filter has <code>${"<name>/#"}</code> here, and one that named no
        storage has the broker-wide default. It is read once at startup, so
        this is what the process is running: a file edited underneath it does
        not change this answer, exactly as it does not change the broker.
        Nothing here is a credential, because nothing in the schema is one - a
        password file, an acl_file and a bridge's client key are all paths.
      </p>
      <div class="card"><pre class="mono">${highlightYaml(toYaml(d, 0))}</pre></div>
    </div>`;
}

// Retained messages: the value the broker keeps for each broadcast topic a
// client asked to retain. Browse them, open one in the tree, or clear one -
// which is an empty retained publish, the way MQTT deletes a retained value.
// **Pages, because a fleet's retained values and an afternoon's dead letters
// are both lists nobody scrolls.**
//
// **The filter runs before the page, and that is the whole rule.** Filtering
// a page rather than the list would answer "no match" about rows sitting on
// page four - the reader's search would depend on where they happened to be
// standing, which is the sort of wrong answer nobody thinks to doubt.
const PER_PAGE = 50;

// Pager keeps a page number that cannot point past the end of what it is
// showing. A filter that shortens the list moves the reader to the last page
// that exists rather than to an empty one.
// **The hook holds only the page number**, and the arithmetic is a plain
// function below. A hook must be called on every render, and both panels
// return early while their data loads - so a hook after those guards is
// called on some renders and not others, which React answers by throwing and
// the page answers by rendering nothing at all. Measured: a blank screen.
function usePage() {
  const [page, setPage] = useState(0);
  return [page, setPage];
}

// Where a page starts and ends, clamped so a filter that shortens the list
// cannot strand the reader on a page that no longer exists.
function pageOf(page, total) {
  const pages = Math.max(1, Math.ceil(total / PER_PAGE));
  const at = Math.min(Math.max(page, 0), pages - 1);
  return { page: at, pages, from: at * PER_PAGE,
           to: Math.min(at * PER_PAGE + PER_PAGE, total) };
}

// **What a sticky column row has to clear.** The pinned strip's height is not
// a constant - a filter line appears, a channel row appears, the text wraps at
// one width and not another - so a `top:` in the stylesheet would be wrong
// somewhere. It is measured here and written on the pane as a custom
// property, which is the one place both the strip and the table can see it.
//
// **Called before any early return**, like every other hook in this file: a
// hook after a `return` runs on some renders and not others, and the page
// answers that by rendering nothing at all.
function usePinnedHead() {
  const ref = useRef(null);
  useEffect(() => {
    const head = ref.current;
    if (!head) return;
    const pane = head.closest(".detail") || document.documentElement;
    const set = () => pane.style.setProperty("--panelhead-h", head.offsetHeight + "px");
    set();
    if (typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(set);
    watch.observe(head);
    return () => { watch.disconnect(); pane.style.removeProperty("--panelhead-h"); };
  });
  return ref;
}

function Pager({ page, pages, setPage, total, shown, noun, quiet }) {
  // **The count shows even on a single page.** Hiding the whole row when
  // everything fits answers "am I seeing all of it?" with silence - and a
  // reader who has been told the list is paged then cannot tell a short list
  // from a control that is missing. The buttons are what disappear.
  //
  // **Except in the pinned strip**, which is what `quiet` is: the panel's own
  // heading is two lines above and already carries the count, so the strip
  // saying "35 dead letters" under a title reading "Dead letters 35" is one
  // number printed twice and a reader checking whether they agree.
  if (total <= PER_PAGE) {
    if (quiet) return null;
    return html`<div class="pager"><span class="sub">${total}${" "}${noun}</span></div>`;
  }
  return html`
    <div class="pager">
      <button class="chip" disabled=${page === 0}
              onClick=${() => setPage(0)} title="First page">«</button>
      <button class="chip" disabled=${page === 0}
              onClick=${() => setPage(page - 1)}>previous</button>
      <span class="sub">${shown.from + 1}${"–"}${shown.to}${" of "}${total}${quiet ? "" : " " + noun}
        ${"  ·  page "}${page + 1}${" of "}${pages}</span>
      <button class="chip" disabled=${page >= pages - 1}
              onClick=${() => setPage(page + 1)}>next</button>
      <button class="chip" disabled=${page >= pages - 1}
              onClick=${() => setPage(pages - 1)} title="Last page">»</button>
    </div>`;
}

// **A confirmation this page draws, rather than the browser's own.**
//
// `window.confirm` is a modal the page cannot style, cannot lay out over more
// than one paragraph, and cannot be driven by anything but a human - it
// blocks the whole tab, so a test harness that clicks a button behind it
// simply hangs. What it is asked here is not "OK?" but a paragraph about
// duplicate work, and that reads badly in a system alert.
//
// **Escape is No**, which is the rule that matters: the safe answer must be
// the one a hurried operator gets by dismissing the box. So is a click on the
// backdrop, and so is the button that has focus when it opens.
function Confirm({ open, title, body, yes, no, onYes, onNo }) {
  const box = useRef(null);
  useEffect(() => {
    if (!open) return;
    const key = e => {
      if (e.key === "Escape") { e.preventDefault(); onNo(); }
    };
    window.addEventListener("keydown", key);
    // Focus lands on the dialog rather than on a button, so Enter does not
    // answer a question the reader has not read yet.
    if (box.current) box.current.focus();
    return () => window.removeEventListener("keydown", key);
  }, [open, onNo]);
  if (!open) return null;
  return html`
    <div class="modal-backdrop" onClick=${onNo}>
      <div class="modal" role="dialog" aria-modal="true" tabIndex=${-1} ref=${box}
           onClick=${e => e.stopPropagation()}>
        <h3>${title}</h3>
        <div class="modal-body">${body}</div>
        <div class="modal-buttons">
          <button onClick=${onNo}>${no || "No"}</button>
          <button class="primary" onClick=${onYes}>${yes || "Yes"}</button>
        </div>
      </div>
    </div>`;
}

function Retained({ onOpen }) {
  const [items, setItems] = useState(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState(null);
  // The same filter the dead-letter list has, remembered the same way: a
  // fleet's retained values run to hundreds of near-identical topics, and
  // this page is refreshed constantly while somebody works through them.
  const [find, setFind] = useState(() => cookie("saguin_viewer_retained_filter") || "");
  useEffect(() => { cookie("saguin_viewer_retained_filter", find); }, [find]);
  const [pageNo, setPageNo] = usePage();
  const headRef = usePinnedHead();
  const load = () => api("/api/retained").then(r => setItems(r.body ? r.body.items : []));
  useEffect(() => { load(); }, []);
  const [asking, setAsking] = useState(null);
  const clear = topic => {
    setBusy(topic); setErr(null);
    api("/api/publish", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ topic, payload: "", format: "text", qos: 1, retain: true, content_type: "", properties: {} }),
    }).then(r => {
      setBusy("");
      if (r.body && r.body.ok) setItems(list => list.filter(x => x.topic !== topic));
      else setErr((r.body && r.body.error) || "the clear was not acknowledged");
    });
  };
  // **A payload that is not text says why it is not.** Protobuf and anything
  // else binary reads as `[35 bytes]` with no explanation, which looks like
  // the page failing rather than like bytes that are not characters - and
  // the reason is already in the answer, unused. Opening the topic is where
  // it can be deserialized against its schema.
  const preview = p => !p ? "" : p.kind === "json" || p.kind === "text" ? (p.text || "").slice(0, 300)
    : p.kind === "empty" ? "(empty)"
    : `[${p.bytes} bytes${p.why ? " - " + p.why : ""}]`;
  if (!items) return html`<div class="empty">Reading retained messages…</div>`;
  const needle = find.trim().toLowerCase();
  // **Filter first, then page.** Paging a filtered list is a search; filtering
  // a page is a search whose answer depends on where the reader was standing.
  const shown = needle
    ? items.filter(it => String(it.topic || "").toLowerCase().includes(needle))
    : items;
  const pager = pageOf(pageNo, shown.length);
  const page = shown.slice(pager.from, pager.to);
  return html`<div>
    <div class="panelhead panelhead-table" ref=${headRef}>
    <h2>Retained messages <span class="sub">${items.length}</span></h2>
    <p class="why">The value the broker keeps for each <b>broadcast</b> topic a
      client asked to retain - an LWT, a discovery record - as this viewer has seen
      it since connecting. A <code>latest</code> channel's current values are not
      these: they belong to their channel and are in the tree, even though MQTT
      delivers both with the retain flag set. Clearing one publishes an empty retained message to it,
      which is how MQTT deletes a retained value.</p>
    ${err && html`<p class="why" style=${{ color: "var(--critical)" }}>${err}</p>`}
    <div style=${{ display: "flex", gap: "6px", alignItems: "center",
                   margin: "0 0 10px", maxWidth: "420px" }}>
      <input value=${find} onInput=${e => setFind(e.target.value)}
             placeholder="filter by topic"
             style=${{ flex: 1, minWidth: 0, fontFamily: "var(--mono)",
                       fontSize: "12px" }} />
      ${find && html`
        <button class="chip" style=${{ cursor: "pointer" }}
                title="Empty the filter box"
                onClick=${() => setFind("")}>clear</button>`}
    </div>
    ${find && html`
      <p class="sub" style=${{ margin: "0 0 8px" }}>
        ${shown.length}${" of "}${items.length}${" match "}<code>${find}</code>.
      </p>`}
    ${/* **The pager rides in the pinned strip as well as under the table.**
          Paging from the bottom means scrolling to the end to move, then
          scrolling back to the top to read - and the reader who has just
          filtered is at the top, where the control they need was not. The
          copy below stays: a reader at the end of a page should not have to
          come back up either. */
      shown.length > 0 && html`
      <${Pager} page=${pager.page} pages=${pager.pages} setPage=${setPageNo}
                total=${shown.length} noun="retained topics" quiet
                shown=${{ from: pager.from, to: pager.to }} />`}
    </div>
    ${items.length === 0
      ? html`<div class="empty">Nothing retained on this broker yet - or nothing that arrived since this
          page connected. A retained value appears here when a client publishes one to
          a topic no channel claims.</div>`
      : shown.length === 0
      ? html`<div class="empty">No retained topic has <code>${find}</code> in its name.</div>`
      : html`<table class="ops undertheh">
          <thead><tr><th>topic</th><th>value kept</th><th>arrived</th><th></th></tr></thead>
          <tbody>${page.map(it => html`<tr key=${it.topic}>
            <td class="mono"><a href="#" title="Open this topic"
                onClick=${e => { e.preventDefault();
                                 onOpen(`${it.channel || "broadcast"}/${it.topic}`); }}>${it.topic}</a></td>
            <td class="mono sub" style=${{ maxWidth: "480px", overflow: "hidden",
                textOverflow: "ellipsis", whiteSpace: "nowrap" }}>${preview(it.payload)}</td>
            ${/* **One row per topic is the whole of what a retained value
                  is**, and this column is what stops that reading as a lost
                  message. Publish five times to one topic and four were
                  delivered and are gone; the fifth is the slot's contents and
                  the only thing a new subscriber is handed. The tree lists
                  all five, because that is arrival history rather than the
                  store - two different facts, and this says which is which
                  without making the reader open both. */
              html`<td class="mono sub"
                title=${it.seen > 1
                  ? `${it.seen} messages have arrived on this topic since the viewer `
                    + `connected. A retained value is one slot per topic, so this row `
                    + `is the last one published with the retain flag - the others `
                    + `were delivered and kept nowhere. The tree has all of them.`
                  : "Messages seen on this topic since the viewer connected"}
                >${it.seen || "-"}</td>`}
            <td><button class="chip" disabled=${busy === it.topic}
                onClick=${() => setAsking(it.topic)}>${busy === it.topic ? "clearing…" : "clear"}</button></td>
          </tr>`)}</tbody>
        </table>
        <${Pager} page=${pager.page} pages=${pager.pages} setPage=${setPageNo}
                  total=${shown.length} noun="retained topics"
                  shown=${{ from: pager.from, to: pager.to }} />`}
    <${Confirm} open=${!!asking} title="Clear this retained message?"
      yes="Clear it" no="Cancel"
      onNo=${() => setAsking(null)}
      onYes=${() => { const t = asking; setAsking(null); clear(t); }}
      body=${html`<div>
        <p><code>${asking}</code></p>
        <p>This publishes an empty retained message to that topic, which is how
          MQTT deletes a retained value. Whoever subscribes next is handed
          nothing for it.</p>
      </div>`} />
  </div>`;
}

// **Dead letters, and putting one back.**
//
// A dead-letter channel is browsable in the tree like any other append
// channel, and that is not the same as answering the question an operator
// opens it with: which jobs failed, why, and can I put this one back now the
// bug is fixed. This lists the records with their `saguin-dlq-*` reasons
// surfaced, and requeues one at a time.
//
// **One record per click, and no drain-all.** RFC 0003 names the hazard:
// redriving into a queue whose bug is not fixed dead-letters the job again,
// and doing it in a loop fills a disk. One at a time makes that self-limiting.
//
// **A refusal is shown, never swallowed.** The commonest one is an ACL that
// grants the viewer `read` on the dead-letter channel and no `write` on the
// queue - two grants on two channels - and the broker's word for it is "Not
// authorized", which names neither. The backend's hint does.
function DeadLetters({ onOpen }) {
  const [data, setData] = useState(null);
  const [chan, setChan] = useState(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState(null);
  // **A dead-letter channel is where a fleet's failures pile up**, and after
  // an afternoon of them the one job somebody is looking for is a scroll
  // away. Matched as a substring rather than as a topic filter: what an
  // operator has in hand is usually a device name or a job id out of a log
  // line, not a well-formed `+`/`#` filter.
  // Remembered like the tree's filter, and for the same reason: this page is
  // refreshed constantly while somebody works through a list, and a filter
  // that empties itself on every refresh is one they retype every time.
  const [find, setFind] = useState(() => cookie("saguin_viewer_dlq_filter") || "");
  useEffect(() => { cookie("saguin_viewer_dlq_filter", find); }, [find]);
  // **Hiding what has been put back is how a list gets worked through.** An
  // operator redriving an afternoon's failures wants the ones they have not
  // reached yet, and the rows they have dealt with are noise between them.
  // Remembered like the filter, and off by default: a list that hides rows on
  // first sight is one somebody reads as shorter than it is.
  const [pageNo, setPageNo] = usePage();
  const headRef = usePinnedHead();
  const [hidePutBack, setHidePutBack] = useState(
    () => cookie("saguin_viewer_dlq_hide_putback") === "1");
  useEffect(() => { cookie("saguin_viewer_dlq_hide_putback", hidePutBack ? "1" : "0"); },
            [hidePutBack]);
  useEffect(() => { api("/api/deadletters").then(r => r.body && setData(r.body)); }, []);
  useEffect(() => {
    if (!data) return;
    const first = chan || data.channels[0];
    if (!first) return;
    if (!chan) setChan(first);
    api(`/api/deadletters?channel=${encodeURIComponent(first)}`)
      .then(r => r.body && setData(d => ({ ...d, ...r.body })));
  }, [data && data.channels && data.channels.join(","), chan]);

  const reload = () => api(`/api/deadletters?channel=${encodeURIComponent(chan)}`)
    .then(r => r.body && setData(d => ({ ...d, ...r.body })));

  const [asking, setAsking] = useState(null);
  const requeue = rec => {
    const key = rec.topic + "#" + rec.offset;
    setBusy(key); setMsg(null);
    api("/api/requeue", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ topic: rec.topic, offset: rec.offset }),
    }).then(r => {
      setBusy("");
      const b = r.body || {};
      // **Read `ok`, never the status code.** A broker that acknowledges and
      // refuses answers 200 with ok:false - the shape /api/publish already
      // has - so a check on the HTTP status alone would report a refused
      // requeue as a success, which is the worst kind of failure here.
      if (b.ok) { setMsg({ ok: true, text: `${b.from} → ${b.to}. ${b.note || ""}` }); reload(); }
      else setMsg({ ok: false, text: b.error || "the requeue was not acknowledged",
                    hint: b.hint });
    });
  };

  if (!data) return html`<div class="empty">Reading dead letters…</div>`;
  if (!data.channels.length)
    return html`<div class="empty">No dead-letter channels on this broker. A queue
      derives one when it is configured with a retry policy; nothing has one here.</div>`;
  const rows = data.records || [];
  // Case-insensitive, because a topic is lower case far more often than the
  // thing an operator pastes out of a log line is.
  const needle = find.trim().toLowerCase();
  const shown = rows.filter(r =>
    (!needle || String(r.topic || "").toLowerCase().includes(needle)) &&
    (!hidePutBack || !r.redriven));
  // Counted separately from the filter, because "8 hidden" and "8 do not
  // match" are different facts and an operator acts on them differently.
  const putBack = rows.filter(r => r.redriven).length;
  const pager = pageOf(pageNo, shown.length);
  const page = shown.slice(pager.from, pager.to);
  return html`<div>
    <div class="panelhead panelhead-table" ref=${headRef}>
    <h2>Dead letters <span class="sub">${rows.length}</span></h2>
    <p class="why">Work a queue gave up on, after its attempts ran out or the broker
      refused it. Putting one back publishes it to the topic it came from, which is all
      a requeue is - the job returns as a new record with a fresh attempt count, and its
      ${" "}<code>saguin-id</code> goes with it so a consumer can tell it is the same work.
      This page holds the last ${data.per_topic} records per topic.</p>
    ${data.channels.length > 1 && html`<nav class="subtabs">
      ${data.channels.map(c => html`<button key=${c} class=${c === chan ? "on" : ""}
        onClick=${() => { setChan(c); setMsg(null); }}>${c}</button>`)}
    </nav>`}
    ${msg && html`<p class="why" style=${{ color: msg.ok ? "var(--ok)" : "var(--critical)",
                                           fontWeight: 600 }}>
      ${msg.ok ? "Requeued: " : "Not requeued: "}${msg.text}
      ${msg.hint && html`<br /><span style=${{ fontWeight: 400 }}>${msg.hint}</span>`}</p>`}
    <div style=${{ display: "flex", gap: "6px", alignItems: "center",
                   margin: "0 0 10px", maxWidth: "420px" }}>
      <input value=${find} onInput=${e => setFind(e.target.value)}
             placeholder="filter by topic"
             style=${{ flex: 1, minWidth: 0, fontFamily: "var(--mono)",
                       fontSize: "12px" }} />
      ${find && html`
        <button class="chip" style=${{ cursor: "pointer" }}
                title="Empty the filter box"
                onClick=${() => setFind("")}>clear</button>`}
      <label class="sub" style=${{ display: "flex", alignItems: "center", gap: "6px",
                                   whiteSpace: "nowrap", cursor: "pointer" }}
             title=${"Hide the rows this viewer has already put back. The dead letter "
                     + "itself is never removed - an append channel's records are written "
                     + "once - so this hides them from view and changes nothing on the broker."}>
        <input type="checkbox" checked=${hidePutBack}
               onChange=${e => setHidePutBack(e.target.checked)} />
        hide put back${putBack ? ` (${putBack})` : ""}
      </label>
    </div>
    ${(find || (hidePutBack && putBack > 0)) && html`
      <p class="sub" style=${{ margin: "0 0 8px" }}>
        ${"showing "}${shown.length}${" of "}${rows.length}
        ${find && html`${" matching "}<code>${find}</code>`}
        ${hidePutBack && putBack > 0 && html`${find ? ", " : " - "}${putBack}${" already put back, hidden"}`}.
      </p>`}
    ${/* The same pager as the one under the table, in the pinned strip: a
          reader who has just filtered is at the top, and that is where the
          control to move through the result belongs. */
      shown.length > 0 && html`
      <${Pager} page=${pager.page} pages=${pager.pages} setPage=${setPageNo}
                total=${shown.length} noun="dead letters" quiet
                shown=${{ from: pager.from, to: pager.to }} />`}
    </div>
    ${rows.length === 0
      ? html`<div class="empty">${data.hint || `Nothing on ${chan}.`}</div>`
      : shown.length === 0
      ? html`<div class="empty">${
          find
            ? html`No dead letter on ${chan} has <code>${find}</code> in its topic${
                hidePutBack && putBack > 0 ? ", among the ones not already put back" : ""}.`
            : html`Every dead letter on ${chan} has already been put back by this
                viewer. Untick <b>hide put back</b> to see them - they are still on the
                channel, because an append channel's records are never removed.`}</div>`
      : html`<table class="ops undertheh">
          <thead><tr><th>topic</th><th>reason</th><th>attempts</th><th>queue</th>
            <th>failed</th><th>put back</th><th>goes back to</th><th></th></tr></thead>
          <tbody>${page.map(r => html`<tr key=${r.topic + "#" + r.offset}>
            <td class="mono"><a href="#" title="Open this topic"
                onClick=${e => { e.preventDefault(); onOpen(`${chan}/${r.topic}`); }}>${r.topic}</a></td>
            <td>${r.reason || "-"}</td>
            <td class="mono">${r.attempts == null ? "-" : r.attempts}</td>
            <td>${r.queue || "-"}</td>
            <td class="mono sub">${r.failed_at || "-"}</td>
            <td class=${r.redriven ? "mono" : "mono sub"}
                style=${r.redriven ? { color: "var(--warn)", fontWeight: 600 } : null}
                title=${r.redriven
                  ? `Put back ${r.redriven.count} time(s), last at ${r.redriven.at} by ${r.redriven.by}. `
                    + `The dead letter itself never changes - an append channel's records are written once - `
                    + `so this is what this viewer remembers, for as long as the broker keeps the record.`
                  : "This viewer has no record of this work being put back"}>
              ${r.redriven ? `${r.redriven.count}× ${r.redriven.by}` : "-"}</td>
            <td class="mono">${r.back || html`<span class="dim" title=${r.why_not}>cannot tell</span>`}</td>
            <td>${r.back && html`<button class="chip" disabled=${busy === r.topic + "#" + r.offset}
                onClick=${() => setAsking(r)}>
                ${busy === r.topic + "#" + r.offset ? "requeueing…" : "requeue"}</button>`}</td>
          </tr>`)}</tbody>
        </table>
        <${Pager} page=${pager.page} pages=${pager.pages} setPage=${setPageNo}
                  total=${shown.length} noun="dead letters"
                  shown=${{ from: pager.from, to: pager.to }} />`}
    <${Confirm} open=${!!asking}
      title=${asking && asking.redriven ? "Put this job back again?" : "Put this job back?"}
      yes="Put it back" no="Cancel"
      onNo=${() => setAsking(null)}
      onYes=${() => { const r = asking; setAsking(null); requeue(r); }}
      body=${asking && html`<div>
        <p><code>${asking.topic}</code><br />→ <code>${asking.back}</code></p>
        <p>It goes back to ${asking.queue || "its queue"} as a <b>new record</b>: a new
          offset and an attempt count starting at 1. If the bug that failed it is not
          fixed, it will be dead-lettered again.</p>
        ${asking.redriven && html`
          <p class="modal-warn">This page has already put this work back
            ${" "}${asking.redriven.count}× - last at ${asking.redriven.at} by
            ${" "}${asking.redriven.by}. Doing it again publishes another copy of the
            job, and nothing removes the first: the queue cannot tell a duplicate from
            new work, because a requeue is an ordinary publish.</p>`}
      </div>`} />
  </div>`;
}

function App() {
  // **The theme is remembered in a cookie**, and read in index.html before
  // this file runs, so the first paint is already the right colour rather
  // than a white flash on a dark theme.
  const [theme, setTheme] = useState(
    () => document.documentElement.dataset.theme || "dark");
  const toggleTheme = () => setTheme(t => {
    const next = t === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    cookie("saguin_viewer_theme", next);
    return next;
  });

  // The tab survives a refresh like every other choice on this page. Anything
  // not in this list - a value left by an earlier version - is topics rather
  // than a tab that renders nothing.
  //
  // **The list is the thing to extend when a tab is added, and it was not.**
  // It read `=== "dashboard" ? "dashboard" : "topics"` when there were two
  // tabs; the Alarms tab arrived and this stayed, so the cookie was written
  // faithfully and then thrown away on the next load - an operator watching
  // the Alarms tab was put back on Topics by a refresh.
  const TABS = ["topics", "dashboard", "alarms"];
  const [tab, setTabRaw] = useState(
    () => TABS.includes(cookie("saguin_viewer_tab")) ? cookie("saguin_viewer_tab") : "topics");
  const setTab = t => { cookie("saguin_viewer_tab", t); setTabRaw(t); };

  const [pretty, setPretty] = usePretty();
  const [decode, setDecode] = useDecode();
  const [copyAs, setCopyAs] = usePref("copy_as", "text");

  // The sidebar's width, dragged and remembered. Topic names are long and
  // how much room they deserve is a matter of what somebody is looking at,
  // not something this page can pick for them.
  const [treeW, setTreeW] = useState(
    () => Number(cookie("saguin_viewer_tree_width")) || 320);
  const drag = useCallback(e => {
    e.preventDefault();
    const move = ev => {
      // Bounded, because a divider dragged to either edge leaves a pane
      // that cannot be got back without clearing a cookie.
      setTreeW(Math.min(Math.max(ev.clientX, 200), window.innerWidth - 320));
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.style.userSelect = "";
      setTreeW(w => { cookie("saguin_viewer_tree_width", w); return w; });
    };
    // Without this the drag selects the tree's text as it passes over it.
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  }, []);

  const [msgQ, setMsgQ] = useState(() => cookie("saguin_viewer_msg_filter") || "");

  useEffect(() => { cookie("saguin_viewer_msg_filter", msgQ); }, [msgQ]);
  const [treeFilter, setTreeFilter] = useState(
    () => cookie("saguin_viewer_tree_filter") || "");
  useEffect(() => { cookie("saguin_viewer_tree_filter", treeFilter); }, [treeFilter]);
  const [rows, setRows] = useState([]);
  const [state, setState] = useState({});
  // **How many arrivals every topic keeps, which is one setting for the page
  // rather than for whichever topic is open** - the rings are resized
  // together, so it lives here beside the tab state and not in the list it
  // happens to be read from. The broker's configured value is the starting
  // point; a choice is remembered and re-applied on the next load, because
  // the server starts again at its configured default.
  const [keep, setKeepRaw] = useState(0);
  // What the open topic's list is showing, reported up so the numbers sit
  // beside the control that changes them.
  const [counts, setCounts] = useState(null);
  useEffect(() => {
    if (keep || !state.messages_per_topic) return;
    const saved = Number(cookie("saguin_viewer_topic_keep")) || 0;
    setKeepRaw(saved || state.messages_per_topic);
    if (saved && saved !== state.messages_per_topic) {
      api("/api/keep", { method: "POST", headers: { "content-type": "application/json" },
                         body: JSON.stringify({ keep: saved }) });
    }
  }, [state.messages_per_topic, keep]);
  const keepChoices = state.keep_choices || KEEP;
  const setKeep = n => {
    setKeepRaw(n);
    cookie("saguin_viewer_topic_keep", n);
    api("/api/keep", { method: "POST", headers: { "content-type": "application/json" },
                       body: JSON.stringify({ keep: n }) });
  };
  // The dashboards shown as a fixed sub-tab row inside the Dashboard tab, in
  // config order. Empty is a viewer with none, and the built-in shows instead.
  // Which sub-tab is selected is the Dashboards component's own state; the
  // main tab here is only ever "topics" or "dashboard".
  const [dashboards, setDashboards] = useState([]);
  useEffect(() => { api("/api/dashboards").then(r => r.body && setDashboards(r.body)); }, []);

  // **Alarms that reach the operator**, so a firing one is seen from any tab
  // rather than only on its own card on whichever tab is open - the badge on
  // the Alarms tab, the tab's own table, and, where the operator has asked,
  // a browser notification when a new one starts firing.
  //
  // **One evaluation, the backend's.** This page used to evaluate the
  // thresholds itself as well, for a strip and a count on the Dashboard tab,
  // and the two readers disagreed: the page honoured a `{provider="…"}`
  // selector and the recorder did not. There is now one source for what is
  // firing - the record - so what pops, what the badge counts and what the
  // table lists cannot come apart.
  const [alarmLog, setAlarmLog] = useState(null);
  useEffect(() => {
    let live = true, timer = null;
    const tick = () => api("/api/alarms").then(r => {
      if (!live) return;
      if (r.body) setAlarmLog(r.body);
      timer = setTimeout(tick, 30000);
    });
    tick();
    return () => { live = false; if (timer) clearTimeout(timer); };
  }, []);
  // **"Notify me" now says what it can actually do.** It failed silently in
  // every direction an operator meets it, and a control that does nothing and
  // says nothing reads as a broken button:
  //
  //   * it lived in the Dashboard tab's alarm strip, which renders only while
  //     something is firing - so it could not be armed in the quiet moment an
  //     operator wants it, nor turned off afterwards. It is on the Alarms tab
  //     head now, where it is always there.
  //   * the dedup set was filled on every poll whether or not the toggle was
  //     on, so the alarm in front of the operator was already "seen" and the
  //     click produced nothing observable. Nothing is seen while it is off, so
  //     arming during a firing alarm pops that alarm.
  //   * a refused or dismissed permission prompt was swallowed, and the button
  //     stayed "🔕 notify me" with no reason given.
  //   * over plain HTTP to anything but localhost - which is how a LAN
  //     deployment is opened - the old guard fell through to the plain toggle
  //     and showed "🔔 notifying" while nothing could ever be delivered.
  //
  // **And then the fix for that last one blamed the wrong thing.** It tested
  // `typeof Notification !== "undefined"`, on the belief that an insecure
  // origin has no Notification API. It has one. What an insecure origin has is
  // `Notification.permission === "denied"`, permanently and before any prompt -
  // so the page told an operator with notifications switched on that their
  // browser was blocking the page, which is not what is happening and sends
  // them to a settings screen that cannot fix it. `isSecureContext` is the
  // difference between "you blocked this" and "this page is not served over
  // HTTPS", and they need different sentences because they need different
  // actions. Driven in Chromium against this deployment before it was written:
  // over http:// to a LAN address, `typeof Notification` is "function" and
  // `permission` is already "denied".
  // **One source of truth: the browser's most recent answer, held here.**
  // Arming read `requestPermission()`'s result and staying armed re-read
  // `Notification.permission` on every poll - two sources, and they do
  // disagree: driven here, `requestPermission()` resolves "granted" while
  // `permission` still reads "denied". The control then armed and switched
  // itself back off on the next tick, which is "clicking it does nothing"
  // wearing a different hat. So the page asks the browser only when the
  // operator clicks, keeps what it was told, and never second-guesses it
  // afterwards from a value that can lag.
  const notifyAPI = typeof Notification !== "undefined";
  const secure = typeof window !== "undefined" && window.isSecureContext;
  const [perm, setPerm] = useState(() => (notifyAPI ? Notification.permission : "unsupported"));
  // Off unless the browser is already granting, so a cookie written by a
  // browser that could deliver cannot light up one that cannot.
  const [notify, setNotify] = useState(
    () => cookie("saguin_viewer_notify") === "1"
          && notifyAPI && Notification.permission === "granted");
  const [notifyNote, setNotifyNote] = useState(null);
  useEffect(() => { cookie("saguin_viewer_notify", notify ? "1" : "0"); }, [notify]);
  const firing = (alarmLog && alarmLog.firing) || [];
  const firingKeys = firing.map(e => e.dash + "|" + e.title).join(",");
  const notifiedKeys = useRef(new Set());
  const refused = () => {
    notifiedKeys.current = new Set();
    setNotify(false);
    setNotifyNote(notifyDeliveryFailed({
      api: notifyAPI, secure,
      perm: notifyAPI ? Notification.permission : "unsupported" }));
  };
  useEffect(() => {
    if (!notify) { notifiedKeys.current = new Set(); return; }
    for (const e of firing) {
      const k = e.dash + "|" + e.title;
      if (notifiedKeys.current.has(k)) continue;
      raiseNotification("saguin alarm", `${e.title} - ${e.dash}`, refused);
    }
    notifiedKeys.current = new Set(firing.map(e => e.dash + "|" + e.title));
  }, [firingKeys, notify]);
  const toggleNotify = () => {
    if (notify) { setNotify(false); setNotifyNote(null); return; }
    const plan = notifyPlan({ api: notifyAPI, secure, perm });
    setNotifyNote(plan.note);
    if (plan.act === "arm") { setNotify(true); return; }
    if (plan.act !== "ask") return;
    Promise.resolve()
      .then(() => Notification.requestPermission())
      .catch(() => "denied")
      .then(p => {
        const answer = notifyAnswer(p, { api: notifyAPI, secure });
        setPerm(p);
        setNotify(answer.arm);
        setNotifyNote(answer.note);
      });
  };

  const reachableNow = useReachable();
  // The selected topic lives in the URL, so a topic can be linked to - and
  // so the page can be opened straight at one, which is what makes it
  // possible to check what it renders without a hand on a mouse.
  const fromHash = () => decodeURIComponent(location.hash.replace(/^#/, "")) || null;
  const [open, setOpen] = useState(() => {
    const t = fromHash();
    if (!t) return new Set();
    const parts = t.split("/");
    return new Set(parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join("/")));
  });
  const [selected, setSelectedRaw] = useState(fromHash);

  // **What the right panel is showing when it is not a topic**, and it is
  // remembered like everything else. It was state and nothing more, so a
  // refresh dropped whichever panel was open and fell back to the topic
  // still named in the URL - which reads as the page having navigated
  // somewhere by itself.
  const [view, setViewRaw] = useState(() => {
    try { return JSON.parse(cookie("saguin_viewer_view") || "null"); }
    catch (e) { return null; }
  });
  const setView = v => { cookie("saguin_viewer_view", JSON.stringify(v ?? null));
                         setViewRaw(v); };

  // **The broker's own views are a group that folds away, and it starts
  // folded.** There are ten of them and they sat above the channel tree, so
  // the thing this page is mostly for - the topics - began below the fold on
  // a short window. Folded by default rather than remembered-open-first,
  // because the complaint is about the first paint: an operator who wants
  // the group open opens it once and it stays that way.
  const [opsOpen, setOpsOpenRaw] = useState(
    () => cookie("saguin_viewer_ops_open") === "1");
  const setOpsOpen = v => { cookie("saguin_viewer_ops_open", v ? "1" : "0");
                            setOpsOpenRaw(v); };
  const setSelected = t => {
    setView(null);
    setSelectedRaw(t); location.hash = encodeURIComponent(t);
  };

  // **Filling the filter box is a change to a control the reader is not
  // looking at**, so the box says so once - see the animation in app.css.
  //
  // Turned off and on across a frame rather than simply on, because a CSS
  // animation restarts when the class arrives and not when the state that
  // set it changes: clicking a second topic while the first pulse was
  // still running would otherwise do nothing visible, which is exactly the
  // case this exists for.
  const [filled, setFilled] = useState(false);
  const filterBox = useRef(null);
  useEffect(() => {
    if (!filled) return;
    const id = setTimeout(() => setFilled(false), 900);
    return () => clearTimeout(id);
  }, [filled]);

  // **Filter rather than reveal**, and it is about size. The tree is built
  // from every row on every render, and expanding down to one topic renders
  // every sibling on the way - on a flat fleet of a million topics that is
  // a million nodes to show one. Narrowing the rows first builds a tree of
  // one instead. The reader is told, which is what the pulse is.
  const showTopic = (channel, topic) => {
    setTreeFilter(topic);
    setFilled(false);
    requestAnimationFrame(() => setFilled(true));
    // **The caret goes where the change did**, which outlasts the pulse: the
    // focus ring stays until the reader clicks away, and it is already where
    // they would go to edit or clear the filter. Nothing is lost by taking
    // focus here - what they just clicked was a link in the feed.
    if (filterBox.current) {
      filterBox.current.focus();
    }
    setSelected((channel || "broadcast") + "/" + topic);
  };

  const refresh = useCallback(() => {
    // **A null body means the viewer did not answer**, and the last good
    // answer is better than none: keeping it is what lets the page go on
    // showing what it had while the header says it is stale. Assigning it
    // blanked the page instead - `rows.length` and `state.connected` on
    // nothing at all - which is a worse failure than the one being fixed.
    api("/api/tree").then(r => r.body && setRows(r.body));
    api("/api/state").then(r => {
      if (!r.body) return;
      setState(r.body);
      // From the broker's own catalogue, so a channel added to saguin.yaml -
      // or a dead-letter channel a queue derived, which nothing configured -
      // appears here without this file being edited.
      window.__channels = Object.fromEntries(
        (r.body.channels || []).map(c => [c.name, c.type]));
      window.__seekable = Object.fromEntries(
        (r.body.channels || []).map(c => [c.name, c.seekable]));
      window.__starts = Object.fromEntries(
        (r.body.channels || []).map(c => [c.name, c.start]));
      window.__held = Object.fromEntries(
        (r.body.channels || []).map(c => [c.name, c.held]));
    });
  }, []);

  useEffect(() => { refresh(); const id = setInterval(refresh, 1000);
                    return () => clearInterval(id); }, [refresh]);

  const toggle = path => setOpen(prev => {
    const next = new Set(prev);
    next.has(path) ? next.delete(path) : next.add(path);
    return next;
  });

  // **A tree of three hundred devices is unusable without this**, which is
  // the whole reason it exists. It is deliberately not remembered: a filter
  // restored on load would hide almost everything and read as a broker that
  // had lost its topics.
  const match = treeFilter.trim().toLowerCase();
  const shownRows = match
    ? rows.filter(r => r.topic.toLowerCase().includes(match) ||
                       (r.channel || "broadcast").toLowerCase().includes(match))
    : rows;
  const tree = toTree(shownRows, match ? {} : window.__channels);
  // While filtering, every level on the way to a match is open - a filter
  // that left the matches collapsed would be a filter that shows nothing.
  const openNow = match
    ? new Set(shownRows.flatMap(r => {
        const parts = [r.channel || "broadcast", ...r.topic.split("/")];
        return parts.map((_, i) => parts.slice(0, i + 1).join("/"));
      }))
    : open;
  const roots = Object.keys(tree).sort();
  // The channel is the first level of a tree path and the topic is the
  // rest, because toTree puts the channel there.
  const channel = selected ? selected.split("/")[0] : null;
  const topic = selected ? topicOf(selected) : null;
  const seekable = window.__seekable && window.__seekable[channel];

  // Named once: the header counts them when folded, and the folded case
  // looks the active one up here. Two copies of this list would be two
  // places to add the next view to, and the second would be forgotten.
  const opsViews = [
    ["config", "Resolved configuration", "⚙"],
    ["users", "Who may connect", "🔑"],
    ["acl", "What may a client do", "🛡"],
    ["sessions", "Sessions, and hanging one up", "🔌"],
    ["route", "Where does a topic land?", "🧭"],
    ["feed", "Everything, as it arrives", "📡"],
    ["consumers", "Who is behind", "👁"],
    ["positionlost", "Who lost records", "🕳"],
    ["retained", "Retained messages", "📌"],
    ["deadletters", "Dead letters, and putting one back", "✉"],
    ["publish", "Publish a message", "📤"],
  ];

  // **Ninety-nine is where the number stops being a number.** Past it what a
  // reader takes from the badge is "a lot", and a four-digit count in a
  // sidebar row pushes the label it belongs to out of the way - so it becomes
  // `+99` and the exact figure is one click away, on the page that can show
  // them.
  // A zero is a count too - hiding it answers "am I seeing all of it?" with
  // silence, which is the thing the badge is there to stop.
  const badge = n => (n === undefined ? null : html`
    <span class="opscount" title=${n > 99 ? `${n} held - the list has the exact figure` : null}
      >${n > 99 ? "+99" : n}</span>`);

  const sidebarButton = (key, label, icon) => html`
    <button class=${"opsbutton" + (view === key ? " on" : "")}
            onClick=${() => setView(view === key ? null : key)}>
      <span class="opsicon" aria-hidden="true">${icon}</span>
      <span class="opslabel">${label}</span>
      ${badge((state.counts || {})[key])}</button>`;

  return html`
    <header>
      <a class="brand" href="/"
         title="Back to the top of the viewer"
         onClick=${e => {
           // **The hash goes with it.** `href="/"` alone leaves a fragment
           // naming a topic in the address bar, and the page restores its
           // selection from that - so clicking home would land on the same
           // topic it started from and read as a link that does nothing.
           e.preventDefault();
           // The origin and a bare slash: setting the hash and then assigning
           // "/" left the query behind and the browser treated it as a
           // same-document move, so the click cleared the fragment and went
           // nowhere.
           location.href = location.origin + "/";
         }}>
        <img class="mark" src="img/favicon.ico" alt="" width="22" height="22" />
        <h1>Sagüin Viewer</h1>
      </a>
      ${reachableNow
        ? html`<span class="pill">
            <span class=${"dot " + (state.connected ? "up" : "down")}></span>
            ${state.connected ? "connected" : "not connected"}
            ${state.error ? " - " + state.error : ""}
          </span>`
        : html`<span class="pill" style=${{ color: "var(--bad)" }}
                     title="Nothing on this page is updating. Start the viewer again and it resumes on its own.">
            <span class="dot down"></span>
            the viewer is not answering - showing what it last had
          </span>`}
      ${replyRefused(state) &&
        html`<span class="pill" style=${{ color: "var(--bad)" }}
                   title=${"The broker refused this page's reply topic, " +
                           replyRefused(state) + ", so every request that asks " +
                           "for an answer - a point read, a channel seek, the " +
                           "sessions verbs - is accepted and answered nowhere. " +
                           "An acl_file has to grant this client read on " +
                           "viewer-reply/+. The viewer's log says the same."}>
          <span class="dot down"></span>
          replies refused - answers will not arrive
        </span>`}
      <span class="pill">MQTT ${mqttName(state.protocol || "5")}</span>
      <span class="pill"><code>${state.client_id}</code></span>
      ${(() => {
        // **A refused subscription, said from every tab.** The broker answers
        // a filter this credential may not read with a reason code in the
        // SUBACK - an ordinary acknowledgement - so the connection is fine and
        // the only symptom is an empty page. Under an acl_file that is the
        // usual answer to the default `#`. Naming it here is the difference
        // between "the viewer is broken" and "this credential cannot read
        // that". The reply topic is left out: it is refused by the same ACL
        // and is not the setting an operator would change.
        const asked = (state.subscribe || "").length ? [state.subscribe] : [];
        const bad = (state.subscriptions || [])
          .filter(x => !x.granted && asked.some(a => a === x.filter));
        if (!bad.length) return "";
        return html`<span class="pill warnpill"
          title=${`The broker answered "${bad[0].reason}". Narrow broker.mqtt.subscribe `
                  + `to what this credential may read.`}>
          ⚠ subscription refused: <code>${bad[0].filter}</code></span>`;
      })()}
      <nav class="tabs">
        <button class=${tab === "topics" ? "on" : ""}
                onClick=${() => setTab("topics")}>Broker</button>
        <button class=${tab === "dashboard" ? "on" : ""}
                onClick=${() => setTab("dashboard")}>Dashboard</button>
        <button class=${(tab === "alarms" ? "on" : "") + (firing.length ? " hasalarm" : "")}
                onClick=${() => setTab("alarms")}>Alarms${firing.length
                  ? html` <span class="alarmbadge">${firing.length}</span>` : ""}</button>
      </nav>
      <div class="stats">
        <span class="stat"><b>${(state.topics || 0).toLocaleString()}</b> topics</span>
        <span class="stat"><b>${(state.received || 0).toLocaleString()}</b> messages seen</span>
        <button class="iconbtn" title=${"Switch to the " + (theme === "dark" ? "light" : "dark") + " theme"}
                onClick=${toggleTheme}>${theme === "dark" ? "☀" : "☾"}</button>
      </div>
    </header>
    ${tab === "dashboard"
      ? html`<div class="detail detail-dash" style=${{ height: "calc(100vh - var(--header-h))" }}>
          ${dashboards.length
            ? html`<${Dashboards} dashboards=${dashboards} />`
            : html`<${Dashboard} />`}</div>`
      : tab === "alarms"
      ? html`<div class="detail detail-alarms"
                  style=${{ height: "calc(100vh - var(--header-h))", overflow: "auto" }}>
          <${AlarmsView} log=${alarmLog} notify=${notify} notifyNote=${notifyNote}
                         toggleNotify=${toggleNotify} /></div>`
      : html`
    <main style=${{ gridTemplateColumns: `${treeW}px 6px 1fr` }}>
      <div class="tree">
        <button class="sectionlabel sectionfold"
                aria-expanded=${opsOpen ? "true" : "false"}
                title=${opsOpen ? "Fold the broker's own views away"
                                : "What this broker is doing, and what you can do to it"}
                onClick=${() => setOpsOpen(!opsOpen)}>
          <span class=${"twist" + (opsOpen ? " open" : "")}>▶</span>
          <span>Operations</span>
          ${!opsOpen && html`<span class="foldcount">${opsViews.length}</span>`}
        </button>
        ${opsOpen
          ? opsViews.map(([key, label, icon]) => sidebarButton(key, label, icon))
          : // **Folded, the one you are looking at stays.** Otherwise
            // folding the group hides the row that says where you are, and
            // the page reads as though nothing is selected while the panel
            // beside it is plainly showing something.
            opsViews.filter(([key]) => view === key)
                    .map(([key, label, icon]) => sidebarButton(key, label, icon))}
        <div class="sectionlabel" style=${{ paddingTop: "16px" }}>Channels and topics</div>
        <div style=${{ display: "flex", gap: "6px", alignItems: "center",
                       marginBottom: "8px" }}>
          <input ref=${filterBox}
                 value=${treeFilter} onInput=${e => setTreeFilter(e.target.value)}
                 class=${filled ? "filled" : ""}
                 placeholder="filter topics"
                 style=${{ flex: 1, minWidth: 0, fontFamily: "var(--mono)",
                           fontSize: "12px" }} />
          ${treeFilter && html`
            <button class="chip" style=${{ cursor: "pointer" }}
                    title="Empty the filter box"
                    onClick=${() => setTreeFilter("")}>clear</button>`}
        </div>
        ${match && html`
          <p class="why" style=${{ padding: "0 8px 8px", fontSize: "11px" }}>
            ${shownRows.length} of ${rows.length} topics match. Channels with no
            match are hidden while filtering.
          </p>`}
        ${roots.length
          ? roots.map(k => html`
              <${Node} key=${k} name=${k} node=${tree[k]} path=${k} depth=${0}
                       open=${openNow} toggle=${toggle}
                       selected=${selected} select=${setSelected}
                       showQueue=${name => setView({ queue: name })} />`)
          : match
          ? html`<div class="empty">No topic matches <code>${treeFilter}</code>.</div>`
          : html`<div class="empty">Waiting for the first message…</div>`}
        ${roots.length > 0 && !roots.includes("broadcast") && html`
          <p class="why" style=${{ padding: "12px 8px 0", fontSize: "11px" }}>
            No broadcast topics yet. A topic no channel claims is stored
            nowhere, so none replays when this page connects - they appear here
            as messages flow.
          </p>`}
      </div>
      <div class="resizer" onMouseDown=${drag}
           title="Drag to resize; the width is remembered"></div>
      <div class="detail">
        ${view === "feed"      ? html`<${Feed} showTopic=${showTopic} />`
        : view === "publish"   ? html`<${Publish} topic="" standalone=${true} />`
        : view === "users"     ? html`<${Users} />`
        : view === "acl"       ? html`<${Acl} />`
        : view === "config"    ? html`<${ConfigView} />`
        : view === "route"     ? html`<${RouteLookup} />`
        : view === "consumers" ? html`<${Consumers} />`
        : view === "positionlost" ? html`<${PositionLost} />`
        : view === "sessions"  ? html`<${Sessions} />`
        : view === "retained"  ? html`<${Retained} onOpen=${setSelected} />`
        : view === "deadletters" ? html`<${DeadLetters} onOpen=${setSelected} />`
        : typeof view === "object" && view
          ? html`<${QueueView} channel=${view.queue} />`
        : selected
          ? html`
              <div class="panelhead">
                <${Publish} topic=${topic} channel=${channel}
                    kind=${(window.__channels || {})[channel]} />
                ${seekable && html`<${Seek} channel=${channel} onDone=${refresh} />`}
                <div class="panelhead panelhead-inline"
                     style=${{ display: "flex", alignItems: "center", gap: "12px",
                               flexWrap: "wrap" }}>
                  <h2 style=${{ margin: 0 }}>Messages on
                    <span class="topicname">${topic}</span>
                    <span class="sub">${" in "}${channel}</span>
                    ${/* **The state, beside the log, because they are two
                          different facts and the list cannot carry the
                          second.** A retained value is one slot for the
                          topic; the messages below are what arrived and
                          nothing in them was ever stored. So "is there a
                          retained value now" cannot be read off the list -
                          a retained publish reaches a live subscriber with
                          the flag clear, indistinguishable from any other
                          message - and clearing one deletes no message at
                          all. This says it from the store instead. */
                      html`<${RetainedBadge} topic=${topic} />`}</h2>
                  ${(window.__channels || {})[channel] !== "latest" && html`
                    <label class="chip"
                           title=${"How many arrivals this page keeps for each topic. It "
                                  + "resizes the ring on the server, so asking for more "
                                  + "means more actually arrives - not a longer slice of "
                                  + "what was already held."}>keep
                      <select value=${keep}
                              onChange=${e => setKeep(Number(e.target.value))}
                              style=${{ padding: "0 2px", border: "none",
                                        background: "transparent", font: "inherit",
                                        color: "var(--text)" }}>
                        ${(keepChoices.includes(keep) ? keepChoices
                                                      : [keep, ...keepChoices])
                          .map(n => html`<option key=${n} value=${n}>${n}</option>`)}
                      </select></label>
                    ${counts && html`
                      <span class="chip">showing <b>${counts.shown}</b>
                        ${" of "}${counts.held}</span>`}
                    ${counts && counts.cap && counts.held >= counts.cap && html`
                      <span class="chip"
                            title=${"The ring is full, so older arrivals were dropped by "
                                   + "this page rather than by the broker. A larger keep "
                                   + "holds more."}
                        >ring full at ${counts.cap}</span>`}`}
                  <${PayloadControls} pretty=${pretty} setPretty=${setPretty}
                                      copyAs=${copyAs} setCopyAs=${setCopyAs}
                                      decode=${decode} setDecode=${setDecode} />
                  <input value=${msgQ} onInput=${e => setMsgQ(e.target.value)}
                         placeholder=${decode
                           ? "search payloads, deserialized and raw"
                           : "search payloads as they arrived"}
                         title=${"Matches anywhere in the payload, the content type "
                                 + "or a user property. The deserialized record is searched "
                                 + "only while deserializing is switched on."}
                         style=${{ minWidth: "230px", fontFamily: "var(--mono)",
                                   fontSize: "12px" }} />
                  ${msgQ && html`
                    <button class="chip" style=${{ cursor: "pointer" }}
                            onClick=${() => setMsgQ("")}>clear</button>`}
                </div>
              </div>
              <${FieldChart} topic=${topic} />
              <${Messages} topic=${topic} q=${msgQ} onCounts=${setCounts} />`
          : html`<div class="empty">Pick a topic on the left, a queue to see what
              it is holding, or ask who is behind.</div>`}
      </div>
    </main>`}`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
