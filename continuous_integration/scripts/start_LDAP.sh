#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${LDAP_COMPOSE_FILE:-$ROOT_DIR/continuous_integration/docker-configs/ldap-docker-compose.yml}"
COMPOSE_PROJECT="${LDAP_COMPOSE_PROJECT:-}"
LDAP_HOST="${LDAP_HOST:-127.0.0.1}"
LDAP_PORT="${LDAP_PORT:-1389}"
LDAP_ADMIN_DN="cn=admin,dc=example,dc=org"
LDAP_ADMIN_PASSWORD="adminpassword"
LDAP_BASE_DN="dc=example,dc=org"

compose_cmd() {
    if [[ -n "$COMPOSE_PROJECT" ]]; then
        docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" "$@"
    else
        docker compose -f "$COMPOSE_FILE" "$@"
    fi
}

get_openldap_container_id() {
    compose_cmd ps -q openldap | tr -d '[:space:]'
}

wait_for_ldap() {
    local timeout_seconds="${1:-60}"
    local deadline=$((SECONDS + timeout_seconds))

    while (( SECONDS < deadline )); do
        if python - <<PY >/dev/null 2>&1
import socket

with socket.create_connection(("${LDAP_HOST}", ${LDAP_PORT}), timeout=1):
    pass
PY
        then
            return 0
        fi
        sleep 1
    done

    return 1
}

wait_for_ldap_bind() {
    local container_id="$1"
    local timeout_seconds="${2:-60}"
    local deadline=$((SECONDS + timeout_seconds))

    while (( SECONDS < deadline )); do
        if docker exec "$container_id" ldapsearch \
            -x \
            -H "ldap://127.0.0.1:389" \
            -D "$LDAP_ADMIN_DN" \
            -w "$LDAP_ADMIN_PASSWORD" \
            -b "$LDAP_BASE_DN" \
            -s base \
            "(objectclass=*)" dn >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done

    return 1
}

ldap_entry_exists() {
    local container_id="$1"
    local dn="$2"

    docker exec "$container_id" ldapsearch \
        -x \
        -H "ldap://127.0.0.1:389" \
        -D "$LDAP_ADMIN_DN" \
        -w "$LDAP_ADMIN_PASSWORD" \
        -b "$dn" \
        -s base \
        "(objectclass=*)" dn >/dev/null 2>&1
}

ldap_add_if_missing() {
    local container_id="$1"
    local dn="$2"
    local ldif="$3"

    if ldap_entry_exists "$container_id" "$dn"; then
        return 0
    fi

    docker exec -i "$container_id" ldapadd \
        -x \
        -H "ldap://127.0.0.1:389" \
        -D "$LDAP_ADMIN_DN" \
        -w "$LDAP_ADMIN_PASSWORD" >/dev/null <<EOF
${ldif}
EOF
}

seed_ldap_test_users() {
    local container_id="$1"

    ldap_add_if_missing "$container_id" "ou=users,$LDAP_BASE_DN" "dn: ou=users,$LDAP_BASE_DN
objectClass: organizationalUnit
ou: users"

    ldap_add_if_missing "$container_id" "cn=user01,ou=users,$LDAP_BASE_DN" "dn: cn=user01,ou=users,$LDAP_BASE_DN
objectClass: inetOrgPerson
cn: user01
sn: user01
uid: user01
userPassword: password1"

    ldap_add_if_missing "$container_id" "cn=user02,ou=users,$LDAP_BASE_DN" "dn: cn=user02,ou=users,$LDAP_BASE_DN
objectClass: inetOrgPerson
cn: user02
sn: user02
uid: user02
userPassword: password2"
}

# Start LDAP server in docker container
compose_cmd up -d
wait_for_ldap 90
CONTAINER_ID="$(get_openldap_container_id)"
if [[ -z "$CONTAINER_ID" ]]; then
    echo "Unable to determine LDAP container id from compose project." >&2
    exit 1
fi
wait_for_ldap_bind "$CONTAINER_ID" 90
seed_ldap_test_users "$CONTAINER_ID"
docker ps
