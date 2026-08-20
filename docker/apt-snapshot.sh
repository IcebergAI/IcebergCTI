#!/bin/sh
# Point APT at snapshot.debian.org for one timestamp, so `apt-get upgrade`
# resolves to the same package set on every build of a given commit (#324).
#
# The suite comes from the running base image rather than being hard-coded, so a
# Debian release bump in the base needs no change here.
#
# Two controls, covering two different attacks, and neither substitutes for the
# other:
#
#   * APT's GPG verification authenticates that a Release/Packages set was
#     published by Debian. It says nothing about *which* published state this
#     is, so it cannot detect a replay.
#   * Check-Valid-Until is the anti-replay control, and a snapshot has to
#     disable it: a snapshot's Release file is deliberately older than apt's
#     freshness window, which is what makes it a snapshot.
#
# That leaves the timestamp itself needing to be authenticated, which is why
# this is https and must stay https. TLS binds the response to the host and
# path that were requested, so an attacker cannot answer a request for this
# snapshot with a different — validly signed, validly stale — archive state.
# Over http they could, and the build would accept it: signatures verify,
# freshness is off, and the image quietly ends up with an older package set
# than APT_SNAPSHOT claims, including the vulnerable versions the upgrade in
# the Dockerfile exists to remove.
#
# If the base image ever ships without ca-certificates, this fails the build
# rather than degrading to http. That is the intended direction to fail.
set -eu

snapshot="${1:?usage: apt-snapshot <YYYYMMDDTHHMMSSZ>}"
codename="$(. /etc/os-release && echo "${VERSION_CODENAME:?unknown Debian suite}")"
base="https://snapshot.debian.org/archive"

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
