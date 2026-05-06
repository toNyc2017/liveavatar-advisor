"""
Replace the background of a talking-head video using MediaPipe's
selfie segmentation model (via the modern Tasks API).

Usage:
    python bg_replace.py INPUT.mp4 OUTPUT.mp4 BACKGROUND.{jpg,png,mp4}
    python bg_replace.py INPUT.mp4 OUTPUT.mp4 --color 128,128,130

The output is a video file matching the input's resolution / fps / duration
with the background swapped. Audio from the input is muxed back in via ffmpeg
(OpenCV's writer is video-only).

Compatible with mediapipe >= 0.10.30 on Python 3.13 / 3.14, where the legacy
`mp.solutions.selfie_segmentation` namespace was removed in favor of the
`mediapipe.tasks.python.vision.ImageSegmenter` Tasks API.

The first run auto-downloads the segmenter model (~1.5 MB) to
~/.cache/mediapipe/selfie_segmenter.tflite. Subsequent runs use the cached copy.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
import numpy as np


MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/image_segmenter/"
    "selfie_segmenter/float16/1/selfie_segmenter.tflite"
)
MODEL_PATH = os.path.expanduser("~/.cache/mediapipe/selfie_segmenter.tflite")


def ensure_model() -> str:
    """Download the segmenter model on first run, then cache it locally."""
    if os.path.exists(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 100_000:
        return MODEL_PATH
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    print(f"downloading selfie_segmenter.tflite to {MODEL_PATH} (one-time)...")
    urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    print(f"model cached: {os.path.getsize(MODEL_PATH)} bytes")
    return MODEL_PATH


def load_background(path: str, w: int, h: int, n_frames: int):
    """Return either a single (h,w,3) BG frame or a list of n_frames if BG is video.

    Source images / videos are center-cropped to 16:9 (or whatever the target
    aspect is), then resized to exactly (w, h) without distortion.
    """
    ext = os.path.splitext(path)[1].lower()
    target_aspect = w / h

    def _aspect_crop_resize(img: np.ndarray) -> np.ndarray:
        ih, iw = img.shape[:2]
        ia = iw / ih
        if ia < target_aspect:
            # narrower than target — crop top + bottom
            new_h = int(iw / target_aspect)
            y0 = (ih - new_h) // 2
            img = img[y0:y0 + new_h, :]
        elif ia > target_aspect:
            # wider than target — crop left + right
            new_w = int(ih * target_aspect)
            x0 = (iw - new_w) // 2
            img = img[:, x0:x0 + new_w]
        return cv2.resize(img, (w, h))

    if ext in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
        bg = cv2.imread(path)
        if bg is None:
            sys.exit(f"could not read background image: {path}")
        return _aspect_crop_resize(bg)
    elif ext in (".mp4", ".mov", ".m4v", ".mkv"):
        cap = cv2.VideoCapture(path)
        frames = []
        for _ in range(n_frames):
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop
                ok, frame = cap.read()
                if not ok:
                    break
            frames.append(_aspect_crop_resize(frame))
        cap.release()
        return frames
    else:
        sys.exit(f"unsupported background type: {path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("input", help="input video (.mp4, .mov, etc)")
    p.add_argument("output", help="output video (.mp4)")
    p.add_argument("background", nargs="?", help="background image or video path")
    p.add_argument("--color", help="solid BG color as 'R,G,B' (alternative to background path)")
    p.add_argument("--feather", type=int, default=5, help="mask blur kernel (odd int, default 5)")
    p.add_argument("--no-audio", action="store_true", help="skip muxing audio back in")
    args = p.parse_args()

    if not args.background and not args.color:
        sys.exit("Either a background path or --color R,G,B is required")

    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        sys.exit(f"could not open input: {args.input}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    print(f"input: {w}x{h} @ {fps:.2f}fps, {n_frames} frames")

    # Resolve background
    if args.color:
        r, g, b = [int(x.strip()) for x in args.color.split(",")]
        bg = np.zeros((h, w, 3), dtype=np.uint8)
        bg[:] = (b, g, r)  # OpenCV is BGR
    else:
        bg = load_background(args.background, w, h, n_frames)

    # Set up the Tasks-API segmenter in VIDEO mode (frame-coherent)
    model_path = ensure_model()
    options = mp_vision.ImageSegmenterOptions(
        base_options=mp_python.BaseOptions(model_asset_path=model_path),
        running_mode=mp_vision.RunningMode.VIDEO,
        output_category_mask=False,
        output_confidence_masks=True,
    )
    segmenter = mp_vision.ImageSegmenter.create_from_options(options)

    # Write video-only first, then mux audio at the end
    silent_out = args.output + ".silent.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(silent_out, fourcc, fps, (w, h))

    feather_k = max(1, args.feather)
    if feather_k % 2 == 0:
        feather_k += 1

    frame_duration_ms = 1000.0 / fps
    timestamp_ms = 0.0
    i = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        # Tasks API expects mp.Image in SRGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = segmenter.segment_for_video(mp_image, int(timestamp_ms))

        # confidence_masks[0] is the foreground (person) confidence in [0,1]
        mask = np.asarray(result.confidence_masks[0].numpy_view(), dtype=np.float32)
        if feather_k > 1:
            mask = cv2.GaussianBlur(mask, (feather_k, feather_k), 0)
        mask3 = np.dstack([mask] * 3)

        # Pick the BG frame: still image OR ith frame of bg video
        bg_frame = bg[i % len(bg)] if isinstance(bg, list) else bg

        composite = (
            frame.astype(np.float32) * mask3
            + bg_frame.astype(np.float32) * (1.0 - mask3)
        )
        out.write(composite.astype(np.uint8))

        i += 1
        timestamp_ms += frame_duration_ms
        if i % 60 == 0:
            print(f"  {i}/{n_frames}")

    cap.release()
    out.release()
    segmenter.close()

    if args.no_audio:
        os.replace(silent_out, args.output)
        print(f"done (no audio): {args.output}")
        return

    # Mux audio from the input back in via ffmpeg
    print("muxing audio...")
    cmd = [
        "ffmpeg", "-y",
        "-i", silent_out,
        "-i", args.input,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-shortest",
        "-movflags", "+faststart",
        args.output,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.remove(silent_out)
    print(f"done: {args.output}")


if __name__ == "__main__":
    main()
