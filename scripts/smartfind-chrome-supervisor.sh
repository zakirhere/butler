#!/bin/zsh

PROFILE="/Users/zakir/zakbot/butler/data/smartfind-profile"
CDP_URL="http://127.0.0.1:9222/json/version"

while true; do
    if ! /usr/bin/curl --max-time 2 -fsS "$CDP_URL" >/dev/null 2>&1; then
        /usr/bin/open -na "Google Chrome" --args \
            --remote-debugging-port=9222 \
            --user-data-dir="$PROFILE" \
            --no-first-run \
            --no-default-browser-check \
            "https://milpitas.eschoolsolutions.com/ui/#/substitute/jobs/available" \
            >/dev/null 2>&1
        # Chrome may take a while to create the remote-debugging endpoint.
        # Do not launch another instance while this one is starting.
        for _ in {1..12}; do
            /bin/sleep 5
            /usr/bin/curl --max-time 2 -fsS "$CDP_URL" >/dev/null 2>&1 && break
        done
        /bin/sleep 10
    fi
    while /usr/bin/curl --max-time 2 -fsS "$CDP_URL" >/dev/null 2>&1; do
        /bin/sleep 10
    done
    /bin/sleep 2
done
