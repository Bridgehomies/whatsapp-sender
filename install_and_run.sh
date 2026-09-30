#!/bin/bash

echo ""
echo "  ========================================"
echo "   WhatsApp Bulk Sender — Mac Setup"
echo "  ========================================"
echo ""

# Check Python3
if ! command -v python3 &>/dev/null; then
    echo "  [ERROR] Python 3 not found."
    echo "  Install it from: https://www.python.org/downloads/"
    exit 1
fi

echo "  [OK] Python found: $(python3 --version)"
echo "  [..] Installing dependencies..."
echo ""

pip3 install customtkinter pillow --quiet --upgrade

echo ""
echo "  [OK] Dependencies ready."
echo "  [..] Launching app..."
echo ""

# Run from script's directory
cd "$(dirname "$0")"
python3 app.py
