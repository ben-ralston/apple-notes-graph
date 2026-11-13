#!/bin/bash
# Create a stable local self-signed code-signing identity for AppleNotesMCP.app.
#
# Usage: ./apple_notes_mcp_app/sign-setup.sh
#
# Creates a self-signed certificate in the user's login keychain that can be used
# to sign AppleNotesMCP.app with a stable identity across rebuilds. Without this,
# ad-hoc signing causes macOS to re-prompt for FDA every time the .app is rebuilt.
#
# After running, export NOTES_GRAPH_SIGN_IDENTITY in your shell rc:
#   export NOTES_GRAPH_SIGN_IDENTITY="Apple Notes MCP Local Signing"

set -euo pipefail

CERT_NAME="Apple Notes MCP Local Signing"

# Check that a usable codesigning identity exists — not just a certificate.
# `security find-certificate` matches even if the private key is missing, but
# codesign needs the identity to appear in `find-identity -p codesigning`.
if security find-identity -v -p codesigning 2>/dev/null | grep -q "\"$CERT_NAME\""; then
    echo "Codesigning identity already exists in login keychain: $CERT_NAME"
    echo ""
    echo "To use it, add to your shell rc:"
    echo "  export NOTES_GRAPH_SIGN_IDENTITY=\"$CERT_NAME\""
    exit 0
fi

# Note: `find-certificate -c NAME -a` exits 0 with empty output when nothing matches
# (reports "found 0 certs" as success). The non-`-a` form correctly exits 44 on no match.
if security find-certificate -c "$CERT_NAME" >/dev/null 2>&1; then
    echo "WARNING: a certificate named '$CERT_NAME' exists but has no usable"
    echo "         private key for codesigning. Delete it in Keychain Access"
    echo "         (login keychain) and re-run this script to create a fresh one."
    exit 1
fi

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

CONFIG="$TMP_DIR/openssl.cnf"
KEY="$TMP_DIR/signing.key"
CERT="$TMP_DIR/signing.crt"

cat > "$CONFIG" << EOF
[req]
distinguished_name = req_distinguished_name
prompt = no
x509_extensions = v3_ca

[req_distinguished_name]
CN = $CERT_NAME

[v3_ca]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF

# Use /usr/bin/openssl (macOS-native LibreSSL). Homebrew's OpenSSL 3.x generates
# PKCS#12 files with AES-256+PBKDF2 by default, which macOS `security import`
# cannot read (fails with "MAC verification failed").
OPENSSL=/usr/bin/openssl

echo "Generating self-signed code-signing certificate..."
"$OPENSSL" req -new -x509 -nodes \
    -newkey rsa:2048 \
    -keyout "$KEY" \
    -out "$CERT" \
    -days 3650 \
    -config "$CONFIG"

echo "Importing into login keychain..."
# Combine into a PKCS#12 bundle so codesign can find both cert and key.
P12="$TMP_DIR/signing.p12"
"$OPENSSL" pkcs12 -export \
    -in "$CERT" \
    -inkey "$KEY" \
    -out "$P12" \
    -name "$CERT_NAME" \
    -passout pass:notes-graph-local

security import "$P12" \
    -k "$HOME/Library/Keychains/login.keychain-db" \
    -P notes-graph-local \
    -T /usr/bin/codesign

echo ""
echo "Done. Add to your shell rc and reload:"
echo "  export NOTES_GRAPH_SIGN_IDENTITY=\"$CERT_NAME\""
echo ""
echo "Then rebuild: ./run mcp-app-build"
