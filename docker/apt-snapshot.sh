#!/bin/sh
# Point APT at snapshot.debian.org for one timestamp, so `apt-get upgrade`
# resolves to the same package set on every build of a given commit (#324).
#
# The suite comes from the running base image rather than being hard-coded, so a
# Debian release bump in the base needs no change here. Check-Valid-Until is
# disabled because a snapshot's Release file is deliberately older than apt's
# freshness window — that is what makes it a snapshot.
#
# http, not https: apt authenticates packages with the archive's GPG signatures,
# which is what a mirror URL's transport cannot add to, and this runs before
# ca-certificates is guaranteed to be installed. A tampered transport still
# fails the signature check.
set -eu

snapshot="${1:?usage: apt-snapshot <YYYYMMDDTHHMMSSZ>}"
codename="$(. /etc/os-release && echo "${VERSION_CODENAME:?unknown Debian suite}")"
base="http://snapshot.debian.org/archive"

rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources
cat > /etc/apt/sources.list.d/snapshot.list <<EOF
deb ${base}/debian/${snapshot}/ ${codename} main
deb ${base}/debian/${snapshot}/ ${codename}-updates main
deb ${base}/debian-security/${snapshot}/ ${codename}-security main
EOF
cat > /etc/apt/apt.conf.d/99snapshot <<'EOF'
Acquire::Check-Valid-Until "false";
Acquire::Retries "3";
EOF
