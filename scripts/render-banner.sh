#!/usr/bin/env bash
# Render docs/assets/banner.svg -> .github/banner.png with librsvg (ImageMagick's
# built-in SVG renderer drops gradients). Uses host rsvg-convert if present,
# otherwise a throwaway pinned ubuntu:26.04 container.
set -euo pipefail
cd "$(dirname "$0")/.."

if command -v rsvg-convert >/dev/null 2>&1; then
	rsvg-convert -w 1200 -h 400 -o .github/banner.png docs/assets/banner.svg
else
	docker run --rm -e DEBIAN_FRONTEND=noninteractive -v "$PWD:/w" -w /w ubuntu:26.04 bash -c \
		"apt-get update -qq >/dev/null \
		 && apt-get install -y -qq --no-install-recommends librsvg2-bin fonts-dejavu-core >/dev/null \
		 && rsvg-convert -w 1200 -h 400 -o .github/banner.png docs/assets/banner.svg \
		 && chown $(id -u):$(id -g) .github/banner.png"
fi
echo ".github/banner.png rendered"
