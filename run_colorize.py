"""Stage 1 — colorize a LiDAR point cloud with the RGB of an orthomosaic.

    python run_colorize.py config.yaml [--dry-run]

Geometry and classification are preserved bit for bit; only RGB (and, under the
'flag' policy, a color_valid extra dimension) are added. The per-point raster
lookup lives in raster_sampler.RasterSampler, which is index-agnostic: the same
call samples an ExG/TGI/NDVI raster the day we fuse indices instead of colours.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import laspy
import numpy as np
import rasterio
import yaml
from pyproj import CRS

from raster_sampler import RasterSampler

# input point format -> output point format carrying RGB (LAS spec)
RGB_FORMAT = {0: 2, 1: 3, 2: 2, 3: 3, 6: 7, 7: 7, 8: 8}


def fail(msg):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="validate and report, write nothing")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    root = Path(args.config).resolve().parent
    cloud_path = (root / cfg["input"]["cloud"]).resolve()
    raster_path = (root / cfg["input"]["raster"]).resolve()
    policy = cfg.get("no_color_policy", "flag")
    if policy not in ("flag", "skip", "drop"):
        fail(f"no_color_policy must be flag|skip|drop, got '{policy}'")

    # ---------------------------------------------------------------- validate
    if not cloud_path.is_file():
        fail(f"point cloud not found: {cloud_path}")
    if not raster_path.is_file():
        fail(f"orthomosaic not found: {raster_path}")

    expected = CRS.from_user_input(cfg["crs"])
    with laspy.open(str(cloud_path)) as f:
        hdr = f.header
    cloud_crs = hdr.parse_crs()
    with rasterio.open(str(raster_path)) as ds:
        raster_crs = CRS.from_wkt(ds.crs.to_wkt())
        raster_bounds = ds.bounds
        raster_res = ds.res
        raster_count = ds.count

    if cloud_crs is None:
        fail(f"point cloud declares no CRS; expected {expected.to_string()}")
    if not cloud_crs.equals(expected):
        fail(f"point cloud CRS {cloud_crs.to_string()} != config crs {expected.to_string()}")
    if not raster_crs.equals(expected):
        fail(f"raster CRS {raster_crs.to_string()} != config crs {expected.to_string()}")

    cbox = (hdr.mins[0], hdr.mins[1], hdr.maxs[0], hdr.maxs[1])
    if cbox[0] >= raster_bounds.right or cbox[2] <= raster_bounds.left \
            or cbox[1] >= raster_bounds.top or cbox[3] <= raster_bounds.bottom:
        fail(f"cloud bbox {cbox} does not intersect raster bbox {tuple(raster_bounds)}")

    samp = cfg.get("raster_sampling", {})
    bands = samp.get("bands", [1, 2, 3])
    if max(bands) > raster_count:
        fail(f"config asks for band {max(bands)} but the raster has {raster_count}")
    if len(bands) != 3:
        fail(f"colorization needs exactly 3 bands (R,G,B), got {bands}")

    out_pf = RGB_FORMAT.get(hdr.point_format.id)
    if out_pf is None:
        fail(f"no RGB-capable output format for input point format {hdr.point_format.id}")

    area_cloud = (cbox[2] - cbox[0]) * (cbox[3] - cbox[1])
    density = hdr.point_count / area_cloud if area_cloud else 0.0
    px = raster_res[0] * raster_res[1]

    print(f"[OK] CRS            {expected.to_string()} (cloud + raster)")
    print(f"[OK] cloud          {hdr.point_count:,} pts, format {hdr.point_format.id} "
          f"-> {out_pf}, LAS {hdr.version}"
          + (" -> 1.4 (ExtraBytes)" if policy == "flag" and str(hdr.version) < "1.4" else ""))
    print(f"[OK] bbox           cloud inside raster bbox")
    print(f"[OK] raster         {raster_res[0]:.3f} m/px, {raster_count} bands, "
          f"sampling {bands} alpha={samp.get('alpha_band')}")
    print(f"[OK] resolution     {1 / px:.2f} px/m2 vs cloud density "
          f"{density:.2f} pts/m2 ({density * px:.2f} pts per pixel)")
    print(f"[OK] policy         {policy}")

    out_dir = (root / cfg["output"]["dir"]).resolve()
    out_path = out_dir / cfg["output"]["name"]
    if args.dry_run:
        print(f"[DRY-RUN] would write {out_path}")
        return

    # ---------------------------------------------------------------- colorize
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)

    # ExtraBytes VLRs are only standard from LAS 1.4 on: PDAL silently ignores the
    # color_valid dimension if it is written into a 1.2 file.
    out_version = "1.4" if policy == "flag" and str(hdr.version) < "1.4" else str(hdr.version)
    out_hdr = laspy.LasHeader(version=out_version, point_format=laspy.PointFormat(out_pf))
    out_hdr.scales = hdr.scales
    out_hdr.offsets = hdr.offsets
    out_hdr.global_encoding = hdr.global_encoding
    out_hdr.add_crs(cloud_crs)
    if policy == "flag":
        out_hdr.add_extra_dim(laspy.ExtraBytesParams(
            name="color_valid", type="u1",
            description="1=RGB from raster, 0=none"))   # LAS caps this at 32 bytes

    sampler = RasterSampler(
        raster_path, bands=bands, alpha_band=samp.get("alpha_band"),
        alpha_min=samp.get("alpha_min", 128), bbox=cbox,
        max_window_mb=samp.get("max_window_mb", 4096))
    print(f"[..] raster window in memory: {sampler.window_mb:.0f} MB")
    scale16 = 257 if np.dtype(sampler.values.dtype) == np.uint8 else 1

    n_total = n_valid = n_written = 0
    chunk_size = int(cfg.get("chunk_size", 5_000_000))
    with laspy.open(str(cloud_path)) as fin, \
            laspy.open(str(out_path), mode="w", header=out_hdr) as fout:
        for chunk in fin.chunk_iterator(chunk_size):
            vals, valid = sampler.sample(np.asarray(chunk.x), np.asarray(chunk.y))
            n_total += len(chunk)
            n_valid += int(valid.sum())

            keep = valid if policy == "drop" else np.ones(len(chunk), dtype=bool)
            rec = laspy.ScaleAwarePointRecord.zeros(int(keep.sum()), header=out_hdr)
            rec.copy_fields_from(chunk[keep] if policy == "drop" else chunk)

            rgb = vals[keep].astype(np.uint16) * scale16
            rec.red, rec.green, rec.blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
            if policy == "flag":
                rec.color_valid = valid.astype(np.uint8)

            fout.write_points(rec)
            n_written += len(rec)
            print(f"    {n_total:,}/{hdr.point_count:,} pts", end="\r", flush=True)
    sampler.close()

    elapsed = time.time() - t0
    n_invalid = n_total - n_valid
    pct = 100 * n_valid / n_total if n_total else 0.0
    print(f"\n[OK] coloured       {n_valid:,} / {n_total:,} pts ({pct:.2f} %)")
    print(f"[OK] no valid colour {n_invalid:,} pts ({100 - pct:.2f} %) -> policy '{policy}'")
    print(f"[OK] written        {out_path} ({n_written:,} pts, "
          f"{out_path.stat().st_size / 1024 ** 2:.1f} MB) in {elapsed:.0f} s")

    manifest = {
        "stage": "colorize",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "input_cloud": str(cloud_path),
        "input_raster": str(raster_path),
        "output": str(out_path),
        "crs": expected.to_string(),
        "point_format_in": hdr.point_format.id,
        "point_format_out": out_pf,
        "las_version_in": str(hdr.version),
        "las_version_out": out_version,
        "no_color_policy": policy,
        "bands": bands,
        "alpha_band": samp.get("alpha_band"),
        "alpha_min": samp.get("alpha_min", 128),
        "rgb_scale_8bit_to_16bit": scale16,
        "points_in": int(n_total),
        "points_coloured": int(n_valid),
        "points_no_colour": int(n_invalid),
        "pct_coloured": round(pct, 4),
        "points_out": int(n_written),
        "raster_res_m": [raster_res[0], raster_res[1]],
        "cloud_density_pts_m2": round(density, 2),
        "pts_per_pixel": round(density * px, 2),
        "elapsed_s": round(elapsed, 1),
        "versions": {"laspy": laspy.__version__, "rasterio": rasterio.__version__,
                     "numpy": np.__version__},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    report = f"""# Colorize report

| | |
|---|---|
| Cloud | `{cloud_path.name}` ({n_total:,} pts, format {hdr.point_format.id} -> {out_pf}) |
| Raster | `{raster_path.name}` ({raster_res[0]:.3f} m/px, bands {bands}, alpha {samp.get('alpha_band')}) |
| CRS | {expected.to_string()} |
| Coloured | {n_valid:,} pts ({pct:.2f} %) |
| No valid colour | {n_invalid:,} pts ({100 - pct:.2f} %), policy `{policy}` |
| Output | `{out_path.name}` ({n_written:,} pts) |
| Resolution vs density | {1 / px:.2f} px/m2 vs {density:.2f} pts/m2 -> {density * px:.2f} pts share each pixel |
| Time | {elapsed:.0f} s |
"""
    (out_dir / "reporte_colorize.md").write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
