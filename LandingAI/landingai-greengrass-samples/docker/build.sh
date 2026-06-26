#!/bin/bash
set -e

docker build -t "${ECR_REPO}/[my-image]${1}" .
