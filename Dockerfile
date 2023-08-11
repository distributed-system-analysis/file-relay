FROM python:3.9
LABEL org.opencontainers.image.authors="Pbench Maintainers <pbench@googlegroups.com>"

WORKDIR /var/tmp

RUN python3 -m pip install --upgrade pip

# Since we install the relay file in/from /src, we seem to need to include it
# in the PYTHONPATH.
# FIXME:  how do we get this to install conventionally so we don't need this definition?
ENV PYTHONPATH=/src/file-relay/relay/

ENTRYPOINT ["relay"]

# Copy the files from the context area to a source directory; install the
# dependencies; then install the app.
COPY . /src/file-relay
RUN python3 -m pip install -r /src/file-relay/relay/requirements.txt
RUN python3 -m pip install /src/file-relay
