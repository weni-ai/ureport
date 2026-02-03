#!/bin/bash

set -e

APP_CELERY_INIT="ureport"

export PREFIX_ENV="${PREFIX_ENV:-"UREPORT"}"

################################################################################
# Get variable using a prefix
# Globals:
# 	PREFIX_ENV: Prefix used on variables
# Arguments:
# 	$1: Variable name to get the content, in format "${PREFIX_ENV}_$1"
# 	$2: Default value, if variable is empty or not exist
# Outputs:
# 	Output is the content of prefixed variable
# Returns:
# 	Nothing.
################################################################################
function get_env() {
	local env_name="${PREFIX_ENV}_${1}"
	if [ "${!env_name}" != "" ]; then
		echo -n "${!env_name}"
	else
		echo -n "${2}"
	fi
}

################################################################################
# Set variable using a prefix.  The variable is set in global context.
# Globals:
# 	PREFIX_ENV: Prefix used on variables
# Arguments:
# 	$1: Variable name to set the content, in format "${PREFIX_ENV}_$1"
# Outputs:
# 	Nothing
# Returns:
# 	Nothing.
################################################################################
function set_env() {
	export "${PREFIX_ENV}_${1}"
}

################################################################################
# Execute gosu to change user and group uid, but works with exec and more
# friendly to nonroot. This is used to exec the same command when root or a
# normal execute a command on a container.
# If the inicial argument after the ID is exec, this function will try to be
# compatible with exec of bash.
# Globals:
# 	${PREFIX_ENV}_GOSU_ALLOW_ID: Default 0. If id 0 not has some kind of cap drop, set to something not equal to 0 and not empty.
# Arguments:
# 	$@: Same argument as gosu
# Outputs:
# 	Output the same stdout and stderr of executed program of command line arguments
# Returns:
# 	Return the same return code of executed program of command line arguments
################################################################################
do_gosu() {
	user="$1"
	shift 1

	is_exec="false"
	if [ "$1" = "exec" ]; then
		is_exec="true"
		shift 1
	fi

	# If user is 0, he can change uid and gid
	if [ "$(id -u)" = "$(get_env GOSU_ALLOW_ID '0')" ]; then
		if [ "${is_exec}" = "true" ]; then
			exec gosu "${user}" "$@"
		else
			gosu "${user}" "$@"
			return "$?"
		fi
	else
		if [ "${is_exec}" = "true" ]; then
			exec "$@"
		else
			eval '"$@"'
			return "$?"
		fi
	fi
}

bootstrap_conf(){
	find "${PROJECT_PATH}" -not -user "${APP_UID}" -exec chown "${APP_UID}:${APP_GID}" {} \+
}

bootstrap_conf

if [[ "start" == "$1" ]]; then
	echo "Collect static files"
	do_gosu "${APP_UID}:${APP_GID}" python manage.py collectstatic --noinput || echo "Deu erro em: collectstatic"

	echo "Compress static files"
	do_gosu "${APP_UID}:${APP_GID}" python manage.py compress --extension=.haml,.html

	echo "Compile Messages"
	do_gosu "${APP_UID}:${APP_GID}" python manage.py compilemessages

	# gunicorn ureport.wsgi:application --max-requests 5000 -b 0.0.0.0:8080 -c $PROJECT_PATH/gunicorn.conf.py
	do_gosu "${APP_UID}:${APP_GID}" exec gunicorn ureport.wsgi:application \
		--max-requests 5000 --bind "0.0.0.0:${APP_PORT}" --capture-output \
		--error-logfile - -c "${PROJECT_PATH}/gunicorn.conf.py"
elif [[ "celery-worker" == "$1" ]]; then
	do_gosu "${APP_UID}:${APP_GID}" exec celery -A "${APP_CELERY_INIT}" \
		worker --loglevel=INFO -E
elif [[ "celery-beat" == "$1" ]]; then
	do_gosu "${APP_UID}:${APP_GID}" exec celery -A "${APP_CELERY_INIT}" \
		beat --loglevel=INFO
elif [[ "healthcheck-celery-worker" == "$1" ]]; then
	HEALTHCHECK_OUT=$(
		do_gosu "${APP_UID}:${APP_GID}" celery -A "${APP_CELERY_INIT}" \
			inspect ping -d "celery@${HOSTNAME}"  2>&1
	)
	echo "${HEALTHCHECK_OUT}"
	grep -F -qs "celery@${HOSTNAME}: OK" <<<"${HEALTHCHECK_OUT}" || exit 1
	exit 0
elif [[ "healthcheck-http-get" == "$1" ]]; then
	do_gosu "${APP_UID}:${APP_GID}" curl -SsLf "${2}" -o /tmp/null --connect-timeout 3 --max-time 20 -w "%{http_code} %{http_version} %{response_code} %{time_total}\n" || exit 1
	exit 0
elif [[ "healthcheck" == "$1" ]]; then
	do_gosu "${APP_UID}:${APP_GID}" curl -SsLf "http://127.0.0.1:${APP_PORT}/" -o /tmp/null --connect-timeout 3 --max-time 20 -w "%{http_code} %{http_version} %{response_code} %{time_total}\n" || exit 1
	exit 0
fi

exec "$@"

# vim: nu ts=4 noet ft=bash:
