#!/bin/sh
# Starts as root only to align the `app` account with PUID/PGID and own the data mount,
# then re-executes itself as that user. Under rootless podman with --userns=keep-id the
# container already starts unprivileged and the switch is skipped.
set -eu

data_dir="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
	puid="${PUID:-1000}"
	pgid="${PGID:-1000}"
	if [ "$(id -u app)" != "${puid}" ] || [ "$(id -g app)" != "${pgid}" ]; then
		groupmod --non-unique --gid "${pgid}" app
		usermod --non-unique --uid "${puid}" --gid "${pgid}" app
	fi
	mkdir -p "${data_dir}/audio"
	chown -R "${puid}:${pgid}" "${data_dir}"
	echo "running as uid ${puid} gid ${pgid}"
	exec setpriv --reuid="${puid}" --regid="${pgid}" --init-groups /usr/local/bin/entrypoint.sh "$@"
fi

if ! mkdir -p "${data_dir}/audio" 2>/dev/null || [ ! -w "${data_dir}" ]; then
	echo "error: ${data_dir} is not writable by uid $(id -u)." >&2
	echo "       Run the container as root with PUID/PGID set to the owner of the data folder," >&2
	echo "       or chown the folder to uid $(id -u) on the host." >&2
	exit 1
fi

echo "running database migrations"
alembic upgrade head

exec "$@"
