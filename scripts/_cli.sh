#!/bin/bash
set -eo pipefail

show-help(){
    echo "USAGE: $(basename "$0") <command> [arguments]"
    echo ""

    # Get all commands
    commands=()
    while IFS='' read -r line; do commands+=("$line"); done < \
        <(compgen -A "function" | grep "run-" | sed "s/run-//")

    # Group commands by category
    echo "Setup:"
    for cmd in initialize; do
        if [[ " ${commands[*]} " =~ " ${cmd} " ]]; then
            printf "  %-20s %s\n" "$cmd" "$(help-for "$cmd")"
        fi
    done

    echo ""
    echo "Notes Graph UI (Electron):"
    for cmd in ui-start ui-build ui-test ui-app-install; do
        if [[ " ${commands[*]} " =~ " ${cmd} " ]]; then
            printf "  %-20s %s\n" "$cmd" "$(help-for "$cmd")"
        fi
    done

    echo ""
    echo "MCP Server (Python):"
    for cmd in mcp-test; do
        if [[ " ${commands[*]} " =~ " ${cmd} " ]]; then
            printf "  %-20s %s\n" "$cmd" "$(help-for "$cmd")"
        fi
    done

    echo ""
    echo "Testing:"
    for cmd in test; do
        if [[ " ${commands[*]} " =~ " ${cmd} " ]]; then
            printf "  %-20s %s\n" "$cmd" "$(help-for "$cmd")"
        fi
    done

    echo ""
    echo "UI Helper Daemon:"
    for cmd in ui-helper-start ui-helper-install ui-helper-uninstall ui-helper-status; do
        if [[ " ${commands[*]} " =~ " ${cmd} " ]]; then
            printf "  %-20s %s\n" "$cmd" "$(help-for "$cmd")"
        fi
    done

    echo ""
    echo "Apple Notes MCP .app (Claude Desktop FDA wrapper):"
    for cmd in mcp-app-build mcp-app-install mcp-app-uninstall mcp-app-status mcp-app-spike; do
        if [[ " ${commands[*]} " =~ " ${cmd} " ]]; then
            printf "  %-20s %s\n" "$cmd" "$(help-for "$cmd")"
        fi
    done

    echo ""
    echo "Demo library (synthetic notes):"
    for cmd in demo-build demo-ui demo-mcp; do
        if [[ " ${commands[*]} " =~ " ${cmd} " ]]; then
            printf "  %-20s %s\n" "$cmd" "$(help-for "$cmd")"
        fi
    done

    echo ""
    echo "Remote connector (claude.ai, issue #167):"
    for cmd in remote-start remote-check remote-revoke-tokens remote-allow-client remote-writes remote-kill remote-plist; do
        if [[ " ${commands[*]} " =~ " ${cmd} " ]]; then
            printf "  %-20s %s\n" "$cmd" "$(help-for "$cmd")"
        fi
    done
}

name=$1
case "$name" in
    "" | "-h" | "--help" | "help")
        show-help
        ;;
    *)
        shift
        if compgen -A "function" | grep "run-$name" >/dev/null ; then
            # Check for per-command help flag
            if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
                help-detail "$name"
            else
                run-"${name}" "$@"
            fi
        else
            echo "ERROR: run-$name not found."
            exit 123
        fi
        ;;
esac
