#!/usr/bin/env bash
cd "$(dirname "$0")"
uvicorn backend.app:app --host 0.0.0.0 --port 8080
