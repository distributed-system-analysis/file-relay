import errno
import hashlib
from http import HTTPStatus
from io import BytesIO
import os
from pathlib import Path
import shutil
from typing import Any, Callable, IO, NamedTuple, Optional, Union

from _pytest.monkeypatch import MonkeyPatch
from bottle import HTTPResponse
from click.testing import CliRunner, Result
import humanize
import pytest

from relay import relay


def mock_app_stub(
    host: str = relay.DEFAULT_ADDRESS,
    port: int = relay.DEFAULT_PORT,
    debug: bool = False,
) -> Callable:
    """Helper which returns a mock function for the Bottle application

    The mock verifies that it was called with the provided arguments, and
    then just returns.  (Normally, the app function would run "forever".)
    """

    def mock_func(*_args, **kwargs):
        """Mock Bottle app which just validates its inputs and returns"""
        assert kwargs["host"] == host
        assert kwargs["port"] == port
        assert kwargs["debug"] == debug

    return mock_func


def mock_app_raise(exc: Exception) -> Callable:
    """Helper which returns a mock function for the Bottle application

    The mock just raises the provided exception.
    """

    def mock_func(*_args, **_kwargs):
        """Mock Bottle app which raises an exception."""
        raise exc

    return mock_func


def mock_app_method_call(
    *, method: Callable, validate: Callable, method_args
) -> Callable:
    """Helper which returns a mock function for the Bottle application

    The mock invokes the specified HTTP callback method with the provided
    arguments and then invokes the specified validation function with the
    response.
    """

    def mock_func(*_args, **_kwargs):
        """Mock Bottle app which calls an HTTP method callback"""
        response = method(**method_args)
        validate(response)

    return mock_func


class MockRequest:
    """Mock for Bottle.Request

    This code was extracted from Bottle.BaseRequest and lightly edited.
    """

    __slots__ = ("content_length", "environ")

    def __init__(self, environ=None):
        """Wrap a WSGI environ dictionary.

        Attempts to set properties on this object will add/modify
        entries in the dictionary.
        """
        self.environ = {} if environ is None else environ

    def __getitem__(self, key):
        return self.environ[key]

    def __setitem__(self, key, value):
        self.environ[key] = value


