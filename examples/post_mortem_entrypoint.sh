#!/bin/sh
# Invoked as Dockerfile entrypoint. This script starts a tmux session
# running the Python script and keeps the container alive.
set -e

# tmux runs the script in detached mode
echo "Starting tmux session..."
tmux new -d -s pm_session "python post_mortem_example.py"

# Keep the container running by executing the main process passed to Docker
# or, if none is passed, wait indefinitely.
tail -f /dev/null
