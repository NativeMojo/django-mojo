import os

from mojo.apps.fileman.renderer.image import ImageRenderer
from mojo.apps.fileman.renderer import svg_raster
from mojo.helpers import logit

logger = logit.get_logger(__name__, "fileman.log")


class VectorRenderer(ImageRenderer):
    """Renderer for vector images (SVG).

    An SVG is rasterized once to a bounded PNG, and everything after that is
    the ordinary image path: role table, resize/crop modes, output format and
    temp-file cleanup are all inherited from ImageRenderer. That inheritance is
    the point — "SVG behaves like every other image" is a property of the class
    hierarchy rather than of two role tables kept in sync by hand.

    Rasterization itself is isolated in a subprocess — see
    `renderer/svg_raster.py` for the engine choice and the caps.
    """

    # Selection is by content type, NOT by File.category. `get_file_category`
    # maps image/svg+xml to the coarse "image" bucket, which would route SVG to
    # the PIL path where it silently fails; and changing that mapping would drop
    # SVGs out of every consumer that filters media on category == "image".
    supported_categories = ['image']

    @classmethod
    def supports_file(cls, file):
        content_type = (file.content_type or "").split(";")[0].strip().lower()
        if content_type in svg_raster.SVG_CONTENT_TYPES:
            return True
        return (file.filename or "").lower().endswith(".svg")

    def __init__(self, file):
        super().__init__(file)
        # The rasterized PNG is memoized for the life of this renderer, and so
        # is a refusal. One instance serves every role in both
        # create_all_renditions() and regenerate_renditions(), so an attacker
        # pays the timeout once per file rather than once per requested role.
        self._raster_png = None
        self._raster_state = None

    def _rasterize_once(self):
        """Populate `_raster_state` / `_raster_png`. Safe to call repeatedly."""
        if self._raster_state is not None:
            return

        svg_path = None
        try:
            backend = self.file.file_manager.backend
            svg_path = self.get_temp_path(".svg")
            backend.download(self.file.storage_file_path, svg_path)
            with open(svg_path, "rb") as fh:
                svg_bytes = fh.read()

            self._raster_png = svg_raster.rasterize(svg_bytes)
            self._raster_state = "ok"
        except svg_raster.SvgRasterError as e:
            # `not_svg` is the one recoverable refusal: the bytes are not an SVG
            # document, so the file may still be a raster image PIL can open.
            # Every other refusal is terminal and must not be retried.
            self._raster_state = "not_svg" if e.not_svg else "failed"
            if e.not_svg:
                logger.info("file %s declared svg but is not svg (%s); "
                            "falling back to the image renderer", self.file.id, str(e))
            else:
                logger.warning("svg rasterization refused for file %s: %s",
                               self.file.id, str(e))
        except Exception as e:
            self._raster_state = "failed"
            logger.error("svg rasterization failed for file %s: %s",
                         self.file.id, str(e)[:200])
        finally:
            # create_rendition only unlinks the path this method RETURNS, so the
            # intermediate .svg download is ours to clean up.
            if svg_path and os.path.exists(svg_path):
                try:
                    os.unlink(svg_path)
                except OSError:
                    pass

    def _download_original(self):
        """Return a path to the rasterized PNG, or None.

        Overrides the parent's plain download so that everything downstream —
        `_process_image`, `_get_output_format`, `create_rendition` — sees an
        ordinary PNG. Returning None makes the inherited `create_rendition`
        return None too: no rendition row, no exception out of the job, which is
        exactly how a file with no usable renderer behaved before this existed.
        """
        self._rasterize_once()

        if self._raster_state == "not_svg":
            return super()._download_original()

        if self._raster_state != "ok" or not self._raster_png:
            return None

        # A fresh file per call: the inherited create_rendition unlinks the path
        # it is given, and it is called once per role.
        png_path = self.get_temp_path(".png")
        try:
            with open(png_path, "wb") as fh:
                fh.write(self._raster_png)
        except OSError as e:
            logger.error("failed to stage rasterized png for file %s: %s",
                         self.file.id, str(e))
            return None
        return png_path
