import logging
import os
from typing import BinaryIO, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
import fastapi.middleware.cors
import fastapi.staticfiles
import uvicorn
from starlette.responses import FileResponse, Response, RedirectResponse

# Byte range request code adapted from https://github.com/tiangolo/fastapi/discussions/7718

app = fastapi.FastAPI()


def send_bytes_range_requests(
    file_obj: BinaryIO, start: int, end: int, chunk_size: int = 10_000
):
    """Send a file in chunks using Range Requests specification RFC7233

    `start` and `end` parameters are inclusive due to specification
    """
    with file_obj as f:
        f.seek(start)
        while (pos := f.tell()) <= end:
            read_size = min(chunk_size, end + 1 - pos)
            yield f.read(read_size)


def _get_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    def _invalid_range():
        return HTTPException(
            status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=f"Invalid request range (Range:{range_header!r})",
        )

    try:
        h = range_header.replace("bytes=", "").split("-")
        start = int(h[0]) if h[0] != "" else 0
        end = int(h[1]) if h[1] != "" else file_size - 1
    except ValueError:
        raise _invalid_range()

    if start > end or start < 0 or end > file_size - 1:
        raise _invalid_range()
    return start, end


def range_requests_response(
    request: Request, file_path: str, content_type: str
) -> Response:
    """Returns StreamingResponse using Range Requests of a given file"""
    stat_result = os.stat(file_path)
    if request.method == "HEAD":
        return FileResponse(file_path, stat_result=stat_result)

    file_size = stat_result.st_size
    range_header = request.headers.get("range")

    headers = {
        "content-type": content_type,
        "accept-ranges": "bytes",
        "content-encoding": "identity",
        "content-length": str(file_size),
        "access-control-expose-headers": (
            "content-type, accept-ranges, content-length, "
            "content-range, content-encoding"
        ),
    }
    start = 0
    end = file_size - 1
    status_code = status.HTTP_200_OK

    if range_header is not None:
        start, end = _get_range_header(range_header, file_size)
        size = end - start + 1
        headers["content-length"] = str(size)
        headers["content-range"] = f"bytes {start}-{end}/{file_size}"
        status_code = status.HTTP_206_PARTIAL_CONTENT

    logging.debug(f"request method is {request.method}, request url is {request.url} request headers are {request.headers}, response headers are {headers}")
    return StreamingResponse(
        send_bytes_range_requests(open(file_path, mode="rb"), start, end),
        headers=headers,
        status_code=status_code,
    )


class FastAPIServer:
    def __init__(self, port: int, data_path: str, ui_path: str, browser_path: Optional[str] = None):
        self.port = port
        self.data_path = data_path
        self.ui_path = ui_path
        self.browser_path = browser_path
        self.app = FastAPI()
        self.app.add_middleware(
            fastapi.middleware.cors.CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"]
        )
        self.setup_routes()

    def setup_routes(self):

        @self.app.get("/data/{identifier:path}")
        @self.app.head("/data/{identifier:path}")
        def get_data(request: Request, identifier: str):
            path = os.path.join(self.data_path, identifier)
            return range_requests_response(
                request, file_path=path, content_type="application/x-parquet"
            )
        self.app.mount(
            "/ui",
            fastapi.staticfiles.StaticFiles(directory=self.ui_path, html=True),
            name="ui",
        )
        if (self.browser_path is not None):
            self.app.mount(
                "/",
                fastapi.staticfiles.StaticFiles(directory=self.browser_path, html=True),
                name="root",
            )
        else:
            _url = f"https://radiantearth.github.io/stac-browser/#/external/http:/localhost:{self.port}/data/stac.json?.language=en"

            @self.app.get("/")
            def index():
                return RedirectResponse(url=_url)

    def run(self):
        uvicorn.run(self.app, host="0.0.0.0", port=self.port)
