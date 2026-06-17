# Materialize SkyPilot cloud credentials for clouds that require $HOME-rooted
# files. Sourced by [feature.live-skypilot.activation.scripts] on every pixi
# env enter, so the files self-repair after container rebuilds.
#
# Reads from already-exported env vars (.env is loaded by python-dotenv +
# pixi's own `.env` handling before activation). Missing keys silently skip —
# operator can run `sky check` to discover what's still unset.
#
# IMPORTANT: this file is shell-sourced. Never `exit` — only `return`. Never
# write to stdout when invoked at activation (pixi shows the output verbatim;
# noise hides real errors).

_kf_write_lambda_key() {
    if [ -z "${LAMBDA_API_KEY:-}" ]; then
        return 0
    fi
    mkdir -p "$HOME/.lambda_cloud"
    chmod 700 "$HOME/.lambda_cloud"
    printf 'api_key = %s\n' "$LAMBDA_API_KEY" > "$HOME/.lambda_cloud/lambda_keys"
    chmod 600 "$HOME/.lambda_cloud/lambda_keys"
}

_kf_write_vast_key() {
    if [ -z "${VAST_API_KEY:-}" ]; then
        return 0
    fi
    mkdir -p "$HOME/.config/vastai"
    chmod 700 "$HOME/.config/vastai"
    printf '%s\n' "$VAST_API_KEY" > "$HOME/.config/vastai/vast_api_key"
    chmod 600 "$HOME/.config/vastai/vast_api_key"
}

_kf_write_lambda_key
_kf_write_vast_key

unset -f _kf_write_lambda_key _kf_write_vast_key
