#!/bin/zsh

setopt pipefail

# Return success when the service is running in either launchd domain used for
# per-user agents. Ventura and later may register a LaunchAgent in user/<uid>
# instead of gui/<uid>, while older/existing installations can still use gui.
uid="${1:-$(id -u)}"
label="${2:-ai.hermes.gateway}"
launchctl_bin="${WATCHDOG_LAUNCHCTL:-launchctl}"

for domain in "user/$uid" "gui/$uid"; do
  if "$launchctl_bin" print "$domain/$label" 2>/dev/null \
      | grep -q 'state = running'; then
    exit 0
  fi
done

exit 1
