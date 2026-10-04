# saguin-viewer, as an image of its own.
#
# **It lives beside the program rather than in a stack that uses it.** The
# viewer is a client of any saguin, and a demo stack pointing it at a broker
# is one deployment of it - so the image is defined here and a stack builds
# from this file. Written the other way round, running the viewer against
# your own broker would mean building from an example's Dockerfile.
#
# Build from the repository root, because the build context needs this
# directory:
#
#   docker build -f web/Dockerfile -t saguin-viewer .
#
# and run it against a broker of your own, overriding as much or as little
# as you need - every setting in saguin-viewer.yaml is `${VAR:-default}`:
#
#   docker run --rm -p 8080:8080 \
#     -e SAGUIN_VIEWER_LISTEN=0.0.0.0:8080 \
#     -e SAGUIN_MQTT_HOST=broker.example.com \
#     -e SAGUIN_OPS_URL=http://broker.example.com:9090 \
#     -e SAGUIN_OPS_USER=operator -e SAGUIN_OPS_PASSWORD="$OPS_PASSWORD" \
#     saguin-viewer
#
# **`0.0.0.0` in a container and nowhere else.** The viewer defaults to
# loopback, which is right for an operator running it on their own machine
# and wrong inside a container - loopback there is the container, so the
# published port would reach nothing.

# **Debian slim rather than Alpine, and that is the deserializers' doing.**
# grpcio-tools publishes manylinux wheels and no musl ones, so on Alpine it
# compiles protoc from source for about ten minutes and the image ends up
# carrying a build toolchain. On this base both deserializers arrive as wheels in
# seconds. The cost is a larger base; the alternative was an image that could
# not read the payloads the registry exists to describe.
FROM docker.io/library/python:3.13-slim

# **Installed from web/requirements.txt rather than listed again here.**
# A second copy of a dependency set is a second thing to keep in step, and
# the first one to drift silently is the one nobody runs - this image is
# built far less often than the file is edited. The pins and the reasons for
# them are in that file, which is the viewer's own now that the viewer has
# its own repository: it shared saguin's examples/requirements.txt with the
# guided tour, and a tour in another repository is not a dependency of this.
COPY web/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

WORKDIR /viewer
COPY web/app.py ./app.py
COPY web/saguin-viewer.yaml ./saguin-viewer.yaml
COPY web/static ./static
# **The dashboards, and this line is why the image did not start.** The
# configuration above names seven dashboard files, and a dashboard file that is
# not there stops the viewer at startup - deliberately, so a bad card is a
# refusal rather than a blank tab. They arrived the day after this file was
# last edited and nobody extended it, so every run of the image exited on
# `dashboard "Default": no such file` until it was assembled and run.
#
# `dashboard:` is also the one setting a container cannot work around: it is a
# mapping rather than a `${VAR:-default}` string, so no environment variable
# can point it elsewhere or empty it.
#
# The list above is enumerated rather than `COPY . .` so the image carries no
# tests and no caches - and enumeration is exactly what drifted here, which is
# why TheImageCarriesWhatTheConfigNeeds in the test suite now holds this file to
# the configuration beside it.
COPY web/dashboards ./dashboards

USER nobody
EXPOSE 8080

# The configuration file, with the environment supplying whatever should
# differ. Named explicitly rather than relied on: the program would find it
# beside itself anyway, and a reader of this file should not have to know
# that to see which file is being read.
ENTRYPOINT ["python3", "-u", "app.py", "saguin-viewer.yaml"]
