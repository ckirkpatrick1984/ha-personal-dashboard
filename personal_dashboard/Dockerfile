ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG GLANCE_VERSION=0.8.6
ARG VIKUNJA_VERSION=2.6.0

# Checksums verified out-of-band before this file was written. Pinning them
# means a silently re-tagged upstream release will fail the build instead of
# being installed.
ARG GLANCE_SHA256_aarch64=8887f3ffbaa7083713ea51ed6d476d1d68d307836d43a5146fbeb8fd1fbd6513
ARG GLANCE_SHA256_amd64=d27fb887eece24859f6ed3881ebb28c9d0a4fe6ae01acfa31bb39543fbf69784
ARG VIKUNJA_SHA256_aarch64=b30e0ef2ba86ed1138a53097dc386eb0ac42483f85526325687f909c3430477b
ARG VIKUNJA_SHA256_amd64=44f55c5620cf90f9f125ee2d2fda31db1946230da6d1dbc0f7943aed68db4264

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apk add --no-cache curl unzip tzdata python3

RUN set -euo pipefail; \
    case "${BUILD_ARCH}" in \
      aarch64) REL_ARCH=arm64; \
               GLANCE_SHA="${GLANCE_SHA256_aarch64}"; \
               VIKUNJA_SHA="${VIKUNJA_SHA256_aarch64}" ;; \
      amd64)   REL_ARCH=amd64; \
               GLANCE_SHA="${GLANCE_SHA256_amd64}"; \
               VIKUNJA_SHA="${VIKUNJA_SHA256_amd64}" ;; \
      *) echo "Unsupported architecture: ${BUILD_ARCH}" >&2; exit 1 ;; \
    esac; \
    \
    curl -fsSL -o /tmp/glance.tar.gz \
      "https://github.com/glanceapp/glance/releases/download/v${GLANCE_VERSION}/glance-linux-${REL_ARCH}.tar.gz"; \
    echo "${GLANCE_SHA}  /tmp/glance.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/glance.tar.gz -C /usr/local/bin glance; \
    chmod +x /usr/local/bin/glance; \
    \
    curl -fsSL -o /tmp/vikunja.zip \
      "https://github.com/go-vikunja/vikunja/releases/download/v${VIKUNJA_VERSION}/vikunja-v${VIKUNJA_VERSION}-linux-${REL_ARCH}-full.zip"; \
    echo "${VIKUNJA_SHA}  /tmp/vikunja.zip" | sha256sum -c -; \
    unzip -j -o /tmp/vikunja.zip -d /tmp/vikunja; \
    mv "/tmp/vikunja/vikunja-v${VIKUNJA_VERSION}-linux-${REL_ARCH}" /usr/local/bin/vikunja; \
    chmod +x /usr/local/bin/vikunja; \
    \
    rm -rf /tmp/glance.tar.gz /tmp/vikunja.zip /tmp/vikunja; \
    /usr/local/bin/glance --version; \
    /usr/local/bin/vikunja version

COPY rootfs /

# The digest sidecar is stdlib-only by design: no pip, no wheels, no build
# toolchain in the image. Fail the build rather than ship a syntax error that
# would only surface as a dead service at runtime.
RUN python3 -m compileall -q /usr/lib/digest

RUN chmod +x \
      /etc/s6-overlay/s6-rc.d/init-dashboard/run \
      /etc/s6-overlay/s6-rc.d/vikunja/run \
      /etc/s6-overlay/s6-rc.d/vikunja/finish \
      /etc/s6-overlay/s6-rc.d/digest/run \
      /etc/s6-overlay/s6-rc.d/digest/finish \
      /etc/s6-overlay/s6-rc.d/glance/run \
      /etc/s6-overlay/s6-rc.d/glance/finish

LABEL \
  io.hass.name="Personal Dashboard" \
  io.hass.description="Glance dashboard plus Vikunja task manager" \
  io.hass.type="addon" \
  io.hass.arch="aarch64|amd64"
