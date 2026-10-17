# Reproducible build of the lease-gated Session Router exit for Ubuntu 24.04 x86_64.
#
#   podman build --network host -f docker/Dockerfile --target out -o dist .
#   (or: docker build -f docker/Dockerfile --target out -o dist .)
#
# Base image pinned by digest. APT_SNAPSHOT pins the Ubuntu archive to a point in time
# (snapshot.ubuntu.com) so the same inputs give the same binary; scripts/make-release.sh
# records the value used. Leave it empty to build against the current archive.
ARG BASE=docker.io/library/ubuntu:24.04@sha256:f610ab94648195aa356059f5b41d6085c9d4d903c072430cdd1af7bdb646106b
FROM ${BASE} AS build
ARG APT_SNAPSHOT=
ARG JOBS=4
ENV DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 TZ=UTC
RUN set -eux; \
    apt-get update -q && apt-get install -y -q --no-install-recommends ca-certificates; \
    if [ -n "$APT_SNAPSHOT" ]; then \
      echo "APT::Snapshot \"$APT_SNAPSHOT\";" > /etc/apt/apt.conf.d/50snapshot; \
    fi; \
    apt-get update -q
COPY scripts/build-session-router.sh /src/scripts/build-session-router.sh
RUN set -eu; pkgs=$(/src/scripts/build-session-router.sh --print-deps --tests); \
    apt-get install -y -q --no-install-recommends $pkgs
COPY patches /src/patches
COPY contrib /src/contrib
COPY scripts /src/scripts
RUN /src/scripts/build-session-router.sh --tests --jobs "$JOBS" --out /out --work /build

FROM scratch AS out
COPY --from=build /out/ /
