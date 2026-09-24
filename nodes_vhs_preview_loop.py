"""
Keeps the VHS video/audio preview routes working on a Windows event loop that
cannot spawn subprocesses.

VideoHelperSuite transcodes previews by shelling out to ffmpeg through
``asyncio.create_subprocess_exec``. On Windows only the ProactorEventLoop
implements that; under a SelectorEventLoop every preview request dies with

    File "asyncio/base_events.py", line 528, in _make_subprocess_transport
    NotImplementedError

and dumps a full traceback into the console. On Linux the same code path is
fine, which is why upstream never hits it.

comfyui-videohelpersuite is fixed locally (its own commit guards both routes),
but that lives on somebody else's repo and is lost the next time the pack
updates. This patch is the belt to those braces.

It is an aiohttp middleware rather than a wrapped function: VHS registers its
handlers with PromptServer at import time, so by the time any deferred patch of
ours could run, the route table already holds a direct reference to the
original coroutine and rebinding the module attribute would change nothing.

The middleware is deliberately inert everywhere else -- it returns immediately
unless the request is for one of the two VHS preview routes AND the running
loop genuinely cannot spawn subprocesses, so it costs one string comparison per
request and goes quiet the moment ComfyUI runs on a Proactor loop.
"""

import asyncio
import logging
import sys

log = logging.getLogger("coachbate")

_PREVIEW_ROUTES = ("/vhs/viewvideo", "/vhs/viewaudio")


def _subprocess_unavailable():
    """True when the running loop cannot spawn ffmpeg (Windows, non-Proactor)."""
    if sys.platform != "win32":
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    return proactor is None or not isinstance(loop, proactor)


def _get_vhs_server():
    """Find the loaded videohelpersuite.server module, or None."""
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "") or ""
        if name.endswith("videohelpersuite.server") and hasattr(module, "resolve_path"):
            return module
    return None


async def _serve_untranscoded(request):
    """Hand the browser the source file instead of an ffmpeg transcode.

    Returns an aiohttp response, or None to let VHS handle the request after
    all (which will raise, but a visible upstream traceback beats us silently
    swallowing a request we did not understand).
    """
    from aiohttp import web

    vhs_server = _get_vhs_server()
    if vhs_server is None:
        return None

    query = request.rel_url.query
    path_res = await vhs_server.resolve_path(query)
    if isinstance(path_res, web.Response):
        return path_res
    file, filename, output_dir = path_res

    # An image-sequence preview is a directory of stills; there is nothing to
    # serve without ffmpeg to stitch it together.
    if query.get("format", "video") == "folder":
        return web.Response(status=204)

    # Same check VHS makes before returning a file directly: without it this
    # route would be arbitrary read access to the filesystem.
    if not vhs_server.is_safe_path(output_dir, strict=True):
        return web.Response(status=204)

    response = web.FileResponse(path=file)
    response.headers["Content-Disposition"] = f'filename="{filename}"'
    return response


def patch_vhs_preview_loop():
    """Install the preview fallback middleware. Idempotent; returns True on success."""
    try:
        from aiohttp import web
        from server import PromptServer
    except Exception as exc:
        log.warning("[CoachBate] VHS preview loop patch skipped: %s", exc)
        return False

    instance = getattr(PromptServer, "instance", None)
    app = getattr(instance, "app", None)
    if app is None:
        return False

    if getattr(app, "_coachbate_vhs_preview_loop_patch", False):
        return True

    @web.middleware
    async def vhs_preview_loop_middleware(request, handler):
        if request.path not in _PREVIEW_ROUTES or not _subprocess_unavailable():
            return await handler(request)
        try:
            response = await _serve_untranscoded(request)
        except Exception as exc:
            log.warning(
                "[CoachBate] VHS preview fallback failed (%s: %s); deferring to VHS.",
                type(exc).__name__, exc,
            )
            response = None
        if response is None:
            return await handler(request)
        return response

    app.middlewares.append(vhs_preview_loop_middleware)
    app._coachbate_vhs_preview_loop_patch = True
    return True