class TestRelay:
    SERVER_ID_SWITCH = "--server_id"
    BIND_SWITCH = "--bind"
    FILDIR_SWITCH = "--files-directory"
    BDEBUG_SWITCH = "--debug"

    FAKE_DISK_UTL = {
        "bytes_used": 1200000,
        "bytes_remaining": 80000000000,
        "string": "0.00001% full, 80Gb remaining",
    }

    END_OF_MAIN = AssertionError("Unexpectedly reached the body of main()")
    SERVER_ID_TEXT = "ThisIsMyServerServerID"

    DEFAULT_OPTS = f"{SERVER_ID_SWITCH} {SERVER_ID_TEXT} {BIND_SWITCH} ''"

    @staticmethod
    def invoke_main(**kwargs) -> Result:
        """Helper function which invokes the relay.main() function

        Args:
            kwargs:  keyword arguments to be passed to Click.CliRunner()

                If the "args" key is not present, the default arguments will be
                supplied; however, if it is present but false-y, it will be
                removed from the dictionary, which allows the caller to perform
                the invocation without command line arguments.

        Returns:
            The Click.testing.Result of the invocation
        """
        if "args" not in kwargs:
            kwargs["args"] = TestRelay.DEFAULT_OPTS
        elif not kwargs["args"]:
            del kwargs["args"]

        runner = CliRunner(mix_stderr=False)
        # noinspection PyTypeChecker
        return runner.invoke(relay.main, **kwargs)

    @staticmethod
    def check_result(result: Result, exit_code=0, stdout="", stderr=""):
        """Helper function which checks the results of an invocation"""
        assert (
            stderr in result.stderr  # Will pass if specified stderr is empty
        ), f"Unexpected stderr: '{result.stderr}', stdout: '{result.stdout}'"
        assert (
            stdout in result.stdout  # Will pass if specified stdout is empty
        ), f"Unexpected stdout: '{result.stdout}', stderr: '{result.stderr}'"
        assert (
            result.exit_code == exit_code
        ), "Expected exit code of {:d}: exit_code = {:d}, stderr: {}, stdout: {}".format(
            exit_code, result.exit_code, result.stderr, result.stdout
        )

    @staticmethod
    def do_setup(
        m: MonkeyPatch,
        /,
        calls: Optional[list[tuple[str, str]]] = None,
        files_dir: str = relay.DEFAULT_FILES_DIRECTORY,
        files_dir_exists: bool = True,
        files_dir_is_dir: bool = True,
        file_id: Optional[str] = None,
        func: Callable = mock_app_stub(),
        hexdigest: Optional[str] = None,
        outfile: Optional[BytesIO] = None,
        raise_exception: Optional[Exception] = None,
    ):
        """Helper function which performs common setup for a test scenario

        This function creates mock classes for pathlib.Path and hashlib.<hash>
        and sets up mocks for the following functions:
          - bottle.app.run()
          - hashlib.sha256()
          - hashlib.<hash>.hexdigest()
          - hashlib.<hash>.update()
          - pathlib.Path.exists()
          - pathlib.Path.is_dir()
          - pathlib.Path.open()
          - pathlib.Path.unlink()
          - the Path "/" operators
          - relay.get_disk_utilization()

        The mock classes seem to be necessary in order to intercept the
        respective member functions, possibly because these are native
        implementations instead of "pure Python" (or, maybe I just don't
        know what I'm doing).

        The mocks are closures which capture the parameters to this function
        and use them to drive their behaviors.

        This function also replaces the Bottle.app.run() method with a mock.
        This drives one of three behaviors:
          - For command invocation tests, this allows us to cause the
            application to terminate immediately -- by either returning
            normally or raising an exception -- when it would otherwise run
            forever; this is used to drive either success and catastrophic
            failure scenarios.
          - For HTTP method callback tests, this allows us to invoke the target
            method without having to run an actual web server and issuing
            requests to it.

        Args:
            m: the Monkeypatch context
            calls: a list which records the calls made to the mocks
            files_dir: the path to be mocked
            files_dir_exists: value to be returned by Path.exists()
            files_dir_is_dir: value to be returned by Path.is_dir()
            file_id:  the name of the uploaded file and its hash value
            func: Bottle application replacement
            hexdigest:  a string to be returned by hashlib.<hash>.hexdigest()
            outfile:  a file-like object to be returned by path.open()
            raise_exception:  Exception to raise from Path.unlink()
        """

        if calls is None:
            calls = []

        hexdigest_str = hexdigest if hexdigest else file_id

        class MockHash:
            """Mock for hashlib.sha256()"""

            def update(self, _data):
                """Mock for hashlib.<hash>.update()

                We don't need to do anything, since what we're going to return
                is already determined by `hexdigest_str`.
                """
                calls.append(("MockHash.update", str(self)))

            def hexdigest(self) -> str:
                """Mock for hashlib.<hash>.update()

                Return the mocked value for the hex hash digest.
                """
                calls.append(("MockHash.hexdigest", str(self)))
                return hexdigest_str

        class MockPath:
            """Mock for pathlib.Path"""

            def __init__(self, *args, **_kwargs):
                """Constructor for mock Path

                Create and store a "real" Path object for the requested file
                path so that, if the request file path is _not_ one that we
                want to mock, we can perform the real operations.
                """
                assert not _kwargs
                self.path = Path(*args)
                calls.append(("__init__", str(self.path)))

            def __str__(self) -> str:
                return str(self.path)

            def __truediv__(self, key: Union[str, Path]) -> "MockPath":
                """Mock for the Path `/` operator (when the Path is the left operand)."""
                new_path = self.path / key
                calls.append(("truediv", str(new_path)))
                return MockPath(new_path)

            def __rtruediv__(self, key: Union[str, Path]) -> "MockPath":
                """Mock for the Path `/` operator (when the Path is the right operand)."""
                new_path = key / self.path
                calls.append(("rtruediv", str(new_path)))
                return MockPath(new_path)

            def exists(self):
                """Mock for the Path.exists() function

                If the mocked path matches the target directory, return the
                mock value; otherwise, return the value from the real
                Path.exists() function.
                """
                calls.append(("exists", str(self.path)))
                if str(self.path) == files_dir:
                    return files_dir_exists
                else:
                    return self.path.exists()

            def is_dir(self):
                """Mock for the Path.is_dir() function

                If the mocked path matches the target directory, return the
                mock value; otherwise, return the value from the real
                Path.is_dir() function.
                """
                calls.append(("is_dir", str(self.path)))
                if str(self.path) == files_dir:
                    return files_dir_is_dir
                else:
                    return self.path.is_dir()

            def open(self, mode: str, *args, **kwargs) -> IO[Any]:
                """Mock for the Path.open() function

                If the mocked path matches the target file, raise an exception
                if requested or return the mock value; otherwise, return the
                value from the real Path.open() function.

                Note that if this mock is called when `file_id` hasn't been
                provided, it will blow an assertion; however, that exception
                will likely be caught and reported as an INTERNAL_SERVER_ERROR,
                which is disappointing and possibly confusing, but better than
                omitting the check.
                """
                assert file_id, "Test bug:  file_id is unspecified"
                calls.append(("open", str(self.path)))
                if str(self.path) == str(Path(files_dir) / file_id):
                    assert "x" in mode
                    if raise_exception:
                        raise raise_exception
                    return outfile
                else:
                    return self.path.open(mode=mode, *args, **kwargs)

            rtruediv = __rtruediv__
            truediv = __truediv__

            def unlink(self, *args, **kwargs):
                """Mock for the Path.unlink() function

                If the mocked path matches the target file, raise an exception
                if requested or just return; otherwise, call the real
                Path.unlink() function.

                Note that if this mock is called when `file_id` hasn't been
                provided, it will blow an assertion; however, that exception
                will likely be caught and reported as an INTERNAL_SERVER_ERROR,
                which is disappointing and possibly confusing, but better than
                omitting the check.
                """
                assert file_id, "Test bug:  file_id is unspecified"
                calls.append(("unlink", str(self.path)))
                if str(self.path) == str(Path(files_dir) / file_id):
                    if raise_exception:
                        raise raise_exception
                    return
                else:
                    return self.path.unlink(*args, **kwargs)

        def mock_get_disk_utilization(dir_path: Path) -> dict[str, int | str]:
            """Mock for relay.get_disk_utilization()

            Returns a static string.

            Note that if the assertion fails, the exception will be caught and
            reported as an INTERNAL_SERVER_ERROR.  This will likely make the
            test fail, but only if it's actually checking the response....
            """
            assert str(dir_path) == relay.DEFAULT_FILES_DIRECTORY
            return TestRelay.FAKE_DISK_UTL

        m.setattr(relay, "get_disk_utilization", mock_get_disk_utilization)
        m.setattr(relay, "Path", MockPath)
        m.setattr(relay, "sha256", lambda *_args, **_kwargs: MockHash())
        m.setattr(relay.app, "run", func)

    @staticmethod
    def test_help(monkeypatch: MonkeyPatch):
        """Test the command with the --help switch"""
        with monkeypatch.context() as m:
            m.setattr(Path, "__init__", mock_app_raise(TestRelay.END_OF_MAIN))
            result = TestRelay.invoke_main(args=["--help"])
        assert result.stdout.startswith(
            "Usage: "
        ), f"Unexpected output: {result.stdout!r}"
        assert TestRelay.SERVER_ID_SWITCH in result.stdout
        assert TestRelay.BIND_SWITCH in result.stdout
        assert TestRelay.FILDIR_SWITCH in result.stdout
        assert TestRelay.BDEBUG_SWITCH in result.stdout
        assert not result.stderr
        assert result.exit_code == 0, f"Unexpected error: {result.stderr!r}"

    @staticmethod
    def test_command_with_all_switches(monkeypatch: MonkeyPatch):
        """Test command line parsing when all switches are specified"""
        host = "myhost.example.com"
        port = 12345
        bind_text = host + ":" + str(port)
        fildir_text = "/mock_tmp"
        mock = mock_app_stub(host=host, port=port, debug=True)
        with monkeypatch.context() as m:
            TestRelay.do_setup(m, files_dir=fildir_text, func=mock)
            result = TestRelay.invoke_main(
                args=[
                    TestRelay.SERVER_ID_SWITCH,
                    TestRelay.SERVER_ID_TEXT,
                    TestRelay.BIND_SWITCH,
                    bind_text,
                    TestRelay.FILDIR_SWITCH,
                    fildir_text,
                    TestRelay.BDEBUG_SWITCH,
                ],
            )
        TestRelay.check_result(result)

    @staticmethod
    def test_command_with_all_defaults(monkeypatch: MonkeyPatch):
        """Test command line parsing when no switches are specified

        The program will prompt for the server ID and the bind address; we
        supply stdin text with a server ID, a newline, and another newline to
        accept the default binding, which we confirm meets our expectations.
        """
        with monkeypatch.context() as m:
            TestRelay.do_setup(m)
            result = TestRelay.invoke_main(args="", input="myserverID\n\n")
        TestRelay.check_result(result)

    @staticmethod
    def test_command_with_missing_files_dir(monkeypatch: MonkeyPatch):
        """Test command invocation with a non-existent files directory"""
        mock = mock_app_raise(TestRelay.END_OF_MAIN)
        file = "/mock"
        with monkeypatch.context() as m:
            TestRelay.do_setup(m, files_dir=file, files_dir_exists=False, func=mock)
            result = TestRelay.invoke_main(
                args=f"{TestRelay.DEFAULT_OPTS} --files-directory {file}"
            )
        TestRelay.check_result(
            result, exit_code=2, stderr=f"Files directory path '{file}' does not exist"
        )

    @staticmethod
    def test_command_with_bad_files_dir(monkeypatch: MonkeyPatch):
        """Test command invocation with an existing, non-directory files directory"""
        mock = mock_app_raise(TestRelay.END_OF_MAIN)
        file = "/mock"
        with monkeypatch.context() as m:
            TestRelay.do_setup(m, files_dir=file, files_dir_is_dir=False, func=mock)
            result = TestRelay.invoke_main(
                args=f"{TestRelay.DEFAULT_OPTS} --files-directory {file}"
            )
        TestRelay.check_result(
            result,
            exit_code=2,
            stderr=f"Files directory path '{file}' is not a directory",
        )

    @staticmethod
    @pytest.mark.parametrize(
        "host,colon,port",
        (
            ("", ":", "12345"),
            ("myhost.example.com", ":", ""),
            ("myhost.example.com", "", ""),
            ("", ":", ""),
            ("", "''", ""),
        ),
    )
    def test_command_with_binding_strings(
        host: str, colon: str, port: str, monkeypatch: MonkeyPatch
    ):
        """Test command invocation with various binding strings"""
        kwargs = {}
        if host:
            kwargs["host"] = host
        if port:
            kwargs["port"] = int(port)
        bind_switch = f"--bind {host}{colon}{port}"
        with monkeypatch.context() as m:
            TestRelay.do_setup(m, func=mock_app_stub(**kwargs))
            result = TestRelay.invoke_main(
                args=f"{TestRelay.SERVER_ID_SWITCH} {TestRelay.SERVER_ID_TEXT} {bind_switch}"
            )
        TestRelay.check_result(result)

    @staticmethod
    @pytest.mark.parametrize(
        "port,message",
        (
            ("100000", "Port value, {}, must be between 0 and 65536"),
            ("notaport", "Port value, {!r}, must be an integer"),
        ),
    )
    def test_command_with_bad_port(port: str, message: str, monkeypatch: MonkeyPatch):
        """Test command invocation which requests an illegal port value"""
        mock = mock_app_raise(TestRelay.END_OF_MAIN)
        with monkeypatch.context() as m:
            TestRelay.do_setup(m, func=mock)
            result = TestRelay.invoke_main(
                args=f"{TestRelay.SERVER_ID_SWITCH} {TestRelay.SERVER_ID_TEXT} --bind :{port}"
            )
        TestRelay.check_result(
            result,
            exit_code=2,
            stderr=message.format(port),
        )

    @staticmethod
    def test_command_with_server_error(monkeypatch: MonkeyPatch):
        """Test command behavior when Bottle application raises an exception"""
        mock = mock_app_raise(RuntimeError("Testing main exception handler"))
        with monkeypatch.context() as m:
            TestRelay.do_setup(m, func=mock)
            result = TestRelay.invoke_main()
        TestRelay.check_result(result, exit_code=2, stderr="Error running the server")

    @staticmethod
    def test_relay_status_operation(monkeypatch: MonkeyPatch):
        """Test GET /<server_id> method operation"""

        def validate_relay(response: HTTPResponse):
            """Validate the response from the HTTP method call"""
            assert response.status_code == HTTPStatus.OK
            assert response.body["disk utilization"] == TestRelay.FAKE_DISK_UTL

        with monkeypatch.context() as m:
            mock = mock_app_method_call(
                method=relay.relay_status,
                validate=validate_relay,
                method_args={"server_id": TestRelay.SERVER_ID_TEXT},
            )
            TestRelay.do_setup(m, func=mock)
            result = TestRelay.invoke_main()
        TestRelay.check_result(result)

    @staticmethod
    def test_shutdown_operation(monkeypatch: MonkeyPatch):
        """Test DELETE /<server_id> method normal operation"""

        def mock_posix_spawn(
            path: str, _argv: list[str], _env: dict[str, str], /, **_kwargs
        ) -> int:
            """Mock for os.posix_spawn()"""
            assert path == "/usr/bin/kill"
            return 0

        def validate_shutdown(response: HTTPResponse):
            """Validate the response from the HTTP method call"""
            assert response.status_code == HTTPStatus.OK
            assert "Good bye!" in response.body

        with monkeypatch.context() as m:
            mock = mock_app_method_call(
                method=relay.shutdown,
                validate=validate_shutdown,
                method_args={"server_id": TestRelay.SERVER_ID_TEXT},
            )
            TestRelay.do_setup(m, func=mock)
            m.setattr(os, "posix_spawn", mock_posix_spawn)
            result = TestRelay.invoke_main()
        TestRelay.check_result(result)

    @staticmethod
    def test_retrieve_file(monkeypatch: MonkeyPatch):
        """Test GET /<server_id>/<file_id> method operation

        The retrieve_file() function is a veneer over the Bottle static_file()
        function (there are no conditionals in the code), so there is really
        only one scenario to test -- success or error, either way the CUT just
        returns what static_file() sends, so there's no point in testing the
        error case.
        """

        file_id = "thisisafileid"
        response_to_send = HTTPResponse(status=HTTPStatus.OK, body="This is a file!")

        def mock_bottle_static_file(
            filename: str, root: str, **_kwargs
        ) -> HTTPResponse:
            """Mock for Bottle.static_file()"""
            assert filename == file_id
            assert str(root) == relay.DEFAULT_FILES_DIRECTORY
            return response_to_send

        def validate_retrieve_file(response: HTTPResponse):
            """Validate the response from the HTTP method call"""
            assert response.status_code == response_to_send.status_code
            assert response.body == response_to_send.body

        with monkeypatch.context() as m:
            mock = mock_app_method_call(
                method=relay.retrieve_file,
                validate=validate_retrieve_file,
                method_args={"server_id": TestRelay.SERVER_ID_TEXT, "file_id": file_id},
            )
            TestRelay.do_setup(m, func=mock)
            m.setattr(relay, "static_file", mock_bottle_static_file)
            result = TestRelay.invoke_main()
        TestRelay.check_result(result)

    @staticmethod
    @pytest.mark.parametrize(
        "exception,status,message",
        (
            (
                FileNotFoundError,
                HTTPStatus.NOT_FOUND,
                "FileNotFoundError: [Errno 2] No such file or directory",
            ),
            (
                PermissionError,
                HTTPStatus.FORBIDDEN,
                "PermissionError: [Errno 13] Permission denied",
            ),
            (Exception, HTTPStatus.INTERNAL_SERVER_ERROR, "Ooopsies"),
            (None, HTTPStatus.OK, "Success"),
        ),
    )
    def test_delete_file(
        exception: type, status: int, message: str, monkeypatch: MonkeyPatch
    ):
        """Test DELETE /<server_id>/<file_id> method operation"""

        calls: list[tuple[str, str]] = []
        file_id = "thisisafileid"

        def validate_delete_file(response: HTTPResponse):
            """Validate the response from the HTTP method call"""
            assert response.status_code == status
            assert message in response.body

        with monkeypatch.context() as m:
            mock = mock_app_method_call(
                method=relay.delete_file,
                validate=validate_delete_file,
                method_args={"server_id": TestRelay.SERVER_ID_TEXT, "file_id": file_id},
            )
            TestRelay.do_setup(
                m,
                calls=calls,
                func=mock,
                raise_exception=exception(message) if exception else None,
                file_id=file_id,
            )
            result = TestRelay.invoke_main()
        TestRelay.check_result(result)
        assert "unlink" in (i[0] for i in calls)

    @staticmethod
    @pytest.mark.parametrize("content_length", (-1, relay.FILE_MAX_SIZE + 1))
    def test_receive_file_content_length_failures(
        content_length: int, monkeypatch: MonkeyPatch
    ):
        """Test PUT /<server_id>/<file_id> method callback with bad content lengths"""

        file_id = "thisisafileid"
        request = MockRequest()
        request.content_length = content_length

        def mock_sha256(*_args, **_kwargs):
            """Mock for hashlib.sha256()

            This mock should not be called in this scenario.
            """
            raise AssertionError("The CUT did not return when expected")

        def validate_receive_file(response: HTTPResponse):
            """Validate the response from the HTTP method call"""
            assert response.status_code == HTTPStatus.BAD_REQUEST
            assert (
                f"Content-Length ({request.content_length}) "
                f"must be greater than zero and less than {relay.FILE_MAX_SIZE}"
            ) in response.body

        with monkeypatch.context() as m:
            mock = mock_app_method_call(
                method=relay.receive_file,
                validate=validate_receive_file,
                method_args={"server_id": TestRelay.SERVER_ID_TEXT, "file_id": file_id},
            )
            TestRelay.do_setup(m, func=mock)
            m.setattr(relay, "request", request)
            m.setattr(hashlib, "sha256", mock_sha256)
            result = TestRelay.invoke_main()
        TestRelay.check_result(result)

    @staticmethod
    @pytest.mark.parametrize(
        "exc,http_status,message,exp_unlink",
        (
            (
                FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST)),
                HTTPStatus.CONFLICT,
                f"[Errno {errno.EEXIST}] File exists",
                False,
            ),
            (
                OSError(errno.ENOSPC, os.strerror(errno.ENOSPC)),
                HTTPStatus.INSUFFICIENT_STORAGE,
                "Out of space",
                True,
            ),
            (
                OSError(errno.E2BIG, "This is a mocked unexpected error"),
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Unexpected error ({errno.E2BIG}) encountered during file upload",
                True,
            ),
            (
                NotImplementedError,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Unexpected error encountered during file upload",
                True,
            ),
        ),
    )
    def test_receive_file_upload_exceptions(
        exc: Exception,
        http_status: HTTPStatus,
        message: str,
        exp_unlink: bool,
        monkeypatch: MonkeyPatch,
    ):
        """Test PUT /<server_id>/<file_id> method callback when exceptions are raised"""

        calls: list[tuple[str, str]] = []
        file_id = "thisisafileid"
        request = MockRequest()
        request.content_length = 1

        def validate_receive_file(response: HTTPResponse):
            """Validate the response from the HTTP method call"""
            assert response.status_code == http_status
            assert message in response.body

        with monkeypatch.context() as m:
            mock = mock_app_method_call(
                method=relay.receive_file,
                validate=validate_receive_file,
                method_args={"server_id": TestRelay.SERVER_ID_TEXT, "file_id": file_id},
            )
            TestRelay.do_setup(
                m, calls=calls, func=mock, file_id=file_id, raise_exception=exc
            )
            m.setattr(relay, "request", request)
            result = TestRelay.invoke_main()
        TestRelay.check_result(result)
        assert ("unlink" in list(i[0] for i in calls)) is exp_unlink

    @staticmethod
    @pytest.mark.parametrize(
        "scenario,message",
        (
            (
                "short body",
                "Expected {expected_length} bytes but received {received_length} bytes",
            ),
            (
                "hash mismatch",
                "Mismatched hash ID:  expecting {expected_hash!r}, got {received_hash!r}",
            ),
        ),
    )
    def test_receive_file_upload_errors(
        scenario: str, message: str, monkeypatch: MonkeyPatch
    ):
        """Test PUT /<server_id>/<file_id> method callback consistency check failures"""

        calls: list[tuple[str, str]] = []
        file_id = "thisisafileid"
        in_file_body = b"This is the contents of the file."
        bad_hash = "thisisamismatchedhash"
        out_file = BytesIO()
        request = MockRequest()
        error_factor = 2 if scenario == "short body" else 1
        request.content_length = len(in_file_body) * error_factor
        request["wsgi.input"] = BytesIO(initial_bytes=in_file_body)
        msg_args = {
            "expected_length": request.content_length,
            "received_length": request.content_length - len(in_file_body),
            "expected_hash": file_id,
            "received_hash": bad_hash,
        }

        def validate_receive_file(response: HTTPResponse):
            """Validate the response from the HTTP method call"""
            assert response.status_code == HTTPStatus.BAD_REQUEST
            assert message.format(**msg_args) in response.body

        with monkeypatch.context() as m:
            mock = mock_app_method_call(
                method=relay.receive_file,
                validate=validate_receive_file,
                method_args={"server_id": TestRelay.SERVER_ID_TEXT, "file_id": file_id},
            )
            TestRelay.do_setup(
                m,
                calls=calls,
                file_id=file_id,
                func=mock,
                hexdigest=bad_hash if scenario == "hash mismatch" else file_id,
                outfile=out_file,
            )
            m.setattr(relay, "request", request)
            result = TestRelay.invoke_main()
        TestRelay.check_result(result)
        assert "unlink" in (i[0] for i in calls)

    @staticmethod
    @pytest.mark.parametrize("chunks", (0, 1, 2))
    def test_receive_file_upload_operation(chunks: int, monkeypatch: MonkeyPatch):
        """Test PUT /<server_id>/<file_id> method callback normal operation"""

        calls: list[tuple[str, str]] = []
        file_id = "thisisafileid"
        bytes_read = [0]
        bytes_written = [0]
        chunk_count = [0]
        chunk_content = [b""]

        class MockIO(BytesIO):
            """A file-like object which we can read from"""

            def read(self, read_size=-1) -> bytes:
                """Mock upload stream"""
                bytes_read[0] += read_size
                assert bytes_read[0] <= request.content_length
                chunk_content[0] = chr(ord("a") + chunk_count[0]).encode() * read_size
                chunk_count[0] += 1
                return chunk_content[0]

            def write(self, write_buf: bytes) -> int:
                """Mock io.BufferedIOBase.write()"""
                size = len(write_buf)
                bytes_written[0] += size
                assert write_buf == chunk_content[0]
                return size

        request = MockRequest()
        request.content_length = chunks * relay.READ_CHUNK_SIZE + 100
        request["wsgi.input"] = MockIO()

        def validate_receive_file(response: HTTPResponse):
            """Validate the response from the HTTP method call"""
            assert response.status_code == HTTPStatus.CREATED
            assert "Success" in response.body

        with monkeypatch.context() as m:
            mock = mock_app_method_call(
                method=relay.receive_file,
                validate=validate_receive_file,
                method_args={"server_id": TestRelay.SERVER_ID_TEXT, "file_id": file_id},
            )
            TestRelay.do_setup(
                m, calls=calls, file_id=file_id, func=mock, outfile=MockIO()
            )
            m.setattr(relay, "request", request)
            result = TestRelay.invoke_main()
        TestRelay.check_result(result)
        assert "unlink" not in (i[0] for i in calls)
        assert request.content_length == bytes_read[0]
        assert request.content_length == bytes_written[0]

    @staticmethod
    def test_get_disk_utilization(monkeypatch: MonkeyPatch):
        """Exercise get_disk_utilization()

        This is a (nearly) trivial function, but we test it so that the unit
        tests show 100% coverage of the CUT.
        """

        class DiskUsageData(NamedTuple):
            total: int = 5 * 1024 * 1024 * 1024
            used: int = 2 * 1024 * 1024 * 1024
            free: int = 3 * 1024 * 1024 * 1024

        expected_dir_path = Path("/mockdir")
        du = DiskUsageData()
        nv = "3.2 GB"

        def mock_disk_usage(dir_path: Path) -> DiskUsageData:
            """Mock shutil.disk_usage()"""
            assert dir_path == expected_dir_path
            return du

        def mock_naturalsize(value: Union[float, str], *args):
            """Mock humanize.naturalsize()"""
            assert len(args) == 0
            assert str(value) == str(du.free)
            return nv

        with monkeypatch.context() as m:
            m.setattr(shutil, "disk_usage", mock_disk_usage)
            m.setattr(humanize, "naturalsize", mock_naturalsize)
            expected = {
                "bytes_used": du.used,
                "bytes_remaining": du.free,
                "string": "{:.3}% full, {} remaining".format(
                    float(du.used) / float(du.total) * 100.0,
                    nv,
                ),
            }
            actual = relay.get_disk_utilization(expected_dir_path)
            assert actual == expected

    @staticmethod
    @pytest.mark.parametrize(
        "status,server_id",
        (
            (HTTPStatus.IM_A_TEAPOT, None),
            (HTTPStatus.NOT_FOUND, "favicon.ico"),
            (HTTPStatus.FORBIDDEN, "incorrectserverid"),
        ),
    )
    def test_validate_server_id(
        status: HTTPStatus, server_id: str, monkeypatch: MonkeyPatch
    ):
        """Test the operation of the validate_server_id() decorator"""

        if not server_id:
            server_id = TestRelay.SERVER_ID_TEXT

        def method_callback(*args, **kwargs):
            """Stub for HTTP method callback"""
            assert (
                status == HTTPStatus.IM_A_TEAPOT
            ), f"Callback called incorrectly when server_id is {server_id}"
            assert "server_id" not in kwargs
            assert len(args) + len(kwargs) == 2
            return HTTPResponse(status=status)

        def validate_validate_server_id(response: HTTPResponse):
            """Validate the response from the validate_server_id() decorator"""
            assert response.status_code == status

        with monkeypatch.context() as m:
            mock = mock_app_method_call(
                method=relay.validate_server_id(method_callback),
                validate=validate_validate_server_id,
                method_args={
                    "server_id": server_id,
                    "parm1": "parm1",
                    "parm2": "parm2",
                },
            )
            TestRelay.do_setup(m, func=mock)
            result = TestRelay.invoke_main()
        TestRelay.check_result(result)
