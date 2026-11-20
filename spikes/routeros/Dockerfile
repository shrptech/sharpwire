# Spike lab image: QEMU (TCG) for CHR arm64, FRR for BGP peers in netns, Python/httpx for REST.
FROM debian:trixie
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      qemu-system-arm qemu-efi-aarch64 qemu-utils iproute2 iputils-ping python3 python3-httpx \
      curl jq expect socat frr openssl procps ca-certificates tcpdump unzip \
 && rm -rf /var/lib/apt/lists/*
