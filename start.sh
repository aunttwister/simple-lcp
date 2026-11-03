#!/bin/bash
cd /your/data/docker-apps/lcp
docker compose up -d --remove-orphans 2>&1
echo "EXIT:$?"
