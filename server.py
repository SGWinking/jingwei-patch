#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""精卫 Jingwei · 局部修复回贴工具 —— 本地服务

把从大图里裁出的局部、经 AI 修复后，自动找回原位并贴回大图；
同时输出与原图裁剪区完全同尺寸的对齐图，方便做前后对比。

本文件分三层：

  1. 算法层（SIFT 对齐、最小二乘、羽化合并、并排预览）
     —— 与旧版逐行一致，**没有做任何数学改动**。
  2. 业务层（上传、裁剪、导出、历史）
     —— 逻辑不变，只把错误处理换成统一结构。
  3. HTTP 层
     —— 统一走 toolkit_core：安全路径解析、受限 CORS、分块传输、统一 JSON。

启动：``python server.py [端口]``，默认 8786。
"""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

import toolkit_core as core

Image.MAX_IMAGE_PIXELS = None

# --------------------------------------------------------------------------
# 路径与版本
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
ASSETS = ROOT / "assets"
RUNS = ROOT / "runs"
PAIR_SESSIONS = ROOT / "pair_sessions"
DEFAULT_OUTPUT_DIR = ROOT / "exports"

APP_SLUG = "jingwei"
APP_NAME = "精卫"
APP_NAME_EN = "Jingwei"
APP_VERSION = "1.2.0"
DEFAULT_PORT = core.PORT_MAP["jingwei"]  # 8786

#: 参数上下限（SERIES-SPEC §7 / S5）
SEARCH_LONG_SIDE_RANGE = (1024, 8192)
DETECT_LONG_SIDE_RANGE = (800, 8192)
FEATHER_RANGE = (1, 400)
EXPORT_LONG_SIDE_RANGE = (256, 24000)

MAX_PREVIEW_SIDE = 1400
MAX_PAIR_PREVIEW_LONG = 4000  # 区域裁剪页里，原图缩到这个长边供浏览器查看
MAX_HISTORY_ITEMS = 30

DEFAULT_SEARCH_LONG_SIDE = 4000
DEFAULT_DETECT_LONG_SIDE = 2600
DEFAULT_EXPORT_LONG_SIDE = 0   # 0 = keep original
MAX_ROT_TOLERANCE_RAD = math.radians(6.0)
RANSAC_REPROJ = 5.0
RANSAC_CONFIDENCE = 0.995
MIN_GOOD_MATCHES = 6
SIFT_FEATURES_PER_LEVEL = 4000
FLANN_TREES = 5
FLANN_CHECKS = 50
RATIO_TEST_THRESHOLD = 0.75


@dataclass
class MatchResult:
    x: int
    y: int
    width: int
    height: int
    scale: float
    rotation_deg: float
    inliers: int
    good_matches: int
    method: str


def ensure_dirs() -> None:
    RUNS.mkdir(exist_ok=True)
    PAIR_SESSIONS.mkdir(exist_ok=True)
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ==========================================================================
# 算法层 —— 以下函数为原实现，逐行保留，请勿改动数学逻辑
# ==========================================================================


def fit_to_long_side(img: Image.Image, long_side: int) -> Image.Image:
    """把图片缩到长边 long_side。PIL.LANCZOS 在千万像素级大图上极慢，
    所以大图走 cv2.INTER_LANCZOS4（C++ 实现，释放 GIL，几十倍快），小图才用 PIL。"""
    if not long_side or long_side <= 0:
        return img
    w, h = img.size
    cur = max(w, h)
    if cur <= long_side:
        return img
    if cur <= 4000:
        ratio = long_side / float(cur)
        new = (max(1, round(w * ratio)), max(1, round(h * ratio)))
        return img.resize(new, Image.Resampling.LANCZOS)
    # 走 cv2 LANCZOS4 才不会卡死
    rgb = img.convert("RGB")
    arr = np.array(rgb)
    ratio = long_side / float(cur)
    new = (max(1, round(w * ratio)), max(1, round(h * ratio)))
    resized = cv2.resize(arr, new, interpolation=cv2.INTER_LANCZOS4)
    return Image.fromarray(resized, "RGB")


def export_outputs_to_dir(
    run_dir: Path,
    output_dir: Path,
    match_result: "MatchResult",
    feather_px: int,
    export_long_side: int,
) -> Path:
    """把一次 run 的全部成果拷/缩放一份到用户指定目录。
    返回创建的子目录路径。export_long_side=0 时保留原图原尺寸。
    注意：大图缩放走 cv2.LANCZOS4，不走 PIL，避免在千万像素级卡死。"""
    sub = output_dir / time.strftime("%Y%m%d-%H%M%S")
    sub.mkdir(parents=True, exist_ok=True)
    print(f"[export] -> {sub}  (long_side={export_long_side})", flush=True)

    def handle(src_name: str) -> None:
        src = run_dir / src_name
        if not src.exists():
            return
        t = time.time()
        dst = sub / src_name
        if src.suffix.lower() == ".json":
            shutil.copyfile(src, dst)
            return
        if export_long_side and export_long_side > 0:
            img = pil_open(src)
            w, h = img.size
            if max(w, h) > export_long_side:
                img = fit_to_long_side(img, export_long_side)
                if img.mode not in ("RGB", "RGBA", "L"):
                    img = img.convert("RGB")
                img.save(dst)
                print(f"[export] {src_name}  {w}x{h} -> {img.size}  {time.time()-t:.2f}s", flush=True)
            else:
                shutil.copyfile(src, dst)
                print(f"[export] {src_name}  {w}x{h} (skip)  {time.time()-t:.2f}s", flush=True)
        else:
            shutil.copyfile(src, dst)
            print(f"[export] {src_name}  copy {src.stat().st_size/1024/1024:.1f}MB  {time.time()-t:.2f}s", flush=True)

    for name in ("source_crop.png", "aligned_patch.png",
                "merged_hard.png", "merged_feather.png",
                "compare_crop_patch.png", "preview_match.png",
                "report.json"):
        handle(name)
    return sub


def make_pair_preview(src_path: Path, dst_path: Path, long_side: int = MAX_PAIR_PREVIEW_LONG) -> tuple[int, int, float]:
    """生成区域裁剪页用的预览图（大图长边缩到 4000）。
    千万像素级以上走 cv2.LANCZOS4，避免 PIL 在巨图上卡死/慢。"""
    img = pil_open(src_path)
    w, h = img.size
    pw, ph, ratio = fit_size((w, h), long_side)
    if ratio == 1.0:
        save_png(img.convert("RGB"), dst_path)
        return pw, ph, 1.0
    if max(w, h) <= 4000:
        preview = img.resize((pw, ph), Image.Resampling.LANCZOS)
        save_png(preview.convert("RGB"), dst_path)
    else:
        rgb = np.array(img.convert("RGB"))
        resized = cv2.resize(rgb, (pw, ph), interpolation=cv2.INTER_LANCZOS4)
        Image.fromarray(resized, "RGB").save(dst_path)
    return pw, ph, ratio


def crop_pair_at_fullres(
    before_path: Path,
    after_path: Path,
    x: int, y: int, w: int, h: int,
    output_dir: Path,
    name_prefix: str = "",
) -> Path:
    """从两张原图同一坐标全分辨率裁出小图，落到 output_dir/timestamp 子目录。"""
    sub = output_dir / (time.strftime("%Y%m%d-%H%M%S") + (("-" + name_prefix) if name_prefix else ""))
    sub.mkdir(parents=True, exist_ok=True)

    box = (x, y, x + w, y + h)
    before = pil_open(before_path)
    after = pil_open(after_path)
    bw, bh = before.size
    aw, ah = after.size
    if (bw, bh) != (aw, ah):
        raise core.ValidationError(f"两张图尺寸不一致：{bw}x{bh} 与 {aw}x{ah}。本页要求两张图像素完全一致。")
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > bw or y + h > bh:
        raise core.ValidationError(f"裁剪框 ({x},{y},{w},{h}) 越界，原图 {bw}x{bh}。")

    before_crop = before.crop(box)
    after_crop = after.crop(box)
    save_png(before_crop, sub / "before.png")
    save_png(after_crop, sub / "after.png")
    return sub


def pil_open(path: Path) -> Image.Image:
    img = Image.open(path)
    img.load()
    return ImageOps.exif_transpose(img)


def save_png(img: Image.Image, path: Path) -> None:
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    img.save(path)


def fit_size(size: tuple[int, int], max_side: int) -> tuple[int, int, float]:
    w, h = size
    if max(w, h) <= max_side:
        return w, h, 1.0
    ratio = max_side / float(max(w, h))
    return max(1, round(w * ratio)), max(1, round(h * ratio)), ratio


def cv_gray_from_pil(img: Image.Image) -> np.ndarray:
    arr = np.array(img.convert("RGB"))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)


def match(
    large_path: Path,
    patch_path: Path,
    search_long_side: int,
    detect_long_side: int = DEFAULT_DETECT_LONG_SIDE,
) -> MatchResult:
    """同尺度 SIFT 对齐。

    把大图和补丁各自缩到 detect_long_side 附近做 SIFT，让两边关键点处于
    相近的视觉尺度（一般 <6 倍差异），再 FLANN+knn+RANSAC 求相似变换。
    最后把缩比还原到原图坐标系。

    坐标关系：
      src  = 补丁缩图坐标，dst = 大图缩图坐标
      缩图空间：dst = s_local * src + t_local
      还原：大图原图坐标 = dst / down_l,  补丁原图坐标 = src / down_p
      → 大图原图 = (s_local * down_p / down_l) * 补丁原图 + t_local / down_l
    """
    large_pil = pil_open(large_path)
    patch_pil = pil_open(patch_path)

    # 大图缩到 search_long_side 用于检测；压缩比 down_l（缩图1px = 原图1/down_l px）
    lw_d, lh_d, down_l = fit_size(large_pil.size, search_long_side)
    if down_l != 1.0:
        large_detect = large_pil.resize((lw_d, lh_d), Image.Resampling.LANCZOS)
    else:
        large_detect = large_pil

    # 补丁也缩到 detect_long_side 附近，使两边处于相近视觉尺度
    pw_d, ph_d, down_p = fit_size(patch_pil.size, detect_long_side)
    if down_p != 1.0:
        patch_detect = patch_pil.resize((pw_d, ph_d), Image.Resampling.LANCZOS)
    else:
        patch_detect = patch_pil

    large_gray = cv_gray_from_pil(large_detect)
    patch_gray = cv_gray_from_pil(patch_detect)

    # contrast 限制自带稳一点，透明区域多的 AI 修复图别全黑
    patch_gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(patch_gray)
    large_gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(large_gray)

    sift = cv2.SIFT_create(nfeatures=SIFT_FEATURES_PER_LEVEL, contrastThreshold=0.02, edgeThreshold=12)
    kp_p, des_p = sift.detectAndCompute(patch_gray, None)
    kp_l, des_l = sift.detectAndCompute(large_gray, None)

    if des_p is None or des_l is None or len(kp_p) < MIN_GOOD_MATCHES or len(kp_l) < MIN_GOOD_MATCHES:
        raise core.ValidationError(
            f"特征点不足。补丁 {0 if des_p is None else len(kp_p)}/大图 {0 if des_l is None else len(kp_l)}。"
            "请确认补丁保留了一定原始边缘纹理、或检查清晰度，或调高搜索长边。"
        )

    # FLANN 加速描述子匹配（比 BFMatcher 通常快 5-10 倍）
    index_params = dict(algorithm=1, trees=FLANN_TREES)
    search_params = dict(checks=FLANN_CHECKS)
    flann = cv2.FlannBasedMatcher(index_params, search_params)
    knn = flann.knnMatch(des_p, des_l, k=2)
    good = [
        m for m, n in knn
        if m.distance < RATIO_TEST_THRESHOLD * n.distance
    ]
    if len(good) < MIN_GOOD_MATCHES:
        raise core.ValidationError(
            f"鲁棒匹配点仅 {len(good)} 对（要求≥{MIN_GOOD_MATCHES}）。"
            "AI 修复区域过大时会丢失边缘纹理，请尽量保留一圈原始边缘。"
        )

    src = np.float32([kp_p[m.queryIdx].pt for m in good])      # 补丁缩图坐标
    dst = np.float32([kp_l[m.trainIdx].pt for m in good])      # 大图缩图坐标

    affine, inliers = cv2.estimateAffinePartial2D(
        src, dst,
        method=cv2.RANSAC,
        ransacReprojThreshold=RANSAC_REPROJ,
        confidence=RANSAC_CONFIDENCE,
        maxIters=5000,
    )
    if affine is None or inliers is None:
        raise core.ValidationError("RANSAC 估计失败，无法稳定对齐这两张图。")

    inlier_mask = inliers.ravel().astype(bool)
    inlier_count = int(inlier_mask.sum())
    if inlier_count < MIN_GOOD_MATCHES:
        raise core.ValidationError(f"几何一致的内点仅 {inlier_count} 对，不足以信任结果。")

    m_mat = affine[:, :2]
    t_vec = affine[:, 2]
    theta = math.atan2(m_mat[1, 0], m_mat[0, 0])
    scale_local = math.hypot(m_mat[0, 0], m_mat[1, 0])
    if scale_local <= 1e-3:
        raise core.ValidationError("估计出的缩放接近 0，匹配不可信。")

    # 缩图空间 → 原大图坐标的还原比
    ratio_full = down_p / down_l   # 缩图 1px(补丁) → 原大图 down_p/down_l px

    src_in = src[inlier_mask]
    dst_in = dst[inlier_mask]

    if abs(theta) <= MAX_ROT_TOLERANCE_RAD:
        method = "sift_scale_translation_ls_two_scale"
        s_full, t_full = fit_scale_translation(src_in, dst_in, down_l, down_p)
        rotation_deg = math.degrees(theta)
    else:
        method = "sift_similarity_ransac_two_scale"
        s_full = scale_local * ratio_full
        t_full = t_vec * (1.0 / down_l)
        rotation_deg = math.degrees(theta)

    pw_full, ph_full = patch_pil.size
    x0 = t_full[0]
    y0 = t_full[1]
    x1 = x0 + pw_full * s_full
    y1 = y0 + ph_full * s_full

    large_w, large_h = large_pil.size
    box_x = int(round(min(x0, x1)))
    box_y = int(round(min(y0, y1)))
    box_x2 = int(round(max(x0, x1)))
    box_y2 = int(round(max(y0, y1)))
    box_x = max(0, min(large_w - 1, box_x))
    box_y = max(0, min(large_h - 1, box_y))
    box_x2 = max(1, min(large_w, box_x2))
    box_y2 = max(1, min(large_h, box_y2))

    return MatchResult(
        x=box_x,
        y=box_y,
        width=box_x2 - box_x,
        height=box_y2 - box_y,
        scale=float(s_full),
        rotation_deg=float(rotation_deg),
        inliers=inlier_count,
        good_matches=len(good),
        method=method,
    )


def fit_scale_translation(
    src: np.ndarray, dst: np.ndarray, down_l: float, down_p: float
) -> tuple[float, np.ndarray]:
    """最小二乘估计 缩图空间 s_local, tx, ty，并还原到原大图坐标。

    缩图空间：dst = s_local * src + t_local。
    原大图坐标：t_full = t_local / down_l；s_full = s_local * down_p / down_l。
    """
    n = src.shape[0]
    A = np.zeros((2 * n, 3), dtype=np.float64)
    b = np.zeros((2 * n,), dtype=np.float64)
    A[0::2, 0] = src[:, 0]
    A[0::2, 1] = 1.0
    A[1::2, 0] = src[:, 1]
    A[1::2, 2] = 1.0
    b[0::2] = dst[:, 0]
    b[1::2] = dst[:, 1]
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    s_local, tx_local, ty_local = float(sol[0]), float(sol[1]), float(sol[2])
    s_full = s_local * (down_p / down_l)
    t_full = np.array([tx_local / down_l, ty_local / down_l], dtype=np.float64)
    return s_full, t_full


def crop_and_align(large_path: Path, patch_path: Path, match_result: MatchResult, run_dir: Path) -> dict[str, Path]:
    large = pil_open(large_path)
    patch = pil_open(patch_path)
    box = (match_result.x, match_result.y, match_result.x + match_result.width, match_result.y + match_result.height)
    source_crop = large.crop(box)
    aligned_patch = patch.resize((match_result.width, match_result.height), Image.Resampling.LANCZOS)

    paths = {
        "source_crop": run_dir / "source_crop.png",
        "aligned_patch": run_dir / "aligned_patch.png",
    }
    save_png(source_crop, paths["source_crop"])
    save_png(aligned_patch, paths["aligned_patch"])
    return paths


def feather_mask(size: tuple[int, int], feather_px: int) -> Image.Image:
    w, h = size
    f = max(1, int(feather_px))
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.minimum.reduce([xx, yy, w - 1 - xx, h - 1 - yy]).astype(np.float32)
    alpha = np.clip(dist / float(f), 0.0, 1.0)
    return Image.fromarray(np.uint8(alpha * 255), "L")


def merge_image(
    large_path: Path,
    aligned_patch_path: Path,
    match_result: MatchResult,
    out_path: Path,
    mode: str,
    feather_px: int,
) -> None:
    large = pil_open(large_path).convert("RGBA")
    patch = pil_open(aligned_patch_path).convert("RGBA")
    x, y = match_result.x, match_result.y

    if mode in ("hard", "alpha"):
        mask = patch.getchannel("A")
    elif mode == "feather":
        base_mask = feather_mask(patch.size, feather_px)
        alpha = patch.getchannel("A")
        mask = mask_multiply(base_mask, alpha)
    else:
        raise core.ValidationError(f"未知合并模式: {mode}")

    large.paste(patch, (x, y), mask)
    save_png(large.convert("RGB"), out_path)


def mask_multiply(a: Image.Image, b: Image.Image) -> Image.Image:
    aa = np.asarray(a, dtype=np.uint16)
    bb = np.asarray(b, dtype=np.uint16)
    return Image.fromarray(np.uint8((aa * bb) // 255), "L")


def preview_with_rect(large_path: Path, match_result: MatchResult, out_path: Path) -> None:
    large = pil_open(large_path).convert("RGB")
    nw, nh, ratio = fit_size(large.size, MAX_PREVIEW_SIDE)
    preview = large.resize((nw, nh), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(preview)
    rect = [
        round(match_result.x * ratio),
        round(match_result.y * ratio),
        max(round(match_result.x * ratio) + 1, round((match_result.x + match_result.width) * ratio)),
        max(round(match_result.y * ratio) + 1, round((match_result.y + match_result.height) * ratio)),
    ]
    for inset in range(3):
        draw.rectangle(
            [rect[0] - inset, rect[1] - inset, rect[2] + inset, rect[3] + inset],
            outline=(239, 68, 68),
            width=1,
        )
    save_png(preview, out_path)


def side_by_side_preview(left_path: Path, right_path: Path, out_path: Path, gap: int = 16, bg=(255, 255, 255)) -> None:
    left = pil_open(left_path).convert("RGB")
    right = pil_open(right_path).convert("RGB")
    target_h = min(720, left.height, right.height)
    if target_h < 1:
        target_h = 1
    lw = max(1, round(left.width * target_h / left.height))
    rw = max(1, round(right.width * target_h / right.height))
    left = left.resize((lw, target_h), Image.Resampling.LANCZOS)
    right = right.resize((rw, target_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (lw + rw + gap, target_h), bg)
    canvas.paste(left, (0, 0))
    canvas.paste(right, (lw + gap, 0))
    save_png(canvas, out_path)


def image_data_url(path: Path) -> str:
    data = path.read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def write_report(run_dir: Path, match_result: MatchResult, options: dict) -> Path:
    report = {
        "match": asdict(match_result),
        "options": options,
        "files": {
            "source_crop": "source_crop.png",
            "aligned_patch": "aligned_patch.png",
            "merged_hard": "merged_hard.png",
            "merged_feather": "merged_feather.png",
            "preview": "preview_match.png",
            "compare": "compare_crop_patch.png",
        },
    }
    path = run_dir / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def zip_run(run_dir: Path) -> Path:
    """PNG/JPG 本身已压缩，再走 DEFLATED 会变更大且更慢（实测 791MB→1.12GB）。
    直接 STORE 模式，仅做容器打包，速度是原来的 N 倍且体积更小。"""
    zip_path = run_dir / "exports.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for f in run_dir.iterdir():
            if f.is_file() and f.name != zip_path.name:
                zf.write(f, f.name)
    return zip_path


# ==========================================================================
# multipart 解析
# ==========================================================================


def parse_multipart(headers, body: bytes) -> dict:
    ctype = headers.get("Content-Type", "")
    if "boundary=" not in ctype:
        return {}
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
    sep = b"--" + boundary
    parts: dict = {}
    chunks = body.split(sep)
    for chunk in chunks:
        if not chunk or chunk == b"--\r\n" or chunk == b"--":
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        if chunk.startswith(b"--"):
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        head_blob, _, body_blob = chunk.partition(b"\r\n\r\n")
        name = None
        filename = None
        for line in head_blob.split(b"\r\n"):
            line_s = line.decode("utf-8", "replace")
            if line_s.lower().startswith("content-disposition:"):
                for kv in line_s.split(";"):
                    kv = kv.strip()
                    if kv.startswith("name="):
                        name = kv[5:].strip().strip('"')
                    elif kv.startswith("filename="):
                        filename = kv[9:].strip().strip('"')
        if name is None:
            continue
        if filename is not None:
            parts[name] = {"filename": filename, "data": body_blob}
        else:
            parts[name] = body_blob.decode("utf-8", "replace")
    return parts


# 未启用备用实现：可在解析时边读边落盘，避免整段请求体进内存。
# 当前两个上传接口仍是「先读完 body 再 parse_multipart」，因为该实现尚未做回归验证。
# 见 CHANGELOG「已知待办」第 1 条。改用它之前请先补上传大小/损坏体的测试。
def parse_multipart_streaming(rfile, headers, on_file) -> dict:
    """流式 multipart 解析：遇到 file part 直接调 on_file 返回的 (chunk_bytes, finalize) 写盘。
    避免 300+MB 整段 body 进内存。"""
    ctype = headers.get("Content-Type", "")
    if "boundary=" not in ctype:
        return {}
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
    sep = b"--" + boundary

    READ_CHUNK = 256 * 1024
    buf = b""
    parts: dict = {}
    state = 0  # 0=seek --boundary; 1=read headers; 2=read body
    cur_name = None
    cur_filename = None
    cur_headers = b""
    cur_writer = None
    finished = False

    while not finished:
        chunk = rfile.read(READ_CHUNK)
        if not chunk:
            break
        buf = buf + chunk
        while True:
            if state == 0:
                idx = buf.find(sep)
                if idx < 0:
                    break
                buf = buf[idx + len(sep):]
                state = 1
                cur_headers = b""
            elif state == 1:
                idx = buf.find(b"\r\n\r\n")
                if idx < 0:
                    cur_headers += buf
                    buf = b""
                    break
                cur_headers += buf[:idx]
                buf = buf[idx + 4:]
                name = None
                filename = None
                for line in cur_headers.split(b"\r\n"):
                    ls = line.decode("utf-8", "replace")
                    if ls.lower().startswith("content-disposition:"):
                        for kv in ls.split(";"):
                            kv = kv.strip()
                            if kv.startswith("name="):
                                name = kv[5:].strip().strip('"')
                            elif kv.startswith("filename="):
                                filename = kv[9:].strip().strip('"')
                cur_name = name
                cur_filename = filename
                if name is not None and filename is not None:
                    cur_writer = on_file(name, filename, cur_headers)
                else:
                    cur_writer = None
                state = 2
            else:  # state == 2
                idx = buf.find(b"\r\n" + sep)
                if idx < 0:
                    safe = max(0, len(buf) - len(sep) - 4)
                    if safe > 0:
                        if cur_writer is not None:
                            cur_writer(buf[:safe], False)
                        elif cur_name is not None:
                            parts[cur_name] = parts.get(cur_name, b"") + buf[:safe]
                        buf = buf[safe:]
                    break
                tail = buf[:idx]
                if cur_writer is not None:
                    cur_writer(tail, True)
                elif cur_name is not None:
                    parts[cur_name] = parts.get(cur_name, b"") + tail
                buf = buf[idx + 2 + len(sep):]
                if cur_writer is not None:
                    parts[cur_name] = {"filename": cur_filename, "data": b"<file>"}
                cur_name = None
                cur_filename = None
                cur_writer = None
                if buf.startswith(b"--"):
                    finished = True
                    break  # final boundary, break both loops
                state = 1  # next part headers already behind --boundary
    return parts


# ==========================================================================
# 业务层
# ==========================================================================


def pick_folder(initial_dir: str = "") -> str | None:
    """子进程调 tkinter 弹目录选择框，避开 Tk 多线程崩溃。"""
    init = (initial_dir or "").replace("\\", "/")
    script = (
        "import tkinter as tk; from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        f"d = filedialog.askdirectory(initialdir={repr(init) if init else 'None'}, title='选择导出目录')\n"
        "import sys; sys.stdout.write(d if d else '')\n"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=180,
        )
        path = (out.stdout or "").strip()
        return path if path else None
    except Exception:
        return None


def list_history(limit: int = MAX_HISTORY_ITEMS) -> list[dict]:
    """扫描 runs/ 目录，按修改时间倒序返回历史记录摘要。"""
    if not RUNS.exists():
        return []
    items = []
    for d in RUNS.iterdir():
        if not d.is_dir():
            continue
        report = d / "report.json"
        if not report.exists():
            continue
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            m = data.get("match", {})
            opts = data.get("options", {})
            items.append({
                "run_id": d.name,
                "mtime": int(d.stat().st_mtime),
                "x": m.get("x"), "y": m.get("y"),
                "width": m.get("width"), "height": m.get("height"),
                "scale": m.get("scale"),
                "inliers": m.get("inliers"), "good_matches": m.get("good_matches"),
                "method": m.get("method"),
                "elapsed_sec": opts.get("elapsed_sec"),
                "output_dir": opts.get("output_dir"),
            })
        except Exception:
            continue
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def build_run_response(run_id: str) -> dict | None:
    """根据 run_id 重建一份和 /api/analyze 一致的响应。"""
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        return None
    run_dir = RUNS / run_id
    if not run_dir.exists() or not (run_dir / "report.json").exists():
        return None
    data = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    match_obj = data.get("match", {})
    mr = MatchResult(
        x=int(match_obj.get("x", 0)), y=int(match_obj.get("y", 0)),
        width=int(match_obj.get("width", 0)), height=int(match_obj.get("height", 0)),
        scale=float(match_obj.get("scale", 0)),
        rotation_deg=float(match_obj.get("rotation_deg", 0)),
        inliers=int(match_obj.get("inliers", 0)),
        good_matches=int(match_obj.get("good_matches", 0)),
        method=match_obj.get("method", ""),
    )
    preview_path = run_dir / "preview_match.png"
    compare_path = run_dir / "compare_crop_patch.png"
    zip_path = run_dir / "exports.zip"
    return {
        "run_id": run_id,
        "match": asdict(mr),
        "elapsed_sec": data.get("options", {}).get("elapsed_sec", 0),
        "preview": image_data_url(preview_path) if preview_path.exists() else "",
        "compare": image_data_url(compare_path) if compare_path.exists() else "",
        "links": {
            "source_crop": f"/runs/{run_id}/source_crop.png",
            "aligned_patch": f"/runs/{run_id}/aligned_patch.png",
            "merged_hard": f"/runs/{run_id}/merged_hard.png",
            "merged_feather": f"/runs/{run_id}/merged_feather.png",
            "preview_png": f"/runs/{run_id}/preview_match.png",
            "compare_png": f"/runs/{run_id}/compare_crop_patch.png",
            "report": f"/runs/{run_id}/report.json",
            "zip": (f"/runs/{run_id}/{zip_path.name}" if zip_path.exists() else ""),
        },
        "output_dir": data.get("options", {}).get("output_dir", ""),
        "export_long_side": data.get("options", {}).get("export_long_side", 0),
    }


def read_multipart_body(handler, max_bytes: int) -> bytes:
    """读取 multipart 请求体，带明确上限与中文提示（SERIES-SPEC §7 / S4）。"""
    raw_length = handler.headers.get("Content-Length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError:
            raise core.ValidationError("Content-Length 不合法。", detail=f"value={raw_length!r}")
        if length <= 0:
            raise core.ValidationError("上传内容为空。")
        if length > max_bytes:
            raise core.PayloadTooLargeError(
                f"上传内容过大，单次上限 {max_bytes // (1024 ** 3)} GB。",
                detail=f"content-length={length}",
            )
        return handler.rfile.read(length)

    body = b""
    while True:
        chunk = handler.rfile.read(1024 * 1024)
        if not chunk:
            break
        body += chunk
        if len(body) > max_bytes:
            raise core.PayloadTooLargeError(
                f"上传内容过大，单次上限 {max_bytes // (1024 ** 3)} GB。",
                detail=f"received={len(body)}",
            )
    return body


def _clamp_int(form: dict, key: str, default: int, low: int, high: int, label: str) -> int:
    raw = form.get(key, str(default))
    if not isinstance(raw, str):
        raw = str(raw)
    try:
        value = int(str(raw).strip() or default)
    except ValueError:
        raise core.ValidationError(f"{label}必须是整数。", field=key, detail=f"value={raw!r}")
    if value < low or value > high:
        raise core.ValidationError(
            f"{label}必须在 {low} 到 {high} 之间。", field=key, detail=f"value={value}"
        )
    return value


def resolve_output_dir(raw: str | None) -> Path | None:
    """把用户填的输出目录变成可写目录；空则返回 None（表示不额外导出）。"""
    if not isinstance(raw, str):
        return None
    value = raw.strip().strip('"')
    if not value:
        return None
    try:
        path = Path(value).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise core.ValidationError("导出目录不可用，请重新选择。", field="output_dir", detail=value)
        return path.resolve()
    except core.ToolkitError:
        raise
    except Exception as exc:
        raise core.ValidationError("导出目录不可用，请重新选择。", field="output_dir", detail=f"{value} ({exc})")


# ==========================================================================
# HTTP 层
# ==========================================================================


class Handler(BaseHTTPRequestHandler):
    server_version = f"Jingwei/{APP_VERSION}"

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    # ----- 基础响应（全部走 toolkit_core） -----

    def send_ok(self, payload: dict | None = None) -> None:
        core.api_ok(self, payload)

    def serve_file(self, path: Path, ctype: str | None = None) -> None:
        core.stream_file(self, path, ctype)

    # ----- 路由 -----

    def do_OPTIONS(self) -> None:  # noqa: N802
        core.handle_options(self)

    def do_GET(self) -> None:  # noqa: N802
        path = core.urlparse_path(self.path)
        try:
            if path == "/api/health":
                core.api_ok(self, core.health_payload(
                    APP_SLUG, APP_VERSION, self.server.server_address[1],
                    name=APP_NAME, nameEn=APP_NAME_EN,
                    outputDir=str(DEFAULT_OUTPUT_DIR),
                ))
                return

            query = self.path.split("?", 1)[1] if "?" in self.path else ""

            if path == "/api/history":
                qs = parse_qs(query)
                try:
                    limit = int(qs.get("limit", [str(MAX_HISTORY_ITEMS)])[0])
                except ValueError:
                    limit = MAX_HISTORY_ITEMS
                limit = max(1, min(200, limit))
                self.send_ok({"items": list_history(limit)})
                return

            if path == "/api/run":
                qs = parse_qs(query)
                run_id = qs.get("id", [""])[0]
                if not run_id:
                    raise core.ValidationError("缺少 run id。", field="id")
                resp = build_run_response(run_id)
                if resp is None:
                    raise core.NotFoundError(f"找不到这次运行记录：{run_id}", detail=run_id)
                self.send_ok(resp)
                return

            if path == "/api/default_output_dir":
                self.send_ok({"path": str(DEFAULT_OUTPUT_DIR)})
                return

            if path.startswith("/api/pair_preview/"):
                rest = path[len("/api/pair_preview/"):]
                target = core.safe_join(PAIR_SESSIONS, rest)
                self.serve_file(target, "image/png")
                return

            if path.startswith("/runs/"):
                rest = path[len("/runs/"):]
                target = core.safe_join(RUNS, rest)
                self.serve_file(target)
                return

            if path.startswith("/assets/"):
                rest = path[len("/assets/"):]
                target = core.safe_join(ASSETS, rest)
                self.serve_file(target)
                return

            # 静态文件：web/ 之内，越界由 safe_join 抛 PathEscapeError
            rel = path.lstrip("/") or "index.html"
            if core.serve_static(self, WEB, rel):
                return

            raise core.NotFoundError(f"找不到页面：{path}", detail=path)
        except Exception as exc:
            core.api_exception(self, exc)

    def do_POST(self) -> None:  # noqa: N802
        path = core.urlparse_path(self.path)
        try:
            if path == "/api/analyze":
                self.handle_analyze()
                return
            if path == "/api/pick_folder":
                self.handle_pick_folder()
                return
            if path == "/api/upload_pair":
                self.handle_upload_pair()
                return
            if path == "/api/crop_pair":
                self.handle_crop_pair()
                return
            raise core.NotFoundError(f"找不到接口：{path}", detail=path)
        except Exception as exc:
            core.api_exception(self, exc)

    # ----- 业务处理 -----

    def handle_pick_folder(self) -> None:
        payload = core.read_json(self)
        initial = str(payload.get("initial_dir", "") or "")
        self.send_ok({"path": pick_folder(initial) or ""})

    def handle_analyze(self) -> None:
        MAX_BODY = 2 * 1024 * 1024 * 1024  # 2 GB
        body = read_multipart_body(self, MAX_BODY)
        form = parse_multipart(self.headers, body)

        large_item = form.get("large")
        patch_item = form.get("patch")
        if not isinstance(large_item, dict) or not isinstance(patch_item, dict):
            raise core.ValidationError("缺少原始大图或局部修复图。")

        search_long_side = _clamp_int(
            form, "search_long_side", DEFAULT_SEARCH_LONG_SIDE, *SEARCH_LONG_SIDE_RANGE, "大图搜索长边")
        detect_long_side = _clamp_int(
            form, "detect_long_side", DEFAULT_DETECT_LONG_SIDE, *DETECT_LONG_SIDE_RANGE, "补丁检测长边")
        feather_px = _clamp_int(form, "feather_px", 40, *FEATHER_RANGE, "羽化像素")

        export_raw = form.get("export_long_side", str(DEFAULT_EXPORT_LONG_SIDE))
        export_long_side = 0
        if str(export_raw).strip() not in ("", "0"):
            export_long_side = _clamp_int(
                form, "export_long_side", 0, *EXPORT_LONG_SIDE_RANGE, "导出长边")

        output_dir = resolve_output_dir(form.get("output_dir"))

        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        run_dir = RUNS / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        large_path = run_dir / core.safe_filename(large_item.get("filename"), fallback="large.png")
        patch_path = run_dir / core.safe_filename(patch_item.get("filename"), fallback="patch.png")
        large_path.write_bytes(large_item["data"])
        patch_path.write_bytes(patch_item["data"])

        t0 = time.time()
        print(f"[analyze] run_id={run_id}  large={large_path.name}  patch={patch_path.name}", flush=True)
        print(f"[analyze] match start  search={search_long_side}  detect={detect_long_side}", flush=True)
        match_result = match(large_path, patch_path, search_long_side, detect_long_side)
        elapsed = time.time() - t0
        print(f"[analyze] match done  {elapsed:.2f}s  "
              f"box=({match_result.x},{match_result.y},{match_result.width},{match_result.height}) "
              f"scale={match_result.scale:.4f}", flush=True)

        print("[analyze] crop_and_align", flush=True)
        paths = crop_and_align(large_path, patch_path, match_result, run_dir)
        preview_path = run_dir / "preview_match.png"
        compare_path = run_dir / "compare_crop_patch.png"
        print("[analyze] preview", flush=True)
        preview_with_rect(large_path, match_result, preview_path)
        side_by_side_preview(paths["source_crop"], paths["aligned_patch"], compare_path)

        merged_hard = run_dir / "merged_hard.png"
        merged_feather = run_dir / "merged_feather.png"
        print("[analyze] merge hard", flush=True)
        merge_image(large_path, paths["aligned_patch"], match_result, merged_hard, "hard", feather_px)
        print("[analyze] merge feather", flush=True)
        merge_image(large_path, paths["aligned_patch"], match_result, merged_feather, "feather", feather_px)

        write_report(
            run_dir,
            match_result,
            {
                "search_long_side": search_long_side,
                "detect_long_side": detect_long_side,
                "feather_px": feather_px,
                "elapsed_sec": round(elapsed, 3),
                "output_dir": str(output_dir) if output_dir else "",
                "export_long_side": export_long_side,
            },
        )
        print("[analyze] zip", flush=True)
        zip_path = zip_run(run_dir)
        print(f"[analyze] zip done: {zip_path.name} "
              f"({zip_path.stat().st_size/1024/1024:.1f}MB)", flush=True)

        saved_subdir = ""
        if output_dir is not None:
            print(f"[analyze] export to {output_dir}  long_side={export_long_side}", flush=True)
            t_exp = time.time()
            saved_subdir = str(export_outputs_to_dir(
                run_dir, output_dir, match_result, feather_px, export_long_side))
            print(f"[analyze] export done in {time.time()-t_exp:.2f}s", flush=True)

        self.send_ok({
            "run_id": run_id,
            "match": asdict(match_result),
            "elapsed_sec": round(elapsed, 3),
            "preview": image_data_url(preview_path),
            "compare": image_data_url(compare_path),
            "links": {
                "source_crop": f"/runs/{run_id}/source_crop.png",
                "aligned_patch": f"/runs/{run_id}/aligned_patch.png",
                "merged_hard": f"/runs/{run_id}/merged_hard.png",
                "merged_feather": f"/runs/{run_id}/merged_feather.png",
                "preview_png": f"/runs/{run_id}/preview_match.png",
                "compare_png": f"/runs/{run_id}/compare_crop_patch.png",
                "report": f"/runs/{run_id}/report.json",
                "zip": f"/runs/{run_id}/{zip_path.name}",
            },
            "output_dir": str(output_dir) if output_dir else "",
            "saved_subdir": saved_subdir,
            "export_long_side": export_long_side,
        })

    def handle_upload_pair(self) -> None:
        MAX_PAIR = 3 * 1024 * 1024 * 1024  # 3 GB
        body = read_multipart_body(self, MAX_PAIR)
        form = parse_multipart(self.headers, body)

        before_item = form.get("before")
        after_item = form.get("after")
        if not isinstance(before_item, dict) or not isinstance(after_item, dict):
            raise core.ValidationError("缺少修复前或修复后图片。")

        session_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        sd = PAIR_SESSIONS / session_id
        sd.mkdir(parents=True, exist_ok=True)
        print(f"[pair] upload {session_id}  total={len(body)/1024/1024:.1f}MB", flush=True)

        before_path = sd / "before.png"
        after_path = sd / "after.png"
        before_path.write_bytes(before_item["data"])
        after_path.write_bytes(after_item["data"])

        t1 = time.time()
        after_img = pil_open(after_path)
        aw, ah = after_img.size
        before_img = pil_open(before_path)
        bw, bh = before_img.size
        print(f"[pair] loaded after={aw}x{ah} before={bw}x{bh}  {time.time()-t1:.2f}s", flush=True)

        # 尺寸不一致：把大的缩到小的（cv2.LANCZOS4，巨图不卡）
        resized_msg = ""
        if (aw, ah) != (bw, bh):
            nz = min((aw, ah), (bw, bh), key=lambda s: s[0] * s[1])   # 较小那个
            big = "after" if (aw * ah) > (bw * bh) else "before"
            big_w, big_h = (aw, ah) if big == "after" else (bw, bh)
            src_path = after_path if big == "after" else before_path
            print(f"[pair] resizing {big} {big_w}x{big_h} -> {nz[0]}x{nz[1]}", flush=True)
            t_rs = time.time()
            # 大图 cv2，小图 PIL
            if big_w > 4000 or big_h > 4000:
                pil = pil_open(src_path).convert("RGB")
                arr = np.array(pil)
                resized = cv2.resize(arr, (nz[0], nz[1]), interpolation=cv2.INTER_LANCZOS4)
                Image.fromarray(resized, "RGB").save(src_path)
            else:
                pil = pil_open(src_path).resize((nz[0], nz[1]), Image.Resampling.LANCZOS)
                save_png(pil, src_path)
            print(f"[pair] resized in {time.time()-t_rs:.2f}s", flush=True)
            aw, ah = nz
            bw, bh = nz
            resized_msg = f"已自动将 {big} 从 {big_w}x{big_h} 缩到 {nz[0]}x{nz[1]}（LANCZOS4 高质量）"

        t2 = time.time()
        apw, aph, ar = make_pair_preview(after_path, sd / "after_preview.png")
        bpw, bph, _br = make_pair_preview(before_path, sd / "before_preview.png")
        print(f"[pair] preview {apw}x{aph}  {time.time()-t2:.2f}s", flush=True)

        self.send_ok({
            "session_id": session_id,
            "full_size": {"w": aw, "h": ah},
            "before_full_size": {"w": bw, "h": bh},
            "preview_size": {"w": apw, "h": aph},
            "ratio_preview_to_full": ar,
            "preview_url": f"/api/pair_preview/{session_id}/after_preview.png",
            "before_preview_url": f"/api/pair_preview/{session_id}/before_preview.png",
            "resized_msg": resized_msg,
        })

    def handle_crop_pair(self) -> None:
        MAX_BODY = 64 * 1024 * 1024  # 只有表单字段，没有文件
        body = read_multipart_body(self, MAX_BODY)
        form = parse_multipart(self.headers, body)

        def field(key: str) -> str:
            value = form.get(key, "")
            return value.strip() if isinstance(value, str) else ""

        session_id = field("session_id")
        if not session_id:
            raise core.ValidationError("缺少 session_id。", field="session_id")
        if "/" in session_id or "\\" in session_id or ".." in session_id:
            raise core.ValidationError("session_id 不合法。", field="session_id", detail=session_id)

        sd = PAIR_SESSIONS / session_id
        if not sd.is_dir():
            raise core.NotFoundError(f"找不到这次会话：{session_id}", detail=session_id)

        before_path = sd / "before.png"
        after_path = sd / "after.png"
        if not before_path.exists() or not after_path.exists():
            raise core.ValidationError("这次会话里缺少图片文件，请重新加载预览。")

        x = _clamp_int(form, "x", 0, 0, 1_000_000, "裁剪 X")
        y = _clamp_int(form, "y", 0, 0, 1_000_000, "裁剪 Y")
        w = _clamp_int(form, "w", 0, 1, 1_000_000, "裁剪宽度")
        h = _clamp_int(form, "h", 0, 1, 1_000_000, "裁剪高度")

        out = resolve_output_dir(field("output_dir")) or DEFAULT_OUTPUT_DIR
        name_prefix = core.safe_filename(field("name_prefix"), fallback="") if field("name_prefix") else ""

        print(f"[pair] crop s={session_id} box=({x},{y},{w},{h}) out={out}", flush=True)
        t0 = time.time()
        sub = crop_pair_at_fullres(before_path, after_path, x, y, w, h, out, name_prefix)
        print(f"[pair] crop done {time.time()-t0:.2f}s  -> {sub}", flush=True)

        self.send_ok({
            "saved_dir": str(sub),
            "before": str(sub / "before.png"),
            "after": str(sub / "after.png"),
            "box": {"x": x, "y": y, "w": w, "h": h},
        })


def main() -> int:
    ensure_dirs()
    host = "127.0.0.1"
    port = int(os.environ.get("JINGWEI_PORT", str(DEFAULT_PORT)))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"端口不合法：{sys.argv[1]}")
            return 2
    httpd = ThreadingHTTPServer((host, port), Handler)
    core.print_banner(APP_NAME, APP_NAME_EN, APP_VERSION, port)
    print(f"  导出目录：{DEFAULT_OUTPUT_DIR}")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
