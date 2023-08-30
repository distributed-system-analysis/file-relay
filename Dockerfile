FROM python:3.9
LABEL org.opencontainers.image.authors="Pbench Maintainers <pbench@googlegroups.com>"

ENTRYPOINT ["relay"]
WORKDIR /var/tmp

# Make sure the packaging and installation tools are up to date.
RUN python3 -m pip install --upgrade pip setuptools wheel

# Copy the files from the context area to a source directory; install the
# dependencies and the app; and remove the sources.
COPY . /src/file-relay
RUN python3 -m pip install -r /src/file-relay/src/requirements.txt /src/file-relay
RUN rm -rf /src/file-relay
