#!/bin/sh
cd "$(dirname "$0")"
echo "AlphaEdge local preview: http://localhost:8000"
python3 -m http.server 8000
