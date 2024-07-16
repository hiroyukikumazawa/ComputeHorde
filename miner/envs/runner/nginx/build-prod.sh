#!/bin/bash
set -eux -o pipefail

IMAGE_NAME="andreeareef/compute-horde-miner-nginx:v0-latest"
docker build --platform=linux/amd64 -t "$IMAGE_NAME" .
