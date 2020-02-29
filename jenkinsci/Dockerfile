ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG C.UTF-8

RUN apt-get update && apt-get upgrade -y && apt-get install -y git curl && curl -s https://packagecloud.io/install/repositories/github/git-lfs/script.deb.sh | bash && apt-get install -y git-lfs && git lfs install && rm -rf /var/lib/apt/lists/*

ARG user=jenkins
ARG group=jenkins
ARG uid=1000
ARG gid=1000
ARG http_port=8080
ARG agent_port=50000
ARG JENKINS_HOME=/var/jenkins_home
ARG REF=/usr/share/jenkins/ref

# Install requirements for add-on
#RUN apk add --no-cache python3

# So let's set it to our add-on persistent data directory.
# WORKDIR /data

# Copy data for add-on
# COPY run.sh /

# RUN chmod a+x /run.sh

# CMD [ "/run.sh" ]
