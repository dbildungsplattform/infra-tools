ARG MONGODB_VERSION="8.0"
ARG DUPICITY_VERSION="0.8.23"
ARG UBUNTU_VERSION="24.04"
ARG KUBERNETES_VERSION="1.34.11"

FROM ubuntu:${UBUNTU_VERSION}
ARG DEBIAN_FRONTEND=noninteractive

ENV TZ=Europe/Berlin
# Consume the build argument in the build stage
ARG MONGODB_VERSION
ARG KUBERNETES_VERSION
RUN set -x \
    && apt-get -y update \
    && apt-get -y upgrade \
    && apt-get -y dist-upgrade \
    && apt-get -y install bsdextrautils \
    && apt-get -y install curl wget gnupg \
    && wget -q https://dl.k8s.io/release/v${KUBERNETES_VERSION}/bin/linux/amd64/kubectl \
    && wget -q https://dl.k8s.io/release/v${KUBERNETES_VERSION}/bin/linux/amd64/kubectl.sha256 \
    && echo "$(cat kubectl.sha256)  kubectl" | sha256sum -c - \
    && rm -f kubectl.sha256 \
    && chmod +x kubectl \
    && mv ./kubectl /usr/local/bin/kubectl \
    && curl -fsSL https://www.mongodb.org/static/pgp/server-${MONGODB_VERSION}.asc | \
        gpg -o /usr/share/keyrings/mongodb-server-${MONGODB_VERSION}.gpg \
        --dearmor \
    && echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-${MONGODB_VERSION}.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/${MONGODB_VERSION} multiverse" | tee /etc/apt/sources.list.d/mongodb-org-${MONGODB_VERSION}.list \
    && echo "deb https://apt.postgresql.org/pub/repos/apt noble-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc > /etc/apt/trusted.gpg.d/postgres.asc \
    && apt-get -y update \
    && apt-get -y install \
        mongodb-mongosh \
        postgresql-client-15 \
        mariadb-client \
        jq \
        openssl \
        duply \
        python3 \
        python3-pip \
        python3-boto3 \
        python3-yaml \
        gpg \
        gettext \
        librsync-dev \
    && apt-get clean -y

    # https://stackoverflow.com/questions/75602063/pip-install-r-requirements-txt-is-failing-this-environment-is-externally-mana
ARG DUPICITY_VERSION
RUN pip install duplicity==${DUPICITY_VERSION} --break-system-packages
