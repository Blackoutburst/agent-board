#!/usr/bin/env bash
# Run on the home server after copying source to /home/ubuntu/collaboration-board.
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then
    echo "This installer requires root to install a system service and edit nginx. Run it through your existing authorized root access." >&2
    exit 1
fi
board_source=/home/ubuntu/collaboration-board
board_data=/home/ubuntu/.local/share/collaboration-board
board_uv=/home/ubuntu/.local/bin/uv
board_site=/etc/nginx/sites-available/files.blackoutburst.com
board_stamp=$(date -u +%Y%m%dT%H%M%SZ)
board_candidate="${board_site}.board-candidate-${board_stamp}"
board_previous="${board_site}.before-board-${board_stamp}"
board_site_changed=0
board_install_complete=0
restore_site_on_failure() {
    if [[ "$board_site_changed" -eq 1 && "$board_install_complete" -eq 0 ]]; then
        cat -- "$board_previous" > "$board_site"
        echo "Board installation failed; previous nginx vhost restored. Inspect the board service before retrying." >&2
    fi
}
trap restore_site_on_failure EXIT
for required in "${board_source}/app.py" "${board_source}/requirements.txt" "${board_site}"; do
    [[ -f "$required" ]] || { echo "Missing required file: $required" >&2; exit 1; }
done
nginx -t
systemctl is-active --quiet file-manager.service || { echo "File Browser must be running before board deployment." >&2; exit 1; }
python3 "${board_source}/deploy/configure_nginx.py" --site "$board_site" --location "${board_source}/deploy/nginx-location.conf" --output "$board_candidate"
install -d -o ubuntu -g ubuntu -m 0700 "$board_data" "${board_data}/attachments"
if [[ ! -x "${board_source}/.venv/bin/python" ]] || ! runuser -u ubuntu -- "${board_source}/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
    if [[ -x "$board_uv" ]]; then
        runuser -u ubuntu -- "$board_uv" venv --seed --allow-existing --no-python-downloads --python /usr/bin/python3 "${board_source}/.venv"
    elif ! runuser -u ubuntu -- python3 -m venv "${board_source}/.venv"; then
        echo "Virtual environment bootstrap failed. This server may lack ensurepip/python3-venv. Use your authorized administration route to provide Python venv support or install uv for ubuntu, then retry. No system packages were installed." >&2
        exit 1
    fi
fi
runuser -u ubuntu -- "${board_source}/.venv/bin/python" -m pip install -r "${board_source}/requirements.txt"
if [[ ! -e /etc/collaboration-board.env ]]; then
    install -o root -g ubuntu -m 0640 "${board_source}/deploy/board.env.example" /etc/collaboration-board.env
fi
install -o root -g root -m 0644 "${board_source}/deploy/collaboration-board.service" /etc/systemd/system/collaboration-board.service
cp -p -- "$board_site" "$board_previous"
board_site_changed=1
cat -- "$board_candidate" > "$board_site"
if ! nginx -t; then
    cat -- "$board_previous" > "$board_site"
    echo "Candidate failed nginx validation; existing configuration restored." >&2
    exit 1
fi
systemctl daemon-reload
systemctl enable collaboration-board.service
systemctl restart collaboration-board.service
if ! curl --fail --silent --show-error --retry 10 --retry-delay 1 --retry-connrefused http://127.0.0.1:9140/health >/dev/null; then
    cat -- "$board_previous" > "$board_site"
    echo "Board health check failed; nginx configuration restored. Inspect journalctl -u collaboration-board." >&2
    exit 1
fi
systemctl reload nginx
board_install_complete=1
echo "Board service active. Board authenticates its own /board/ routes; existing auth gate retained elsewhere. Previous vhost: ${board_previous}"
echo "Verify browser login and authenticated MCP ingress before handing out the public URL."
