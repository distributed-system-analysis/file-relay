# file-relay

Simple HTTP-based ad-hoc file relay utility with a RESTful interface

[![Build status](https://github.com/distributed-system-analysis/file-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/distributed-system-analysis/file-relay/actions/workflows/ci.yml)
[![Unit test code coverage](https://badgen.net/codecov/c/github/distributed-system-analysis/file-relay)](https://codecov.io/gh/distributed-system-analysis/file-relay)

This repo provides a single-file Python script which uses the Bottle web server framework to stand up an ad-hoc
web server with a simple RESTful interface for transferring files between two clients.  This is of particular
utility if the clients are behind separate firewalls and cannot directly connect to each other.

The emphasis is on simplicity, and the expectation is that the service this program provides is transient.  That
is, when a user needs to transfer a file, they would start the program on a host which both clients can reach,
perform the file transfer, and then shut down the service.

This service is not intended to be "industrial strength", it doesn't use or require credentials for access, and
it's not highly optimized.  If you want those things, consider using a commercial S3 service.

That said, it _is_ intended to work on a public-facing network.  A modest level of security is provided by using
"unguessable" values for the components of the URIs.  The first is the "server ID" which is provided to the command
invocation when the utility is started.  If this is a sufficiently long string of arbitrary characters, it should
provide all the same protections as a bearer token, meaning that only clients which know the ID will be able to
access the service.  Analogously, resources (i.e., files) on the service are referenced using the SHA 256 hash of
their contents.  This prevents collisions between uploaded files, and it means that a file can only be accessed by
someone who knows that it is there (and, doing so allows the utility to confirm the file integrity on upload, and
clients can do the same on download, without having to provide additional headers on the requests or responses).

This utility currently offers five methods:

- `PUT /<server_id>/<file_id>`: upload a file
- `GET /<server_id>/<file_id>`: download a file
- `DELETE /<server_id>/<file_id>`: remove an uploaded file
- `GET /<server_id>`: return server status
- `DELETE /<server_id>`: request server shutdown

There are a number of tweaks which should be considered:
- Change the hash algorithm for resource names or make it configurable
- Change the underlying web server from the reference one to Gunicorn or other
- Make the web server able to accept SSL connections or place it behind a
suitably-configured proxy inside the container.
