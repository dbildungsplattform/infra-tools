FROM debian:13-slim

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Europe/Berlin

RUN set -x \
    && apt-get -y update \
    && apt-get -y dist-upgrade \
    && apt-get -y install bsdextrautils curl wget gnupg \
    && echo "deb https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc > /etc/apt/trusted.gpg.d/postgres.asc \
    && apt-get -y update \
    && apt-get -y install \
        postgresql-client-18 \
        mariadb-client \
        jq \
        openssl \
        python3 \
        python3-boto3 \
        python3-pip \ 
        python3-packaging \
        duply \
        duplicity \
        gpg \
    && apt-get clean -y \
    && rm -rf /var/lib/apt/lists/*
