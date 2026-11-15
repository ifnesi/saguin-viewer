# saguin-viewer - Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com), and
this project adheres to [Semantic Versioning](https://semver.org):
MAJOR.MINOR.PATCH.

- **MAJOR** - incompatible or breaking changes
- **MINOR** - new, backward-compatible functionality
- **PATCH** - backward-compatible bug fixes

The newest release is listed first. Dates are ISO 8601 (YYYY-MM-DD).
Categories: Added, Changed, Deprecated, Removed, Fixed, Security.

**A section here means a tag on GitHub.** Nothing is recorded before it is
released, so what is in flight lives in `git log` until it ships under a
version. The tag is also the version: the release workflow publishes
`ghcr.io/ifnesi/saguin-viewer:<version>` from `v<version>`, and no file in
the repository carries a version number of its own.

Each entry says which viewer it concerns: the browser viewer (`web/`) or the
terminal viewer (`cmd/saguin-viewer/`).

## 0.2.1 - 2026-11-15

Fixes to the browser viewer against a Sagüin 0.2.0 broker, most of them
from one cause: 0.2.0 lists `$broadcast` among its channels
(`saguin_channel_info{channel="$broadcast",type="append",filter="#"}`), and
the viewer read it as an ordinary channel whose filter claims every topic.
The terminal viewer is unchanged.

### Added

- Browser: the left menu can be hidden, giving the topic the full width.
  `«` at the right of the Operations row hides it and `»` brings it back.
  The choice is remembered per browser; on a screen narrower than 700px
  with nothing remembered, the menu starts hidden.

### Changed

- Browser: the subscription asks for Retain As Published on MQTT 5, so a
  retained publish arrives with its RETAIN flag set instead of cleared.
  A retained value published by another client now reaches the retained
  panel as it arrives, not at the viewer's next reconnect.
- Browser: the `retained` tag marks the topic's current retained value -
  the newest message published with RETAIN, while the topic holds one -
  and moves with each retained publish. In the tree it says whether the
  topic holds a retained value, so a later plain publish no longer hides
  it.
- Browser: on a screen narrower than 700px the header stays one row with
  the tabs in it, leaving out the title (the logo stays), the protocol and
  client id, the counts, and the connection pill's word (its dot stays).
- Browser: `/api/route` reports a queue's topics as readable: a
  subscription to them is a reader, as on `append`.

### Fixed

- Browser: the retained panel listed nothing and the Operations badge
  read 0 on a 0.2.0 broker, while the broker held retained values. A
  retained publish from the Publish form was not recorded, and an empty
  one was not recognised as a clear.
- Browser: Seek was offered on `$broadcast`, which the broker refuses
  (0x90). It is no longer offered, and `/api/seek` refuses it without
  publishing.
- Browser: a seek the broker refused (`beyond-end`, `below-floor`,
  permission, unknown channel) emptied that channel's topics on the page
  until the viewer reconnected. The topics are now set aside and put back
  when the PUBACK refuses; an accepted seek still clears them for its
  replay.
- Browser: lowering `keep` kept the oldest messages of each topic and
  dropped the newest.
- Browser: opening a row of the retained panel selected `broadcast/<topic>`
  while the tree files it under `$broadcast/<topic>`.
- Browser: "No broadcast topics yet" was shown beside the broadcast topics
  it said were missing.
- Browser: links other than a topic's name kept the browser's default
  blue, close to unreadable on the dark theme.
- Browser: with the menu shown on a phone, a width dragged on a desktop
  put the menu and its hide button past the screen's edge.

## 0.2.0 - 2026-11-14

For the Sagüin 0.2.0 broker, and only for it: this version does not work
with rc1.

### Added

- Browser: the image is published for linux/arm64 as well as linux/amd64,
  as `<version>-amd64` and `<version>-arm64` joined into one index for the
  version and `latest`.
- Browser: dashboards Process, Clients, Publishing, Delivery, Channels,
  Queues, Store, Bridges and Runtime.

### Changed

- Both viewers read the 0.2.0 broker: its design and metrics documents
  (`docs/design.md`, `docs/metrics.md`) instead of rc1's RFCs, and its one
  store instead of named providers.
- Browser: a refused request is shown in the broker's own words
  (`below-floor`, `malformed`, ...).
- Links, the broker checkout the tests use (`../saguin`) and the release
  named in the documents follow the broker's public repository,
  github.com/ifnesi/saguin.
- CI actions run on Node 24, and CI builds the broker with the Go its
  `go.mod` names.

### Removed

- Browser: dashboards Traffic, Storage, Bridge and QoS 2, replaced by the
  ones above.

### Fixed

- Browser: a refused seek or point read waited five seconds and reported
  "no reply"; it now reports the refusal from the PUBACK as it arrives. A
  seek refused 0x87 names the `acl_file` grant it needs.

## 0.1.0 - 2026-10-04

The first release: two viewers for a Sagüin 0.1.0-rc.1 broker, each a
client of the broker rather than part of it. They read its operations
listener and its MQTT `$saguin` topics, share no code and no dependencies,
and each carries its own README.

### Added

- Browser viewer (`web/`): Flask serving static React, also published as
  the image `ghcr.io/ifnesi/saguin-viewer`. A topic tree by channel; the
  messages on any topic, decoded through the broker's schema registry; a
  live feed; a publish form; a channel replay (seek); sessions, and
  disconnecting one; retained messages; dead letters, and putting one
  back on its queue; consumers behind and records lost to retention;
  where a topic lands; the resolved configuration, users and ACL; and
  dashboards and alarms from YAML files, with seven built in.
- Terminal viewer (`cmd/saguin-viewer/`): one static Go binary that draws
  the broker's numbers from its operations listener over SSH, for a box
  with no browser and no Prometheus. `--once` prints one screen; a
  dashboard file chooses what is drawn.
- TLS and mutual TLS to both of the broker's listeners.
