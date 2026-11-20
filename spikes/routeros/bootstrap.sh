#!/bin/sh
# Runs inside the lab container after `/system reset-configuration no-defaults=yes`.
# Bootstrap = what docs/05 says is pre-provisioned: mgmt address + REST over HTTPS (self-signed).
SOCK=${1:-/lab/core.console}; IP=${2:-192.168.88.1}
python3 /spike/console.py "$SOCK" 'Spike-Adm1n!' \
  "/ip address add address=$IP/24 interface=ether1" \
  "/certificate add name=spike-ca common-name=spike-ca key-usage=key-cert-sign,crl-sign days-valid=3650" \
  "/certificate sign spike-ca" \
  "/certificate add name=https common-name=$IP subject-alt-name=IP:$IP days-valid=3650" \
  "/certificate sign https ca=spike-ca" \
  "/ip service set www-ssl certificate=https disabled=no tls-version=only-1.2" \
  "/ip service disable telnet,ftp,www,api,api-ssl,winbox" \
  "/ip service print" "/certificate print"
