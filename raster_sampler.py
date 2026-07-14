"""Sample any raster at arbitrary XY coordinates (vectorised, nearest pixel).

Generic on purpose: the colorizer uses it with a 3-band RGB orthomosaic, but the
same class samples a 1-band NDVI/ExG raster, a DTM, or a multispectral stack.
Nothing here knows about colours.
"""

import numpy as np
import rasterio
from rasterio.windows import Window, from_bounds


class RasterSampler:
    """Nearest-neighbour sampler over the raster window that covers `bbox`.

    The window is read into memory once (uint8 RGBA at 15 cm over a 1 km flight
    is ~130 MB), then every chunk of points is a vectorised fancy-index lookup.
    A point is *invalid* if it falls outside the raster, on a nodata pixel, or
    on a pixel whose alpha is below `alpha_min`.
    """

    def __init__(self, path, bands=None, alpha_band=None, alpha_min=128, bbox=None,
                 max_window_mb=4096):
        self.path = str(path)
        self.ds = rasterio.open(self.path)
        self.crs = self.ds.crs
        self.res = self.ds.res
        self.bounds = self.ds.bounds
        self.bands = list(bands) if bands else list(range(1, self.ds.count + 1))
        self.alpha_band = alpha_band
        self.alpha_min = alpha_min

        if bbox is None:
            window = Window(0, 0, self.ds.width, self.ds.height)
        else:
            window = from_bounds(*bbox, transform=self.ds.transform).round_offsets(
                op="floor").round_lengths(op="ceil")
            # clip to the raster
            col_off = max(0, int(window.col_off))
            row_off = max(0, int(window.row_off))
            col_end = min(self.ds.width, int(window.col_off + window.width))
            row_end = min(self.ds.height, int(window.row_off + window.height))
            if col_end <= col_off or row_end <= row_off:
                raise ValueError(f"bbox {bbox} does not intersect raster {self.bounds}")
            window = Window(col_off, row_off, col_end - col_off, row_end - row_off)

        read_bands = self.bands + ([alpha_band] if alpha_band else [])
        itemsize = np.dtype(self.ds.dtypes[0]).itemsize
        self.window_mb = window.width * window.height * len(read_bands) * itemsize / 1024 ** 2
        if self.window_mb > max_window_mb:
            raise MemoryError(
                f"raster window would need {self.window_mb:.0f} MB "
                f"(> max_window_mb={max_window_mb})")

        data = self.ds.read(read_bands, window=window)                # (nb, h, w)
        self.values = data[: len(self.bands)]
        self.alpha = data[-1] if alpha_band else None
        self.transform = self.ds.window_transform(window)
        self.height, self.width = self.values.shape[1:]
        self.nodata = self.ds.nodatavals[self.bands[0] - 1]

    def sample(self, x, y):
        """Return (values [n, nbands], valid [n] bool) for point arrays x, y."""
        inv = ~self.transform
        col = np.floor(inv.a * x + inv.b * y + inv.c).astype(np.int64)
        row = np.floor(inv.d * x + inv.e * y + inv.f).astype(np.int64)

        inside = (col >= 0) & (col < self.width) & (row >= 0) & (row < self.height)
        col_c = np.where(inside, col, 0)
        row_c = np.where(inside, row, 0)

        vals = self.values[:, row_c, col_c].T                        # (n, nbands)
        valid = inside.copy()

        if self.alpha is not None:
            valid &= self.alpha[row_c, col_c] >= self.alpha_min
        if self.nodata is not None:
            valid &= ~np.all(vals == self.nodata, axis=1)

        vals[~valid] = 0
        return vals, valid

    def close(self):
        self.ds.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
