#!/bin/sh
# Runs inside the container. It reads a registration token from standard input, registers this
# container with the repository as a runner that takes exactly one job, runs that job and exits.
# The token comes on standard input and not in the environment, so the job cannot read it.
set -eu

IFS= read -r token
./config.sh --unattended --ephemeral --replace --no-default-labels \
  --url "https://github.com/${RUNNER_REPOSITORY}" \
  --token "$token" \
  --name "$RUNNER_NAME" \
  --labels "$RUNNER_LABEL"
unset token

exec ./run.sh
