ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG C.UTF-8

RUN apt-get install -y git curl && curl -s https://packagecloud.io/install/repositories/github/git-lfs/script.deb.sh | bash && apt-get install -y git-lfs && git lfs install && rm -rf /var/lib/apt/lists/*



# Install requirements for add-on
#RUN apk add --no-cache python3

# So let's set it to our add-on persistent data directory.
# WORKDIR /data

# Copy data for add-on
# COPY run.sh /

# RUN chmod a+x /run.sh

# CMD [ "/run.sh" ]
