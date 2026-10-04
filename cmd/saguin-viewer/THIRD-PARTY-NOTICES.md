# Third-party notices

Attribution for code this repository does not own. Two sections, because the two
viewers redistribute different things in different ways - and one paragraph at
the end about what is deliberately not here.

`saguin-viewer --licenses` prints this file. **That matters more than it looks
like it does**: the terminal viewer ships as one static binary, copied onto a box
that has no clone of this repository, so a notice left behind in git is not a
notice that travelled with the thing it describes.

**Which is also why the file is here rather than at the repository root**, where
a reader would look for it first - `go:embed` cannot reach above its own package
directory, so a root file could not travel inside the binary at all. The root
README links here, and the browser section below is carried along with the Go
one: a binary that prints everything the project redistributes says more than it
must, which is the harmless direction.

## In the terminal binary

Three Go modules are linked into `saguin-viewer`, so their licences travel with
every copy of it. The list is what `go list -deps ./cmd/saguin-viewer` reports,
and a test in `internal/viewer` holds this file to it: adding a dependency fails
that test until its licence is here, and the text below is compared against the
module cache rather than trusted, so a mangled licence fails too.

**`gopkg.in/yaml.v3` has a NOTICE file, and Apache-2.0 section 4(d) requires
carrying its contents.** That is a hard requirement rather than a courtesy, and
it is the reason this file is not optional. **`golang.org/x/term` and
`golang.org/x/sys` carry a PATENTS file**, which is part of the grant Go's own
libraries are offered under and travels for the same reason.

### golang.org/x/term v0.45.0

#### LICENSE

```
Copyright 2009 The Go Authors.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

   * Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
   * Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
   * Neither the name of Google LLC nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

#### PATENTS

```
Additional IP Rights Grant (Patents)

"This implementation" means the copyrightable works distributed by
Google as part of the Go project.

Google hereby grants to You a perpetual, worldwide, non-exclusive,
no-charge, royalty-free, irrevocable (except as stated in this section)
patent license to make, have made, use, offer to sell, sell, import,
transfer and otherwise run, modify and propagate the contents of this
implementation of Go, where such license applies only to those patent
claims, both currently owned or controlled by Google and acquired in
the future, licensable by Google that are necessarily infringed by this
implementation of Go.  This grant does not include claims that would be
infringed only as a consequence of further modification of this
implementation.  If you or your agent or exclusive licensee institute or
order or agree to the institution of patent litigation against any
entity (including a cross-claim or counterclaim in a lawsuit) alleging
that this implementation of Go or any code incorporated within this
implementation of Go constitutes direct or contributory patent
infringement, or inducement of patent infringement, then any patent
rights granted to you under this License for this implementation of Go
shall terminate as of the date such litigation is filed.
```

### golang.org/x/sys v0.47.0

#### LICENSE

```
Copyright 2009 The Go Authors.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

   * Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
   * Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
   * Neither the name of Google LLC nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

#### PATENTS

```
Additional IP Rights Grant (Patents)

"This implementation" means the copyrightable works distributed by
Google as part of the Go project.

Google hereby grants to You a perpetual, worldwide, non-exclusive,
no-charge, royalty-free, irrevocable (except as stated in this section)
patent license to make, have made, use, offer to sell, sell, import,
transfer and otherwise run, modify and propagate the contents of this
implementation of Go, where such license applies only to those patent
claims, both currently owned or controlled by Google and acquired in
the future, licensable by Google that are necessarily infringed by this
implementation of Go.  This grant does not include claims that would be
infringed only as a consequence of further modification of this
implementation.  If you or your agent or exclusive licensee institute or
order or agree to the institution of patent litigation against any
entity (including a cross-claim or counterclaim in a lawsuit) alleging
that this implementation of Go or any code incorporated within this
implementation of Go constitutes direct or contributory patent
infringement, or inducement of patent infringement, then any patent
rights granted to you under this License for this implementation of Go
shall terminate as of the date such litigation is filed.
```

### gopkg.in/yaml.v3 v3.0.1

#### LICENSE

```

This project is covered by two different licenses: MIT and Apache.

#### MIT License ####

The following files were ported to Go from C files of libyaml, and thus
are still covered by their original MIT license, with the additional
copyright staring in 2011 when the project was ported over:

    apic.go emitterc.go parserc.go readerc.go scannerc.go
    writerc.go yamlh.go yamlprivateh.go

Copyright (c) 2006-2010 Kirill Simonov
Copyright (c) 2006-2011 Kirill Simonov

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

### Apache License ###

All the remaining project files are covered by the Apache license:

Copyright (c) 2011-2019 Canonical Ltd

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

#### NOTICE

```
Copyright 2011-2016 Canonical Ltd.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

## Served to the browser

Three files in `web/static/lib/` are third-party code kept in this repository
rather than pulled from a CDN, so the page works on a machine with no route to
the internet - which is most of the places a broker like this one runs. Being in
the repository means this repository redistributes them, and a test in
`web/tests` holds this file to the contents of that directory: a fourth library
fails it until its licence is here.

Two things were wrong before this file existed, and both are why it does:

* **`htm.js` carried no notice at all.** Minification stripped it, so a
  third-party file was served to every browser with nothing saying whose it was.
* **React's own header says its licence is "in the LICENSE file in the root
  directory of this source tree"**, and the LICENSE file in this repository's
  root is Apache-2.0 - saguin-viewer's own licence, not React's. That pointer
  now resolves to something true, here.

### react 18.3.1 and react-dom 18.3.1

`web/static/lib/react.production.min.js` and
`web/static/lib/react-dom.production.min.js` are the UMD production builds,
byte-identical to `react@18.3.1/umd/react.production.min.js` and
`react-dom@18.3.1/umd/react-dom.production.min.js` as published. Both are one
project under one licence.

```
MIT License

Copyright (c) Facebook, Inc. and its affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### htm, the UMD build

`web/static/lib/htm.js` is htm's `dist/htm.umd.js`, renamed when it was
vendored. Licensed **Apache-2.0**, which is the licence npm reports for the
package and the text its own LICENSE file carries, whose appendix reads
`Copyright 2018 Google Inc.`

**The version cannot be recovered from the file**, and that is worth stating
rather than guessing at: `dist/htm.umd.js` is byte-identical across htm 3.0.4,
3.1.0 and 3.1.1, so the bytes narrow it to that range and no further. Nothing
recorded which was vendored. Attribution does not need the version - the
copyright holder and the licence are what Apache-2.0 asks for, and both are
known - but the next library kept here should have its version written down at
the time, because this is the one fact that cannot be recovered afterwards.

Its licence is the same Apache-2.0 text as this repository's own, in
[LICENSE](LICENSE), with htm's copyright line rather than saguin-viewer's:

```
Copyright 2018 Google Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

## What is deliberately not here

**The web viewer's Python packages.** Flask, PyYAML, paho-mqtt, grpcio-tools and
fastavro are named in `web/requirements.txt` and fetched by pip at install time,
each arriving with its own licence metadata. This repository redistributes none
of them, so listing them here would be a second list to keep in step with
`requirements.txt` - and the first of two lists to drift is the one nobody runs.

Their absence is a decision, written down so that it is not read as an omission
and corrected.
